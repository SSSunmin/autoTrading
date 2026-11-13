"""Standalone scheduler of the daily paper loop: one long-running process, standard library only.

Run:  python -m agent_trading_core.scheduler --markets KRX,US
      python -m agent_trading_core.scheduler --markets KRX,US --plan 6     # the next 6 stage times, then exit

It sleeps until the next stage time of each market — ``daily_loop.stage_time`` from ``MARKETS[...]["session"]`` and
``timezone`` (daylight saving follows the zone, the host's time zone is never read), the stages in
``daily_loop.SCHEDULED`` — then runs every stage that is due (``daily_loop.run_due``, the same call as
``daily_loop.py`` without ``--date`` from cron): the day whose stage time has come while the market's next session
has not opened. A restart therefore runs a stage it slept through once, if that session is still on; a day missed
past the next open is recorded ``skipped(missed)``. A due stage that ended ``failed`` is tried again every
``RETRY`` within its window (a late feed comes in; after midnight a day without a bar is settled as a holiday). Every
outcome is a ``RunRecord`` in ``audit/<market>/runs.jsonl``; the process prints one line per record.

SIGTERM or SIGINT never cuts a stage: the stage running finishes, no further stage starts, and the process exits 0.
Environment: ``TRADING_DATA_DIR`` (data directory, default ``data/``), ``TRADING_ENV_FILE`` (the ``.env`` with the
KIS paper keys, default the repository's), ``TRADING_MARKETS`` (default of ``--markets``).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_trading_core import kis
from agent_trading_core.audit import market_audit
from agent_trading_core.config import MARKETS

ROOT = Path(__file__).resolve().parents[1]
RETRY = timedelta(hours=1)     # a failed stage is run again this often while it is due
NAP = 5.0                      # seconds slept at a time: the clock and the stop flag are looked at in between


def daily_loop():
    """``scripts/daily_loop.py`` of this checkout.

    ponytail: the loop lives in scripts/ (it reuses scripts/baseline_krx), so the scheduler needs the checkout, not
    only the installed package. Move both into the package if it is ever deployed without the repository."""
    spec = importlib.util.spec_from_file_location("daily_loop", ROOT / "scripts" / "daily_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(dl, markets: list[str], after: datetime, n: int) -> list[tuple[datetime, str, str]]:
    """The next ``n`` stage times after ``after``: (aware time, market, stage), earliest first."""
    out = []
    for m in markets:
        local = after.astimezone(ZoneInfo(MARKETS[m]["timezone"])).date()
        for i in range(-1, n + 8):          # weekends and the time-zone shift fit in the margin
            d = local + timedelta(days=i)
            if d.weekday() < 5:
                out += [(t, m, s) for s in dl.SCHEDULED[m] if (t := dl.stage_time(m, s, d)) > after]
    return sorted(out)[:n]


def next_wake(dl, markets: list[str], now: datetime, data: Path) -> datetime:
    """The next stage time, or ``now + RETRY`` when a due stage's last record is ``failed``."""
    wake = plan(dl, markets, now, 1)[0][0]
    for m in markets:
        runs = market_audit(data / "audit", m).runs()
        for s in dl.SCHEDULED[m]:
            day = dl.due_day(m, s, now)
            last = [r for r in runs if (r.stage, r.trade_date) == (s, day)]
            if day and last and last[-1].status == "failed":
                wake = min(wake, now + RETRY)
    return wake


def tick(dl, markets: list[str], data: Path, client, now: datetime, stop: list) -> list:
    """Every due stage of every market, fill before decide; nothing starts once ``stop`` is set, and a market's
    decide does not run after its fill failed (as ``daily_loop --stage all``). The records written."""
    out = []
    for m in markets:
        for s in [s for s in ("fill", "decide") if s in dl.SCHEDULED[m]]:
            if stop:
                return out
            try:
                records = dl.run_due(m, s, data, client, now)
            except Exception as e:      # the audit files themselves: printed, the next wake tries again
                print(f"{m} {s}: 실행 오류 — {type(e).__name__}: {e}"[:500], flush=True)
                break
            for r in records:
                print(dl.summary(r), flush=True)
            out += records
            if records and records[-1].status == "failed":
                break
    return out


def serve(dl, markets: list[str], data: Path, client, stop: list, clock=None, sleep=time.sleep) -> None:
    """Run until ``stop`` is set: due stages, then sleep to the next wake in ``NAP`` steps (a suspended host or a
    changed clock is seen on the next step)."""
    clock = clock or (lambda: datetime.now(kis.KST))
    while not stop:
        tick(dl, markets, data, client, clock(), stop)
        if stop:
            break
        try:
            wake = next_wake(dl, markets, clock(), data)
        except Exception as e:          # an unreadable runs.jsonl: printed, the process keeps its schedule
            print(f"다음 깨어남 계산 오류 — {type(e).__name__}: {e}"[:500], flush=True)
            wake = plan(dl, markets, clock(), 1)[0][0]
        print(f"다음 깨어남 {wake.astimezone(kis.KST):%Y-%m-%d %H:%M} KST", flush=True)
        while not stop and (left := (wake - clock()).total_seconds()) > 0:
            sleep(min(NAP, left))


def stop_on_signals(stop: list) -> None:
    """SIGTERM · SIGINT only set ``stop``: the stage running finishes first."""
    def handler(signum, frame):
        if not stop:
            print(f"종료 신호({signal.Signals(signum).name}) — 진행 중인 단계를 끝내고 멈춤", flush=True)
        stop.append(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    dl = daily_loop()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markets", default=os.environ.get("TRADING_MARKETS") or "KRX,US",
                        help="comma-separated, of " + ",".join(sorted(dl.RUN_IDS)))
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    parser.add_argument("--plan", type=int, metavar="N", help="print the next N stage times and exit")
    args = parser.parse_args(argv)
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    if not markets or set(markets) - set(dl.RUN_IDS):
        parser.error(f"--markets는 {','.join(sorted(dl.RUN_IDS))} 중에서: {args.markets!r}")
    if args.plan:
        for at, m, s in plan(dl, markets, datetime.now(kis.KST), args.plan):
            print(f"{at.astimezone(kis.KST):%Y-%m-%d %a %H:%M} KST  {m} {s}  (현지 {at:%H:%M %Z})")
        return 0
    try:
        client = kis.KisClient()
    except Exception as e:     # a missing variable is named, never a value
        print(f"KIS 클라이언트 실패 — {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    stop: list = []
    stop_on_signals(stop)
    print(f"스케줄러 시작 — {','.join(markets)} · 데이터 {args.data_dir}", flush=True)
    serve(dl, markets, Path(args.data_dir), client, stop)
    print("스케줄러 종료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
