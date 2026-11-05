# Canary Islands car-rental price watcher

Watches people's car-rental bookings and pings them on Telegram when the same
class of car gets cheaper. People onboard themselves through a Telegram bot
(pick office / dates / times / class, type what they paid) — their watch is
stored in Supabase, and a daily GitHub Actions job checks every supplier and
alerts them when the cheapest option drops at least **€25** (`min_savings_eur`)
below what they paid.

Suppliers today: **autoreisen.com** (GET + HTML) and **top-car-hire.com / TopCar**
(session POST + JSON). More can be added as adapters in `watch.py`.

## How it fits together

```
Telegram bot (Supabase Edge Function: carwatch-bot)
    └─ writes each watch to  public.cw_watches
Daily job (GitHub Actions, 08:00 Canary)  →  autoreisen/watch.py
    ├─ reads active watches from cw_watches
    ├─ queries each supplier, finds cheapest in class
    ├─ Telegram-alerts the user if it dropped ≥ €25 below paid (new low only)
    ├─ writes last price back to the watch
    └─ deactivates a watch the day before pickup
```

- **Watches** (who / where / when / paid) live in **Supabase**, created by the bot.
- **Mappings** (office codes, class → supplier groups, which suppliers, threshold)
  live in `config.yaml` — this is the part you hand-tune as you learn each fleet.
- `history.csv` keeps every check (price per supplier, per run) as a price log.

## Files
`watch.py` (checker), `config.yaml` (mapping tables + threshold), `history.csv`
(auto-created log), `../.github/workflows/autoreisen-watch.yml` (daily run),
`../telegram-bot/index.ts` (the bot, deployed as a Supabase Edge Function).

## Environment variables (GitHub Actions secrets)
| Secret | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the bot token from @BotFather (also set in Supabase) |
| `SUPABASE_URL` | e.g. `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | service role key — bypasses RLS to read/write watches |

Set these under **GitHub → repo → Settings → Secrets and variables → Actions**.
Scheduled workflows only run from the **default branch**, so merge to `main`,
then **Actions → Autoreisen price watch → Run workflow** to test immediately.

## config.yaml (mapping tables)

```yaml
min_savings_eur: 25        # alert only when the drop is at least this big

suppliers:
  autoreisen: { enabled: true }
  topcar: { enabled: true }

offices:                   # canonical office key -> each supplier's own code
  tenerife_south:
    autoreisen: 2
    topcar: "50"

classes:                   # your cross-supplier mapping table
  economy_manual:
    label: "Economy manual (Seat Ibiza / VW Polo class)"
    autoreisen: { groups: [C], models: [Ibiza, Polo] }
    topcar:     { groups: [B, B1], models: [Polo, Rio, Ibiza] }
```

The office **keys** (`tenerife_south`, …) and class **keys** (`economy_manual`, …)
must match the ones the bot writes — they are the hardcoded `OFFICES` / `CLASSES`
lists in `../telegram-bot/index.ts`. Add a new office or class in **both** places.

### The class mapping (important)
Each supplier uses its **own** group letters, so `classes` is where you say what
counts as "the same car". A car matches a class when its group is in `groups`
**and** (if given) its model contains one of `models`. Example: `economy_manual`
maps autoreisen group `C` (Seat Ibiza / VW Polo) to TopCar groups `B`/`B1`
(Kia Rio / VW Polo). Tune these as you learn each fleet.

### Office codes
`autoreisen` uses numeric `ofi_rec` codes; `topcar` uses its `pick_up_office`
select value. Verified Nov 2026:

| Office | autoreisen | topcar |
|---|---|---|
| Tenerife South (TFS) | 2 | 50 |
| Tenerife North (TFN) | 1 | 51 |
| Gran Canaria Airport | 18 | 1 |
| Lanzarote Airport | 3 | 60 |
| Fuerteventura Airport | 19 | 10 |

To add any other office: pick it on the supplier's site and read the code from
the form (autoreisen: the `ofi_rec` number in the URL; topcar: the option value
in the pick-up-office `<select>`).

## How alerts work
- **Cheaper:** one alert when the cheapest supplier drops ≥ `min_savings_eur`
  below paid, and again only if it reaches a **new low** (we remember the last
  alerted price per watch, so no daily spam at the same price).
- Each supplier's cheapest-in-class price is written to `history.csv` every run
  (with a `cheapest` flag for the overall winner) — a cross-supplier price
  history and a record for disputes.
- A watch **auto-stops the day before pickup**.

## Run locally (no database)
Test the checker without touching Supabase by pointing it at a JSON file of
watch rows (alerts are only logged unless you also set `TELEGRAM_BOT_TOKEN`):

```bash
pip install -r requirements.txt
cat > watches.json <<'JSON'
[{"id":"test","chat_id":"","user_name":"Me","office":"tenerife_south",
  "pickup":"2026-12-20 12:00","dropoff":"2027-01-21 12:00",
  "car_class":"economy_manual","paid_eur":663,"active":true,"last_alert_price":null}]
JSON
CARWATCH_LOCAL_WATCHES=watches.json python watch.py
```

With `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` set instead, it reads real
watches from the database and writes prices back.
