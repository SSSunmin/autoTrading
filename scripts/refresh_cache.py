"""Daily price refresh from KIS (docs/00 §5): each cache is brought up to yesterday's bar (in the
market's time zone): KIS keeps moving today's bar after the close, so it is never cached (kis.last_final_day).

For one market: its benchmark index, the fixed forty of scripts/paper_backtest.py and today's members of the
point-in-time index history (data/universe/). A cached symbol is fetched from its last date on; a new one gets five
years (kis.update_cache). Files are ``data/cache/<symbol>-KIS-data.csv`` — the yfinance files are not touched.

Every run appends one line to ``data/cache/_refresh_log.jsonl``: counts by status, rows added, the symbols that
failed (with the error, never a credential), the ones that came back with no row, the ones whose last bar is more than ten days old, calls and seconds.
A failed symbol does not stop the run; the exit code is 1 when any failed.

Run:  .venv/Scripts/python scripts/refresh_cache.py --source kis --market KRX [--since auto|YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from paper_backtest import HISTORIES, UNIVERSE, US_UNIVERSE  # the fixed forty of each market, one list

from agent_trading_core import kis
from agent_trading_core.config import MARKETS
from agent_trading_core.errors import VendorNotConfiguredError
from agent_trading_core.universe import load_history, members_on

FIXED = {"KRX": UNIVERSE, "US": US_UNIVERSE}
# the repository's cache, not config.data_cache_dir (~/.agent_trading_core/cache, shared by every checkout)
CACHE = ROOT / "data" / "cache"
LOG = "_refresh_log.jsonl"
STALE_DAYS = 10


def symbols_for(market: str, on) -> list[str]:
    """The benchmark, then the fixed forty, then the index members on ``on`` not already listed."""
    members = sorted(members_on(load_history(HISTORIES[market]), on))
    return list(dict.fromkeys([MARKETS[market]["benchmark"], *FIXED[market], *members]))


def refresh(symbols: list[str], cache_dir: str, client, since: str | None) -> tuple[list[dict], list[dict]]:
    done, failed = [], []
    for i, symbol in enumerate(symbols, 1):
        try:
            done.append(kis.update_cache(symbol, cache_dir, client, since=since))
        except Exception as e:  # one symbol's failure is logged and the rest go on
            failed.append({"symbol": symbol, "error": f"{type(e).__name__}: {e}"[:300]})
        if i % 20 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)} (failed {len(failed)}, calls {client.stats['calls']})", flush=True)
    return done, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="kis", choices=["kis"])
    parser.add_argument("--market", required=True, choices=sorted(FIXED))
    parser.add_argument("--since", default="auto", help="auto = each file's last date; or YYYY-MM-DD to refetch from")
    parser.add_argument("--cache-dir", default=str(CACHE), help="default data/cache, where paper_backtest reads")
    args = parser.parse_args()
    since = None if args.since == "auto" else datetime.strptime(args.since, "%Y-%m-%d").strftime("%Y-%m-%d")

    cache_dir = args.cache_dir
    try:
        client = kis.KisClient()
    except VendorNotConfiguredError as e:
        print(e)
        return 2
    end = kis.last_final_day(args.market)
    symbols = symbols_for(args.market, end)
    print(f"{args.market}: {len(symbols)} symbols up to {end} (since {args.since})")
    t0 = time.monotonic()
    done, failed = refresh(symbols, cache_dir, client, since)
    seconds = round(time.monotonic() - t0, 1)

    stale = sorted(r["symbol"] for r in done if r["last"] < (end - timedelta(days=STALE_DAYS)).isoformat())
    empty = sorted(r["symbol"] for r in done if r["status"] == "empty")    # asked, nothing came back
    entry = {"at": datetime.now(kis.KST).isoformat(timespec="seconds"), "source": args.source, "market": args.market,
             "since": args.since, "last_final_day": end.isoformat(), "symbols": len(symbols),
             "status": dict(Counter(r["status"] for r in done)), "rows_added": sum(r["rows_added"] for r in done),
             "failed": failed, "empty": empty, "stale": stale, "seconds": seconds, **client.stats}
    log = Path(cache_dir) / LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"status {entry['status']}, rows added {entry['rows_added']}, failed {len(failed)}, "
          f"stale(>{STALE_DAYS}d) {len(stale)}, calls {client.stats['calls']}, throttled {client.stats['throttled']}, "
          f"token issued {client.stats['token_issued']}, {seconds}s")
    for f in failed:
        print(f"  failed {f['symbol']}: {f['error']}")
    if empty:
        print(f"  empty (no rows back): {', '.join(empty)}")
    if stale:
        print(f"  stale: {', '.join(stale)}")
    print(f"log → {log}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
