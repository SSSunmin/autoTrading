"""Paper execution: fill the recorded decisions at the next trading day's open (agent_trading_core/execution.py).

Run:  .venv/Scripts/python scripts/paper_execute.py --date 2026-07-24
      .venv/Scripts/python scripts/paper_execute.py --all        # every decision date not executed yet, in order
Reads data/audit/decisions.jsonl and operations.jsonl and the OHLCV cache (data/cache, never downloads);
appends to data/audit/orders.jsonl. TRADING_DATA_DIR overrides data/.

Exit codes: 0 done (also when nothing new was written), 2 bad arguments, 3 refused (halted, not paper mode,
out of order) with nothing written, 4 an audit log line is broken or invalid (nothing written).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.audit import AuditLog, latest_orders
from agent_trading_core.execution import ExecutionRefused, Prices, check_date, execute_date


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", help="decision date YYYY-MM-DD")
    which.add_argument("--all", action="store_true", help="every decision date from the last executed one on")
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    args = parser.parse_args(argv)
    if args.date:
        try:
            check_date(args.date)
        except ValueError:
            parser.error(f"--date는 YYYY-MM-DD 형식이어야 합니다: {args.date!r}")

    data = Path(args.data_dir)
    audit = AuditLog(data / "audit")
    prices = Prices(data / "cache")
    try:
        if args.all:
            done = [o.trade_date for o in latest_orders(audit.orders()).values()]
            dates = sorted({d.trade_date for d in audit.decisions() if not done or d.trade_date >= max(done)})
        else:
            dates = [args.date]
    except ValueError as exc:                    # json / pydantic: a cut-short or invalid line
        print(f"기록 파손 — {type(exc).__name__}: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 4
    for date in dates:
        try:
            written = execute_date(audit, date, prices)
        except ExecutionRefused as exc:
            print(f"{date}: 거부 — {exc}", file=sys.stderr)
            return 3
        except ValueError as exc:
            print(f"{date}: 기록 파손 — {type(exc).__name__}: {str(exc).splitlines()[0]}", file=sys.stderr)
            return 4
        counts = Counter(o.status for o in written)
        print(f"{date}: {len(written)}건 기록 " + (", ".join(f"{k} {n}" for k, n in sorted(counts.items())) or "(새 기록 없음)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
