# Autoreisen price watcher (Tenerife)

Checks autoreisen.com's public rates page once a day, compares the price of your
car group/model with what you paid, and sends a Telegram message if it got at
least €3 cheaper (setting: `min_savings_eur`). Every check is written to `history.csv`.

Files: `watch.py` (the script), `config.yaml` (your bookings), `history.csv`
(price history, created automatically), `../.github/workflows/autoreisen-watch.yml`
(the daily run, 08:00 Canary time).

## One-time setup
1. Create a bot with [@BotFather](https://t.me/BotFather), send it any message,
   then get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
2. In the GitHub repo: Settings → Secrets and variables → Actions → add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Scheduled workflows only run from the **default branch** (`main`), so merge this there.
   Use Actions → "Autoreisen price watch" → *Run workflow* to test it right away.

The pickup/return time parameters are already filled in (`hora_inicio` / `hora_final`,
verified against the site's search form), and the parser is tuned to the live page
layout, so no further setup is needed before the first run.

## Add a booking
Add an entry under `bookings:` in `config.yaml`:

```yaml
  - name: "Tenerife Easter 2027"     # unique; used in history.csv and alerts
    pickup_office: 2                 # 2 = Tenerife South (TFS)
    return_office: 2
    pickup: "2027-03-26 09:00"       # YYYY-MM-DD HH:MM
    return: "2027-04-02 09:00"
    group: "B"                       # car group letter, and/or…
    model: ""                        # …a model substring, e.g. "Ibiza"
    paid_eur: 189.50
```

Commit and push. Bookings whose pickup date has passed are skipped automatically
(you can also set `enabled: false`).

### Office codes
`pickup_office` → `ofi_rec`, `return_office` → `ofi_dev`. Verified Nov 2026:

| Code | Office |
|------|--------|
| 2    | Tenerife South Airport (TFS, Reina Sofía) |
| 1    | Tenerife North Airport (TFN, Los Rodeos) |
| 18   | Gran Canaria Airport |
| 3    | Lanzarote Airport |
| 19   | Fuerteventura Airport |

For `return_office` you can also use `9999` ("same office"). The grey island
headings in the site's dropdown (102, 118, 103, 119) are labels, not real offices.
To find any other office, pick it on the site and read the `ofi_rec` number from the URL.

## After you rebook
Set `paid_eur` for that booking to the new price you paid and commit. Alerts
are compared with the new price, and the "don't repeat the same price" memory is reset
because it is tied to the paid price.

## How alerts work
- **Cheaper:** sent once per new lower price. If the price goes back up and then drops again
  to a *different* price, you get a new alert.
- **⚠️ Warning:** sent if the page can't be fetched, no cars can be parsed (layout
  changed), or no car matches your group/model (sold out or wrong config). The
  workflow run then shows as failed too.
- If the automatic parser misreads the page, set CSS `selectors` in `config.yaml`.

## Run locally
```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python watch.py   # Telegram vars optional
```
