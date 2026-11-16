"""US next-earnings dates, for reading only (docs/10 D8): the ticker screen shows them as a reference badge; no decision,
no executor and no LLM input reads this file.

One yfinance ``Ticker.calendar`` call per symbol, written to ``data/cache/_earnings_us.json`` as
``{symbol: {date, date_to, source, fetched}}`` (``date_to``: the last day of a range Yahoo gives as two dates, else
None). A symbol whose calendar has no earnings date gets ``date: None`` — nothing is made up. A failed call (an HTTP
error such as 404 for an unknown symbol — yfinance would swallow it into an empty calendar, ``yahoo_calendar`` turns
that off) keeps the previous date and records ``error`` and ``failed`` (the day it failed): "조회 실패" and "예정일
없음" stay two different states on the screen.
The screen request never fetches; this command is the only network path. KRX has no source (DART gives no scheduled
dates), so the command takes US symbols only.

Run:  .venv/Scripts/python scripts/refresh_earnings.py [--tickers AAPL MSFT …]   (default: the US fixed forty)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from agent_trading_core.files import replace_file

CACHE = ROOT / "data" / "cache"
FILE = "_earnings_us.json"        # backend/main.py EARNINGS_FILE reads the same name
SOURCE = "yfinance calendar"


def yahoo_calendar(symbol: str) -> dict:
    """The symbol's calendar. yfinance logs an HTTP error (404 for an unknown symbol, 429, …) and answers an empty
    calendar unless ``hide_exceptions`` is off — then a failed call would read as "no date". Off for this call, so the
    error reaches ``refresh`` and is kept as a failure."""
    import yfinance as yf
    from yfinance.config import YfConfig

    hide = YfConfig.debug.hide_exceptions
    YfConfig.debug.hide_exceptions = False
    try:
        return yf.Ticker(symbol).calendar or {}
    finally:
        YfConfig.debug.hide_exceptions = hide


def _iso(v) -> str | None:
    return v.isoformat()[:10] if isinstance(v, date | datetime) else None


def refresh(symbols: list[str], book: dict, fetch=yahoo_calendar, today: str | None = None) -> tuple[dict, dict]:
    """``book`` updated with one ``fetch`` per symbol; (book, counts: calls · dated · none · failed)."""
    today = today or date.today().isoformat()
    counts = {"calls": 0, "dated": 0, "none": 0, "failed": 0}
    for s in symbols:
        counts["calls"] += 1
        try:
            dates = sorted(d for d in map(_iso, fetch(s).get("Earnings Date") or []) if d)
        except Exception as e:  # one symbol's failure is kept on its entry and the rest go on
            book[s] = (book.get(s) or {"date": None, "date_to": None, "source": SOURCE, "fetched": None}) | {
                "error": f"{type(e).__name__}: {e}"[:200], "failed": today}
            counts["failed"] += 1
            continue
        book[s] = {"date": dates[0] if dates else None, "date_to": dates[-1] if len(dates) > 1 else None,
                   "source": SOURCE, "fetched": today}
        counts["dated" if dates else "none"] += 1
    return book, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", help="US symbols; default the fixed forty of scripts/paper_backtest.py")
    parser.add_argument("--cache-dir", default=str(CACHE))
    args = parser.parse_args()
    if args.tickers:
        symbols = [t.upper() for t in args.tickers]
    else:
        from paper_backtest import US_UNIVERSE
        symbols = list(US_UNIVERSE)
    path = Path(args.cache_dir) / FILE
    book = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    book, counts = refresh(symbols, book)
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda tmp: Path(tmp).write_text(json.dumps(book, indent=1, sort_keys=True), encoding="utf-8"))
    print(json.dumps(counts | {"symbols": len(symbols), "file": str(path)}, ensure_ascii=False))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
