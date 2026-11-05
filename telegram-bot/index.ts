// Canary car-rental watcher — Telegram webhook bot (self-service onboarding).
//
// A friend opens the bot, taps through office / dates / times / class and types
// the price they paid; the watch is stored in Supabase (public.cw_watches) and
// the daily Python checker does the rest. The chat id is captured automatically
// from every incoming update, so nobody edits a config file.
//
// Env: TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET (optional), SUPABASE_URL,
//      SUPABASE_SERVICE_ROLE_KEY (the last two are injected by Supabase).
import { createClient } from "jsr:@supabase/supabase-js@2";

const TG = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";
// Telegram Payments provider token (Stripe), from BotFather → bot → Payments.
// When empty, the paywall is off and watches activate for free (dev/legacy).
const PROVIDER = Deno.env.get("STRIPE_PROVIDER_TOKEN") ?? "";
const TRACK_PRICE_CENTS = 999; // €9.99 to activate tracking for one booking
const sb = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

// Canonical keys — MUST match offices/classes in the Python checker (config.yaml).
const OFFICES = [
  { key: "tenerife_south", label: "Tenerife South (TFS)" },
  { key: "tenerife_north", label: "Tenerife North (TFN)" },
  { key: "gran_canaria", label: "Gran Canaria" },
  { key: "lanzarote", label: "Lanzarote" },
  { key: "fuerteventura", label: "Fuerteventura" },
];
const CLASSES = [
  { key: "economy_manual", label: "Economy manual (Ibiza / Polo)" },
  { key: "economy_automatic", label: "Economy automatic" },
];
const officeLabel = (k: string) => OFFICES.find((o) => o.key === k)?.label ?? k;
const classLabel = (k: string) => CLASSES.find((c) => c.key === k)?.label ?? k;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");

// ------------------------------------------------------------------ Telegram
async function tg(method: string, payload: unknown) {
  const r = await fetch(`https://api.telegram.org/bot${TG}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}
const send = (chat: string | number, text: string, reply_markup?: unknown) =>
  tg("sendMessage", { chat_id: chat, text, reply_markup, disable_web_page_preview: true });

// ------------------------------------------------------------------ State
type Draft = Record<string, string>;
async function getState(chat: string) {
  const { data } = await sb.from("cw_bot_state").select("*").eq("chat_id", chat).maybeSingle();
  return data ?? { chat_id: chat, step: null as string | null, draft: {} as Draft };
}
async function setState(chat: string, step: string | null, draft: Draft) {
  await sb.from("cw_bot_state").upsert({ chat_id: chat, step, draft, updated_at: new Date().toISOString() });
}
async function clearState(chat: string) {
  await sb.from("cw_bot_state").delete().eq("chat_id", chat);
}

// ------------------------------------------------------------------ Keyboards
function officeKeyboard() {
  return { inline_keyboard: OFFICES.map((o) => [{ text: o.label, callback_data: `office|${o.key}` }]) };
}
function classKeyboard() {
  return { inline_keyboard: CLASSES.map((c) => [{ text: c.label, callback_data: `class|${c.key}` }]) };
}
function timeKeyboard(which: string) {
  const rows: { text: string; callback_data: string }[][] = [];
  let row: { text: string; callback_data: string }[] = [];
  for (let h = 7; h <= 22; h++) {
    row.push({ text: `${pad(h)}:00`, callback_data: `time|${which}|${pad(h)}:00` });
    if (row.length === 4) { rows.push(row); row = []; }
  }
  if (row.length) rows.push(row);
  return { inline_keyboard: rows };
}
function todayCanary(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Atlantic/Canary", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date()); // YYYY-MM-DD
}
function calendarKeyboard(which: string, year: number, month0: number, minDate: string) {
  const daysInMonth = new Date(Date.UTC(year, month0 + 1, 0)).getUTCDate();
  const startWeekday = (new Date(Date.UTC(year, month0, 1)).getUTCDay() + 6) % 7; // Mon=0
  const thisYM = `${year}-${pad(month0 + 1)}`;
  const minYM = minDate.slice(0, 7);
  const prev = new Date(Date.UTC(year, month0 - 1, 1));
  const next = new Date(Date.UTC(year, month0 + 1, 1));
  const prevYM = `${prev.getUTCFullYear()}-${pad(prev.getUTCMonth() + 1)}`;
  const nextYM = `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}`;
  const rows: { text: string; callback_data: string }[][] = [];
  rows.push([
    { text: prevYM >= minYM ? "‹" : " ", callback_data: prevYM >= minYM ? `nav|${which}|${prevYM}` : "noop" },
    { text: `${MONTHS[month0]} ${year}`, callback_data: "noop" },
    { text: "›", callback_data: `nav|${which}|${nextYM}` },
  ]);
  rows.push(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((d) => ({ text: d, callback_data: "noop" })));
  let week: { text: string; callback_data: string }[] = [];
  for (let i = 0; i < startWeekday; i++) week.push({ text: " ", callback_data: "noop" });
  for (let d = 1; d <= daysInMonth; d++) {
    const ds = `${thisYM}-${pad(d)}`;
    const ok = ds >= minDate;
    week.push({ text: ok ? String(d) : "·", callback_data: ok ? `day|${which}|${ds}` : "noop" });
    if (week.length === 7) { rows.push(week); week = []; }
  }
  if (week.length) { while (week.length < 7) week.push({ text: " ", callback_data: "noop" }); rows.push(week); }
  return { inline_keyboard: rows };
}
const prettyDate = (ds: string) => { const [y, m, d] = ds.split("-"); return `${d} ${MONTHS[+m - 1]} ${y}`; };

// ------------------------------------------------------------------ Watches
async function listWatches(chat: string) {
  const { data } = await sb.from("cw_watches").select("*").eq("chat_id", chat).eq("active", true)
    .order("created_at", { ascending: true });
  if (!data || data.length === 0) {
    await send(chat, "You have no active watches yet.", {
      inline_keyboard: [[{ text: "➕ New watch", callback_data: "new" }]],
    });
    return;
  }
  for (const w of data) {
    const line =
      `🚗 ${classLabel(w.car_class)}\n` +
      `${officeLabel(w.office)}\n` +
      `${prettyDate(w.pickup.slice(0, 10))} → ${prettyDate(w.dropoff.slice(0, 10))}\n` +
      `Paid: €${Number(w.paid_eur).toFixed(2)}` +
      (w.last_price_eur != null ? `  ·  last seen €${Number(w.last_price_eur).toFixed(2)}` : "");
    await send(chat, line, { inline_keyboard: [[{ text: "🗑 Delete", callback_data: `del|${w.id}` }]] });
  }
  await send(chat, "Add another?", { inline_keyboard: [[{ text: "➕ New watch", callback_data: "new" }]] });
}

function watchSummary(draft: Draft, paid: number): string {
  return `${classLabel(draft.car_class)}\n${officeLabel(draft.office)}\n` +
    `${prettyDate(draft.pickup_date)} ${draft.pickup_time} → ${prettyDate(draft.return_date)} ${draft.return_time}\n` +
    `Paid: €${paid.toFixed(2)}`;
}

async function saveWatch(chat: string, name: string, draft: Draft, paid: number) {
  // The watch is only tracked once paid, so it starts inactive (the daily
  // checker only runs active watches). With no provider token configured the
  // paywall is off and it activates immediately (dev / early testers).
  const paywall = PROVIDER !== "";
  const { data, error } = await sb.from("cw_watches").insert({
    chat_id: chat, user_name: name, office: draft.office,
    pickup: `${draft.pickup_date} ${draft.pickup_time}`,
    dropoff: `${draft.return_date} ${draft.return_time}`,
    car_class: draft.car_class, paid_eur: paid,
    active: !paywall,
    status: paywall ? "pending_payment" : "active",
  }).select("id").single();
  await clearState(chat);
  if (error || !data) {
    await send(chat, "⚠️ Sorry, couldn't save that. Please try /new again.");
    return;
  }

  if (!paywall) {
    await send(chat,
      `✅ Watch created!\n\n${watchSummary(draft, paid)}\n\n` +
      `I'll check daily and message you if it drops at least €25. It stops automatically the day before pickup.`,
      { inline_keyboard: [[{ text: "📋 My watches", callback_data: "list" }]] });
    return;
  }

  // Paywall on: send an in-chat invoice; payment flips the watch active.
  await send(chat,
    `Almost done — here's your booking:\n\n${watchSummary(draft, paid)}\n\n` +
    `Switch on daily tracking for €9.99 with the invoice below. ` +
    `I'll watch it until pickup and ping you the moment it drops at least €25.`);
  const inv = await tg("sendInvoice", {
    chat_id: chat,
    title: "Car-rental price tracking",
    description:
      `Daily price watch for your ${classLabel(draft.car_class)} ` +
      `(${prettyDate(draft.pickup_date)} → ${prettyDate(draft.return_date)}). ` +
      `I alert you the moment it drops at least €25 below what you paid, until pickup.`,
    payload: `watch:${data.id}`,
    provider_token: PROVIDER,
    currency: "EUR",
    prices: [{ label: "Price tracking until pickup", amount: TRACK_PRICE_CENTS }],
    start_parameter: "track",
  });
  if (inv && inv.ok === false) {
    console.error("sendInvoice failed", inv);
    await send(chat, "⚠️ Couldn't start the payment just now. Please try /new again.");
  }
}

async function activateWatch(chat: string, sp: any) {
  const wid = String(sp.invoice_payload || "").replace(/^watch:/, "");
  if (!wid) return;
  await sb.from("cw_watches").update({
    active: true, status: "active", paid_at: new Date().toISOString(),
    telegram_payment_charge_id: sp.telegram_payment_charge_id ?? null,
    provider_payment_charge_id: sp.provider_payment_charge_id ?? null,
  }).eq("id", wid);
  await send(chat,
    `✅ Payment received — tracking is ON!\n\n` +
    `I'll check every day and message you the moment your car drops at least €25 ` +
    `below what you paid. It stops automatically the day before pickup.`,
    { inline_keyboard: [[{ text: "📋 My watches", callback_data: "list" }]] });
}

// ------------------------------------------------------------------ Handlers
async function onText(chat: string, name: string, text: string) {
  const t = text.trim();
  if (t === "/start") {
    await clearState(chat);
    await send(chat,
      `👋 Hi ${name}! I watch Canary Islands car-rental prices (autoreisen, TopCar) and ping you if your car gets cheaper.\n\n` +
      `Tap “New watch”, pick your pickup location, dates, times and car class, and tell me what you paid.`,
      { inline_keyboard: [
        [{ text: "➕ New watch", callback_data: "new" }],
        [{ text: "📋 My watches", callback_data: "list" }],
      ] });
    return;
  }
  if (t === "/new") { await startNew(chat); return; }
  if (t === "/list") { await listWatches(chat); return; }
  if (t === "/cancel") { await clearState(chat); await send(chat, "Cancelled."); return; }

  const st = await getState(chat);
  if (st.step === "paid") {
    const n = parseFloat(t.replace(/[^\d.,]/g, "").replace(",", "."));
    if (!isFinite(n) || n <= 0) { await send(chat, "Please send the price as a number, e.g. 665"); return; }
    await saveWatch(chat, name, st.draft, n);
    return;
  }
  await send(chat, "Tap a button, or use /new to add a watch and /list to see them.");
}

async function startNew(chat: string) {
  await setState(chat, "office", {});
  await send(chat, "📍 Where do you pick up the car?", officeKeyboard());
}

async function onCallback(chat: string, name: string, data: string, messageId: number) {
  const [cmd, a, b] = data.split("|");
  if (cmd === "noop") return;
  if (cmd === "new") { await startNew(chat); return; }
  if (cmd === "list") { await listWatches(chat); return; }
  if (cmd === "cancel") { await clearState(chat); await send(chat, "Cancelled."); return; }
  if (cmd === "del") {
    await sb.from("cw_watches").update({ active: false }).eq("id", a).eq("chat_id", chat);
    await send(chat, "🗑 Watch deleted.");
    return;
  }

  const st = await getState(chat);
  const draft: Draft = { ...(st.draft || {}) };

  if (cmd === "office") {
    draft.office = a;
    await setState(chat, "pickup_date", draft);
    const today = todayCanary();
    await send(chat, `📅 Pick-up date at ${officeLabel(a)}:`,
      calendarKeyboard("pickup", +today.slice(0, 4), +today.slice(5, 7) - 1, today));
    return;
  }
  if (cmd === "nav") {
    const [y, m] = b.split("-");
    const min = a === "return" && draft.pickup_date ? draft.pickup_date : todayCanary();
    await tg("editMessageReplyMarkup", {
      chat_id: chat, message_id: messageId,
      reply_markup: calendarKeyboard(a, +y, +m - 1, min),
    });
    return;
  }
  if (cmd === "day") {
    if (a === "pickup") {
      draft.pickup_date = b;
      await setState(chat, "pickup_time", draft);
      await send(chat, `🕙 Pick-up time on ${prettyDate(b)}:`, timeKeyboard("pickup"));
    } else {
      if (draft.pickup_date && b <= draft.pickup_date) {
        await send(chat, "Return must be after pick-up — pick a later day.");
        return;
      }
      draft.return_date = b;
      await setState(chat, "return_time", draft);
      await send(chat, `🕙 Return time on ${prettyDate(b)}:`, timeKeyboard("return"));
    }
    return;
  }
  if (cmd === "time") {
    if (a === "pickup") {
      draft.pickup_time = b;
      await setState(chat, "return_date", draft);
      const base = draft.pickup_date;
      await send(chat, "📅 Return date:",
        calendarKeyboard("return", +base.slice(0, 4), +base.slice(5, 7) - 1, base));
    } else {
      draft.return_time = b;
      await setState(chat, "car_class", draft);
      await send(chat, "🚙 Which car class?", classKeyboard());
    }
    return;
  }
  if (cmd === "class") {
    draft.car_class = a;
    await setState(chat, "paid", draft);
    await send(chat, `💶 What did you pay in total (in €) for this booking?\nJust send the number, e.g. 665`);
    return;
  }
}

// ------------------------------------------------------------------ Entry
Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("ok");
  if (WEBHOOK_SECRET && req.headers.get("x-telegram-bot-api-secret-token") !== WEBHOOK_SECRET) {
    return new Response("forbidden", { status: 401 });
  }
  let update: any;
  try { update = await req.json(); } catch { return new Response("ok"); }
  try {
    if (update.pre_checkout_query) {
      // Must be answered within 10s or Telegram fails the payment.
      await tg("answerPreCheckoutQuery", { pre_checkout_query_id: update.pre_checkout_query.id, ok: true });
    } else if (update.message?.successful_payment) {
      await activateWatch(String(update.message.chat.id), update.message.successful_payment);
    } else if (update.message?.text) {
      const chat = String(update.message.chat.id);
      const name = update.message.from?.first_name || update.message.from?.username || "there";
      await onText(chat, name, update.message.text);
    } else if (update.callback_query) {
      const cq = update.callback_query;
      const chat = String(cq.message.chat.id);
      const name = cq.from?.first_name || cq.from?.username || "there";
      await tg("answerCallbackQuery", { callback_query_id: cq.id });
      await onCallback(chat, name, cq.data || "", cq.message.message_id);
    }
  } catch (e) {
    console.error("handler error", e);
  }
  return new Response("ok");
});
