#!/usr/bin/env python3
"""
Riftbound London Event Watcher
---------------------------------
Scrapes London store pages for **Riftbound** events and posts new ones to a Discord channel
(via webhook). Also supports exporting an .ics calendar and a CSV to help plan your week/month.

Stores covered:
- Dark Sphere (Shepherd's Bush): month calendar (custom parser)
- Spellbound Games (London): Shopify products (reused parser)
- The Brotherhood Games (Bermondsey): Shopify products (reused parser)
- Leisure Games (Finchley): Shopify products (reused parser + loose date)
- Europa Gaming (Wembley): standalone event pages (generic event parser)
- Zombie Games Café (Cricklewood): Wix “product-page” tickets (parser)

Highlights in Discord:
- ⚡ Summoner Skirmish
- 🏅 Regional Qualifier
- 🥇 Regional Championship
- 🌍 Worlds
- 🔴 Release Event
"""
from __future__ import annotations
import os, re, json, hashlib, argparse, calendar
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import List, Optional, Tuple

import pytz, requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
from ics import Calendar, Event as IcsEvent

LONDON_TZ = pytz.timezone("Europe/London")
HEADERS = {"User-Agent": "RiftboundWatcher/1.3 (+https://example.local)"}
DATA_DIR = os.path.join(os.path.dirname(__file__), ".data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

# --------------------------- OP highlight tags -----------------------------
HIGHLIGHT_MAP = {
    "summoner skirmish": "⚡ SUMMONER SKIRMISH",
    "regional qualifier": "🏅 REGIONAL QUALIFIER",
    "regional qualifiers": "🏅 REGIONAL QUALIFIER",
    "regional championship": "🥇 REGIONAL CHAMPIONSHIP",
    "regional championships": "🥇 REGIONAL CHAMPIONSHIP",
    "worlds": "🌍 WORLDS",
    "release event": "🔴 RELEASE EVENT",
}
def _op_tag_for(title: str) -> Optional[str]:
    t = title.lower()
    for k, label in HIGHLIGHT_MAP.items():
        if k in t:
            return label
    return None

@dataclass
class RBEvent:
    title: str
    start: datetime
    end: Optional[datetime]
    url: str
    store: str
    location: Optional[str] = None

    def uid(self) -> str:
        # Title-sensitive id (kept for backward compatibility)
        base = f"{self.store}|{self.title}|{self.start.replace(second=0, microsecond=0).isoformat()}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    def stable_id(self) -> str:
        # Title-agnostic id (store + start minute)
        base = f"{self.store}|{self.start.replace(second=0, microsecond=0).isoformat()}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    def to_dict(self):
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat() if self.end else None
        return d

# ------------------------------- Utils ------------------------------------
def ensure_dirs(): os.makedirs(DATA_DIR, exist_ok=True)
def load_state() -> set:
    ensure_dirs()
    if not os.path.exists(STATE_PATH): return set()
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return set(raw if isinstance(raw, list) else [])
def save_state(seen: set):
    ensure_dirs()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, indent=2)
def londonify(dt: datetime) -> datetime:
    if dt.tzinfo is None: return LONDON_TZ.localize(dt)
    return dt.astimezone(LONDON_TZ)
def guess_end(start: datetime, hours: int = 3) -> datetime: return start + timedelta(hours=hours)

def resolve_calendar_date(month_name: str, year: int, day: int) -> Optional[date]:
    """Dark Sphere sometimes shows a day that belongs to the next month (e.g., '31' under November)."""
    try: month = dtparse.parse(month_name).month
    except Exception: month = 1
    last = calendar.monthrange(year, month)[1]
    if 1 <= day <= last: return date(year, month, day)
    n_year, n_month = (year + 1, 1) if month == 12 else (year, month + 1)
    last_next = calendar.monthrange(n_year, n_month)[1]
    if 1 <= day <= last_next: return date(n_year, n_month, day)
    return None

# ---------------------------- Discord Notify ------------------------------
def post_discord(event: RBEvent) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url: return
    tag = _op_tag_for(event.title)
    header = f"**{tag}**\n" if tag else ""
    content = (
        f"{header}"
        f"**New Riftbound event — {event.store}!**\n"
        f"**{event.title}**\n"
        f"🗓️ {event.start.strftime('%a %d %b %Y %H:%M')}"
    )
    if event.end: content += f" – {event.end.strftime('%H:%M')}"
    if event.location: content += f"\n📍 {event.location}"
    if event.url: content += f"\n🔗 {event.url}"
    try: requests.post(url, json={"content": content}, timeout=15)
    except requests.RequestException: pass

# ---------------------------- Scrapers ------------------------------------
def fetch(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def parse_time_range(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = re.search(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", text)
    if m: return m.group(1), m.group(2)
    m2 = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm)?)", text, re.I)
    if m2: return m2.group(1), None
    return None, None

# ---- Dark Sphere (calendar)
DARKSPHERE_URL = "https://www.darksphere.co.uk/gamingcalendar.php"
def scrape_darksphere(now: datetime) -> List[RBEvent]:
    soup = fetch(DARKSPHERE_URL); events: List[RBEvent] = []
    month_text_el = soup.find(string=re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$", re.I))
    month_name = month_text_el.strip() if month_text_el else now.strftime("%B")
    year = now.year
    current_day: Optional[int] = None
    for node in soup.find_all(True):
        if node.name in ("div","p","span","h1","h2","h3","h4") and node.get_text(strip=True):
            mday = re.match(r"^(\d{1,2})\.\s*[A-Za-z]+$", node.get_text(strip=True))
            if mday: current_day = int(mday.group(1)); continue
        if node.name == "a" and node.get_text(strip=True):
            title = node.get_text(" ", strip=True)
            if "riftbound" not in title.lower(): continue
            time_text = ""
            for sib in list(node.next_siblings)[:3]:
                if hasattr(sib,'get_text'): time_text += " " + sib.get_text(" ", strip=True)
                elif isinstance(sib,str): time_text += " " + sib.strip()
            start_str, end_str = parse_time_range(time_text)

            if current_day is None:
                prev = node
                for _ in range(10):
                    prev = prev.find_previous()
                    if not prev: break
                    txt = prev.get_text(" ", strip=True) if hasattr(prev,'get_text') else str(prev)
                    mday2 = re.match(r"^(\d{1,2})\.\s*[A-Za-z]+$", txt)
                    if mday2: current_day = int(mday2.group(1)); break
            if current_day is None: continue

            event_date = resolve_calendar_date(month_name, year, current_day)
            if not event_date: continue
            start_time = start_str or "19:00"
            start_dt = londonify(dtparse.parse(f"{event_date.isoformat()} {start_time}", dayfirst=False))
            end_dt = londonify(dtparse.parse(f"{event_date.isoformat()} {end_str}", dayfirst=False)) if end_str else guess_end(start_dt, 4)
            events.append(RBEvent(
                title=title, start=start_dt, end=end_dt,
                url=requests.compat.urljoin(DARKSPHERE_URL, node.get("href", "")),
                store="Dark Sphere (Shepherd's Bush)", location="Shepherd's Bush Megastore, London"
            ))
    return events

# ---- Shared helpers for Shopify-style (Spellbound/Brotherhood/Leisure)
def _extract_date_from_title(title: str, default_year: int) -> Optional[date]:
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?", title)
    if not m: return None
    d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (2000 + int(y)) if y and len(y) == 2 else (int(y) if y else default_year)
    try: return date(year, mth, d)
    except ValueError: return None

def _extract_date_loose(text: str, default_year: int) -> Optional[date]:
    try:
        dt = dtparse.parse(text, dayfirst=True, fuzzy=True, default=datetime(default_year,1,1))
        return date(dt.year, dt.month, dt.day)
    except Exception:
        return None

def _scrape_shopify_products(hub_url: str, store_name: str, now: datetime) -> List[RBEvent]:
    soup = fetch(hub_url); events: List[RBEvent] = []
    for a in soup.select("a[href*='/products/']"):
        title = a.get_text(" ", strip=True); href = a.get("href")
        if not href: continue
        url = requests.compat.urljoin(hub_url, href)
        if "riftbound" not in (title or "").lower(): continue
        ev_date = _extract_date_from_title(title, now.year) or _extract_date_loose(title, now.year)
        start_time, end_time = "19:00", "23:00"
        if ev_date is None:
            try:
                psoup = fetch(url); body = psoup.get_text(" ", strip=True)
                ev_date = _extract_date_from_title(body, now.year) or _extract_date_loose(body, now.year)
                s, e = parse_time_range(body); 
                if s: start_time = s
                if e: end_time = e
            except Exception: pass
        if ev_date is None: continue
        start_dt = londonify(dtparse.parse(f"{ev_date.isoformat()} {start_time}", dayfirst=False))
        end_dt = londonify(dtparse.parse(f"{ev_date.isoformat()} {end_time}", dayfirst=False))
        events.append(RBEvent(title=title, start=start_dt, end=end_dt, url=url, store=store_name, location=store_name))
    return list({(e.title, e.start): e for e in events}.values())

# ---- Spellbound (Shopify)
SPELLBOUND_COLLECTION = "https://spellboundgames.co.uk/collections/events"
def scrape_spellbound(now: datetime) -> List[RBEvent]:
    return _scrape_shopify_products(SPELLBOUND_COLLECTION, "Spellbound Games (London)", now)

# ---- Brotherhood (Shopify)
BROTHERHOOD_EVENTS = "https://thebrotherhoodgames.co.uk/product-category/events/"
def scrape_brotherhood(now: datetime) -> List[RBEvent]:
    return _scrape_shopify_products(BROTHERHOOD_EVENTS, "The Brotherhood Games (Bermondsey)", now)

# ---- Leisure Games (Shopify)
LEISURE_TICKETS = "https://leisuregames.com/collections/tickets"
def scrape_leisure(now: datetime) -> List[RBEvent]:
    return _scrape_shopify_products(LEISURE_TICKETS, "Leisure Games (Finchley)", now)

# ---- Zombie Games Café (Wix)
ZOMBIE_TICKETS = "https://www.zombiegamescafe.com/tcg-events-tickets"
ZOMBIE_ALL_TICKETS = "https://www.zombiegamescafe.com/all-tcg-event-tickets"
def _zombie_collect_product_links(soup: BeautifulSoup) -> List[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; label = a.get_text(" ", strip=True).lower()
        if "product-page" in href and "riftbound" in (label + " " + href.lower()):
            links.append(requests.compat.urljoin(ZOMBIE_TICKETS, href))
    return list(dict.fromkeys(links))
def _extract_dt_from_text(text: str, default_year: int) -> Tuple[Optional[date], Optional[str], Optional[str]]:
    txt = " ".join(text.split())
    tm = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm)?)", txt, re.I)
    dm = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", txt)
    start_time = tm.group(1) if tm else None
    ev_date = None
    if dm:
        d, mth, y = int(dm.group(1)), int(dm.group(2)), dm.group(3)
        year = (2000 + int(y)) if y and len(y) == 2 else (int(y) if y else default_year)
        try: ev_date = date(year, mth, d)
        except ValueError: ev_date = None
    return ev_date, start_time, None
def scrape_zombie(now: datetime) -> List[RBEvent]:
    events: List[RBEvent] = []
    for hub in (ZOMBIE_TICKETS, ZOMBIE_ALL_TICKETS):
        try: hub_soup = fetch(hub)
        except Exception: continue
        for url in _zombie_collect_product_links(hub_soup):
            try: psoup = fetch(url)
            except Exception: continue
            title = psoup.title.get_text(strip=True) if psoup.title else url
            body = psoup.get_text(" ", strip=True)
            if "riftbound" not in (title + " " + body).lower(): continue
            ev_date, start_time, end_time = _extract_dt_from_text(title + " " + body, now.year)
            if not ev_date: continue
            start_time = start_time or "18:30"
            start_dt = londonify(dtparse.parse(f"{ev_date.isoformat()} {start_time}", dayfirst=False))
            end_dt = londonify(dtparse.parse(f"{ev_date.isoformat()} {end_time}", dayfirst=False)) if end_time else guess_end(start_dt, 3)
            events.append(RBEvent(title=title, start=start_dt, end=end_dt, url=url, store="Zombie Games Café (Cricklewood)", location="Zombie Games Café, London"))
    return list({(e.title, e.start): e for e in events}.values())

# ---- Europa Gaming (standalone event pages)
EUROPA_HOME = "https://www.europagaming.co.uk/"
def _europa_collect_event_links(soup: BeautifulSoup) -> List[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; label = a.get_text(" ", strip=True).lower()
        if "event-details-registration" in href and "riftbound" in (label + " " + href.lower()):
            links.append(requests.compat.urljoin(EUROPA_HOME, href))
    return list(dict.fromkeys(links))
def scrape_europa(now: datetime) -> List[RBEvent]:
    events: List[RBEvent] = []
    try:
        home = fetch(EUROPA_HOME)
        hrefs = _europa_collect_event_links(home)
    except Exception:
        hrefs = []
    if not hrefs:
        hrefs = [
            "https://www.europagaming.co.uk/event-details-registration/riftbound-release-event",
            "https://www.europagaming.co.uk/event-details-registration/riftbound-summoner-skirmish-europa-gaming-2025-12-13-13-00",
        ]
    for url in hrefs:
        try: psoup = fetch(url)
        except Exception: continue
        title = psoup.title.get_text(strip=True) if psoup.title else url
        body = psoup.get_text(" ", strip=True)
        if "riftbound" not in (title + " " + body).lower(): continue
        s, e = parse_time_range(body)
        evd = _extract_date_from_title(title, now.year) or _extract_date_loose(title, now.year)
        if not evd: evd = _extract_date_from_title(body, now.year) or _extract_date_loose(body, now.year)
        if not evd: continue
        start_time = s or "13:00"
        start_dt = londonify(dtparse.parse(f"{evd.isoformat()} {start_time}", dayfirst=False))
        end_dt = londonify(dtparse.parse(f"{evd.isoformat()} {e}", dayfirst=False)) if e else guess_end(start_dt, 4)
        events.append(RBEvent(title=title, start=start_dt, end=end_dt, url=url, store="Europa Gaming (Wembley)", location="Europa Gaming, Wembley"))
    return list({(e.title, e.start): e for e in events}.values())

# ----------------------- De-dup helpers -----------------------------------
def _canonical_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[-–:]+", " ", t)
    t = t.replace("nexus nights", "nexus night")
    t = re.sub(r"\s+", " ", t).strip()
    return t
def _prefer_event(a: RBEvent, b: RBEvent) -> RBEvent:
    def score(ev: RBEvent) -> tuple:
        url_score = 1 if "event.php" in (ev.url or "") else 0
        title_score = len(ev.title or "")
        return (url_score, title_score)
    return a if score(a) >= score(b) else b
def _dedup_slot_conflicts(events: List[RBEvent]) -> List[RBEvent]:
    by_key: dict = {}
    for ev in events:
        key = (ev.store, ev.start.replace(second=0, microsecond=0))
        if key not in by_key: by_key[key] = ev
        else:
            if "riftbound" in _canonical_title(ev.title) and "riftbound" in _canonical_title(by_key[key].title):
                by_key[key] = _prefer_event(by_key[key], ev)
            else:
                alt_key = (ev.store, ev.start.replace(second=0, microsecond=0), _canonical_title(ev.title))
                cur = by_key.get(alt_key)
                by_key[alt_key] = _prefer_event(cur, ev) if cur else ev
    return list(by_key.values())

# ----------------------- Export helpers (ICS/CSV) --------------------------
def export_ics(events: List[RBEvent], path: str):
    cal = Calendar()
    for ev in events:
        name = f"{ev.store}: {ev.title}"
        tag = _op_tag_for(ev.title)
        if tag: name = f"[{tag}] {name}"
        ics_ev = IcsEvent()
        ics_ev.name = name
        ics_ev.begin = ev.start
        if ev.end: ics_ev.end = ev.end
        ics_ev.url = ev.url
        ics_ev.location = ev.location or ev.store
        ics_ev.uid = ev.stable_id()
        cal.events.add(ics_ev)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(cal)
def export_csv(events: List[RBEvent], path: str):
    import csv
    fields = ["date","start","end","store","title","url","location"]
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for ev in sorted(events, key=lambda e: (e.start, e.store)):
            w.writerow({"date":ev.start.strftime("%Y-%m-%d"), "start":ev.start.strftime("%H:%M"),
                        "end":ev.end.strftime("%H:%M") if ev.end else "", "store":ev.store,
                        "title":ev.title, "url":ev.url, "location":ev.location or ""})

# ----------------------------- Main flow ----------------------------------
def find_events(now: Optional[datetime] = None) -> List[RBEvent]:
    now = londonify(now or datetime.now(tz=LONDON_TZ))
    evs: List[RBEvent] = []
    try: evs.extend(scrape_darksphere(now))
    except Exception as e: print(f"[warn] Dark Sphere scrape failed: {e}")
    try: evs.extend(scrape_spellbound(now))
    except Exception as e: print(f"[warn] Spellbound scrape failed: {e}")
    try: evs.extend(scrape_brotherhood(now))
    except Exception as e: print(f"[warn] Brotherhood scrape failed: {e}")
    try: evs.extend(scrape_leisure(now))
    except Exception as e: print(f"[warn] Leisure Games scrape failed: {e}")
    try: evs.extend(scrape_zombie(now))
    except Exception as e: print(f"[warn] Zombie Games scrape failed: {e}")
    try: evs.extend(scrape_europa(now))
    except Exception as e: print(f"[warn] Europa Gaming scrape failed: {e}")
    evs = [e for e in evs if e.start >= (now - timedelta(days=1))]
    evs = _dedup_slot_conflicts(evs)
    return evs

def run_once(post: bool = True) -> List[RBEvent]:
    seen = load_state()
    now = londonify(datetime.now(tz=LONDON_TZ))
    events = find_events(now)

    new_events: List[RBEvent] = []
    for e in events:
        if e.uid() in seen or e.stable_id() in seen: continue
        new_events.append(e)

    for ev in sorted(new_events, key=lambda e: e.start):
        print(f"NEW: {ev.start:%Y-%m-%d %H:%M} — {ev.store} — {ev.title}\n{ev.url}\n")
        if post: post_discord(ev)
        seen.add(ev.uid()); seen.add(ev.stable_id())

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
            export_ics(events, args.ics_path); print(f"Wrote {args.ics_path}")
        if args.csv_path:
            export_csv(events, args.csv_path); print(f"Wrote {args.csv_path}")

if __name__ == "__main__":
    main()
