"""Daily paper loop (scripts/daily_loop.py) on synthetic KRX and US markets served by a fake KIS client: stage times
from MARKETS (both daylight-saving cases), decide → fill → decide end to end with today's bar still moving (the
provisional row is replaced by the final one, the fill is that day's open only, a fill off the final open is
reported), a rerun that writes nothing, a failed refresh that stops before any record, the executor's refusal as
skipped with the decisions kept, today's missing bar (not final) versus a holiday, a late open-order ticker, the
catch-up of a stage within its session and a missed one after it, and the scheduler process (agent_trading_core.
scheduler): its next times, what it runs on start and at each time, a stop signal during a stage. No network: every price comes from the fake client, names and sectors from local tables."""

import importlib.util
import re
import signal
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from agent_trading_core import kis
from agent_trading_core import scheduler as sched
from agent_trading_core.audit import AuditLog, OperationRecord, latest_orders, market_audit
from agent_trading_core.errors import NoMarketDataError, VendorNetworkError, VendorUnavailableError

_spec = importlib.util.spec_from_file_location("daily_loop", Path(__file__).resolve().parents[1] / "scripts" / "daily_loop.py")
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

UNIVERSE = dl.SETUPS["KRX"]["universe"]
DAYS = pd.bdate_range("2025-06-02", "2026-08-11")
SLIP = dl.paper_config("KRX").slippage


def _frame(i: int, days=DAYS, base=20_000.0) -> pd.DataFrame:
    """An uptrend with a daily wiggle (volatility and range filters pass), a different drift per name."""
    t = np.arange(len(days))
    close = base * (1 + i * 0.05) * np.exp((0.0004 + 0.00005 * i) * t) * (1 + 0.02 * np.sin(1.3 * t + i))
    return pd.DataFrame({"Date": days, "Open": close * 0.99, "High": close * 1.02, "Low": close * 0.98,
                         "Close": close, "Volume": 1_000_000.0})


class FakeKis:
    """Answers every daily endpoint from ``frames`` between start and end, nothing after ``today``; today's bar is
    still moving: its Close (``moved_close``) and Open (``moved_open``) are off the final values. ``down`` symbols
    fail like the network."""

    def __init__(self, frames, down=(), today=None, moved_close=1.01, moved_open=1.0):
        self.frames, self.down, self.stats, self.asked = frames, set(down), {"calls": 0}, []
        self.today, self.moved_close, self.moved_open = today, moved_close, moved_open

    def index_daily(self, symbol, start, end):
        self.asked.append((symbol, start, end))
        if symbol in self.down:
            raise VendorUnavailableError(f"KIS {symbol} 요청 실패: ConnectionError")
        f = self.frames[symbol]
        f = f[(f["Date"] >= pd.Timestamp(start)) & (f["Date"] <= pd.Timestamp(end))].reset_index(drop=True)
        if self.today:
            f = f[f["Date"] <= pd.Timestamp(self.today)].copy()
            now = f["Date"] == pd.Timestamp(self.today)
            f.loc[now, "Close"] *= self.moved_close
            f.loc[now, "Open"] *= self.moved_open
        if f.empty:
            raise NoMarketDataError(symbol, symbol, "no price rows from KIS")
        return f

    stock_daily = us_index_daily = index_daily

    def us_daily(self, symbol, exchange, start, end):
        return self.index_daily(symbol, start, end)

    def us_exchange(self, symbol):
        return "NYS"


def _market(days=DAYS, market="KRX", **kw):
    names = dl.SETUPS[market]["universe"]
    base = 20_000.0 if market == "KRX" else 100.0          # a share fits the book's 10% position cap
    frames = {t: _frame(i, days, base) for i, t in enumerate(names)}
    frames["^KS11" if market == "KRX" else "^GSPC"] = _frame(30, days, base)
    return FakeKis(frames, **kw)


def at(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=kis.KST)


def stage(client, market, name, day, now, data):
    client.today = now.astimezone(dl.ZoneInfo(dl.MARKETS[market]["timezone"])).date().isoformat()
    return dl.run_stage(market, name, day, data, client, now)


def final(client, ticker, day, col="Close"):
    f = client.frames[ticker]
    return float(f.loc[f["Date"] == day, col].iloc[0])


def cached(data, ticker, day, col="Close"):
    f = kis.read_cache(kis.cache_path(ticker, data / "cache"))
    return float(f.loc[f["Date"] == day, col].iloc[0])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # baseline_krx.name_of reads the Yahoo profile of a name the Korean table lacks: never asked in a test
    monkeypatch.setitem(dl.screen_day.__globals__, "name_of", lambda t: None)
    # the scheduler's weekly job is a yfinance child process: a test gives its own
    monkeypatch.setattr(sched, "refresh_earnings", lambda data, stop: pytest.fail("refresh_earnings called"))


def _lines(data: Path, market="KRX") -> dict[str, int]:
    audit = market_audit(data / "audit", market)
    counts = {name: len(getattr(audit, name)()) for name in ("screens", "decisions", "orders", "operations", "runs")}
    return {name: n for name, n in counts.items() if n}                       # the kinds the book has records of


@pytest.mark.unit
def test_stage_times_come_from_the_market_session_in_its_own_zone():
    assert dl.stage_time("KRX", "decide", date(2026, 11, 13)).astimezone(kis.KST).strftime("%H:%M") == "15:35"
    assert dl.stage_time("KRX", "fill", date(2026, 11, 13)).astimezone(kis.KST).strftime("%H:%M") == "09:05"
    # New York standard time (UTC-5): the 16:00 close is 06:00 KST the next day, the 09:30 open 23:30 KST
    winter = dl.stage_time("US", "decide", date(2026, 1, 15)).astimezone(kis.KST)
    assert (winter.day, winter.strftime("%H:%M")) == (16, "06:05")
    assert dl.stage_time("US", "fill", date(2026, 1, 15)).astimezone(kis.KST).strftime("%H:%M") == "23:35"
    # daylight time (UTC-4): an hour earlier in Seoul
    summer = dl.stage_time("US", "decide", date(2026, 7, 15)).astimezone(kis.KST)
    assert (summer.day, summer.strftime("%H:%M")) == (16, "05:05")
    assert dl.stage_time("US", "fill", date(2026, 7, 15)).astimezone(kis.KST).strftime("%H:%M") == "22:35"
    # the US fill is not scheduled (KIS's Open right after the open is not settled): the decide fills instead
    assert dl.SCHEDULED == {"KRX": ("decide", "fill"), "US": ("decide",)}
    # the scheduler's next times: New York's Friday close is Saturday morning in Seoul, its clocks change on 11-01
    times = [(f"{t.astimezone(kis.KST):%m-%d %a %H:%M}", m, s)
             for t, m, s in sched.plan(dl, ["KRX", "US"], at("2026-10-30", "16:00"), 6)]
    assert times == [("10-31 Sat 05:05", "US", "decide"), ("11-02 Mon 09:05", "KRX", "fill"),
                     ("11-02 Mon 15:35", "KRX", "decide"), ("11-03 Tue 06:05", "US", "decide"),
                     ("11-03 Tue 09:05", "KRX", "fill"), ("11-03 Tue 15:35", "KRX", "decide")]


@pytest.mark.unit
def test_a_stage_is_due_from_its_time_until_the_next_session_opens():
    due = lambda m, s, d, hm: dl.due_day(m, s, at(d, hm))
    assert due("KRX", "decide", "2026-08-04", "15:34") is None          # 08-03's ended at 08-04 09:00
    assert due("KRX", "decide", "2026-08-04", "15:35") == "2026-08-04"
    assert due("KRX", "decide", "2026-08-05", "08:59") == "2026-08-04"  # a restart before the open catches up
    assert due("KRX", "decide", "2026-08-05", "09:00") is None          # the next session is on: missed
    assert due("KRX", "fill", "2026-08-05", "08:59") == "2026-08-04"
    assert due("KRX", "decide", "2026-08-09", "20:00") == "2026-08-07"  # Friday's until Monday's open
    assert due("KRX", "decide", "2026-08-10", "09:00") is None
    # New York: Tuesday 06:10 KST is Monday's decide, due until Tuesday's 09:30 EDT open (22:30 KST)
    assert due("US", "decide", "2026-08-04", "06:10") == "2026-08-03"
    assert due("US", "decide", "2026-08-04", "22:29") == "2026-08-03"
    assert due("US", "decide", "2026-08-04", "22:30") is None


@pytest.mark.unit
def test_decide_fill_decide_records_each_step_once_and_a_rerun_writes_nothing(tmp_path, monkeypatch):
    client = _market()
    r1 = stage(client, "KRX", "decide", "2026-08-04", at("2026-08-04", "15:40"), tmp_path)
    assert r1.status == "ok" and r1.counts["provisional"] == len(UNIVERSE) + 1
    assert r1.counts["decisions"] > 0 and r1.counts.get("orders_open", 0) > 0     # the next open is not there yet
    assert kis.provisional(tmp_path / "cache") == {"2026-08-04": sorted([*UNIVERSE, "^KS11"])}
    assert cached(tmp_path, "^KS11", "2026-08-04") == pytest.approx(final(client, "^KS11", "2026-08-04") * 1.01)
    audit = market_audit(tmp_path / "audit", "KRX")
    open_buys = {o.ticker for o in audit.orders() if o.status == "open"}

    client.moved_open = 1.02                       # the 09:10 Open will turn out off the final one
    r2 = stage(client, "KRX", "fill", "2026-08-05", at("2026-08-05", "09:10"), tmp_path)
    assert r2.status == "ok" and r2.counts["orders_filled"] == len(open_buys)
    for o in audit.orders():
        if o.status == "filled":                   # that day's (provisional) open + slippage, nothing else
            want = round(final(client, o.ticker, "2026-08-05", "Open") * 1.02 * (1 + SLIP), 4)
            assert o.fill_date == "2026-08-05" and o.filled_price == pytest.approx(want, rel=1e-9)
    # the filled names and the index had their 08-04 row taken again as final and left the list; 08-05 is listed
    marks = kis.provisional(tmp_path / "cache")
    assert set(marks["2026-08-05"]) == {*open_buys, "^KS11"} and not ({*open_buys, "^KS11"} & set(marks["2026-08-04"]))
    assert cached(tmp_path, "^KS11", "2026-08-04") == final(client, "^KS11", "2026-08-04")

    client.moved_open = 1.0
    r3 = stage(client, "KRX", "decide", "2026-08-05", at("2026-08-05", "15:40"), tmp_path)
    assert r3.status == "ok" and kis.provisional(tmp_path / "cache") == {"2026-08-05": sorted([*UNIVERSE, "^KS11"])}
    assert all(cached(tmp_path, t, "2026-08-04") == final(client, t, "2026-08-04") for t in UNIVERSE)
    r4 = stage(client, "KRX", "decide", "2026-08-06", at("2026-08-06", "15:40"), tmp_path)
    # 08-05 is final now: the fills priced from its provisional Open are reported, not rewritten
    assert r4.counts["fill_open_checked"] == r4.counts["fill_open_mismatch"] == len(open_buys)
    checks = [n for n in r4.notes if n != "휴장 달력 없음 2026"]       # tests run with no calendar (conftest)
    assert "휴장 달력 없음 2026" in r4.notes and len(checks) == len(open_buys) and all("확정 시가" in n for n in checks)
    before = _lines(tmp_path)
    assert before["runs"] == 4
    for name, day, now in (("decide", "2026-08-04", "2026-08-06 15:41"), ("fill", "2026-08-05", "2026-08-06 15:42"),
                           ("decide", "2026-08-06", "2026-08-06 15:43")):
        assert stage(client, "KRX", name, day, at(*now.split()), tmp_path) is None
    assert _lines(tmp_path) == before
    # without the run records, the stage itself still adds no screen, decision or order
    monkeypatch.setattr(AuditLog, "runs", lambda self: [])
    again = stage(client, "KRX", "decide", "2026-08-06", at("2026-08-06", "15:45"), tmp_path)
    assert again.status == "ok" and again.counts["decisions_new"] == 0
    assert {k: v for k, v in _lines(tmp_path).items() if k != "runs"} == {k: v for k, v in before.items() if k != "runs"}


@pytest.mark.unit
def test_fills_of_a_friday_are_checked_by_mondays_decide(tmp_path):
    client = _market()
    assert stage(client, "KRX", "decide", "2026-08-06", at("2026-08-06", "15:40"), tmp_path).status == "ok"
    client.moved_open = 1.02
    r = stage(client, "KRX", "fill", "2026-08-07", at("2026-08-07", "09:10"), tmp_path)       # Friday
    fridays = r.counts["orders_filled"]
    assert fridays > 0
    client.moved_open = 1.0
    stage(client, "KRX", "decide", "2026-08-07", at("2026-08-07", "15:40"), tmp_path)
    r = stage(client, "KRX", "decide", "2026-08-10", at("2026-08-10", "15:40"), tmp_path)    # Monday
    # the session before Monday is Friday, not Sunday: Friday's fills off the final Open are all reported
    assert r.counts["fill_open_checked"] == r.counts["fill_open_mismatch"] == fridays


@pytest.mark.unit
def test_the_us_decide_fills_the_days_open_orders_without_a_fill_stage(tmp_path):
    client = _market(market="US")
    stage(client, "US", "decide", "2026-08-04", at("2026-08-05", "06:10"), tmp_path)
    r = stage(client, "US", "decide", "2026-08-05", at("2026-08-06", "06:10"), tmp_path)
    assert r.status == "ok" and r.counts.get("orders_filled", 0) > 0
    filled = [o for o in market_audit(tmp_path / "audit", "US").orders() if o.status == "filled"]
    assert {o.fill_date for o in filled} == {"2026-08-05"}
    for o in filled:                               # the 08-05 open, read after New York's close
        assert o.filled_price == pytest.approx(round(final(client, o.ticker, "2026-08-05", "Open") * (1 + SLIP), 4))


@pytest.mark.unit
def test_the_us_book_decides_after_new_yorks_close_and_fills_at_its_open(tmp_path):
    client = _market(market="US")
    # 2026-08-05 06:10 KST = 08-04 17:10 EDT: the US decide of 08-04
    r = stage(client, "US", "decide", "2026-08-04", at("2026-08-05", "06:10"), tmp_path)
    assert r.status == "ok" and r.counts["screened"] == 40 and r.counts.get("orders_open", 0) > 0
    assert "^GSPC" in kis.provisional(tmp_path / "cache")["2026-08-04"]            # New York's 08-04
    assert stage(client, "US", "fill", "2026-08-05", at("2026-08-05", "22:20"), tmp_path) is None   # 09:20 EDT
    r = stage(client, "US", "fill", "2026-08-05", at("2026-08-05", "22:40"), tmp_path)
    assert r.status == "ok" and r.counts.get("orders_filled", 0) > 0
    assert {o.fill_date for o in market_audit(tmp_path / "audit", "US").orders() if o.status == "filled"} == {"2026-08-05"}


@pytest.mark.unit
def test_a_failed_refresh_stops_the_stage_before_any_record_and_all_stops_before_decide(tmp_path):
    client = _market(down={"^KS11"}, today="2026-08-04")
    code = dl.main(["--market", "KRX", "--stage", "all", "--date", "2026-08-04", "--data-dir", str(tmp_path)],
                   client=client, now=at("2026-08-04", "15:40"))
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    assert code == 1 and [(r.stage, r.status) for r in runs] == [("fill", "failed")]     # decide never started
    assert "^KS11" in runs[0].error and "갱신 실패" in runs[0].error
    client.down = {UNIVERSE[3]}                     # one name of the universe: decide fails before the screen
    code = dl.main(["--market", "KRX", "--stage", "decide", "--date", "2026-08-04", "--data-dir", str(tmp_path)],
                   client=client, now=at("2026-08-04", "15:40"))
    r = market_audit(tmp_path / "audit", "KRX").runs()[-1]
    assert code == 1 and r.status == "failed" and UNIVERSE[3] in r.error and r.counts["refreshed"] == len(UNIVERSE)
    assert set(_lines(tmp_path)) == {"runs"}                                       # no screen, decision or order
    assert not kis.provisional(tmp_path / "cache")                                 # nor a provisional row
    client.down = set()                                                            # a failed stage runs again
    assert dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:50")).status == "ok"


@pytest.mark.unit
def test_a_refused_executor_is_skipped_with_the_decisions_kept_and_runs_again(tmp_path):
    audit = market_audit(tmp_path / "audit", "KRX")
    audit.record_operation(OperationRecord(kind="halt", operator="admin", role="admin", reason="점검",
                                           target="manual", after={"keep_exits": False}, market="KRX"))
    client = _market(today="2026-08-04")
    code = dl.main(["--market", "KRX", "--stage", "decide", "--date", "2026-08-04", "--data-dir", str(tmp_path)],
                   client=client, now=at("2026-08-04", "15:40"))
    r = audit.runs()[-1]
    assert code == 3 and r.status == "skipped" and "정지" in r.reason and not r.final
    assert r.counts["decisions"] > 0 and len(audit.decisions()) == r.counts["decisions"] and not audit.orders()
    # a scheduler's call (other stage times, a restart) does not run the refused day again
    n = len(audit.runs())
    assert dl.run_due("KRX", "decide", tmp_path, client, at("2026-08-04", "18:00")) == [] and len(audit.runs()) == n
    audit.record_operation(OperationRecord(kind="resume", operator="admin", role="admin", reason="재개", market="KRX"))
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "16:00"))
    assert r.status == "ok" and r.counts["decisions_new"] == 0 and audit.orders()


@pytest.mark.unit
def test_todays_missing_bar_is_not_final_and_the_next_days_catch_up_settles_a_holiday(tmp_path):
    client = _market(DAYS[DAYS != "2026-08-05"])
    assert stage(client, "KRX", "fill", "2026-08-04", at("2026-08-04", "09:10"), tmp_path).status == "ok"
    for hm in ("09:10", "11:00"):                  # a weekday without the index bar: a holiday or a late feed
        r = stage(client, "KRX", "fill", "2026-08-05", at("2026-08-05", hm), tmp_path)
        assert r.status == "failed" and "휴장 또는 지연" in r.error and not r.final
    assert not kis.provisional(tmp_path / "cache").get("2026-08-05")
    # a failed due stage brings the scheduler back in an hour, not at the next stage time
    assert sched.next_wake(dl, ["KRX"], at("2026-08-05", "11:00"), tmp_path) == at("2026-08-05", "12:00")
    # after midnight, still before the next open, the scheduled call (no --date) settles 08-05 on final bars
    client.today = "2026-08-06"
    for hm in ("00:30", "09:10"):
        code = dl.main(["--market", "KRX", "--stage", "fill", "--data-dir", str(tmp_path)], client=client,
                       now=at("2026-08-06", hm))
        assert code == 0
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    assert [(r.trade_date, r.status, r.reason) for r in runs[-2:]] == [
        ("2026-08-05", "skipped", "holiday"), ("2026-08-06", "ok", None)]
    # a weekend day is a holiday at once; before the stage's time nothing runs; a later day is refused
    r = stage(client, "KRX", "decide", "2026-08-08", at("2026-08-08", "15:40"), tmp_path)
    assert r.status == "skipped" and r.reason == "holiday" and r.final
    n = len(market_audit(tmp_path / "audit", "KRX").runs())
    assert stage(client, "KRX", "decide", "2026-08-07", at("2026-08-07", "15:30"), tmp_path) is None
    assert len(market_audit(tmp_path / "audit", "KRX").runs()) == n
    with pytest.raises(ValueError, match="뒤 날짜"):
        dl.run_stage("KRX", "decide", "2026-08-07", tmp_path, client, at("2026-08-06", "15:40"))


@pytest.mark.unit
def test_a_missed_stage_is_caught_up_once_within_its_session_and_recorded_missed_after(tmp_path):
    client = _market()
    assert stage(client, "KRX", "decide", "2026-08-03", at("2026-08-03", "15:40"), tmp_path).status == "ok"
    main = lambda d, hm: dl.main(["--market", "KRX", "--stage", "decide", "--data-dir", str(tmp_path)],
                                 client=client, now=at(d, hm))
    client.today = "2026-08-05"                    # down over 08-04 15:35, back before the next open
    assert main("2026-08-05", "08:30") == 0
    assert main("2026-08-05", "08:40") == 0        # once: the second call finds it done
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    assert [(r.trade_date, r.status) for r in runs] == [("2026-08-03", "ok"), ("2026-08-04", "ok")]
    # 08-04 decided on its own day's bars: its screen saw nothing after 08-04
    screen = next(s for s in market_audit(tmp_path / "audit", "KRX").screens() if s.run_id == "daily_krx_2026-08-04")
    assert screen.as_of == "2026-08-04"
    # down over 08-05 15:35 and past 08-06's open: 08-05 is recorded missed (not final), 08-06 runs on time
    client.today = "2026-08-06"
    assert main("2026-08-06", "15:40") == 0
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    assert [(r.trade_date, r.status, r.reason) for r in runs[2:]] == [
        ("2026-08-05", "skipped", "missed"), ("2026-08-06", "ok", None)]
    assert not runs[2].final and not any(s.run_id == "daily_krx_2026-08-05"
                                         for s in market_audit(tmp_path / "audit", "KRX").screens())
    assert main("2026-08-06", "15:50") == 0 and len(market_audit(tmp_path / "audit", "KRX").runs()) == 4


@pytest.mark.unit
def test_the_scheduler_runs_what_is_due_on_start_then_sleeps_to_each_stage_time(tmp_path, capsys):
    client = _market()
    assert stage(client, "KRX", "decide", "2026-08-03", at("2026-08-03", "15:40"), tmp_path).status == "ok"
    clock, stop = [at("2026-08-05", "08:00")], []   # started after a night down

    def now():
        client.today = clock[0].date().isoformat()
        return clock[0]

    def sleep(seconds):
        assert 0 < seconds <= sched.NAP
        clock[0] += timedelta(seconds=seconds)
        if clock[0] >= at("2026-08-05", "16:00"):
            stop.append("test")

    sched.serve(dl, ["KRX"], tmp_path, client, stop, clock=now, sleep=sleep)
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    # 08-04's fill and decide were still in their session at 08:00: caught up once, then 08-05's on time
    assert [(r.stage, r.trade_date, r.status) for r in runs[1:]] == [
        ("fill", "2026-08-04", "ok"), ("decide", "2026-08-04", "ok"),
        ("fill", "2026-08-05", "ok"), ("decide", "2026-08-05", "ok")]
    wakes = [line.split("깨어남 ")[1] for line in capsys.readouterr().out.splitlines() if "다음 깨어남" in line]
    assert wakes == ["2026-08-05 09:05 KST", "2026-08-05 15:35 KST", "2026-08-06 09:05 KST"]


@pytest.mark.unit
def test_the_scheduler_outlives_an_unreadable_record_file(tmp_path, monkeypatch, capsys):
    stop = []
    monkeypatch.setattr(sched, "next_wake", lambda *a: (_ for _ in ()).throw(ValueError("runs.jsonl 깨짐")))
    sched.serve(dl, ["KRX"], tmp_path, _market(), stop, clock=lambda: at("2026-08-08", "12:00"),
                sleep=lambda s: stop.append("test"))
    out = capsys.readouterr().out
    assert "계산 오류 — ValueError" in out and "다음 깨어남 2026-08-10 09:05 KST" in out


@pytest.mark.unit
def test_a_stop_signal_lets_the_running_stage_finish_and_starts_no_other(tmp_path, monkeypatch):
    client = _market()
    client.today = "2026-08-05"
    stop, ran = [], []
    real = dl.run_due

    def run_due(m, s, data, c, now):               # the signal arrives while the first stage runs
        signal.raise_signal(signal.SIGTERM)
        ran.append(s)
        return real(m, s, data, c, now)

    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        sched.stop_on_signals(stop)
        monkeypatch.setattr(dl, "run_due", run_due)
        sched.serve(dl, ["KRX", "US"], tmp_path, client, stop, clock=lambda: at("2026-08-05", "08:00"),
                    sleep=lambda s: pytest.fail("slept after the stop"))
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert stop == [signal.SIGTERM] and ran == ["fill"]
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    assert [(r.stage, r.trade_date) for r in runs] == [("fill", "2026-08-04")]   # finished, recorded


@pytest.mark.unit
def test_a_fill_whose_ticker_has_no_bar_yet_fails_and_the_order_stays_open(tmp_path):
    client = _market()
    stage(client, "KRX", "decide", "2026-08-04", at("2026-08-04", "15:40"), tmp_path)
    audit = market_audit(tmp_path / "audit", "KRX")
    late = next(o.ticker for o in audit.orders() if o.status == "open")
    client.frames[late] = client.frames[late][client.frames[late]["Date"] != "2026-08-05"]
    r = stage(client, "KRX", "fill", "2026-08-05", at("2026-08-05", "09:10"), tmp_path)
    assert r.status == "failed" and late in r.error and not r.final
    assert all(o.status == "open" for o in audit.orders() if o.ticker == late)
    # after the close the missing bar is final (a halted name): the fill goes on and the decide runs on time
    code = dl.main(["--market", "KRX", "--stage", "all", "--data-dir", str(tmp_path)], client=client,
                   now=at("2026-08-05", "15:40"))
    runs = audit.runs()
    assert code == 0 and [(r.stage, r.trade_date, r.status) for r in runs[-2:]] == [
        ("fill", "2026-08-05", "ok"), ("decide", "2026-08-05", "ok")]
    assert latest_orders(audit.orders())[f"paper-2026-08-04-{late}"].exception_kind == "data_delayed"


@pytest.mark.unit
def test_prices_come_from_the_kis_cache_not_the_yahoo_file(tmp_path):
    client = _market()
    kis.update_cache("^KS11", tmp_path, client, now=at("2026-08-05", "09:00"))
    (tmp_path / "^KS11-YFin-data.csv").write_text("Date,Open,Close\n2026-08-04,1,1\n", encoding="utf-8")
    p = dl.Prices(tmp_path, source="kis")                  # the loop's prices: KIS first
    assert p.calendar("^KS11")[-1] == "2026-08-04"
    assert p.close_asof("^KS11", "2026-08-04") == final(client, "^KS11", "2026-08-04")
    assert p.open_on("^KS11", "2026-08-04") == final(client, "^KS11", "2026-08-04", "Open")
    assert p.frame("NOPE").empty


class FakeManager:
    """An Anthropic client for the decide stage: a memo for a candidate, a sell review for a holding, built from the
    ticker in the user message; with ``refuse_first`` the first call is declined. Counts the calls."""

    def __init__(self, refuse_first=False, sell="Exit"):
        self.refuse_first, self.sell, self.asked = refuse_first, sell, []
        self.messages = self

    def create(self, **kw):
        import json

        from anthropic.types import Message
        text = kw["messages"][0]["content"]
        ticker = re.search(r"종목: (\S+)", text).group(1)
        self.asked.append((ticker, "holding" if "매도 검토" in text else "candidate"))
        close = float(re.search(r"\| Close \| ([\d,.]+) \|", text).group(1).replace(",", ""))
        usage = {"input_tokens": 1000, "output_tokens": 800, "cache_read_input_tokens": 1500,
                 "cache_creation_input_tokens": 0}
        if self.refuse_first:
            self.refuse_first = False
            body = {"content": [], "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": None}}
        elif self.asked[-1][1] == "holding":
            body = {"content": [{"type": "text", "text": json.dumps({
                "checks": [{"condition": "c", "met": self.sell == "Exit", "evidence": "e"}],
                "reasons": ["thesis_broken"] if self.sell == "Exit" else [], "reasoning": "r", "action": self.sell})}],
                "stop_reason": "end_turn"}
        else:
            body = {"content": [{"type": "text", "text": json.dumps({
                "business_summary": "b", "thesis": "t", "bull_case": "u", "bear_case": "d", "valuation_basis": "v",
                "current_price": close, "price_target": close * 1.3, "downside_price": close * 0.9,
                "catalysts": [{"event": "실적"}], "invalidation": ["i"], "rating": "Buy", "conviction": 0.6,
                "exit_plan": "e", "time_horizon": "6개월"})}], "stop_reason": "end_turn"}
        return Message.model_validate({"id": "m", "type": "message", "role": "assistant", "model": kw["model"],
                                       "stop_sequence": None, "usage": usage} | body)


class NoManager:
    messages = property(lambda self: pytest.fail("the rule decider must not touch the model client"))


@pytest.mark.unit
def test_the_rule_decider_stays_the_default_and_never_calls_the_model(tmp_path):
    assert dl.LLM["enabled"] is False and dl.default_decider() == "rule"
    client = _market(today="2026-08-04")
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:40"), llm=NoManager())
    decisions = market_audit(tmp_path / "audit", "KRX").decisions()
    assert r.status == "ok" and decisions and all(d.settings["decider"] == "rule_momentum_baseline" for d in decisions)
    assert "llm_calls" not in r.counts and not any("결정자" in n for n in r.notes)


@pytest.mark.unit
def test_the_llm_decider_writes_memos_for_survivors_then_reviews_holdings_and_never_asks_twice(tmp_path, monkeypatch):
    client, llm = _market(), FakeManager(refuse_first=True)
    run = lambda name, day, hm: (setattr(client, "today", day),
                                 dl.run_stage("KRX", name, day, tmp_path, client, at(day, hm), decider="llm", llm=llm))[1]
    r = run("decide", "2026-08-04", "15:40")
    audit = market_audit(tmp_path / "audit", "KRX")
    survivors = [c.ticker for c in audit.screens()[-1].selected]
    assert r.status == "ok" and r.counts["llm_calls"] == len(survivors) == len(llm.asked)
    assert r.counts["input_tokens"] == 1000 * len(survivors) and r.counts["cache_read_input_tokens"] == 1500 * len(survivors)
    assert any("claude-opus-5-5" in n for n in r.notes)
    decisions = {d.ticker: d for d in audit.decisions()}
    assert set(decisions) == set(survivors)                    # no decision for a name nobody asked about
    assert all(d.source == "agent" and d.settings["decider"] == "claude-opus-5-5" and d.usage for d in decisions.values())
    # the refused name is REVIEW: an exception in the queue, no order; the others are open buys
    orders = latest_orders(audit.orders())
    refused = llm.asked[0][0]
    assert decisions[refused].rating == "REVIEW" and orders[f"paper-2026-08-04-{refused}"].exception_kind == "review"
    assert {o.ticker for o in orders.values() if o.status == "open"} == set(survivors) - {refused}
    raw = sorted((tmp_path / "audit" / "KRX" / "agent_raw" / "2026-08-04").glob("*.json"))
    assert len(raw) == len(survivors)

    assert run("fill", "2026-08-05", "09:10").status == "ok"
    llm.asked.clear()
    r = run("decide", "2026-08-05", "15:40")
    held = {o.ticker for o in latest_orders(audit.orders()).values() if o.side == "Buy" and o.status == "filled"}
    reviews = [t for t, kind in llm.asked if kind == "holding"]
    assert held and sorted(reviews) == sorted(held)            # every holding reviewed, first
    assert [k for _, k in llm.asked[:len(held)]] == ["holding"] * len(held)
    day = [d for d in audit.decisions() if d.trade_date == "2026-08-05" and d.ticker in held]
    assert all((d.rating, d.action, d.sell_reason) == ("Sell", "Sell", "thesis_broken") for d in day)
    assert {o.ticker for o in latest_orders(audit.orders()).values()
            if o.trade_date == "2026-08-05" and o.side == "Sell" and o.status == "open"} == held
    # a rerun without the run records asks nobody again
    monkeypatch.setattr(AuditLog, "runs", lambda self: [])
    llm.asked.clear()
    again = run("decide", "2026-08-05", "15:50")
    assert again.status == "ok" and again.counts["decisions_new"] == 0 and llm.asked == []


@pytest.mark.unit
def test_past_the_call_cap_or_without_a_key_the_rest_get_the_rule_decision(tmp_path, monkeypatch):
    client, llm = _market(today="2026-08-04"), FakeManager()
    monkeypatch.setitem(dl.LLM, "max_calls_per_run", 2)
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:40"), decider="llm", llm=llm)
    audit = market_audit(tmp_path / "audit", "KRX")
    n = len(audit.screens()[-1].selected)
    assert len(llm.asked) == r.counts["llm_calls"] == 2 and r.counts["llm_fallback"] == n - 2
    rule = [d for d in audit.decisions() if d.settings.get("fallback") == "rule"]
    assert len(rule) == n - 2 and all(d.settings["fallback_reason"] == "max_calls_per_run(2)" for d in rule)
    assert all(d.settings["decider"] == "rule_momentum_baseline" and not d.usage for d in rule)
    # no key: the stage still decides (by rule, each record says why) and orders go out
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("TRADING_ENV_FILE", str(tmp_path / "none.env"))
    client.today = "2026-08-05"
    r = dl.run_stage("KRX", "decide", "2026-08-05", tmp_path, client, at("2026-08-05", "15:40"), decider="llm")
    day = [d for d in audit.decisions() if d.trade_date == "2026-08-05"]
    assert r.status == "ok" and "llm_calls" not in r.counts and r.counts["llm_fallback"] == len(day) > 0
    assert all("ANTHROPIC_API_KEY 없음" in d.settings["fallback_reason"] for d in day)


@pytest.mark.unit
def test_a_ticker_whose_prompt_or_record_breaks_does_not_stop_the_stage(tmp_path, monkeypatch):
    client, llm = _market(today="2026-08-04"), FakeManager()
    real = dl.memory_text
    first = []

    def memory(t, *a, **k):
        if not first:
            first.append(t)
            raise KeyError("broken memory")
        return real(t, *a, **k)

    monkeypatch.setitem(dl.agent_decide.__globals__, "memory_text", memory)
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:40"), decider="llm", llm=llm)
    audit = market_audit(tmp_path / "audit", "KRX")
    d = {x.ticker: x for x in audit.decisions()}
    assert r.status == "ok" and r.counts["llm_fallback"] == 1 and first[0] not in {t for t, _ in llm.asked}
    assert d[first[0]].settings["fallback_reason"].startswith("prep: KeyError")
    assert audit.orders()                                      # the day still executed


@pytest.mark.unit
def test_a_paid_answer_that_cannot_be_recorded_or_kept_is_a_review_and_the_stage_goes_on(tmp_path, monkeypatch):
    client, llm = _market(today="2026-08-04"), FakeManager()
    monkeypatch.setattr(dl.agent, "to_record", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad record")))
    monkeypatch.setattr(dl.agent, "_keep", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:40"), decider="llm", llm=llm)
    audit = market_audit(tmp_path / "audit", "KRX")
    decisions = audit.decisions()
    assert r.status == "ok" and r.counts["llm_review"] == r.counts["llm_calls"] == len(decisions) > 0
    assert all(d.rating == "REVIEW" and "record: ValueError: bad record" in d.thesis and d.settings["raw"] is None
               for d in decisions)
    assert all(o.exception_kind == "review" for o in latest_orders(audit.orders()).values())


REFRESH_EARNINGS = sched.refresh_earnings     # the real one: the autouse fixture puts a guard in its place
NO_ANSWER = VendorNetworkError("KIS HHDFS76240000 요청 실패: ConnectionError")


def _wake_after_sleep(client, data, start: datetime, n: int, error=NO_ANSWER, markets=("US",)):
    """The scheduler started at ``start`` (the US decide of 08-04 due) while the first ``n`` daily calls fail with
    ``error`` (2026-11-19: ConnectionError 2 s after the PC woke); it stops 30 minutes in. (runs, printed lines, the
    fake time of the first answered call)."""
    clock, stop, answered, left = [start], [], [], [n]
    def flaky(real):
        def call(*a):
            if left[0]:
                left[0] -= 1
                raise error
            answered.append(clock[0])
            return real(*a)
        return call

    for name in ("index_daily", "stock_daily", "us_index_daily"):
        setattr(client, name, flaky(getattr(client, name)))

    def sleep(seconds):
        assert 0 < seconds <= sched.NAP
        clock[0] += timedelta(seconds=seconds)
        if clock[0] >= start + timedelta(minutes=30):
            stop.append("test")

    sched.serve(dl, list(markets), data, client, stop, clock=lambda: clock[0], sleep=sleep)
    return market_audit(data / "audit", "US").runs(), answered[:1]


@pytest.mark.unit
def test_a_call_with_no_answer_is_asked_again_minutes_later_and_the_stage_records_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sched, "weekly", lambda *a, **k: None)
    client = _market(market="US", today="2026-08-04")
    runs, answered = _wake_after_sleep(client, tmp_path, at("2026-08-05", "06:10"), 1)
    assert [(r.stage, r.trade_date, r.status) for r in runs] == [("decide", "2026-08-04", "ok")]
    assert answered == [at("2026-08-05", "06:12")]                       # NET_WAIT after the failed call
    out = capsys.readouterr().out
    assert out.count("네트워크 오류") == 1 and "2분 뒤 다시 (1/3)" in out


@pytest.mark.unit
def test_a_network_down_past_the_retries_fails_the_stage_once_and_the_hourly_retry_takes_over(tmp_path, monkeypatch,
                                                                                                capsys):
    monkeypatch.setattr(sched, "weekly", lambda *a, **k: None)
    client = _market(market="US", today="2026-08-04")
    runs, _ = _wake_after_sleep(client, tmp_path, at("2026-08-05", "06:10"), 10**6)
    assert [(r.stage, r.status) for r in runs] == [("decide", "failed")]  # one record, after three tries
    assert "ConnectionError" in runs[0].error and "갱신 실패" in runs[0].error
    out = capsys.readouterr().out
    assert [line.split("(")[-1] for line in out.splitlines() if "네트워크 오류" in line] == ["1/3)", "2/3)", "3/3)"]
    assert "다음 깨어남 2026-08-05 07:16 KST" in out                     # failed at 06:16, then RETRY as before


@pytest.mark.unit
def test_an_answer_that_refused_is_not_asked_again(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sched, "weekly", lambda *a, **k: None)
    client = _market(market="US", today="2026-08-04")
    refused = VendorUnavailableError("KIS HHDFS76240000 실패: HTTP 500 EGW00123 서버 오류")
    runs, _ = _wake_after_sleep(client, tmp_path, at("2026-08-05", "06:10"), 10**6, error=refused)
    assert [(r.stage, r.status) for r in runs] == [("decide", "failed")] and "HTTP 500" in runs[0].error
    out = capsys.readouterr().out
    assert "네트워크 오류" not in out and "다음 깨어남 2026-08-05 07:10 KST" in out    # no wait before the record


@pytest.mark.unit
def test_the_weekly_earnings_refresh_is_planned_apart_from_the_stages():
    now = at("2026-11-23", "08:00")
    assert sched.weekly_plan(["KRX", "US"], now, 2) == [(at("2026-11-28", "10:00"), "US", "earnings"),
                                                         (at("2026-12-05", "10:00"), "US", "earnings")]
    assert sched.weekly_plan(["KRX"], now, 2) == []                       # US dates only
    # plan · next_wake (the backend's next run and retry) list stages only
    assert all(s in ("fill", "decide") for _, _, s in sched.plan(dl, ["KRX", "US"], now, 40))
    assert sched.weekly_slot("US", at("2026-11-28", "09:59")) == at("2026-11-21", "10:00")
    assert sched.weekly_slot("US", at("2026-11-28", "10:00")) == at("2026-11-28", "10:00")


@pytest.mark.unit
def test_the_weekly_refresh_runs_once_per_week_and_a_week_in_the_file_is_not_run_again(tmp_path):
    ran, done = [], {}
    run = lambda data, stop: ran.append(data) or "ok"
    sched.weekly(["KRX", "US"], tmp_path, at("2026-11-28", "09:59"), done, [], run)   # no file: last week's, caught up
    sched.weekly(["KRX", "US"], tmp_path, at("2026-11-28", "09:59"), done, [], run)
    sched.weekly(["KRX", "US"], tmp_path, at("2026-11-28", "10:00"), done, [], run)   # this week's time
    sched.weekly(["KRX", "US"], tmp_path, at("2026-11-30", "09:05"), done, [], run)
    assert len(ran) == 2 and ran[0] == tmp_path
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / sched.EARNINGS_FILE).write_text(
        '{"AAPL": {"date": "2027-01-28", "fetched": "2026-11-21"}, "MSFT": {"date": null, "failed": "2026-11-28"}}',
        encoding="utf-8")
    fresh = {}                                                            # a restart: the file says this week ran
    sched.weekly(["US"], tmp_path, at("2026-11-28", "12:00"), fresh, [], run)
    assert len(ran) == 2 and fresh == {"US": at("2026-11-28", "10:00")}
    sched.weekly(["US"], tmp_path, at("2026-11-28", "12:00"), {}, ["stop"], run)    # a stop: nothing starts
    assert len(ran) == 2


@pytest.mark.unit
def test_a_failed_weekly_refresh_is_printed_after_the_stages_and_stops_none(tmp_path, monkeypatch, capsys):
    client = _market()
    client.today = "2026-08-08"
    order = []
    monkeypatch.setattr(sched, "refresh_earnings",
                        lambda data, stop: order.append(len(market_audit(data / "audit", "KRX").runs()))
                        or "failed(exit 1) · x")
    stop = []
    sched.serve(dl, ["KRX", "US"], tmp_path, client, stop, clock=lambda: at("2026-08-08", "10:30"),
                sleep=lambda s: stop.append("test"))
    out = capsys.readouterr().out
    runs = market_audit(tmp_path / "audit", "KRX").runs()
    assert order == [len(runs)] and len(runs) == 2                        # after Friday's fill · decide catch-up
    assert "US earnings 2026-08-08: failed(exit 1) · x" in out
    assert "다음 깨어남" in out                                             # the loop goes on


class Child:
    """A fake ``subprocess.Popen``: done after ``ticks`` one-second waits (None: never), with ``code`` and ``out``."""

    made: ClassVar[list] = []

    def __init__(self, cmd, ticks=0, code=0, out="", stop=None, **kw):
        self.cmd, self.ticks, self.returncode, self.out, self.killed = cmd, ticks, code, out, False
        self.stop, self.waits = stop, 0  # stop: a list, the signal arrives during the first wait
        Child.made.append(self)

    def communicate(self, timeout=None):
        import subprocess

        if self.killed or self.ticks == 0:
            return self.out, ""
        self.waits += 1
        if self.ticks is not None:
            self.ticks -= 1
        if self.stop is not None:
            self.stop.append("SIGTERM")
        raise subprocess.TimeoutExpired(self.cmd, timeout)

    def kill(self):
        self.killed = True


@pytest.mark.unit
def test_the_refresh_child_process_failing_hanging_or_stopped_is_one_line(tmp_path, monkeypatch):
    def child(**kw):
        monkeypatch.setattr(sched.subprocess, "Popen", lambda cmd, **k: Child(cmd, **kw))

    child(ticks=3, out='{"calls": 40}\n')                                # done after three one-second waits
    assert REFRESH_EARNINGS(tmp_path) == 'ok · {"calls": 40}'
    assert Child.made[-1].cmd[-2:] == ["--cache-dir", str(tmp_path / "cache")] and not Child.made[-1].killed
    child(code=1, out='{"failed": 2}\n')
    assert REFRESH_EARNINGS(tmp_path) == 'failed(exit 1) · {"failed": 2}'
    child(ticks=None)                                                     # hangs: killed at WEEKLY_TIMEOUT
    assert REFRESH_EARNINGS(tmp_path) == f"failed — 시간 초과({sched.WEEKLY_TIMEOUT}초)" and Child.made[-1].killed
    stop = []
    child(ticks=None, stop=stop)
    assert REFRESH_EARNINGS(tmp_path, stop) == "중단 — 종료 신호" and Child.made[-1].killed
    assert Child.made[-1].waits == 1                                      # a stop: killed after that wait, not at the timeout
    monkeypatch.setattr(sched.subprocess, "Popen", lambda cmd, **k: (_ for _ in ()).throw(FileNotFoundError("python")))
    assert REFRESH_EARNINGS(tmp_path).startswith("failed — FileNotFoundError")


@pytest.mark.unit
def test_an_earnings_file_of_an_unexpected_shape_is_rewritten_and_an_error_stops_no_wake(tmp_path, monkeypatch, capsys):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / sched.EARNINGS_FILE).write_text(
        '{"AAPL": {"fetched": 20261121}, "MSFT": {"fetched": "2026-11-21"}}', encoding="utf-8")
    monkeypatch.setattr(sched, "refresh_earnings", lambda data, stop: "ok · 다시 씀")
    stop = []
    sched.serve(dl, ["US"], tmp_path, _market(market="US"), stop, clock=lambda: at("2026-11-28", "12:00"),
                sleep=lambda s: stop.append("test"))
    out = capsys.readouterr().out                   # a date of another type: the script runs and rewrites the file
    assert "US earnings 2026-11-28: ok · 다시 씀" in out and "주 1회 오류" not in out
    # whatever else the weekly job raises is printed and the loop goes on
    monkeypatch.setattr(sched, "earnings_day", lambda data: (_ for _ in ()).throw(RuntimeError("뜻밖의 오류")))
    stop = []
    sched.serve(dl, ["US"], tmp_path, _market(market="US"), stop, clock=lambda: at("2026-11-28", "12:00"),
                sleep=lambda s: stop.append("test"))
    out = capsys.readouterr().out
    assert "주 1회 오류 — RuntimeError: 뜻밖의 오류" in out and "다음 깨어남" in out
