"""Standalone scheduler of the daily paper loop: one long-running process, standard library only.

Run:  python -m agent_trading_core.scheduler --markets KRX,US
      python -m agent_trading_core.scheduler --markets KRX,US --plan 6     # the next 6 stage times, then exit

It sleeps until the next stage time of each market's next session (``market_calendar.is_session``: no wake on a
weekend or a listed holiday) — ``daily_loop.stage_time`` from ``MARKETS[...]["session"]`` (a US half day's early
close from the calendar) and
``timezone`` (daylight saving follows the zone, the host's time zone is never read), the stages in
``daily_loop.SCHEDULED`` — then runs every stage that is due (``daily_loop.run_due``, the same call as
``daily_loop.py`` without ``--date`` from cron): the day whose stage time has come while the market's next session
has not opened. A restart therefore runs a stage it slept through once, if that session is still on; a day missed
past the next open is recorded ``skipped(missed)``. A due stage that ended ``failed`` is tried again every
``RETRY`` within its window (a late feed comes in; after midnight a day without a bar is settled as a holiday). Every
outcome is a ``RunRecord`` in ``audit/<market>/runs.jsonl``; the process prints one line per record.
Inside a stage, a KIS call that got no answer (``errors.VendorNetworkError``: connection, name resolution, timeout —
the host woke from sleep seconds ago) is made again after ``NET_WAIT``, up to ``NET_TRIES`` times per wake
(``Patient``), before the stage sees it: the stage still ends with one record, ``ok`` or the ``failed`` it got.
Once a week (``WEEKLY``: Saturday 10:00 KST, with US in ``--markets``) it also refreshes the US next-earnings dates
(``scripts/refresh_earnings.py`` as a child process, after the wake's stages, ``WEEKLY_TIMEOUT`` at most): a week the
process slept through is run at its next wake, a week already in the file is not run again, and a failure is only
printed — no stage waits for it or is stopped by it. ``plan`` · ``next_wake`` (the backend's next run) leave it out;
``weekly_plan`` and ``--plan`` show it.

SIGTERM or SIGINT never cuts a stage: the stage running finishes, no further stage starts, and the process exits 0.
Environment: ``TRADING_DATA_DIR`` (data directory, default ``data/``), ``TRADING_ENV_FILE`` (the ``.env`` with the
KIS paper keys, default the repository's), ``TRADING_MARKETS`` (default of ``--markets``).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_trading_core import kis, market_calendar
from agent_trading_core.audit import market_audit
from agent_trading_core.config import MARKETS
from agent_trading_core.errors import VendorNetworkError

ROOT = Path(__file__).resolve().parents[1]
RETRY = timedelta(hours=1)     # a failed stage is run again this often while it is due
NAP = 5.0                      # seconds slept at a time: the clock and the stop flag are looked at in between
# 2026-11-19 06:05 KST the US decide ran 2 s after the PC woke and failed with ConnectionError (ok by hand at 09:21):
# a call with no answer is asked again this long after, this many times per wake for all calls together
NET_WAIT = timedelta(minutes=2)
NET_TRIES = 3
# market → (job, weekday, KST time). Saturday 10:00 KST: neither market is open and no stage is near (the US Friday
# decide runs 05:05 / 06:05 KST, the KRX fill Monday 09:05). The file is a reference badge only (docs/10 D8)
WEEKLY = {"US": ("earnings", 5, dtime(10, 0))}
WEEKLY_TIMEOUT = 600           # seconds: 40 yfinance calls take about a minute
EARNINGS_FILE = "_earnings_us.json"      # scripts/refresh_earnings.py FILE


def daily_loop():
    """``scripts/daily_loop.py`` of this checkout.

    ponytail: the loop lives in scripts/ (it reuses scripts/baseline_krx), so the scheduler needs the checkout, not
    only the installed package. Move both into the package if it is ever deployed without the repository."""
    spec = importlib.util.spec_from_file_location("daily_loop", ROOT / "scripts" / "daily_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Patient:
    """The KIS client as the stages of one wake see it: a call (``index_daily`` · ``us_daily`` · …, read-only) that
    raised ``VendorNetworkError`` is made again after ``NET_WAIT``, while ``left`` (``NET_TRIES`` per wake, shared by
    every call — a network that stays down fails the stage in minutes, not minutes per name) lasts and no stop came;
    then the error goes on to the stage, which records it as before. Any other error goes on at once."""

    def __init__(self, client, stop: list, clock, sleep):
        self.client, self.stop, self.clock, self.sleep, self.left = client, stop, clock, sleep, NET_TRIES

    def __getattr__(self, name):
        attr = getattr(self.client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            while True:
                try:
                    return attr(*args, **kwargs)
                except VendorNetworkError as e:
                    if self.left <= 0 or self.stop:
                        raise
                    self.left -= 1
                    print(f"네트워크 오류({e}) — {NET_WAIT.total_seconds() / 60:g}분 뒤 다시 "
                          f"({NET_TRIES - self.left}/{NET_TRIES})", flush=True)
                    until = self.clock() + NET_WAIT
                    while not self.stop and (left := (until - self.clock()).total_seconds()) > 0:
                        self.sleep(min(NAP, left))
                    if self.stop:       # a stop during the wait: the stage ends with this error, nothing more asked
                        raise
        return call


def plan(dl, markets: list[str], after: datetime, n: int) -> list[tuple[datetime, str, str]]:
    """The next ``n`` stage times after ``after``: (aware time, market, stage), earliest first — sessions only
    (``market_calendar.is_session``: weekends and listed holidays have none)."""
    out = []
    for m in markets:
        local = after.astimezone(ZoneInfo(MARKETS[m]["timezone"])).date()
        for i in range(-1, n + 14):         # weekends, the longest closure and the time-zone shift fit in the margin
            d = local + timedelta(days=i)
            if market_calendar.is_session(m, d):
                out += [(t, m, s) for s in dl.SCHEDULED[m] if (t := dl.stage_time(m, s, d)) > after]
    return sorted(out)[:n]


def weekly_plan(markets: list[str], after: datetime, n: int) -> list[tuple[datetime, str, str]]:
    """The next ``n`` weekly job times after ``after``, as ``plan`` lists stages: (aware KST time, market, job)."""
    out, today = [], after.astimezone(kis.KST).date()
    for m in [m for m in markets if m in WEEKLY]:
        job, weekday, at = WEEKLY[m]
        first = today + timedelta(days=(weekday - today.weekday()) % 7)
        out += [(t, m, job) for i in range(n + 1) if (t := datetime.combine(first + timedelta(weeks=i), at, kis.KST)) > after]
    return sorted(out)[:n]


def weekly_slot(market: str, now: datetime) -> datetime:
    """The latest weekly job time of ``market`` at or before ``now``."""
    _, weekday, at = WEEKLY[market]
    d = now.astimezone(kis.KST).date()
    slot = datetime.combine(d - timedelta(days=(d.weekday() - weekday) % 7), at, kis.KST)
    return slot if slot <= now else slot - timedelta(weeks=1)


def earnings_day(data: Path) -> str | None:
    """The latest day the earnings file was refreshed (an entry's ``fetched`` or ``failed``), None without one."""
    try:
        book = json.loads((data / "cache" / EARNINGS_FILE).read_text(encoding="utf-8"))
        return max((d for e in book.values() for d in (e.get("fetched"), e.get("failed")) if d), default=None)
    except (OSError, ValueError, AttributeError, TypeError):    # no file, or one the script will rewrite: run it
        return None


def refresh_earnings(data: Path, stop: list = ()) -> str:
    """``scripts/refresh_earnings.py`` into ``data/cache`` as a child process; its outcome as one line, never raises.
    Waited on a second at a time: past ``WEEKLY_TIMEOUT`` or once ``stop`` is set (SIGTERM — a container's stop gives
    seconds, not minutes) the child is killed; the script writes its file whole (``replace_file``) or not at all."""
    try:
        p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "refresh_earnings.py"), "--cache-dir",
                              str(data / "cache")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", env=os.environ | {"PYTHONUTF8": "1"})
    except Exception as e:      # no interpreter, no script: printed, the stages go on
        return f"failed — {type(e).__name__}: {e}"[:500]
    waited = 0.0
    while True:
        try:
            out, err = p.communicate(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            waited += 1.0
            if stop or waited >= WEEKLY_TIMEOUT:
                p.kill()
                p.communicate()
                return "중단 — 종료 신호" if stop else f"failed — 시간 초과({WEEKLY_TIMEOUT}초)"
    last = (out.strip().splitlines() or err.strip().splitlines() or [""])[-1]
    return (f"ok · {last}" if p.returncode == 0 else f"failed(exit {p.returncode}) · {last}")[:500]


def weekly(markets: list[str], data: Path, now: datetime, done: dict, stop: list, run=None) -> None:
    """Each weekly job whose latest time has come and was not run for it: once per time (``done``: market → the time
    tried, success or not), a time the file already covers counts as run. A failure is printed, nothing raised."""
    for m in [m for m in markets if m in WEEKLY]:
        if stop:
            return
        slot = weekly_slot(m, now)
        if m not in done and (day := earnings_day(data)) and day >= slot.date().isoformat():
            done[m] = slot
        if done.get(m) == slot:
            continue
        done[m] = slot
        print(f"{m} {WEEKLY[m][0]} {slot:%Y-%m-%d}: {(run or refresh_earnings)(data, stop)}", flush=True)


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
    patient, done = Patient(client, stop, clock, sleep), {}
    while not stop:
        patient.left = NET_TRIES        # each wake asks again on its own
        tick(dl, markets, data, patient, clock(), stop)
        try:                            # after the stages: none of them waits for it
            weekly(markets, data, clock(), done, stop)
        except Exception as e:          # an earnings file of an unexpected shape: printed, the schedule goes on
            print(f"주 1회 오류 — {type(e).__name__}: {e}"[:500], flush=True)
        if stop:
            break
        try:
            wake = min([next_wake(dl, markets, clock(), data), *[t for t, _, _ in weekly_plan(markets, clock(), 1)]])
        except Exception as e:          # an unreadable runs.jsonl: printed, the process keeps its schedule
            print(f"다음 깨어남 계산 오류 — {type(e).__name__}: {e}"[:500], flush=True)
            try:
                wake = plan(dl, markets, clock(), 1)[0][0]
            except Exception as e:      # an unreadable calendar file too: printed, tried again in RETRY
                print(f"일정 계산 오류 — {type(e).__name__}: {e}"[:500], flush=True)
                wake = clock() + RETRY
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
        now = datetime.now(kis.KST)
        rows = sorted(plan(dl, markets, now, args.plan) + weekly_plan(markets, now, args.plan))[:args.plan]
        rows += [w for w in weekly_plan(markets, now, len(WEEKLY)) if w not in rows]    # a weekly job past N too
        for at, m, s in rows:
            where = "주 1회" if s == WEEKLY.get(m, ("",))[0] else f"현지 {at:%H:%M %Z}"
            print(f"{at.astimezone(kis.KST):%Y-%m-%d %a %H:%M} KST  {m} {s}  ({where})")
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
