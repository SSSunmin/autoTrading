"""Sell reasons (feature/sell-reasons): a sell review's action and every reason it gave, read back from its recorded text
(no model call), on the decision card, the exception queue, the positions and ``GET /api/reviews`` — apart from a rule
exit's reason, which stays on its own order. The ticker tab's verdict mapping is the same function."""

import importlib

import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import DecisionRecord, OrderRecord, market_audit
from agent_trading_core.schemas import SellAction, SellReason
from tests.test_api import _cache
from tests.test_ticker_detail import DAYS, _memo, _rec, _review

R = SellReason


@pytest.fixture
def book(tmp_path, monkeypatch):
    """KRX, three names bought on memos 06-01 (filled 06-02):
    - 000660.KS trimmed by a review 06-02 (thesis_broken + overweight, 2%) → partial sell 06-04; a Hold review 06-04
    - 005380.KS exited by a review 06-02 → full sell 06-04
    - 005930.KS falls 30% on 06-04: the rule's stop-loss exit, and the agent's Exit review the same day (the day's loss
      halts the loop, so that review places no order)
    - 000270.KS: a Trim review 06-05 whose order is an exception (no close), and a rule exit order rejected that day
    US: AAPL an Exit review, never executed."""
    from agent_trading_core.execution import Prices, execute_date
    from agent_trading_core.risk import RiskConfig

    data = tmp_path / "data"
    _cache(data, "^KS11", [(d, 100 + i, 100 + i) for i, d in enumerate(DAYS)])
    for t in ("000660.KS", "005380.KS"):
        _cache(data, t, [(d, 10_000, 10_000) for d in DAYS])
    _cache(data, "005930.KS", [("2026-06-01", 10_000, 10_000), ("2026-06-02", 10_000, 10_000),
                               ("2026-06-04", 7_000, 7_000), ("2026-06-05", 7_000, 7_000)])
    krx = market_audit(data / "audit", "KRX")
    for t in ("000660.KS", "005380.KS", "005930.KS"):
        krx.record_decision(_rec(t, "2026-06-01", _memo()))
    prices, risk = Prices(data / "cache"), RiskConfig(regime_ma_days=3)
    execute_date(krx, "2026-06-01", prices, risk=risk)
    krx.record_decision(_rec("000660.KS", "2026-06-02", _review(SellAction.TRIM, (True, False), [R.THESIS_BROKEN, R.OVERWEIGHT], 2)))
    krx.record_decision(_rec("005380.KS", "2026-06-02", _review(SellAction.EXIT, (True, True), [R.THESIS_BROKEN])))
    execute_date(krx, "2026-06-02", prices, risk=risk)
    krx.record_decision(_rec("000660.KS", "2026-06-04", _review(SellAction.HOLD)))
    krx.record_decision(_rec("005930.KS", "2026-06-04", _review(SellAction.EXIT, (False, False), [R.HARD_RULE, R.WOULD_NOT_REBUY])))
    execute_date(krx, "2026-06-04", prices, risk=risk)
    krx.record_decision(_rec("000270.KS", "2026-06-05", _review(SellAction.TRIM, (False, False), [R.TARGET_REACHED], 5)))
    krx.record_order(OrderRecord(order_id="paper-2026-06-05-000270.KS", ticker="000270.KS", trade_date="2026-06-05",
                                 side="Sell", quantity=0, status="exception", exception_kind="vendor_unavailable",
                                 reason="결정일 종가 없음"))
    krx.record_order(OrderRecord(order_id="paper-2026-06-05-000270.KS-exit", ticker="000270.KS", trade_date="2026-06-05",
                                 side="Sell", quantity=3, status="rejected", reason="거부"))
    krx.record_decision(DecisionRecord(ticker="000270.KS", trade_date="2026-06-05", rating="Sell", action="Sell",
                                       source="rule", thesis="**Rating**: Sell\n\n규칙 청산: 손절"))
    us = market_audit(data / "audit", "US")
    us.record_decision(_rec("AAPL", "2026-06-04", _review(SellAction.EXIT, (False, False), [R.CATALYST_GONE, R.BETTER_ALTERNATIVE]), "US"))
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from backend import main
    importlib.reload(main)
    return TestClient(main.app)


@pytest.mark.unit
def test_reviews_list_every_reason_the_action_and_the_linked_order(book):
    rows = book.get("/api/reviews").json()
    assert [(r["trade_date"], r["ticker"], r["action"], r["verdict_ko"]) for r in rows] == [
        ("2026-06-05", "000270.KS", "Trim", "축소"), ("2026-06-04", "005930.KS", "Exit", "청산"),   # same day: ticker, descending
        ("2026-06-04", "000660.KS", "Hold", "논지 유지"), ("2026-06-02", "005380.KS", "Exit", "청산"),
        ("2026-06-02", "000660.KS", "Trim", "축소")]
    by = {(r["trade_date"], r["ticker"]): r for r in rows}
    trim = by[("2026-06-02", "000660.KS")]
    assert trim["reasons"] == ["thesis_broken", "overweight"] and trim["reasons_ko"] == ["논지 깨짐", "비중 초과"]
    assert trim["trim_to_pct"] == 2 and (trim["met"], trim["total"]) == (1, 2)
    (o,) = trim["orders"]
    assert o["status"] == "filled" and o["fill_date"] == "2026-06-04"
    assert 0 < o["partial_quantity"] == o["filled_quantity"] < o["held_before"]
    (full,) = by[("2026-06-02", "005380.KS")]["orders"]
    assert full["status"] == "filled" and full["partial_quantity"] == 0 and full["filled_quantity"] > 0
    hold = by[("2026-06-04", "000660.KS")]
    assert hold["reasons"] == [] and hold["reasons_ko"] == [] and hold["orders"] == [] and hold["trim_to_pct"] is None
    # the same day's rule stop-loss order is not the review's
    assert all(not x["order_id"].endswith("-exit") for x in by[("2026-06-04", "005930.KS")]["orders"])
    assert by[("2026-06-05", "000270.KS")]["orders"][0]["status"] == "exception"


@pytest.mark.unit
def test_reviews_filter_by_action_ticker_and_dates_and_markets_stay_apart(book):
    assert [r["ticker"] for r in book.get("/api/reviews", params={"action": "Exit"}).json()] == ["005930.KS", "005380.KS"]
    assert [r["trade_date"] for r in book.get("/api/reviews", params={"ticker": "000660.ks"}).json()] == ["2026-06-04", "2026-06-02"]
    assert [r["ticker"] for r in book.get("/api/reviews", params={"from": "2026-06-04", "to": "2026-06-04"}).json()] == [
        "005930.KS", "000660.KS"]
    assert book.get("/api/reviews", params={"action": "Sell"}).status_code == 422
    assert book.get("/api/reviews", params={"from": "2026-06-05", "to": "2026-06-01"}).status_code == 422
    (us,) = book.get("/api/reviews", params={"market": "US"}).json()
    assert (us["ticker"], us["market"], us["reasons_ko"]) == ("AAPL", "US", ["촉매 소멸", "더 좋은 대안"])
    assert "AAPL" not in {r["ticker"] for r in book.get("/api/reviews").json()}


@pytest.mark.unit
def test_cards_carry_the_review_and_a_rule_exit_keeps_its_own_reason(book):
    day = {(d["ticker"], d["source"]): d for d in book.get("/api/decisions", params={"date": "2026-06-04"}).json()}
    agent, rule = day[("005930.KS", "agent")], day[("005930.KS", "rule")]
    assert agent["review"]["reasons"] == ["hard_rule", "would_not_rebuy"] and agent["review"]["verdict"] == "exit"
    assert [o["order_id"] for o in agent["orders"]] == []
    # the rule exit: no review, its reason is on its order
    assert rule["review"] is None and rule["orders"][0]["rules_applied"] == ["stop_loss"]
    assert day[("000660.KS", "agent")]["review"]["verdict_ko"] == "논지 유지"
    memo = book.get("/api/decisions", params={"date": "2026-06-01"}).json()
    assert {d["review"] for d in memo} == {None}                 # a memo is not a review


@pytest.mark.unit
def test_exception_queue_and_positions_show_the_review(book):
    q = {x["ref"]: x for x in book.get("/api/exceptions", params={"date": "2026-06-05"}).json()}
    trim = q["paper-2026-06-05-000270.KS"]
    assert trim["review"]["action"] == "Trim" and trim["review"]["reasons_ko"] == ["목표가 도달"] and trim["review"]["trim_to_pct"] == 5
    assert q["paper-2026-06-05-000270.KS-exit"]["review"] is None
    rows = {r["ticker"]: r for r in book.get("/api/positions").json()["positions"]}
    assert rows["000660.KS"]["last_review"]["trade_date"] == "2026-06-04" and rows["000660.KS"]["last_review"]["action"] == "Hold"
    assert "005380.KS" not in rows and "005930.KS" not in rows      # sold in full, stopped out
    # a review of an earlier holding of the same ticker is not this holding's (review 1R #1): from the entry on
    from backend import main
    st = main._store(None)
    assert main._last_review(st, "005380.KS", "2026-06-05", "2026-06-04") is None       # bought again after the Exit
    assert main._last_review(st, "005380.KS", "2026-06-05", "2026-06-02")["action"] == "Exit"
    assert main._last_review(st, "000660.KS", "2026-06-02", "2026-06-02")["action"] == "Trim"   # as_of bound


@pytest.mark.unit
def test_a_failed_yahoo_call_is_a_failure_not_no_date(monkeypatch):
    """yfinance logs an HTTP error and answers an empty calendar while ``hide_exceptions`` is on (its default): the
    refresh turns it off for the call, so a 404 is kept as a failure, and puts it back."""
    import yfinance
    from yfinance.config import YfConfig

    from scripts import refresh_earnings

    class Fake:                                   # yfinance's behaviour on an unknown symbol, without the network
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def calendar(self):
            if self.symbol == "AAPL":
                return {"Earnings Date": []}
            if YfConfig.debug.hide_exceptions:
                return {}
            raise RuntimeError("HTTP Error 404: Quote not found")
    monkeypatch.setattr(yfinance, "Ticker", Fake)
    # yfinance's default; agent_trading_core.yahoo turns it off on import, which the refresh script never does.
    # Set and put back by hand: yfinance's config object does not take monkeypatch's restore
    outer = YfConfig.debug.hide_exceptions
    YfConfig.debug.hide_exceptions = True
    try:
        book, counts = refresh_earnings.refresh(["AAPL", "ZZZZQ"], {}, refresh_earnings.yahoo_calendar, today="2026-11-16")
        assert YfConfig.debug.hide_exceptions is True                                         # put back
    finally:
        YfConfig.debug.hide_exceptions = outer
    assert counts == {"calls": 2, "dated": 0, "none": 1, "failed": 1}
    assert "error" not in book["AAPL"] and book["AAPL"]["date"] is None                       # 예정일 없음
    assert book["ZZZZQ"]["error"] == "RuntimeError: HTTP Error 404: Quote not found"           # 조회 실패
