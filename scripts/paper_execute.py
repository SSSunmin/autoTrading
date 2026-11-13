"""Paper execution: fill the recorded decisions at the next trading day's open (agent_trading_core/execution.py).

Run:  .venv/Scripts/python scripts/paper_execute.py --date 2026-07-24
      .venv/Scripts/python scripts/paper_execute.py --all        # every trading day not executed yet, in order
      .venv/Scripts/python scripts/paper_execute.py --market US --all
One market per run (``--market``, default KRX): its own book under data/audit/<market>/ (decisions, orders,
operations), its own index calendar (MARKETS[market]["benchmark"]: KRX ^KS11, US ^GSPC), currency and caps.
KRX also reads the files written straight under data/audit/ before the market axis (never moved or rewritten).
Reads the OHLCV cache (data/cache, never downloads); appends orders, and rule-exit decisions / the executor's own
halts (daily loss, −20% drawdown) and the next session's lift of a daily-loss halt to decisions / operations.
TRADING_DATA_DIR overrides data/.

``--all`` runs every trading day from the first decision (or the last executed day) to the end of the price
data, not only decision days: the rule exits are judged at each day's close. It ends with the audit count of
orders that have no decision behind them (PAPER_TRADING.max_unexplained_orders).

Exit codes: 0 done (also when nothing new was written), 2 bad arguments, 3 refused (halted with exits stopped,
not paper mode, out of order) with nothing written, 4 an audit log line is broken or invalid (nothing written).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.audit import latest_orders, market_audit, trading_state
from agent_trading_core.config import MARKETS
from agent_trading_core.execution import (
    EXIT_SUFFIX,
    ExecutionRefused,
    Prices,
    check_date,
    execute_date,
    paper_config,
    unexplained_orders,
)


def _broken(exc: ValueError, where: str = "") -> int:
    print(f"{where}기록 파손 — {type(exc).__name__}: {str(exc).splitlines()[0]}", file=sys.stderr)
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", help="trading day YYYY-MM-DD")
    which.add_argument("--all", action="store_true", help="every trading day from the last executed one on")
    parser.add_argument("--market", default="KRX", choices=sorted(MARKETS), help="which book (default KRX)")
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    args = parser.parse_args(argv)
    if args.date:
        try:
            check_date(args.date)
        except ValueError:
            parser.error(f"--date는 YYYY-MM-DD 형식이어야 합니다: {args.date!r}")

    data = Path(args.data_dir)
    audit = market_audit(data / "audit", args.market)
    prices = Prices(data / "cache")
    cfg = paper_config(args.market)
    try:
        if args.all:
            done = [o.trade_date for o in latest_orders(audit.orders()).values()]
            agent_days = [d.trade_date for d in audit.decisions() if d.source == "agent"]
            start = max(done) if done else min(agent_days, default=None)
            calendar = prices.calendar(cfg.index)
            # a decision dated on a holiday still runs on its own date (it fills at the next session's open)
            dates = sorted({d for d in calendar + agent_days if start and d >= start})
        else:
            dates = [args.date]
    except ValueError as exc:                    # json / pydantic: a cut-short or invalid line
        return _broken(exc)
    halts = resumes = 0
    for date in dates:
        try:
            seen = len(audit.operations())
            written = execute_date(audit, date, prices, cfg=cfg)
            ops = [op for op in audit.operations()[seen:] if op.operator == "system"]   # the executor's own lines
        except ExecutionRefused as exc:
            print(f"{date}: 거부 — {exc}", file=sys.stderr)
            return 3
        except ValueError as exc:
            return _broken(exc, f"{date}: ")
        exits = sum(o.order_id.endswith(EXIT_SUFFIX) and o.trade_date == date for o in written)
        halts += sum(op.kind == "halt" for op in ops)
        resumes += sum(op.kind == "resume" for op in ops)
        if written or ops or not args.all:
            counts = Counter(o.status for o in written)
            print(f"{date}: {len(written)}건 기록 " + (", ".join(f"{k} {n}" for k, n in sorted(counts.items())) or "(새 기록 없음)")
                  + (f" · 규칙 청산 {exits}" if exits else "")
                  + "".join(f" · {'자동 정지' if op.kind == 'halt' else '자동 재가동'}: {op.reason}" for op in ops))
    try:
        orders, decisions = audit.orders(), audit.decisions()
    except ValueError as exc:
        return _broken(exc)
    missing = unexplained_orders(orders, decisions)
    current = latest_orders(orders).values()
    state = trading_state(audit.operations())
    print(f"{args.market} · 주문 {len(current)}건 · 규칙 청산 {sum(o.order_id.endswith(EXIT_SUFFIX) for o in current)}건 · "
          f"자동 정지 {halts}회 · 자동 재가동 {resumes}회 · 미설명 주문 {len(missing)}건 · 상태 "
          + ("실행 중" if state["running"] else f"정지({state['halted_by']}: {state['halt_reason']})"))
    for o in missing:
        print(f"  미설명: {o.order_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
