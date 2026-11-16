"""DART cache warm-up and the "재무 스냅샷(DART)" section of KRX tickers (agent_trading_core/dart.py, docs/01 §12-8).
No model call. The section is built as the daily loop builds it with ``LLM["fundamentals"]`` on (the close from the
KIS cache), whatever the flag says, so the cache under ``data/cache/dart/`` is ready before the flag is switched.

Per ticker it prints the DART calls, the cache hits and the seconds; ``--show`` prints the sections. Exit 1 when a
ticker has no figures (the reason is printed, never the key).

Run:  .venv/Scripts/python scripts/refresh_dart.py --day 2026-11-13 --screen [--show]
      .venv/Scripts/python scripts/refresh_dart.py --day 2026-11-13 --tickers 005930.KS 000660.KS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core import agent, dart, kis

CACHE = ROOT / "data" / "cache"


def screened(day: str) -> list[str]:
    """The selected tickers of the day's recorded KRX screen (data/audit/KRX/screens.jsonl)."""
    for line in (ROOT / "data" / "audit" / "KRX" / "screens.jsonl").read_text(encoding="utf-8").splitlines()[::-1]:
        run = json.loads(line)
        if run.get("run_id") == f"daily_krx_{day}":
            return [c["ticker"] for c in sorted(run["candidates"], key=lambda c: c.get("rank") or 0) if c.get("selected")]
    raise SystemExit(f"KRX {day}: 기록된 선별 없음")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", required=True, help="the decision day D (filings received before D)")
    ap.add_argument("--tickers", nargs="*", default=[])
    ap.add_argument("--screen", action="store_true", help="add the day's selected KRX tickers")
    ap.add_argument("--show", action="store_true", help="print each section")
    args = ap.parse_args(argv)
    tickers = list(dict.fromkeys(args.tickers + (screened(args.day) if args.screen else [])))
    client, failed = dart.DartClient(CACHE / "dart"), 0
    print("| 종목 | 결과 | 호출 | 캐시 적중 | 초 |\n|---|---|---:|---:|---:|")
    for t in tickers:
        before, started = dict(client.stats), time.monotonic()
        try:
            close = agent.market_section(kis.read_cache(kis.cache_path(t, CACHE)), args.day).close
            text, summary = dart.build(client, t, args.day, close)
            result = summary
        except dart.DartError as e:
            text, result, failed = dart.missing_text(str(e)), f"없음({e})", failed + 1
        print(f"| {t} | {result} | {client.stats['calls'] - before['calls']} | "
              f"{client.stats['cache_hits'] - before['cache_hits']} | {time.monotonic() - started:.2f} |", flush=True)
        if args.show:
            print(f"\n### {t}\n\n{text}\n")
    print(f"합계: 호출 {client.stats['calls']} · 캐시 적중 {client.stats['cache_hits']} (일 한도 20,000)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
