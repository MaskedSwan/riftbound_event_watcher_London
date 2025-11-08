#!/usr/bin/env python3
"""
Riftbound London Event Watcher
---------------------------------
Scrapes London store pages for **Riftbound** events and posts new ones to a Discord channel
(via webhook). Also supports exporting an .ics calendar and a CSV to help plan your week/month.

Preconfigured stores:
- Dark Sphere (Shepherd's Bush): https://www.darksphere.co.uk/gamingcalendar.php
- Spellbound Games (London): https://spellboundgames.co.uk/collections/events

Usage
-----
# 1) Install deps
python -m venv .venv && . .venv/bin/activate
pip install requests beautifulsoup4 python-dateutil pytz ics python-slugify

# 2) Set your Discord webhook
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/XXX/YYY"

# 3) Run once (prints findings, posts only *new* ones)
python riftbound_watcher.py run

# Optional: export calendar & csv
python riftbound_watcher.py export --ics events.ics --csv events.csv

# Recommended: cron it (runs every 2 hours). Edit with `crontab -e`:
0 */2 * * * cd /path/to/repo && . .venv/bin/activate && DISCORD_WEBHOOK_URL=... python riftbound_watcher.py run >> watcher.log 2>&1

Notes
-----
- State is tracked in .data/state.json so we only ping you for genuinely new events.
- You can extend/add stores by editing the `STORES` list at the bottom.
- London timezone is assumed (Europe/London) and dates are normalized to ISO-8601.
"""
from __future__ import annotations
import os
import re
import json
import time
import hashlib
import argparse
import calendar
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import List, Optional, Iterable, Tuple

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
from ics import Calendar, Event as IcsEvent
from slugify import slugify

LONDON_TZ = pytz.timezone("Europe/London")
HEADERS = {"User-Agent": "RiftboundWatcher/1.0 (+https://example.local)"}
DATA_DIR = os.path.join(os.path.dirname(__file__), ".data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

@dataclass
class RBEvent:
    title: str
    start: datetime
    end: Optional[datetime]
    url: str
    store: str
    location: Optional[str] = None

    def uid(self) -> str:
        base = f"{self.store}|{self.title}|{self.start.isoformat()}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    def to_dict(self):
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        return d

# ------------------------------- Utils ------------------------------------

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_state() -> set:
    ensure_dirs()
    if not os.path.exists(STATE_PATH):
        return set()
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        seen = set(json.load(f))
    return seen


def save_state(seen: set):
    ensure_dirs()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, indent=2)


def londonify(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return LONDON_TZ.localize(dt)
    return dt.astimezone(LONDON_TZ)


def guess_end(start: datetime, hours: int = 3) -> datetime:
    return start + timedelta(hours=hours)


def resolve_calendar_date(month_name: str, year: int, day: int) -> Optional[date]:
    """
    Dark Sphere sometimes shows a day that actually belongs to the *next* month
    (e.g., '31' while the page header still says 'November').
    We first try the stated month; if invalid, we try the next month.
    """
    try:
        month = dtparse.parse(month_name).month
    except Exception:
        month = 1
    # try current month
    last = calendar.monthrange(year, month)[1]
    if 1 <= day <= last:
        return date(year, month, day)
    # try next month
    if month == 12:
        n_year, n_month = year + 1, 1
    else:
        n_year, n_month = year, month + 1
    last_next = calendar.monthrange(n_year, n_month)[1]
    if 1 <= day <= last_next:
        return date(n_year, n_month, day)
    return None


# ---------------------------- Discord Notify ------------------------------

def post_discord(event: RBEvent) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return  # Print-only mode; no webhook set
    content = (
        f"**New Riftbound event — {event.store}!**\n"
        f"**{event.title}**\n"
        f"🗓️ {event.start.strftime('%a %d %b %Y %H:%M')}"
    )
    if event.end:
        content += f" – {event.end.strftime('%H:%M')}"
    if event.location:
        content += f"\n📍 {event.location}"
    if event.url:
        content += f"\n🔗 {event.url}"

    payload = {"content": content}
    try:
        requests.post(url, json=payload, timeout=15)
    except requests.RequestException:
        pass


# ---------------------------- Scrapers ------------------------------------

def fetch(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_time_range(text: str) -> Tuple[Optional[str], Optional[str]]:
    # e.g. "19:00 - 23:00" or "12:00 - 19:00"
    m = re.search(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", text)
    if m:
        return m.group(1), m.group(2)
    # sometimes single time like "12:00"
    m2 = re.search(r"(\d{1,2}:\d{2})", text)
    if m2:
        return m2.group(1), None
    return None, None


# ---- Dark Sphere (Shepherd's Bush) calendar page
DARKSPHERE_URL = "https://www.darksphere.co.uk/gamingcalendar.php"


def scrape_darksphere(now: datetime) -> List[RBEvent]:
    soup = fetch(DARKSPHERE_URL)
    events: List[RBEvent] = []

    # Month (e.g. "November") appears near the top
    month_text_el = soup.find(string=re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$", re.I))
    month_name = month_text_el.strip() if month_text_el else now.strftime("%B")
    year = now.year

    # The site lists days as headings like "9. Sunday"; capture the day number while iterating.
    current_day: Optional[int] = None

    for node in soup.find_all(True):
        # Detect day headers like "9. Sunday"
        if node.name in ("div", "p", "span", "h1", "h2", "h3", "h4") and node.get_text(strip=True):
            mday = re.match(r"^(\d{1,2})\.\s*[A-Za-z]+$", node.get_text(strip=True))
            if mday:
                current_day = int(mday.group(1))
                continue

        # Find links that look like events
        if node.name == "a" and node.get_text(strip=True):
            title = node.get_text(" ", strip=True)
            if "riftbound" not in title.lower():
                continue
            # look ahead/around for a time range within nearby siblings
            time_text = ""
            for sib in list(node.next_siblings)[:3]:
                if hasattr(sib, 'get_text'):
                    time_text += " " + sib.get_text(" ", strip=True)
                elif isinstance(sib, str):
                    time_text += " " + sib.strip()
            start_str, end_str = parse_time_range(time_text)

            if current_day is None:
                # fallback: try to search backwards for a day header
                prev = node
                for _ in range(10):
                    prev = prev.find_previous()
                    if not prev:
                        break
                    txt = prev.get_text(" ", strip=True) if hasattr(prev, 'get_text') else str(prev)
                    mday2 = re.match(r"^(\d{1,2})\.\s*[A-Za-z]+$", txt)
                    if mday2:
                        current_day = int(mday2.group(1))
                        break

            if current_day is None:
                # Can't determine date — skip
                continue

            # Build datetime (robust across month boundaries)
            event_date = resolve_calendar_date(month_name, year, current_day)
            if not event_date:
                # Can't resolve a valid date; skip
                continue
            start_time = start_str or "19:00"  # default evening
            start_dt = londonify(dtparse.parse(f"{event_date.isoformat()} {start_time}", dayfirst=False))
            end_dt = londonify(dtparse.parse(f"{event_date.isoformat()} {end_str}", dayfirst=False)) if end_str else guess_end(start_dt, 4)

            ev = RBEvent(
                title=title,
                start=start_dt,
                end=end_dt,
                url=requests.compat.urljoin(DARKSPHERE_URL, node.get("href", "")),
                store="Dark Sphere (Shepherd's Bush)",
                location="Shepherd's Bush Megastore, London"
            )
            events.append(ev)

    # Deduplicate by UID
    uniq = {e.uid(): e for e in events}
    return list(uniq.values())


# ---- Spellbound Games (Shopify collection of events)
SPELLBOUND_COLLECTION = "https://spellboundgames.co.uk/collections/events"


def _extract_date_from_title(title: str, default_year: int) -> Optional[date]:
    # Common formats in titles: "Nexus Night - 12/09" or "Riftbound – Friday 12/09" etc.
    m = re.search(r"(\d{1,2})[\-/](\d{1,2})(?:[\-/](\d{2,4}))?", title)
    if not m:
        return None
    d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (2000 + int(y)) if y and len(y) == 2 else (int(y) if y else default_year)
    try:
        return date(year, mth, d)
    except ValueError:
        return None


def scrape_spellbound(now: datetime) -> List[RBEvent]:
    soup = fetch(SPELLBOUND_COLLECTION)
    events: List[RBEvent] = []

    cards = soup.select("a[href*='/products/']")  # product tiles
    for a in cards:
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        if "riftbound" not in title.lower():
            continue
        href = a.get("href")
        url = requests.compat.urljoin(SPELLBOUND_COLLECTION, href)

        # Try to get date from the card title; fallback to product page
        ev_date = _extract_date_from_title(title, now.year)
        start_time = "19:00"  # default evening start for weeklies
        end_time = "23:00"

        if ev_date is None:
            # fetch product page for more context
            try:
                psoup = fetch(url)
                body_txt = psoup.get_text(" ", strip=True)
                ev_date = _extract_date_from_title(body_txt, now.year)
                # time in the body?
                s, e = parse_time_range(body_txt)
                if s:
                    start_time = s
                if e:
                    end_time = e
            except Exception:
                pass

        if ev_date is None:
            # Can't date it — skip
            continue

        start_dt = londonify(dtparse.parse(f"{ev_date.isoformat()} {start_time}", dayfirst=False))
        end_dt = londonify(dtparse.parse(f"{ev_date.isoformat()} {end_time}", dayfirst=False))

        events.append(RBEvent(
            title=title,
            start=start_dt,
            end=end_dt,
            url=url,
            store="Spellbound Games (London)",
            location="Spellbound Games, London"
        ))

    # Dedup by UID
    uniq = {e.uid(): e for e in events}
    return list(uniq.values())


# ----------------------- Export helpers (ICS/CSV) --------------------------

def export_ics(events: List[RBEvent], path: str):
    cal = Calendar()
    for ev in events:
        ics_ev = IcsEvent()
        ics_ev.name = f"{ev.store}: {ev.title}"
        ics_ev.begin = ev.start
        if ev.end:
            ics_ev.end = ev.end
        ics_ev.url = ev.url
        ics_ev.location = ev.location or ev.store
        ics_ev.uid = ev.uid()
        cal.events.add(ics_ev)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(cal)


def export_csv(events: List[RBEvent], path: str):
    import csv
    fields = ["date", "start", "end", "store", "title", "url", "location"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for ev in sorted(events, key=lambda e: (e.start, e.store)):
            w.writerow({
                "date": ev.start.strftime("%Y-%m-%d"),
                "start": ev.start.strftime("%H:%M"),
                "end": ev.end.strftime("%H:%M") if ev.end else "",
                "store": ev.store,
                "title": ev.title,
                "url": ev.url,
                "location": ev.location or "",
            })


# ----------------------------- Main flow ----------------------------------

def find_events(now: Optional[datetime] = None) -> List[RBEvent]:
    now = londonify(now or datetime.now(tz=LONDON_TZ))
    evs = []
    try:
        evs.extend(scrape_darksphere(now))
    except Exception as e:
        print(f"[warn] Dark Sphere scrape failed: {e}")
    try:
        evs.extend(scrape_spellbound(now))
    except Exception as e:
        print(f"[warn] Spellbound scrape failed: {e}")

    # future-only, unique by UID
    evs = [e for e in evs if e.start >= (now - timedelta(days=1))]
    uniq = {e.uid(): e for e in evs}
    return list(uniq.values())


def run_once(post: bool = True) -> List[RBEvent]:
    seen = load_state()
    now = londonify(datetime.now(tz=LONDON_TZ))
    events = find_events(now)

    new_events = [e for e in events if e.uid() not in seen]

    # Notify
    for ev in sorted(new_events, key=lambda e: e.start):
        print(f"NEW: {ev.start:%Y-%m-%d %H:%M} — {ev.store} — {ev.title}\n{ev.url}\n")
        if post:
            post_discord(ev)
        seen.add(ev.uid())

    save_state(seen)
    return events


def main():
    ap = argparse.ArgumentParser(description="Riftbound London Event Watcher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="Scrape and notify (Discord webhook if set)")

    exp = sub.add_parser("export", help="Export ICS and/or CSV of upcoming events")
    exp.add_argument("--ics", dest="ics_path", default=None)
    exp.add_argument("--csv", dest="csv_path", default=None)

    args = ap.parse_args()

    if args.cmd == "run":
        run_once(post=True)
    elif args.cmd == "export":
        events = run_once(post=False)
        if args.ics_path:
            path = args.ics_path
            export_ics(events, path)
            print(f"Wrote {path}")
        if args.csv_path:
            path = args.csv_path
            export_csv(events, path)
            print(f"Wrote {path}")


if __name__ == "__main__":
    main()
