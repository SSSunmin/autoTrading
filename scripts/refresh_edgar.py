"""SEC EDGAR cache warm-up and the "재무 스냅샷(SEC EDGAR)" section of US tickers (agent_trading_core/edgar.py, docs/01
§12-9). No model call. The section is built as the daily loop builds it with ``LLM["fundamentals"]`` on (the close
from the KIS cache), whatever the flag says, so the cache under ``data/cache/edgar/`` is ready before the flag is
switched. Without SEC_USER_AGENT nothing is requested.

Per ticker it prints the SEC calls, the cache hits and the seconds; ``--show`` prints the sections. Exit 1 when a
ticker has no figures (the reason is printed, never the User-Agent).

Run:  .venv/Scripts/python scripts/refresh_edgar.py --day 2026-11-12 --screen [--show]
      .venv/Scripts/python scripts/refresh_edgar.py --day 2026-11-12 --tickers AAPL MSFT
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core import agent, dart, edgar, kis
from agent_trading_core.audit import market_audit

CACHE = ROOT / "data" / "cache"


def screened(day: str) -> list[str]:
    """The selected tickers of the day's recorded US screen (run ``daily_us_<day>``)."""
    run = next((r for r in market_audit(ROOT / "data" / "audit", "US").screens() if r.run_id == f"daily_us_{day}"), None)
    if run is None:
        raise SystemExit(f"US {day}: 기록된 선별 없음")
    return [c.ticker for c in sorted(run.selected, key=lambda c: c.rank or 0)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", required=True, help="the decision day D (values and filings filed before D)")
    ap.add_argument("--tickers", nargs="*", default=[])
    ap.add_argument("--screen", action="store_true", help="add the day's selected US tickers")
    ap.add_argument("--show", action="store_true", help="print each section")
    args = ap.parse_args(argv)
    tickers = list(dict.fromkeys(args.tickers + (screened(args.day) if args.screen else [])))
    client, failed = edgar.EdgarClient(CACHE / "edgar"), 0
    print("| 종목 | 결과 | 호출 | 캐시 적중 | 초 |\n|---|---|---:|---:|---:|")
    for t in tickers:
        before, started = dict(client.stats), time.monotonic()
        try:      # no price file: the section without per-share figures, as the loop would not ask then
            close = agent.market_section(kis.read_cache(kis.cache_path(t, CACHE)), args.day).close
        except Exception as e:
            close = None
            print(f"(종가 없음 {t}: {type(e).__name__})", flush=True)
        try:
            text, result = edgar.build(client, t, args.day, close)
        except edgar.EdgarError as e:
            text, result, failed = dart.missing_text(str(e)), f"없음({e})", failed + 1
        print(f"| {t} | {result} | {client.stats['calls'] - before['calls']} | "
              f"{client.stats['cache_hits'] - before['cache_hits']} | {time.monotonic() - started:.2f} |", flush=True)
        if args.show:
            print(f"\n### {t}\n\n{text}\n")
    print(f"합계: 호출 {client.stats['calls']} · 캐시 적중 {client.stats['cache_hits']} (요청 간격 "
          f"{edgar.EDGAR['min_interval']}초, SEC 한도 초당 10회)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
