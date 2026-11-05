# Canary Islands car-rental price watcher

Checks each supplier's public rates once a day, finds the cheapest car in the
class you chose, compares the cheapest **across suppliers** with what you paid,
and sends a Telegram alert if it got at least €3 cheaper (`min_savings_eur`).
Every check is written to `history.csv`.

Suppliers today: **autoreisen.com** (GET + HTML) and **top-car-hire.com / TopCar**
(session POST + JSON). More can be added as adapters in `watch.py`.

One Telegram bot serves **many users** — share it with friends, each watches
their own bookings and gets their own alerts.

Files: `watch.py` (script), `config.yaml` (suppliers, offices, class mapping,
users + bookings), `history.csv` (auto-created), `../.github/workflows/autoreisen-watch.yml`
(daily run, 08:00 Canary time).

## One-time setup

### 1. Make the Telegram bot
1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot`, pick a
   name; it gives you a **bot token**.
2. In GitHub: Settings → Secrets and variables → Actions → add `TELEGRAM_BOT_TOKEN`
   (the token). You do **not** add chat ids as secrets — they go in `config.yaml`.

### 2. Each user's chat id
Every person who wants alerts does this once:
1. Open your bot in Telegram and press **Start** (send it any message).
2. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser — find
   `"chat":{"id":123456789,...}`. That number is their `telegram_chat_id`.
3. Paste it under that person in `config.yaml` (see below).

A user with an empty `telegram_chat_id` is still checked and logged, but their
alerts are only printed in the workflow log, not sent.

### 3. Merge & run
Scheduled workflows only run from the **default branch** (`main`), so merge there.
Then Actions → "Autoreisen price watch" → *Run workflow* to test immediately.

## config.yaml

```yaml
min_savings_eur: 3

suppliers:
  autoreisen: { enabled: true }
  topcar: { enabled: true }

offices:                 # canonical office -> each supplier's own code
  tenerife_south:
    autoreisen: 2
    topcar: "50"

classes:                 # your cross-supplier mapping table
  economy_manual:
    label: "Economy manual (Seat Ibiza / VW Polo class)"
    autoreisen: { groups: [C], models: [Ibiza, Polo] }
    topcar:     { groups: [B, B1], models: [Polo, Rio, Ibiza] }

users:
  - name: "Erwin"
    telegram_chat_id: "123456789"
    bookings:
      - name: "Tenerife Xmas 2026"
        office: tenerife_south
        pickup: "2026-12-20 10:00"
        return: "2027-01-21 10:00"
        car_class: economy_manual
        paid_eur: 665.00
```

### Add a user
Add an entry under `users:` with their `name`, `telegram_chat_id`, and a
`bookings:` list (can be empty to start).

### Add a booking
Under a user's `bookings:`, add `name`, `office` (a key from `offices`), `pickup`
/ `return` (`YYYY-MM-DD HH:MM`), `car_class` (a key from `classes`), and `paid_eur`.
Past-pickup bookings are skipped automatically; `enabled: false` pauses one.

### The class mapping (important)
Each supplier uses its **own** group letters, so `classes` is where you say what
counts as "the same car". A car matches a class when its group is in `groups`
**and** (if given) its model contains one of `models`. Example: for
`economy_manual`, autoreisen group `C` (Seat Ibiza / VW Polo) maps to TopCar
groups `B`/`B1` (Kia Rio / VW Polo). Tune these as you learn each fleet.

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

## After you rebook
Set `paid_eur` for that booking to the new price and commit. The "don't repeat
the same price" memory is tied to the paid price, so it resets automatically.

## How alerts work
- **Cheaper:** one alert per new lower price, naming the cheapest supplier and
  listing all suppliers' prices for that class.
- **⚠️ Warning:** if no supplier can be read / has the class for a booking.
- Each supplier's cheapest-in-class price is logged to `history.csv` every run
  (with a `cheapest` flag for the overall winner), giving you a price history and
  a cross-supplier comparison over time.

## Run locally
```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python watch.py   # token optional; without it, alerts are logged
```
