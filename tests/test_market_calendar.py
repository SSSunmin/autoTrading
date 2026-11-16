"""Exchange holiday calendars known in advance (agent_trading_core.market_calendar, data/calendar/): sessions, the
next session, a US half day's close; the scheduler's plan and the loop's due day skip a listed holiday, the loop
records it ``skipped(holiday)`` without running, a year with no file falls back to weekdays with a note, a closure no
list had is still caught after the fact, each market its own list — both stores. The real files (2026 · 2027, from
KRX's and NYSE's lists) for the dates measured; a test calendar (TRADING_CALENDAR_DIR) inside the fake market's days.
No network: tests/test_daily_loop.py's fake KIS client."""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from agent_trading_core import market_calendar as cal
from agent_trading_core import scheduler as sched
from agent_trading_core.audit import market_audit
from tests.test_daily_loop import DAYS, _market, at, dl

ET = ZoneInfo("America/New_York")
D1, HOLIDAY, D3 = "2026-08-05", "2026-08-06", "2026-08-07"        # Wed · Thu · Fri


@pytest.fixture(autouse=True)
def tracked_lists(monkeypatch):
    """The tracked files under data/calendar/ (tests/conftest.py points every other test at none)."""
    monkeypatch.delenv("TRADING_CALENDAR_DIR", raising=False)


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    return request.param


@pytest.fixture
def test_calendar(tmp_path, monkeypatch):
    """KRX 2026 with one listed holiday, 2026-08-06; no US file (the US falls back to weekdays)."""
    folder = tmp_path / "calendar"
    folder.mkdir()
    (folder / "KRX-2026.json").write_text(json.dumps({
        "market": "KRX", "year": 2026, "source": "test", "fetched": "2026-11-16",
        "holidays": [{"date": HOLIDAY, "name": "시험 휴일"}], "early_close": {}}), encoding="utf-8")
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(folder))
    return folder


def _decide(client, day, data, now=None):
    client.today = day
    return dl.run_stage("KRX", "decide", day, data, client, now or at(day, "15:40"))


# ---- the real lists ------------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_the_files_name_their_source_and_hold_the_lists_counted():
    for (market, year), (n, early) in {("KRX", 2026): (17, {}), ("KRX", 2027): (16, {}),
                                       ("US", 2026): (10, {"2026-11-27": "13:00", "2026-12-24": "13:00"}),
                                       ("US", 2027): (10, {"2027-11-26": "13:00"})}.items():
        body = cal.year_calendar(market, year)
        assert body["source"].startswith("https://") and body["fetched"] == "2026-11-16"
        assert len(body["holidays"]) == n and body["early_close"] == early
        assert all(date.fromisoformat(h["date"]).weekday() < 5 for h in body["holidays"])    # weekdays only
        assert cal.source(market, year) == f"{body['source']} (2026-11-16)"


@pytest.mark.parametrize("market, day, session", [
    ("KRX", "2026-12-25", False), ("US", "2026-12-25", False),            # Christmas, both
    ("KRX", "2027-01-01", False), ("US", "2027-01-01", False),            # New Year's Day, both
    ("KRX", "2026-02-17", False), ("KRX", "2026-09-25", False),           # Seollal, Chuseok
    ("US", "2026-02-17", True), ("US", "2026-09-25", True),               # each market its own list
    ("KRX", "2026-12-31", False), ("US", "2026-12-31", True),             # KRX's year-end closing day
    ("US", "2026-11-26", False), ("US", "2026-11-27", True),              # Thanksgiving; the half day trades
    ("KRX", "2026-12-26", False), ("KRX", "2026-12-24", True),            # a Saturday; a weekday
])
@pytest.mark.unit
def test_sessions_from_the_lists(market, day, session):
    assert cal.is_session(market, date.fromisoformat(day)) is session


@pytest.mark.unit
def test_the_next_session_skips_holidays_and_weekends():
    assert cal.next_session("KRX", date(2026, 9, 23)) == date(2026, 9, 28)      # Chuseok Thu · Fri, then the weekend
    assert cal.next_session("KRX", date(2026, 2, 13)) == date(2026, 2, 19)      # Seollal Mon–Wed after a weekend
    assert cal.next_session("US", date(2026, 12, 24)) == date(2026, 12, 28)     # Christmas Friday → Monday
    assert cal.next_session("KRX", date(2026, 12, 30)) == date(2027, 1, 4)      # 12-31 and 01-01 across the years


@pytest.mark.unit
def test_a_half_day_closes_early_and_the_decide_follows():
    assert cal.session_close("US", date(2026, 11, 27)) == time(13, 0)
    assert cal.session_close("US", date(2026, 11, 30)) == time(16, 0)
    assert cal.session_close("KRX", date(2026, 11, 27)) == time(15, 30)
    assert dl.stage_time("US", "decide", date(2026, 11, 27)) == datetime(2026, 11, 27, 13, 5, tzinfo=ET)
    assert dl.stage_time("US", "decide", date(2026, 11, 30)) == datetime(2026, 11, 30, 16, 5, tzinfo=ET)
    assert dl.stage_time("US", "decide", date(2026, 12, 24)) == datetime(2026, 12, 24, 13, 5, tzinfo=ET)


def _plan(markets, after, n):
    return [f"{t.astimezone(dl.kis.KST):%m-%d %a %H:%M} {m} {s}" for t, m, s in sched.plan(dl, markets, after, n)]


@pytest.mark.unit
def test_the_plan_never_points_at_a_holiday():
    assert _plan(["KRX", "US"], at("2026-12-24", "16:00"), 4) == [
        "12-25 Fri 03:05 US decide",                       # the 24th's half day: 13:05 ET
        "12-28 Mon 09:05 KRX fill", "12-28 Mon 15:35 KRX decide", "12-29 Tue 06:05 US decide"]
    assert _plan(["KRX"], at("2026-09-23", "16:00"), 2) == ["09-28 Mon 09:05 KRX fill", "09-28 Mon 15:35 KRX decide"]
    # the 25th's decide, then no Thanksgiving one (it would be 11-27 06:05 KST), the half day's at 13:05 ET, Monday's
    assert _plan(["US"], at("2026-11-26", "06:00"), 3) == ["11-26 Thu 06:05 US decide", "11-28 Sat 03:05 US decide",
                                                          "12-01 Tue 06:05 US decide"]


@pytest.mark.unit
def test_the_exam_days_session_moves_an_hour_and_its_stages_with_it():
    exam = date(2026, 11, 19)
    assert (cal.session_open("KRX", exam), cal.session_close("KRX", exam)) == (time(10, 0), time(16, 30))
    assert cal.year_calendar("KRX", 2026)["sessions"]["2026-11-19"]["source"].startswith("https://")
    assert dl.stage_time("KRX", "fill", exam) == at("2026-11-19", "10:05")
    assert dl.stage_time("KRX", "decide", exam) == at("2026-11-19", "16:35")
    assert dl.next_open("KRX", date(2026, 11, 18)) == at("2026-11-19", "10:00")
    assert dl.due_day("KRX", "decide", at("2026-11-19", "09:30")) == "2026-11-18"     # before the late open
    assert dl.stage_time("KRX", "decide", date(2026, 11, 20)) == at("2026-11-20", "15:35")
    assert _plan(["KRX"], at("2026-11-18", "16:00"), 3) == [
        "11-19 Thu 10:05 KRX fill", "11-19 Thu 16:35 KRX decide", "11-20 Fri 09:05 KRX fill"]


@pytest.mark.unit
def test_a_refetch_keeps_the_hand_written_sessions_and_a_shorter_list_needs_force(tmp_path, monkeypatch):
    import fetch_calendar as fc

    (tmp_path / "KRX-2026.json").write_text(json.dumps({
        "market": "KRX", "year": 2026, "source": "s", "fetched": "2026-01-01",
        "holidays": [{"date": "2026-01-01", "name": "a"}, {"date": "2026-02-16", "name": "b"}], "early_close": {},
        "sessions": {"2026-11-19": {"hours": "10:00-16:30"}}}), encoding="utf-8")
    shorter = {"source": "s", "holidays": [{"date": "2026-01-01", "name": "a"}], "early_close": {}}
    monkeypatch.setattr(fc, "krx", lambda year: shorter)
    args = ["--market", "KRX", "--year", "2026", "--out", str(tmp_path)]
    assert fc.main(args) == 1
    assert fc.main([*args, "--force"]) == 0
    body = json.loads((tmp_path / "KRX-2026.json").read_text(encoding="utf-8"))
    assert body["holidays"] == shorter["holidays"] and body["sessions"] == {"2026-11-19": {"hours": "10:00-16:30"}}


@pytest.mark.unit
def test_the_scheduler_outlives_a_broken_calendar_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(tmp_path))
    (tmp_path / "KRX-2026.json").write_text("{", encoding="utf-8")
    stop = []
    sched.serve(dl, ["KRX"], tmp_path, _market(), stop, clock=lambda: at("2026-08-08", "12:00"),
                sleep=lambda s: stop.append("test"))
    out = capsys.readouterr().out
    assert "일정 계산 오류 — JSONDecodeError" in out and "다음 깨어남 2026-08-08 13:00 KST" in out


@pytest.mark.unit
def test_a_year_with_no_file_is_every_weekday(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(tmp_path))
    assert not cal.covered("KRX", 2026) and cal.source("KRX", 2026) is None
    assert cal.is_session("KRX", date(2026, 12, 25)) and not cal.is_session("KRX", date(2026, 12, 26))
    assert cal.session_close("US", date(2026, 11, 27)) == time(16, 0)


@pytest.mark.unit
def test_a_file_under_another_name_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(tmp_path))
    (tmp_path / "KRX-2026.json").write_text(json.dumps({"market": "US", "year": 2026, "holidays": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="파일 이름과 다름"):
        cal.is_session("KRX", date(2026, 8, 6))


# ---- the loop over a test calendar ---------------------------------------------------------------------------------

@pytest.mark.unit
def test_a_listed_holiday_is_never_due_and_is_recorded_without_running(store, test_calendar, tmp_path):
    client = _market()                                       # it has a bar on the holiday: the list decides
    assert _decide(client, D1, tmp_path).status == "ok"
    # on the holiday, after its close: the day due is still the 5th (done) — nothing runs, nothing fails
    assert dl.due_day("KRX", "decide", at(HOLIDAY, "16:00")) == D1
    asked = len(client.asked)
    client.today = HOLIDAY
    assert dl.run_due("KRX", "decide", tmp_path, client, at(HOLIDAY, "16:00")) == []
    assert len(client.asked) == asked
    client.today = D3
    out = dl.run_due("KRX", "decide", tmp_path, client, at(D3, "15:40"))
    assert [(r.trade_date, r.status, r.reason) for r in out] == [(HOLIDAY, "skipped", "holiday"), (D3, "ok", None)]
    assert out[0].notes == ["휴장 달력: 시험 휴일 — test (2026-11-16)"] and out[0].counts == {}
    saved = [(r.trade_date, r.status, r.reason) for r in market_audit(tmp_path / "audit", "KRX").runs()]
    assert saved == [(D1, "ok", None), (HOLIDAY, "skipped", "holiday"), (D3, "ok", None)]
    assert not [n for r in out for n in r.notes if n.startswith("휴장 달력 없음")]


@pytest.mark.unit
def test_the_fill_after_a_holiday_fills_the_session_before_it(store, test_calendar, tmp_path):
    client = _market(days=DAYS[DAYS != pd.Timestamp(HOLIDAY)])            # a real holiday has no bar
    assert _decide(client, D1, tmp_path).status == "ok"                   # open orders for the next session
    assert dl.next_open("KRX", date.fromisoformat(D1)) == at(D3, "09:00")
    client.today = D3
    out = dl.run_due("KRX", "fill", tmp_path, client, at(D3, "09:10"))
    # no fill record before: the loop starts on its first day, so the holiday has no record to write
    assert [(r.trade_date, r.status) for r in out] == [(D3, "ok")] and out[0].counts["orders_filled"] > 0
    filled = {o.fill_date for o in market_audit(tmp_path / "audit", "KRX").orders() if o.status == "filled"}
    assert filled == {D3}


@pytest.mark.unit
def test_a_date_run_on_a_listed_holiday_writes_its_record_and_asks_nothing(store, test_calendar, tmp_path):
    client = _market()
    client.today = D3
    r = dl.run_stage("KRX", "decide", HOLIDAY, tmp_path, client, at(D3, "08:00"))
    assert (r.status, r.reason, r.notes) == ("skipped", "holiday", ["휴장 달력: 시험 휴일 — test (2026-11-16)"])
    assert client.asked == [] and dl.run_stage("KRX", "decide", HOLIDAY, tmp_path, client, at(D3, "08:00")) is None


@pytest.mark.unit
def test_no_file_for_the_year_runs_every_weekday_and_says_so(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(tmp_path / "none"))
    client = _market()
    assert _decide(client, D1, tmp_path).status == "ok"
    r = _decide(client, HOLIDAY, tmp_path)                                # a weekday: it runs
    assert r.status == "ok" and "휴장 달력 없음 2026" in r.notes


@pytest.mark.unit
def test_each_market_its_own_list(store, test_calendar, tmp_path):
    assert not cal.is_session("KRX", date.fromisoformat(HOLIDAY)) and cal.is_session("US", date.fromisoformat(HOLIDAY))
    client = _market(market="US")
    client.today = HOLIDAY
    r = dl.run_stage("US", "decide", HOLIDAY, tmp_path, client, at(D3, "06:10"))
    assert r.status == "ok" and "휴장 달력 없음 2026" in r.notes          # the US has no file in the test calendar


@pytest.mark.unit
def test_a_closure_no_list_had_is_still_caught_after_the_fact(store, test_calendar, tmp_path):
    # the 4th: a weekday the list does not close, but no market bar that day (a sudden closure)
    days = DAYS[DAYS != pd.Timestamp("2026-08-04")]
    client = _market(days=days)
    client.today = "2026-08-04"
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:40"))
    assert (r.status, r.error) == ("failed", "당일 벤치마크(^KS11) 행 없음 — 휴장 또는 지연")      # today: not final
    client.today = D1
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at(D1, "08:00"))
    assert (r.status, r.reason) == ("skipped", "holiday") and not any(n.startswith("휴장 달력") for n in r.notes)
