"""Exchange holiday calendars known in advance: which days are sessions, the session after a day, a day's close.

One file per market and year, ``data/calendar/<market>-<year>.json`` (scripts/fetch_calendar.py, from the exchange's
own list — ``source`` and ``fetched`` in the file): ``holidays`` (weekdays with no session), ``early_close``
(date -> local close time, the US half days) and ``sessions`` (date -> ``{hours: "HH:MM-HH:MM", name, source,
fetched}``, a day whose hours move — KRX's college-entrance-exam day opens 10:00 and closes 16:30; written by hand
from the exchange's notice, kept when the list is fetched again). The daily loop and the scheduler — and so the dashboard's next run,
which calls ``scheduler.plan`` — all ask here. A year with no file falls back to every weekday a session, the
behaviour before the calendar (``covered`` says which; the loop notes "휴장 달력 없음 <year>"). A closure no list
had (a sudden one) is still caught after the fact by the loop: no benchmark bar on the day.

The session hours themselves stay ``config.MARKETS[market]["session"]`` in the market's ``timezone``.
TRADING_CALENDAR_DIR overrides the folder (tests).
"""

from __future__ import annotations

import json
import os
from datetime import date, time, timedelta
from pathlib import Path

from agent_trading_core.config import MARKETS

ROOT = Path(__file__).resolve().parents[1]
_loaded: dict[Path, tuple[int, dict]] = {}


def calendar_dir() -> Path:
    return Path(os.environ.get("TRADING_CALENDAR_DIR") or ROOT / "data" / "calendar")


def year_calendar(market: str, year: int) -> dict | None:
    """The market's calendar file of ``year`` (re-read when it changes), None without one."""
    path = calendar_dir() / f"{market}-{year}.json"
    try:
        stamp = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    hit = _loaded.get(path)
    if hit is None or hit[0] != stamp:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("market") != market or body.get("year") != year:
            raise ValueError(f"{path.name}: market/year {body.get('market')}/{body.get('year')} — 파일 이름과 다름")
        hit = _loaded[path] = (stamp, body)
    return hit[1]


def covered(market: str, year: int) -> bool:
    return year_calendar(market, year) is not None


def holiday(market: str, day: date) -> str | None:
    """The holiday's name (``""`` when the list gives none) if ``day`` is a listed holiday, else None."""
    cal = year_calendar(market, day.year)
    if cal is None:
        return None
    return next((h["name"] for h in cal["holidays"] if h["date"] == day.isoformat()), None)


def is_session(market: str, day: date) -> bool:
    """A weekday that is not a listed holiday (no calendar for the year: every weekday)."""
    return day.weekday() < 5 and holiday(market, day) is None


def next_session(market: str, day: date) -> date:
    """The first session after ``day``."""
    d = day + timedelta(days=1)
    while not is_session(market, d):
        d += timedelta(days=1)
    return d


def _hours(market: str, day: date) -> tuple[time, time]:
    cal = year_calendar(market, day.year) or {}
    moved = cal.get("sessions", {}).get(day.isoformat())
    opens, closes = (moved["hours"] if moved else MARKETS[market]["session"]).split("-")
    return time.fromisoformat(opens), time.fromisoformat(cal.get("early_close", {}).get(day.isoformat()) or closes)


def session_open(market: str, day: date) -> time:
    """The day's open: the calendar's ``sessions`` hours that day, else the market's regular open."""
    return _hours(market, day)[0]


def session_close(market: str, day: date) -> time:
    """The day's close: the calendar's ``early_close`` that day, else its ``sessions`` hours, else the regular close."""
    return _hours(market, day)[1]


def source(market: str, year: int) -> str | None:
    """Where the year's list came from and when: ``"<url> (<fetched>)"``, None without a file."""
    cal = year_calendar(market, year)
    return f"{cal['source']} ({cal['fetched']})" if cal else None
