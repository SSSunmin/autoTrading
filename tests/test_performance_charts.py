"""Performance 6-A · 6-B (performance-charts): the book's daily series (time-weighted NAV — a deposit is no gain —
drawdown, exposure, cash) by hand, the reference line, empty and one-day books, the market split, the cache that
re-reads after a record, the rating bars against ``backtest.summarize``, settlement failures, the loop's price
fallback lines, both stores."""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from agent_trading_core import backtest
from agent_trading_core.audit import OrderRecord, RunRecord, market_audit
from agent_trading_core.execution import Prices, book_series
from agent_trading_core.memory import TradingMemoryLog
from tests.test_live_capital import BUY, CFG, D, _book, _capital
from tests.test_market_axis import _cache

# --- the series by hand ----------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_the_series_leaves_a_deposit_out_of_the_nav_by_hand():
    """test_live_capital's book: 10,000, 50 A @100 on D1, closes 100 · 120 · 90 · 108 · 99, +5,000 at D3's close.
    NAV 10,000 · 11,000 · 9,500 · 10,089.66 · 9,794.83 → nav_pct 0 · +10% · −5% · +0.90% · −2.05%; the peak is D2's
    11,000 → drawdown 0 · 0 · −13.64% · −8.28% · −10.96%. Exposure = holdings / equity: 5,000/10,000 · 6,000/11,000 ·
    4,500/14,500 · 5,400/15,400 · 4,950/14,950."""
    ops = [_capital(10_000.0, "start", D[0]), _capital(15_000.0, "flow", D[2], flow=5_000.0)]
    s = book_series([BUY], ops, _book([100.0, 120.0, 90.0, 108.0, 99.0]), CFG)
    nav = [10_000, 11_000, 9_500, 9_500 * 15_400 / 14_500, 9_500 * 15_400 / 14_500 * 14_950 / 15_400]
    assert (s["start"], s["as_of"], [r["date"] for r in s["rows"]]) == (D[0], D[4], D)
    assert [r["nav_pct"] for r in s["rows"]] == pytest.approx([n / 10_000 - 1 for n in nav])
    assert [r["drawdown_pct"] for r in s["rows"]] == pytest.approx([0, 0, 9_500 / 11_000 - 1, nav[3] / 11_000 - 1,
                                                                   nav[4] / 11_000 - 1])
    exposure = [5_000 / 10_000, 6_000 / 11_000, 4_500 / 14_500, 5_400 / 15_400, 4_950 / 14_950]
    assert [r["exposure_pct"] for r in s["rows"]] == pytest.approx(exposure)
    assert [r["cash_pct"] for r in s["rows"]] == pytest.approx([1 - x for x in exposure])
    assert s["rows"][2]["equity"] == pytest.approx(14_500)                    # the money moved; the NAV did not


@pytest.mark.unit
def test_an_empty_book_has_no_rows_and_a_one_day_book_has_one():
    prices = _book([100.0] * 5)
    assert book_series([], [], prices, CFG) == {"start": None, "as_of": D[4], "initial_cash": 999.0, "rows": []}
    one = book_series([BUY], [], prices, CFG, as_of=D[0])
    assert [(r["date"], r["nav_pct"], r["drawdown_pct"]) for r in one["rows"]] == [(D[0], pytest.approx(0.0), 0.0)]
    assert book_series([BUY], [], Prices(frames={}), CFG)["rows"] == []        # no calendar: nothing to replay


# --- the API ------------------------------------------------------------------------------------------------------

@pytest.fixture(params=["sqlite", "jsonl"])
def data(request, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
    for index in ("^KS11", "^GSPC"):
        _cache(data, index, [(d, 100 + i, 100 + i) for i, d in enumerate(days)])
    _cache(data, "AAPL", [(d, o, c) for d, o, c in zip(days, (100, 100, 110, 99), (100, 110, 99, 99), strict=True)])
    # the US reference: rules_on_regime equity 500 on 05-28, 1,000 on 05-29 (the last day before the start — the base), then 1,010 · 1,020 · 990
    out = data / "results" / "paper_us"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps({"variants": {"rules_on_regime": {"equity_csv": "rules_on_regime_equity.csv"}}}),
                                      encoding="utf-8")
    (out / "rules_on_regime_equity.csv").write_text(
        "Date,equity,^GSPC,exposure\n2026-05-28,500,1,0\n2026-05-29,1000,1,0\n2026-06-01,1010,1,0\n2026-06-02,1020,1,0\n2026-06-03,990,1,0\n",
        encoding="utf-8")
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    return data


def _client():
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), main


def _buy_aapl(data):
    market_audit(data / "audit", "US").record_order(OrderRecord(
        order_id="paper-2026-06-01-AAPL", ticker="AAPL", trade_date="2026-06-01", side="Buy", quantity=100,
        filled_quantity=100, filled_price=100.0, status="filled", fill_date="2026-06-02", market="US"))


@pytest.mark.unit
def test_the_series_api_by_market_range_and_reference(data):
    api, _ = _client()
    _buy_aapl(data)
    us = api.get("/api/performance/series", params={"market": "US"}).json()
    # 100,000 cash; 100 AAPL @100 filled 06-02 → closes 110 · 99 · 99 (no costs in the US paper book but its sell fees)
    assert (us["start"], us["as_of"], [r["date"] for r in us["rows"]]) == ("2026-06-01", "2026-06-04",
                                                                           ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"])
    assert [r["nav_pct"] for r in us["rows"]] == pytest.approx([0, 0.01, -0.001, -0.001])
    assert [r["drawdown_pct"] for r in us["rows"]] == pytest.approx([0, 0, 99_900 / 101_000 - 1, 99_900 / 101_000 - 1])
    assert us["rows"][1]["exposure_pct"] == pytest.approx(11_000 / 101_000)
    # reference: base 1,000 (05-29, the last row before the start) → +1% · +2% · −1%, nothing for 06-04
    assert [r["baseline_pct"] for r in us["rows"]][:3] == pytest.approx([0.01, 0.02, -0.01])
    assert us["rows"][3]["baseline_pct"] is None and us["reference"]["covered_until"] == "2026-06-03"
    krx = api.get("/api/performance/series", params={"market": "KRX"}).json()
    assert (krx["start"], krx["rows"], krx["reference"]["error"] is not None) == (None, [], True)   # the US book is its own
    cut = api.get("/api/performance/series", params={"market": "US", "from": "2026-06-02", "to": "2026-06-03"}).json()
    assert [r["date"] for r in cut["rows"]] == ["2026-06-02", "2026-06-03"]
    assert cut["rows"][0]["nav_pct"] == pytest.approx(0.01)                        # still against the start
    for q in ({"from": "2026-6-2"}, {"to": "x"}, {"from": "2026-06-03", "to": "2026-06-02"}):
        assert api.get("/api/performance/series", params={"market": "US"} | q).status_code == 422
    assert api.get("/api/performance/series", params={"market": "XX"}).status_code == 400


@pytest.mark.unit
def test_the_series_cache_rereads_after_a_record(data):
    api, _ = _client()
    _buy_aapl(data)
    first = api.get("/api/performance/series", params={"market": "US"}).json()
    again = api.get("/api/performance/series", params={"market": "US"}).json()
    assert (first["cache"], again["cache"], again["rows"]) == ("miss", "hit", first["rows"])
    # a deposit at the 06-03 close: the equity moves, the NAV does not — and the cache must not serve the old book
    market_audit(data / "audit", "US").record_operation(_capital(150_000.0, "flow", "2026-06-03", flow=50_000.0))
    after = api.get("/api/performance/series", params={"market": "US"}).json()
    assert after["cache"] == "miss"
    assert after["rows"][2]["equity"] == pytest.approx(first["rows"][2]["equity"] + 50_000)
    assert [r["nav_pct"] for r in after["rows"]] == pytest.approx([r["nav_pct"] for r in first["rows"]])
    # an order record invalidates it too
    market_audit(data / "audit", "US").record_order(OrderRecord(
        order_id="paper-2026-06-03-AAPL-exit", ticker="AAPL", trade_date="2026-06-03", side="Sell", quantity=100,
        filled_quantity=100, filled_price=99.0, status="filled", fill_date="2026-06-04", market="US"))
    sold = api.get("/api/performance/series", params={"market": "US"}).json()
    assert sold["cache"] == "miss" and sold["rows"][3]["exposure_pct"] == 0.0


# --- 6-B: the rating bars and the settlement failures ------------------------------------------------------------

def _entry(date, ticker, rating, alpha, raw, pending=False, holding="5d"):
    return {"date": date, "ticker": ticker, "rating": rating, "pending": pending, "alpha": alpha, "raw": raw,
            "holding": holding, "resolved": None, "decision": "", "reflection": ""}


MEMORY = [
    _entry("2026-06-01", "A", "Buy", "+2.0%", "+3.0%"), _entry("2026-06-01", "B", "Buy", "-1.0%", "+1.0%"),
    _entry("2026-06-01", "C", "Sell", "-4.0%", "-5.0%"), _entry("2026-06-01", "D", "Hold", "+1.0%", "+0.5%"),
    _entry("2026-06-01", "E", "REVIEW", "+9.0%", "+9.0%"), _entry("2026-06-01", "F", "Buy", None, None, pending=True),
]


@pytest.mark.unit
def test_the_rating_bars_are_the_scoring_paths_by_hand(monkeypatch, tmp_path):
    """Buy: alphas +2 · −1 → mean +0.5%, hit 1/2, raw +2%. Sell: −4 → hit 1/1 (a negative alpha), raw −5%. Hold: no hit
    rate. REVIEW and the pending one are out. ``backtest.summarize`` on the same entries gives the same."""
    _, main = _client()
    bars = {b["rating"]: b for b in main._rating_bars(MEMORY)}
    assert (bars["Buy"]["count"], bars["Buy"]["mean_alpha"], bars["Buy"]["hit_rate"], bars["Buy"]["mean_raw"]) == \
        (2, pytest.approx(0.005), 0.5, pytest.approx(0.02))
    assert (bars["Sell"]["hit_rate"], bars["Sell"]["mean_raw"], bars["Hold"]["hit_rate"]) == (1.0, pytest.approx(-0.05), None)
    assert (bars["Overweight"]["count"], bars["Overweight"]["mean_alpha"]) == (0, None)
    monkeypatch.setattr(TradingMemoryLog, "load_entries", lambda self: MEMORY)
    log = tmp_path / "m.md"
    log.write_text("", encoding="utf-8")
    scored = backtest.summarize(log).by_rating
    for r, s in scored.items():
        assert (bars[r]["count"], bars[r]["hit_rate"]) == (s.count, s.hit_rate)
        assert bars[r]["mean_alpha"] == pytest.approx(s.mean_alpha)


@pytest.mark.unit
def test_settlement_failures_by_reason(monkeypatch, data):
    """Review 1R #1: the window is the scoring path's (``memory.settlement.fetch_returns``) — entry the first close after
    the decision, exit ``holding`` sessions on. 06-01 + 5 sessions is past the 06-01..06-04 calendar, so 2-day windows:
    decided 06-01 → entry 06-02, exit 06-04. AAPL priced through 06-04 → waiting for the settle run; EDGE ends 06-03, a
    day short of its exit → price_ended; GONE ends 06-01 → price_ended; NOFILE → no_price. Decided 06-02 (exit past the
    calendar): waiting even with no file (LATE); AAPL 06-03 likewise."""
    _cache(data, "GONE", [("2026-06-01", 10, 10)])
    _cache(data, "EDGE", [(d, 10, 10) for d in ("2026-06-01", "2026-06-02", "2026-06-03")])
    _, main = _client()
    st = type("S", (), {"market": "US", "memory": [
        _entry("2026-06-01", "AAPL", "Buy", None, None, pending=True, holding="2d"),
        _entry("2026-06-01", "EDGE", "Buy", None, None, pending=True, holding="2d"),
        _entry("2026-06-01", "GONE", "Buy", None, None, pending=True, holding="2d"),
        _entry("2026-06-01", "NOFILE", "Sell", None, None, pending=True, holding="2d"),
        _entry("2026-06-02", "LATE", "Sell", None, None, pending=True, holding="2d"),
        _entry("2026-06-03", "AAPL", "Buy", None, None, pending=True, holding="2d"),
        _entry("2026-06-01", "E", "REVIEW", None, None, pending=True)]})()
    out = main._settlement(st)
    assert {r["id"]: (r["count"], r["tickers"]) for r in out["by_reason"]} == {"no_price": (1, ["NOFILE"]),
                                                                              "price_ended": (2, ["EDGE", "GONE"])}
    assert (out["failed"], out["waiting"]) == (3, 3)


@pytest.mark.unit
def test_a_log_value_with_spaces_parses_as_the_scoring_path_does():
    """Review 1R #3: backtest._alpha strips before the %; the bars must count the same entries."""
    _, main = _client()
    bar = {b["rating"]: b for b in main._rating_bars([_entry("2026-06-01", "A", "Buy", " +1.0% ", "+2.0% ")])}["Buy"]
    assert (bar["count"], bar["mean_alpha"], bar["mean_raw"]) == (1, pytest.approx(0.01), pytest.approx(0.02))


@pytest.mark.unit
def test_the_loops_price_fallback_lines_show_from_the_last_run(data):
    api, _ = _client()
    assert api.get("/api/data/status").json()["price_fallback"] == {"KRX": None, "US": None}
    a = market_audit(data / "audit", "KRX")
    line = "가격 Yahoo 폴백(KIS 파일 없음): 005930.KS — 2026-06-04 실행, Yahoo 마지막 행 2026-06-04"
    a.record_run(RunRecord(market="KRX", stage="fill", trade_date="2026-06-04", started_at="2026-06-04T00:05:00Z",
                           status="ok", notes=["체결 3건", line]))
    fb = api.get("/api/data/status").json()["price_fallback"]
    assert fb["US"] is None and (fb["KRX"]["stage"], fb["KRX"]["notes"]) == ("fill", [line])
    a.record_run(RunRecord(market="KRX", stage="decide", trade_date="2026-06-04", started_at="2026-06-04T06:35:00Z",
                           status="ok", notes=["결정 2건"]))
    assert api.get("/api/data/status").json()["price_fallback"]["KRX"] is None      # the last run had none


@pytest.mark.unit
def test_the_rerun_pending_label_says_what_happens():
    from agent_trading_core.krx_names import EXCEPTION_PENDING_KO
    assert "미연결" not in EXCEPTION_PENDING_KO["rerun"] and "decide" in EXCEPTION_PENDING_KO["rerun"]


# --- holiday-calendar follow-ups (TASK 추가 ①②④) -------------------------------------------------------------------

@pytest.fixture
def tracked_calendar(monkeypatch):
    """The tracked exchange lists under data/calendar/ (conftest starts every test without one)."""
    from pathlib import Path
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(Path(__file__).resolve().parents[1] / "data" / "calendar"))


@pytest.mark.unit
def test_next_trading_day_past_the_prices_skips_a_listed_holiday(tracked_calendar):
    """① 12-24 halt → KRX 12-25 is a holiday, the next session is 12-28 (Mon); US 11-25 → 11-26 Thanksgiving → 11-27.
    Inside the price calendar the bars still decide."""
    from agent_trading_core.execution import next_trading_day
    prices = ["2026-06-01", "2026-06-02"]
    assert next_trading_day(prices, "2026-12-24", "KRX") == "2026-12-28"
    assert next_trading_day(prices, "2026-12-30", "KRX") == "2027-01-04"           # 12-31 year-end closing
    assert next_trading_day(prices, "2026-11-25", "US") == "2026-11-27"
    assert next_trading_day(prices, "2026-06-01", "KRX") == "2026-06-02"


@pytest.mark.unit
def test_a_daily_loss_halt_on_christmas_eve_resumes_at_the_next_session(tracked_calendar):
    from datetime import UTC, datetime

    from agent_trading_core.audit import OperationRecord
    from agent_trading_core.execution import effective_state
    halt = OperationRecord(kind="halt", operator="system", role="auto", reason="일일 손실", target="daily_loss",
                           market="KRX", scope="KRX", before={"running": True}, after={"running": False, "date": "2026-12-24"})
    st = effective_state([halt], ["2026-12-23", "2026-12-24"], "KRX", datetime(2026, 12, 24, 10, tzinfo=UTC))
    assert st["auto_resume_at"].startswith("2026-12-28T00:00")


@pytest.mark.unit
def test_the_session_window_follows_the_calendar(tracked_calendar):
    """② US 11-27 half day closes 13:00 ET (03:00 KST next day); KRX 11-19 exam day 10:00-16:30; a plain day the config."""
    from datetime import date

    _, main = _client()
    o, c = main.session_window("US", date(2026, 11, 27))
    assert (f"{o:%m-%d %H:%M}", f"{c:%m-%d %H:%M}") == ("11-27 23:30", "11-28 03:00")
    o, c = main.session_window("KRX", date(2026, 11, 19))
    assert (f"{o:%H:%M}", f"{c:%H:%M}") == ("10:00", "16:30")
    o, c = main.session_window("KRX", date(2026, 11, 18))
    assert (f"{o:%H:%M}", f"{c:%H:%M}") == ("09:00", "15:30")


@pytest.mark.unit
def test_a_broken_calendar_file_errs_only_its_market_card(data, tmp_path, monkeypatch):
    """④ a cut-short KRX list: /api/markets answers, the KRX card says 일정 계산 오류 (no weekday guess), US is whole."""
    cal = tmp_path / "calendar"
    cal.mkdir()
    for y in (2026, 2027):
        (cal / f"KRX-{y}.json").write_text('{"market": "KRX", "year": ', encoding="utf-8")
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(cal))
    api, _ = _client()
    r = api.get("/api/markets")
    assert r.status_code == 200
    cards = {c["market"]: c for c in r.json()}
    k, u = cards["KRX"], cards["US"]
    assert (k["next_run_kst"], k["next_run_label_ko"], k["session_kst"]) == (None, "일정 계산 오류", None)
    assert k["schedule_error"].startswith("국내 휴장 달력을 읽지 못함: JSONDecodeError")
    assert k["error"] is None and k["equity"] is not None                  # the book still reads
    assert u["schedule_error"] is None and u["next_run_kst"] and u["session_kst"]
