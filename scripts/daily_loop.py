"""Daily paper loop of one market: refresh → today's close → screen + rule-baseline decisions → paper orders.

Run:  .venv/Scripts/python scripts/daily_loop.py --market KRX --stage decide       # after the close (15:35 KST)
      .venv/Scripts/python scripts/daily_loop.py --market KRX --stage fill         # after the open (09:05 KST)
      .venv/Scripts/python scripts/daily_loop.py --market US --stage all --date 2026-11-12
      python -m agent_trading_core.scheduler --markets KRX,US                       # runs the stages on time

Stages, for one trading day of the market (default: today in the market's time zone):

- ``decide`` (session close + 5 min): KIS incremental refresh (``kis.update_cache``) of the benchmark, the book's
  universe (baseline_krx.SETUPS) and every ticker with an order → today's bar as a provisional row
  (``kis.fetch_today_close``) → the screen as of the day and the rule baseline's decisions (``audit/<market>/``) →
  ``execution.execute_date`` for the day: the decisions become ``open`` orders filling at the next session's open.
- ``fill`` (session open + 5 min): refresh the benchmark and the tickers of open orders, today's bar (its Open) as a
  provisional row → ``execute_date`` of the previous session again, which fills its open orders at today's open.
- ``all``: fill, then decide (a catch-up run after the close).

Each stage appends one ``RunRecord`` (``audit/<market>/runs.jsonl``). A stage that ended ``ok`` or ``skipped``
(holiday) is not run again for that day: a second call prints so and writes nothing; the screen, decisions and
orders are also keyed (screen run id, decision per screen and ticker, the executor's order id), so a rerun after a
failure writes only what is missing. A failure stops the stage (and ``all`` before decide) with ``failed``. The
executor's refusal (halted, not paper mode) is ``skipped`` with its reason: the decisions are still recorded.
A past day that is not in the benchmark's final bars is ``skipped`` (``holiday``); on the market's today a weekday
with no benchmark bar yet is ``failed`` (a holiday or a late feed — the next call, or the next day's catch-up,
settles it), and a fill whose open-order ticker has no bar yet is ``failed`` during the session (it may still come);
after the close it is final and goes to the executor as a ``data_delayed`` exception. Before the stage's time on the market's today the call does nothing.
A call without ``--date`` (a scheduler: ``agent_trading_core.scheduler``, cron, systemd) runs the day that is due
(``due_day``): the latest weekday whose stage time has come, while the market's next session has not opened — so a
restart, a late call or a retry in that window runs it once more if it is not done, and after midnight a day with
no bar is settled as a holiday on final bars. Weekdays before it that nobody ran get ``skipped`` (``missed``).
``decide`` also compares the fills of the session before with that day's final Open (``check_fill_opens``): a fill
priced from a provisional row is reported in ``notes`` when they differ, never rewritten. Prices come from the KIS
cache (``<symbol>-KIS-data.csv``), never the Yahoo files.

Exit codes: 0 ok / skipped(holiday, missed) / nothing to do, 1 failed, 2 bad arguments, 3 skipped (executor
refused). TRADING_DATA_DIR overrides data/.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from baseline_krx import SETUPS, decide_from_screen, screen_day

from agent_trading_core import kis
from agent_trading_core.audit import AuditLog, RunRecord, latest_orders, market_audit
from agent_trading_core.config import MARKETS
from agent_trading_core.execution import ExecutionRefused, Prices, check_date, execute_date, paper_config
from agent_trading_core.files import locked

STAGES = ("decide", "fill")
# What agent_trading_core.scheduler runs. No US fill: at 09:37 ET on 2026-11-13 KIS's today Open was off Yahoo's regular open by
# +1.84% (XOM) · +1.52% (CVX) · -0.21% (BRK-B) · -0.15% (JPM) — the opening prints are not settled yet. The US
# decide fills the day's open orders from the bar after the close instead. `--stage fill` still runs by hand.
SCHEDULED = {"KRX": STAGES, "US": ("decide",)}
RUN_IDS = {"KRX": "daily_krx", "US": "daily_us"}   # screen run id: <prefix>_<date>, apart from the measurement grids
AFTER = timedelta(minutes=5)                       # a stage runs this long after the open (fill) or close (decide)


def stage_time(market: str, stage: str, day: date) -> datetime:
    """When ``stage`` of ``day`` may run: the market's session open (fill) or close (decide) + 5 min, aware in the
    market's time zone (``MARKETS[market]["session"]`` · ``timezone``), so daylight saving follows the zone."""
    tz = ZoneInfo(MARKETS[market]["timezone"])
    opens, closes = MARKETS[market]["session"].split("-")
    at = time.fromisoformat(opens if stage == "fill" else closes)
    return datetime.combine(day, at, tz) + AFTER


def next_open(market: str, day: date) -> datetime:
    """The open of the weekday session after ``day``: until then a stage of ``day`` is still caught up."""
    d = day + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return stage_time(market, "fill", d) - AFTER


def due_day(market: str, stage: str, now: datetime) -> str | None:
    """The trading day whose ``stage`` is due at ``now``: the latest weekday whose stage time has come, if the next
    session has not opened yet (the same session); None once it has (that day was missed)."""
    local = now.astimezone(ZoneInfo(MARKETS[market]["timezone"])).date()
    for back in range(7):
        d = local - timedelta(days=back)
        if d.weekday() < 5 and stage_time(market, stage, d) <= now:
            return d.isoformat() if now < next_open(market, d) else None
    return None


class KisPrices(Prices):
    """``execution.Prices`` over the KIS cache (``<symbol>-KIS-data.csv``) instead of the Yahoo files.

    ponytail: copies ``Prices.frame`` and its private ``_frames`` · ``_files`` (execution.py, api's). Drop this
    class once ``Prices`` takes its price source as an argument (stage 4, when the executor reads KIS)."""

    def frame(self, ticker: str) -> pd.DataFrame:
        if ticker in self._frames or self.cache_dir is None:
            return super().frame(ticker)
        path = kis.cache_path(ticker, self.cache_dir)
        if not path.exists():
            return pd.DataFrame({"Open": [], "Close": []}, index=pd.Index([], dtype=object), dtype=float)
        stamp = path.stat().st_mtime_ns
        hit = self._files.get(ticker)
        if hit and hit[0] == stamp:
            return hit[1]
        raw = kis.read_cache(path)
        f = pd.DataFrame({"Open": raw["Open"].to_numpy(dtype=float), "Close": raw["Close"].to_numpy(dtype=float)},
                         index=raw["Date"].dt.strftime("%Y-%m-%d"))
        f = f[~f.index.duplicated(keep="last")].sort_index()
        self._files[ticker] = (stamp, f)
        return f


def _counts(orders, prefix: str) -> dict[str, int]:
    return {f"{prefix}_{k}": n for k, n in Counter(o.status for o in orders).items()}


def run_stage(market: str, stage: str, day: str, data: Path, client, now: datetime | None = None) -> RunRecord | None:
    """One stage of ``day``; the RunRecord it appended, or None when it did nothing (already done, or too early).
    One run of a market's stage at a time (a lock beside its audit files); the done check is made holding it."""
    now = now or datetime.now(kis.KST)
    audit = market_audit(data / "audit", market)
    local_today = now.astimezone(ZoneInfo(MARKETS[market]["timezone"])).date().isoformat()
    if day > local_today:
        raise ValueError(f"{day}는 {market} 현지 오늘({local_today}) 뒤 날짜")
    if day == local_today and now < stage_time(market, stage, date.fromisoformat(day)):
        print(f"{market} {stage} {day}: {stage_time(market, stage, date.fromisoformat(day)):%H:%M} 전 — 실행 안 함")
        return None
    with locked(audit.directory / f"daily_loop_{stage}"):
        if any(r.final for r in audit.runs() if (r.stage, r.trade_date) == (stage, day)):
            print(f"{market} {stage} {day}: 이미 완료 — 새 기록 없음")
            return None
        return _run(audit, market, stage, day, data / "cache", client, now, day == local_today)


def _run(audit: AuditLog, market: str, stage: str, day: str, cache: Path, client, now: datetime,
         today: bool) -> RunRecord:
    started, counts, notes = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), {}, []
    bench = MARKETS[market]["benchmark"]

    def done(status: str, reason: str | None = None, error: str | None = None) -> RunRecord:
        record = RunRecord(market=market, stage=stage, trade_date=day, started_at=started, status=status,
                           reason=reason, error=error, counts=counts, notes=notes)
        audit.record_run(record)
        return record

    try:
        orders = latest_orders(audit.orders()).values()
        open_before = sorted({o.ticker for o in orders if o.status == "open" and o.trade_date < day})
        symbols = list(dict.fromkeys([bench, *(SETUPS[market]["universe"] if stage == "decide" else []),
                                      *(sorted({o.ticker for o in orders}) if stage == "decide" else open_before)]))
        failed = []
        for s in symbols:
            try:
                kis.update_cache(s, cache, client, now=now)
            except Exception as e:  # the rest still refresh; the stage fails below
                failed.append(f"{s} {type(e).__name__}: {e}"[:200])
        counts["refreshed"] = len(symbols) - len(failed)
        if failed:
            return done("failed", error="갱신 실패 " + "; ".join(failed))
        if today:                       # not final yet: today's bar as a provisional row
            got = kis.fetch_today_close(market, symbols, cache, client, now)
            counts["provisional"] = len(got["written"])
            if got["failed"]:
                return done("failed", error="당일 시세 실패 " + "; ".join(f"{f['symbol']} {f['error']}" for f in got["failed"]))
            if bench in got["missing"] and date.fromisoformat(day).weekday() < 5:
                # no index bar yet on a weekday: a holiday or a late feed — not final, the next call runs again;
                # a past day is judged on final bars (below) and a holiday then ends it
                return done("failed", error=f"당일 벤치마크({bench}) 행 없음 — 휴장 또는 지연")
            # in the session a missing bar may still come (the executor would make the order a data_delayed
            # exception for good); after the close it is final — a halted name — and goes to the executor as such
            in_session = now < stage_time(market, "decide", date.fromisoformat(day)) - AFTER
            late = [t for t in got["missing"] if t in open_before and stage == "fill" and in_session]
            if late:
                return done("failed", error="미체결 주문 종목 당일 행 없음(장중) " + ", ".join(late))
        prices = KisPrices(cache)
        calendar = prices.calendar(bench)
        if day not in calendar:
            return done("skipped", reason="holiday")
        cfg = paper_config(market)
        prev = max((d for d in calendar if d < day), default=None)     # the session before: its bar is final
        if stage == "decide":
            run_id = f"{RUN_IDS[market]}_{day}"
            # a rerun decides from the screen already recorded, never from a new one over moved prices
            run = next((r for r in audit.screens() if r.run_id == run_id), None)
            if run is None:
                run = screen_day(market, day, cache_dir=str(cache), run_id=RUN_IDS[market],
                                 load=lambda t, d, cache_dir: kis.load_ohlcv(t, d, cache_dir, client=client))
                audit.record_screen(run)
            have = {d.ticker for d in audit.decisions() if d.screen_run_id == run.run_id}
            records = [r for c in run.candidates if (r := decide_from_screen(market, run, c.ticker, day)[1])]
            for r in records:
                if r.ticker not in have:
                    audit.record_decision(r)
            counts |= {"screened": run.universe_size, "decisions": len(records),
                       "decisions_new": sum(r.ticker not in have for r in records)}
            checked, moved = check_fill_opens(audit, prices, cfg, prev) if prev else (0, [])
            counts |= {"fill_open_checked": checked, "fill_open_mismatch": len(moved)}
            notes += [f"체결가 ≠ 확정 시가 기준: {m}" for m in moved]
            written = execute_date(audit, day, prices, cfg=cfg)
        else:
            written = execute_date(audit, prev, prices, cfg=cfg) if open_before and prev else []
        counts |= _counts(written, "orders")
        return done("ok")
    except ExecutionRefused as e:
        return done("skipped", reason=str(e))
    except Exception as e:
        return done("failed", error=f"{type(e).__name__}: {e}"[:500])


def check_fill_opens(audit: AuditLog, prices: Prices, cfg, day: str) -> tuple[int, list[str]]:
    """Fills on ``day`` (now a final bar) against that bar's Open: a fill stage priced them from a provisional row,
    and the recorded fill price is never rewritten — a difference is only reported. Returns (checked, differing)."""
    filled = [o for o in latest_orders(audit.orders()).values()
              if o.status in ("filled", "partial") and o.fill_date == day and o.filled_price is not None]
    moved = []
    for o in filled:
        px = prices.open_on(o.ticker, day)
        want = None if px is None else round(px * (1 + cfg.slippage if o.side == "Buy" else 1 - cfg.slippage), 4)
        if want is None or abs(o.filled_price / want - 1) > 1e-6:
            moved.append(f"{o.order_id} {o.filled_price} vs {want}")
    return len(filled), moved


def missed_days(audit: AuditLog, stage: str, day: str) -> list[str]:
    """Weekdays after the stage's last recorded day and before ``day`` with no record at all: runs nobody made (the
    process was down past the next session's open). None before the first record — the loop starts on its first day."""
    seen = {r.trade_date for r in audit.runs() if r.stage == stage}
    if not seen:
        return []
    d, out = date.fromisoformat(max(seen)) + timedelta(days=1), []
    while d.isoformat() < day:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def run_due(market: str, stage: str, data: Path, client, now: datetime | None = None) -> list[RunRecord]:
    """What every scheduler runs (the scheduler module, this CLI without ``--date`` from cron): the day due at ``now``
    (``due_day``), after a ``skipped(missed)`` record for each weekday before it that nobody ran. The records written,
    [] when nothing is due, the day is done, or the executor refused it (halted: after the resume, ``--date``)."""
    now = now or datetime.now(kis.KST)
    day = due_day(market, stage, now)
    if day is None:
        print(f"{market} {stage}: 할 단계 없음(다음 세션 개장 뒤 — 지난 날은 --date로)")
        return []
    audit = market_audit(data / "audit", market)
    out = []
    with locked(audit.directory / f"daily_loop_{stage}"):
        for d in missed_days(audit, stage, day):
            out.append(RunRecord(market=market, stage=stage, trade_date=d, status="skipped", reason="missed",
                                 started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
            audit.record_run(out[-1])
        last = [r for r in audit.runs() if (r.stage, r.trade_date) == (stage, day)][-1:]
    if last and last[0].status == "skipped" and not last[0].final:
        print(f"{market} {stage} {day}: 실행기 거부로 끝남({last[0].reason}) — 재가동 뒤 --date {day}로")
        return out
    r = run_stage(market, stage, day, data, client, now)
    return out + ([r] if r else [])


def summary(r: RunRecord) -> str:
    extra = r.reason or r.error or ""
    return (f"{r.market} {r.stage} {r.trade_date}: {r.status}" + (f"({extra})" if extra else "") + " · "
            + " · ".join(f"{k} {v}" for k, v in r.counts.items()) + "".join(f"\n  {n}" for n in r.notes))


def main(argv: list[str] | None = None, client=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", choices=sorted(RUN_IDS), required=True)
    parser.add_argument("--stage", choices=[*STAGES, "all"], required=True)
    parser.add_argument("--date", help="trading day YYYY-MM-DD (default: the day due now — due_day)")
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    args = parser.parse_args(argv)
    now = now or datetime.now(kis.KST)
    if args.date:
        try:
            check_date(args.date)
        except ValueError:
            parser.error(f"--date는 YYYY-MM-DD 형식이어야 합니다: {args.date!r}")
    if client is None:
        try:
            client = kis.KisClient()
        except Exception as e:     # a missing variable is named, never a value
            print(f"KIS 클라이언트 실패 — {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    data, code = Path(args.data_dir), 0
    for stage in (["fill", "decide"] if args.stage == "all" else [args.stage]):
        try:
            if args.date:
                records = [r for r in [run_stage(args.market, stage, args.date, data, client, now)] if r]
            else:
                records = run_due(args.market, stage, data, client, now)
        except ValueError as e:
            parser.error(str(e))
        for r in records:
            print(summary(r))
            if r.status == "failed":
                return 1
            if r.status == "skipped" and r.reason not in ("holiday", "missed"):
                code = 3
    return code


if __name__ == "__main__":
    sys.exit(main())
