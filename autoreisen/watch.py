#!/usr/bin/env python3
"""Canary Islands car-rental price watcher (multi-user, multi-supplier).

Watches live in Supabase (public.cw_watches) — created by the Telegram bot, so
nobody edits a config file. For every active watch this queries each enabled
supplier (autoreisen, topcar), finds the cheapest car in that class, compares
the cheapest across suppliers with what the user paid, logs it to history.csv,
and sends a Telegram alert to that user's chat when it is at least
`min_savings_eur` cheaper than the last price we alerted them about. It writes
the latest price back to the watch and deactivates a watch the day before pickup.

config.yaml still holds the cross-supplier mapping tables (offices, classes,
suppliers, min_savings_eur); the watches themselves come from the database.

Env:
  TELEGRAM_BOT_TOKEN           bot token (without it, alerts are only logged)
  SUPABASE_URL                 project URL, e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service role key (bypasses RLS)

Local testing without the database: set CARWATCH_LOCAL_WATCHES to a JSON file
holding a list of watch rows; the DB read/writeback is then skipped and alerts
are only logged.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
import yaml
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.yaml"
HISTORY_PATH = HERE / "history.csv"
HISTORY_FIELDS = [
    "checked_at", "user", "booking", "supplier",
    "group", "model", "price_eur", "paid_eur", "cheapest", "alerted",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 60
EPS = 0.005  # float compare tolerance for euros

log = logging.getLogger("carwatch")


class ParseError(Exception):
    """A supplier page did not look like we expected (layout probably changed)."""


# --------------------------------------------------------------------------- #
# Shared price / text helpers
# --------------------------------------------------------------------------- #
# "1.234,56 €", "€ 123.45", "123,45€", "123 EUR"
PRICE_RE = re.compile(
    r"(?:€|EUR)\s*(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?)"
    r"|(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|EUR)",
    re.IGNORECASE,
)
# "Group A", "Grupo B1", "Gruppe C", "Category D", "Cat. E"
GROUP_RE = re.compile(
    r"\b(?:group|grupo|gruppe|groep|category|categor[ií]a|cat\.)\s*:?\s*([A-Z]\d?)\b",
    re.IGNORECASE,
)
# Leading group code, as autoreisen.com writes it: "A- Citröen C3", "C - Seat Ibiza".
LEADING_GROUP_RE = re.compile(r"^\s*([A-Z]\d?)\s*[-–—]\s*(?=\S)")
GROUP_PREFIX_RE = re.compile(r"^\s*[A-Z]\d?\s*[-–—]\s*")


def to_float(raw: str) -> float:
    s = raw.replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3:  # thousands separator, e.g. 1.234
            s = s.replace(".", "")
    return float(s)


def prices_in(text: str) -> list[float]:
    out = []
    for m in PRICE_RE.finditer(text):
        try:
            out.append(to_float(m.group(1) or m.group(2)))
        except ValueError:
            pass
    return out


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def group_in(text: str) -> str:
    """Car group letter from either 'Group A' wording or a leading 'A- …' code."""
    m = GROUP_RE.search(text) or LEADING_GROUP_RE.match(text)
    return m.group(1).upper() if m else ""


def strip_group_prefix(text: str) -> str:
    """'A- Citröen C3 5P' -> 'Citröen C3 5P' (leaves text without a prefix alone)."""
    return GROUP_PREFIX_RE.sub("", text).strip() or text


def parse_dt(value) -> datetime:
    """Accept 'YYYY-MM-DD HH:MM' (and tolerate seconds / 'T' separator)."""
    s = str(value).strip().replace("T", " ")
    s = re.sub(r"(\d{2}:\d{2}):\d{2}.*$", r"\1", s)  # drop seconds / tz if present
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# autoreisen.com adapter (GET + HTML)
# --------------------------------------------------------------------------- #
AUTOREISEN_URL = "https://www.autoreisen.com/car-hire/rates-fleet.php"
AUTOREISEN_SELECTORS = {
    "car": "article.car-feature",
    "group": "strong.title",
    "model": "strong.title",
    "price": ".price-block a.price",
}


def autoreisen_url(office, pickup: datetime, ret: datetime) -> str:
    params = {
        "ofi_rec": office, "ofi_dev": office,
        "dia_inicio": pickup.day, "mes_inicio": f"{pickup.month:02d}-{pickup.year}",
        "dia_final": ret.day, "mes_final": f"{ret.month:02d}-{ret.year}",
        "hora_inicio": pickup.strftime("%H:%M"), "hora_final": ret.strftime("%H:%M"),
        "fin": 1,
    }
    return f"{AUTOREISEN_URL}?{urlencode(params)}"


def fetch_autoreisen(office, pickup: datetime, ret: datetime) -> tuple[list[dict], str]:
    url = autoreisen_url(office, pickup, ret)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_cars(resp.text, AUTOREISEN_SELECTORS), url


def parse_cars(html: str, selectors: dict | None = None) -> list[dict]:
    """Return [{'group': 'A', 'model': 'Fiat Panda', 'price': 123.45}, ...]."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    selectors = selectors or {}
    blocks = soup.select(selectors["car"]) if selectors.get("car") else _guess_car_blocks(soup)

    cars = [c for c in (_parse_block(b, selectors) for b in blocks) if c]
    if not cars:
        raise ParseError(
            f"Found {len(blocks)} candidate car blocks but could not read group+price from any "
            "(autoreisen layout may have changed)."
        )
    return cars


def _guess_car_blocks(soup: BeautifulSoup) -> list:
    candidates = []
    for el in soup.find_all(["div", "article", "li", "tr", "section", "form", "table"]):
        text = el.get_text(" ", strip=True)
        if group_in(text) and prices_in(text):
            candidates.append(el)
    ids = {id(c) for c in candidates}
    return [c for c in candidates if not any(id(d) in ids for d in c.find_all(True))]


def _parse_block(block, selectors: dict) -> dict | None:
    text = block.get_text(" ", strip=True)

    if selectors.get("group"):
        el = block.select_one(selectors["group"])
        group = group_in(clean(el.get_text(" "))) if el else ""
    else:
        group = group_in(text)

    if selectors.get("price"):
        el = block.select_one(selectors["price"])
        found = prices_in(el.get_text(" ")) if el else []
        price = found[0] if found else None
    else:
        price = _guess_total_price(block)

    if selectors.get("model"):
        el = block.select_one(selectors["model"])
        model = clean(el.get_text(" ")) if el else ""
    else:
        model = _guess_model(block)
    model = strip_group_prefix(model)

    if not group or price is None:
        return None
    return {"group": group, "model": model, "price": price}


def _guess_total_price(block) -> float | None:
    for el in block.find_all(string=re.compile(r"total", re.IGNORECASE)):
        node = el.parent
        for _ in range(3):
            found = prices_in(node.get_text(" ", strip=True))
            if found:
                return max(found)
            node = node.parent or node
    found = prices_in(block.get_text(" ", strip=True))
    return max(found) if found else None


def _guess_model(block) -> str:
    for tag in block.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b"]):
        t = clean(tag.get_text(" "))
        if t and not prices_in(t) and not GROUP_RE.fullmatch(t) and len(t) < 80:
            return GROUP_RE.sub("", t).strip(" -|:") or t
    img = block.find("img", alt=True)
    return clean(img["alt"]) if img else ""


# --------------------------------------------------------------------------- #
# top-car-hire.com adapter (session POST + JSON)
# --------------------------------------------------------------------------- #
TOPCAR_BASE = "https://top-car-hire.com"
TOPCAR_HOME = f"{TOPCAR_BASE}/en/"


def fetch_topcar(office, pickup: datetime, ret: datetime) -> tuple[list[dict], str]:
    """TopCar renders prices from a JSON endpoint after a stateful search POST."""
    office = str(office)
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})

    home = s.get(TOPCAR_HOME, timeout=TIMEOUT)
    home.raise_for_status()
    m = re.search(r'name="_token"\s+value="([^"]+)"', home.text)
    if not m:
        raise ParseError("TopCar: could not find the search form CSRF token.")
    token = m.group(1)

    step1 = s.post(
        f"{TOPCAR_BASE}/en/booking/1",
        data={
            "_token": token,
            "pick_up_office": office, "give_back_office": office,
            "pick_up_date": pickup.strftime("%d/%m/%Y"),
            "give_back_date": ret.strftime("%d/%m/%Y"),
            "pick_up_hour": pickup.strftime("%H:%M"),
            "give_back_hour": ret.strftime("%H:%M"),
            "edad": "40",  # 30+ driver (no young-driver surcharge)
        },
        timeout=TIMEOUT,
    )
    step1.raise_for_status()
    meta = re.search(r'name="csrf-token"\s+content="([^"]+)"', step1.text)
    csrf = meta.group(1) if meta else token

    veh = s.post(
        f"{TOPCAR_BASE}/reservar/obtenerVehiculos",
        data={"categoria": "1", "lang": "en"},  # categoria 1 = "All" vehicles
        headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"},
        timeout=TIMEOUT,
    )
    veh.raise_for_status()
    try:
        items = veh.json().get("items") or []
    except ValueError as e:
        raise ParseError(f"TopCar: vehicles response was not JSON ({e}).")

    cars = []
    for it in items:
        try:
            price = float(str(it["price"]).replace(",", "."))
        except (KeyError, ValueError):
            continue
        cars.append({
            "group": str(it.get("item_category", "")).upper(),
            "model": clean(it.get("item_name", "")),
            "price": price,
        })
    if not cars:
        raise ParseError("TopCar: search returned no vehicles (sold out, or flow changed).")
    return cars, step1.url or TOPCAR_HOME


# --------------------------------------------------------------------------- #
# Supplier registry
# --------------------------------------------------------------------------- #
SUPPLIERS = {
    "autoreisen": {"label": "Autoreisen", "fetch": fetch_autoreisen},
    "topcar": {"label": "TopCar", "fetch": fetch_topcar},
}


def car_matches(car: dict, spec: dict) -> bool:
    """Does this car count as the wanted class for this supplier?"""
    groups = [str(g).upper() for g in (spec.get("groups") or [])]
    models = [str(m).lower() for m in (spec.get("models") or [])]
    if groups and car["group"] not in groups:
        return False
    if models and not any(m in car["model"].lower() for m in models):
        return False
    return bool(groups or models)


# --------------------------------------------------------------------------- #
# Supabase watch store (REST)
# --------------------------------------------------------------------------- #
SB_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
LOCAL_WATCHES = os.environ.get("CARWATCH_LOCAL_WATCHES") or ""


def sb_enabled() -> bool:
    return bool(SB_URL and SB_KEY)


def _sb_headers(extra: dict | None = None) -> dict:
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def fetch_watches() -> list[dict]:
    """Active watches, from Supabase or (for local testing) a JSON file."""
    if sb_enabled():
        r = requests.get(
            f"{SB_URL}/rest/v1/cw_watches",
            headers=_sb_headers({"Accept": "application/json"}),
            params={"select": "*", "active": "eq.true", "order": "created_at.asc"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    if LOCAL_WATCHES:
        data = json.loads(Path(LOCAL_WATCHES).read_text())
        return [w for w in data if w.get("active", True)]
    raise SystemExit(
        "No watch source: set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY "
        "(production) or CARWATCH_LOCAL_WATCHES=<file.json> (local testing)."
    )


def update_watch(watch_id: str, fields: dict) -> None:
    if not sb_enabled():
        log.info("  (local mode) would update watch %s -> %s", watch_id, fields)
        return
    r = requests.patch(
        f"{SB_URL}/rest/v1/cw_watches",
        headers=_sb_headers({"Content-Type": "application/json", "Prefer": "return=minimal"}),
        params={"id": f"eq.{watch_id}"},
        data=json.dumps(fields),
        timeout=TIMEOUT,
    )
    if not r.ok:
        log.error("  watch %s update failed: %s %s", watch_id, r.status_code, r.text[:200])


# --------------------------------------------------------------------------- #
# History / Telegram
# --------------------------------------------------------------------------- #
def append_history(row: dict) -> None:
    new = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def send_telegram(chat_id: str, text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        log.warning("Telegram not sent (no token or chat id). Message was:\n%s", text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        log.error("Telegram send failed for chat %s: %s %s", chat_id, r.status_code, r.text[:300])
    else:
        log.info("Telegram message sent to chat %s", chat_id)


# --------------------------------------------------------------------------- #
# Core: check one watch
# --------------------------------------------------------------------------- #
def check_watch(watch: dict, cfg: dict) -> None:
    wid = str(watch.get("id") or "")
    chat = str(watch.get("chat_id") or "")
    uname = watch.get("user_name") or "there"
    paid = float(watch["paid_eur"])
    label_name = f'{watch.get("office")} {str(watch.get("pickup"))[:10]}'
    min_savings = float(cfg.get("min_savings_eur", 25))
    last_alert = watch.get("last_alert_price")
    last_alert = float(last_alert) if last_alert not in (None, "") else None
    pickup, ret = parse_dt(watch["pickup"]), parse_dt(watch["dropoff"])

    offices = (cfg.get("offices") or {}).get(watch["office"])
    car_class = (cfg.get("classes") or {}).get(watch["car_class"])
    if not offices or not car_class:
        log.error("[%s] unknown office %r or class %r — check config.yaml",
                  label_name, watch.get("office"), watch.get("car_class"))
        return
    label = car_class.get("label", watch["car_class"])
    log.info("[%s] %s, %s→%s, paid €%.2f (last alert %s)",
             uname, label, pickup.date(), ret.date(), paid,
             f"€{last_alert:.2f}" if last_alert is not None else "none")

    enabled = {s: v for s, v in (cfg.get("suppliers") or {}).items() if (v or {}).get("enabled", True)}
    results, problems = [], []
    for sup_name in enabled:
        sup = SUPPLIERS.get(sup_name)
        office_code = offices.get(sup_name)
        if not sup or office_code is None:
            continue  # this supplier not available for this office; silently skip
        try:
            cars, url = sup["fetch"](office_code, pickup, ret)
        except (requests.RequestException, ParseError) as e:
            log.error("  %s FAILED: %s", sup_name, e)
            problems.append(f"{sup['label']}: {e}")
            continue
        spec = car_class.get(sup_name) or {}
        matches = [c for c in cars if car_matches(c, spec)]
        if not matches:
            groups = ", ".join(sorted({c["group"] for c in cars}))
            log.warning("  %s: no car in class (groups seen: %s)", sup_name, groups)
            continue
        best = min(matches, key=lambda c: c["price"])
        best["supplier"], best["label"], best["url"] = sup_name, sup["label"], url
        results.append(best)
        log.info("  %-10s cheapest in class: €%8.2f  %s (%s)",
                 sup["label"], best["price"], best["model"], best["group"])

    if not results:
        log.warning("[%s] no price read at any supplier", label_name)
        if problems and chat:
            send_telegram(chat, f"⚠️ {label}: couldn't read a price right now.\n" + "\n".join(problems))
        update_watch(wid, {"last_checked_at": _utcnow_iso()})
        return

    overall = min(results, key=lambda c: c["price"])
    savings = paid - overall["price"]
    log.info("[%s] cheapest overall: €%.2f at %s vs paid €%.2f → savings €%.2f",
             label_name, overall["price"], overall["label"], paid, savings)

    # Alert only when we clear the threshold AND it is a new low vs the last alert.
    alerted = False
    if savings >= min_savings - EPS and (last_alert is None or overall["price"] < last_alert - EPS):
        others = "\n".join(
            f"• {r['label']}: €{r['price']:.2f} ({r['model']})"
            for r in sorted(results, key=lambda c: c["price"])
        )
        send_telegram(chat,
            f"🚗 Cheaper car rental found!\n"
            f"{label} — {pickup.date()} → {ret.date()}\n\n"
            f"Cheapest now: {overall['model']} at {overall['label']}\n"
            f"You paid: €{paid:.2f}\n"
            f"Now:      €{overall['price']:.2f}\n"
            f"Save:     €{savings:.2f}\n\n"
            f"All suppliers:\n{others}\n\n"
            f"Rebook ({overall['label']}): {overall['url']}"
        )
        alerted = True

    # Log every supplier's cheapest-in-class for the price history / disputes.
    now = _utcnow_iso()
    for r in results:
        row = {
            "checked_at": now, "user": uname, "booking": label_name, "supplier": r["supplier"],
            "group": r["group"], "model": r["model"], "price_eur": f"{r['price']:.2f}",
            "paid_eur": f"{paid:.2f}",
            "cheapest": "yes" if r is overall else "no",
            "alerted": "yes" if (alerted and r is overall) else "no",
        }
        append_history(row)

    writeback = {"last_checked_at": now, "last_price_eur": round(overall["price"], 2)}
    if alerted:
        writeback["last_alert_price"] = round(overall["price"], 2)
    update_watch(wid, writeback)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    watches = fetch_watches()
    log.info("Loaded %d active watch(es) from %s",
             len(watches), "Supabase" if sb_enabled() else "local file")
    if not watches:
        return 0

    now = datetime.now()
    for w in watches:
        try:
            pickup = parse_dt(w["pickup"])
        except (KeyError, ValueError) as e:
            log.error("watch %s: bad pickup %r (%s) — skipping", w.get("id"), w.get("pickup"), e)
            continue
        # Stop a watch the day before pickup (nothing left to save once you've collected the car).
        if pickup <= now + timedelta(days=1):
            log.info("watch %s: pickup %s is within a day — deactivating", w.get("id"), pickup.date())
            update_watch(str(w.get("id") or ""), {"active": False})
            continue
        try:
            check_watch(w, cfg)
        except Exception as e:  # one bad watch must not stop the rest
            log.exception("watch %s failed: %s", w.get("id"), e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
