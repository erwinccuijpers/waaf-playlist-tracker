#!/usr/bin/env python3
"""Autoreisen (Tenerife) car-rental price watcher.

Fetches the public rates page for each booking in config.yaml, finds the
price for your car group/model, logs it to history.csv and sends a Telegram
alert when it is at least `min_savings_eur` cheaper than what you paid.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional; without them
messages are only logged).
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
import yaml
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.yaml"
HISTORY_PATH = HERE / "history.csv"
HISTORY_FIELDS = ["checked_at", "booking", "group", "model", "price_eur", "paid_eur", "alerted"]

BASE_URL = "https://www.autoreisen.com/car-hire/rates-fleet.php"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log = logging.getLogger("autoreisen")

# "1.234,56 €", "€ 123.45", "123,45€", "123 EUR"
PRICE_RE = re.compile(
    r"(?:€|EUR)\s*(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?)"
    r"|(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|EUR)",
    re.IGNORECASE,
)
# "Group A", "Grupo B1", "Gruppe C", "Category D", "Cat. E"
GROUP_RE = re.compile(r"\b(?:group|grupo|gruppe|groep|category|categor[ií]a|cat\.)\s*:?\s*([A-Z]\d?)\b", re.IGNORECASE)
# Leading group code, as autoreisen.com writes it: "A- Citröen C3", "C - Seat Ibiza".
# A single capital (optionally a digit) followed by a dash and then the model text.
LEADING_GROUP_RE = re.compile(r"^\s*([A-Z]\d?)\s*[-–—]\s*(?=\S)")
# Same prefix, for stripping it off the model name (keeps the dash out of the result).
GROUP_PREFIX_RE = re.compile(r"^\s*[A-Z]\d?\s*[-–—]\s*")


def group_in(text: str) -> str:
    """Car group letter from either 'Group A' wording or a leading 'A- …' code."""
    m = GROUP_RE.search(text) or LEADING_GROUP_RE.match(text)
    return m.group(1).upper() if m else ""


def strip_group_prefix(text: str) -> str:
    """'A- Citröen C3 5P' -> 'Citröen C3 5P' (leaves text without a prefix alone)."""
    return GROUP_PREFIX_RE.sub("", text).strip() or text


class ParseError(Exception):
    """The page did not look like we expected (layout probably changed)."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_float(raw: str) -> float:
    s = raw.replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        # whichever separator comes last is the decimal one
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


def build_url(booking: dict, site: dict) -> str:
    pu = datetime.strptime(str(booking["pickup"]), "%Y-%m-%d %H:%M")
    re_ = datetime.strptime(str(booking["return"]), "%Y-%m-%d %H:%M")
    params = {
        "ofi_rec": booking.get("pickup_office", 2),
        "ofi_dev": booking.get("return_office", booking.get("pickup_office", 2)),
        "dia_inicio": pu.day,
        "mes_inicio": f"{pu.month}-{pu.year}",
        "dia_final": re_.day,
        "mes_final": f"{re_.month}-{re_.year}",
    }
    fmt = site.get("time_format", "%H:%M")
    if site.get("pickup_time_param"):
        params[site["pickup_time_param"]] = pu.strftime(fmt)
    if site.get("return_time_param"):
        params[site["return_time_param"]] = re_.strftime(fmt)
    params.update(site.get("extra_params") or {"fin": 1})
    return f"{BASE_URL}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_cars(html: str, selectors: dict | None = None) -> list[dict]:
    """Return [{'group': 'A', 'model': 'Fiat Panda', 'price': 123.45}, ...]."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    selectors = selectors or {}
    if selectors.get("car"):
        blocks = soup.select(selectors["car"])
    else:
        blocks = _guess_car_blocks(soup)

    cars = []
    for block in blocks:
        car = _parse_block(block, selectors)
        if car:
            cars.append(car)
    if not cars:
        raise ParseError(
            f"Found {len(blocks)} candidate car blocks but could not read group+price from any "
            "(site layout may have changed; see 'selectors' in config.yaml)."
        )
    return cars


def _guess_car_blocks(soup: BeautifulSoup) -> list:
    """Smallest elements whose text names a car group and carries a EUR price."""
    candidates = []
    for el in soup.find_all(["div", "article", "li", "tr", "section", "form", "table"]):
        text = el.get_text(" ", strip=True)
        if group_in(text) and prices_in(text):
            candidates.append(el)
    ids = {id(c) for c in candidates}
    # keep only the innermost: drop a candidate if one of its descendants is also a candidate
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
    # Prefer a price close to the word "total"; otherwise the largest amount in
    # the block (a per-day price is always lower than the total).
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


def find_match(cars: list[dict], booking: dict) -> dict | None:
    group = str(booking.get("group") or "").strip().upper()
    model = str(booking.get("model") or "").strip().lower()
    matches = [
        c for c in cars
        if (not group or c["group"] == group) and (not model or model in c["model"].lower())
    ]
    return min(matches, key=lambda c: c["price"]) if matches else None


# --------------------------------------------------------------------------- #
# History / Telegram
# --------------------------------------------------------------------------- #
def read_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    with HISTORY_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def append_history(row: dict) -> None:
    new = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def already_alerted(history: list[dict], name: str, price: float, paid: float) -> bool:
    """True if the last alert for this booking (at this paid price) was for the same price."""
    alerts = [
        r for r in history
        if r["booking"] == name and r.get("alerted") == "yes"
        and abs(float(r["paid_eur"]) - paid) < 0.005
    ]
    return bool(alerts) and abs(float(alerts[-1]["price_eur"]) - price) < 0.005


def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram not configured; message would have been:\n%s", text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        log.error("Telegram send failed: %s %s", r.status_code, r.text[:300])
    else:
        log.info("Telegram message sent")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def check_booking(booking: dict, cfg: dict, history: list[dict]) -> bool:
    """Returns True on success, False if something needs attention."""
    site = cfg.get("site") or {}
    name = booking["name"]
    paid = float(booking["paid_eur"])
    min_savings = float(booking.get("min_savings_eur", cfg.get("min_savings_eur", 3)))
    url = build_url(booking, site)
    want = " / ".join(x for x in [booking.get("group") and f"group {booking['group']}", booking.get("model")] if x)
    log.info("[%s] %s, paid €%.2f — %s", name, want, paid, url)

    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=60)
        resp.raise_for_status()
        cars = parse_cars(resp.text, site.get("selectors"))
    except (requests.RequestException, ParseError) as e:
        log.error("[%s] FAILED: %s", name, e)
        send_telegram(f"⚠️ Autoreisen watcher: could not read prices for '{name}'.\n{e}\n\n{url}")
        return False

    for c in sorted(cars, key=lambda c: (c["group"], c["price"])):
        log.info("    group %-3s €%8.2f  %s", c["group"], c["price"], c["model"])

    match = find_match(cars, booking)
    if not match:
        groups = ", ".join(sorted({c["group"] for c in cars}))
        log.error("[%s] no car matches %s (groups on page: %s)", name, want, groups)
        send_telegram(
            f"⚠️ Autoreisen watcher: '{name}' — no car matching {want} on the page "
            f"({len(cars)} cars parsed, groups: {groups}). Sold out, or layout changed?\n\n{url}"
        )
        return False

    price, savings = match["price"], paid - match["price"]
    log.info("[%s] current €%.2f (%s) vs paid €%.2f → savings €%.2f", name, price, match["model"], paid, savings)

    alerted = "no"
    if savings >= min_savings:
        if already_alerted(history, name, price, paid):
            log.info("[%s] cheaper, but already alerted at €%.2f — not repeating", name, price)
        else:
            send_telegram(
                f"🚗 Cheaper car rental: {name}\n"
                f"{match['model']} (group {match['group']})\n"
                f"Paid:  €{paid:.2f}\n"
                f"Now:   €{price:.2f}\n"
                f"Save:  €{savings:.2f}\n\n"
                f"Rebook: {url}"
            )
            alerted = "yes"

    row = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "booking": name,
        "group": match["group"],
        "model": match["model"],
        "price_eur": f"{price:.2f}",
        "paid_eur": f"{paid:.2f}",
        "alerted": alerted,
    }
    append_history(row)
    history.append(row)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    bookings = cfg.get("bookings") or []
    if not bookings:
        log.error("No bookings in %s", CONFIG_PATH)
        return 1
    history = read_history()
    ok = True
    for b in bookings:
        if b.get("enabled", True) is False:
            continue
        if datetime.strptime(str(b["pickup"]), "%Y-%m-%d %H:%M") < datetime.now():
            log.info("[%s] pickup date has passed — skipping", b["name"])
            continue
        ok = check_booking(b, cfg, history) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
