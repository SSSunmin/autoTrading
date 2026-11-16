"""Ticker detail (feature/ticker-detail): the 논지 점검 tab reads the recorded memos and sell reviews back (no model call),
the 보유 tab is the paper book's row, the earnings date is a cached reference written only by scripts/refresh_earnings.py,
and sector / exchange come from files. Markets stay apart."""

import importlib
import json
import socket
from datetime import date

import pytest
from fastapi.testclient import TestClient

from agent_trading_core.agent import CANDIDATE, HOLDING, Outcome, to_record
from agent_trading_core.audit import DecisionRecord, market_audit, read_thesis
from agent_trading_core.schemas import (
    Catalyst,
    InvalidationCheck,
    InvestmentMemo,
    PortfolioRating,
    SellAction,
    SellReason,
    SellReview,
)
from scripts import refresh_earnings
from tests.test_api import _cache

DAYS = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05"]


def _memo(rating=PortfolioRating.OVERWEIGHT, invalidation=("영업이익률 2분기 연속 12% 미만", "HBM 점유율 40% 아래")):
    return InvestmentMemo(
        business_summary="메모리 반도체. HBM 비중 증가.", thesis="HBM 공급 부족이 이익 추정치를 끌어올린다.",
        bull_case="HBM4 조기 인증.", bear_case="재고 조정.", valuation_basis="P/B 1.4배.",
        current_price=10_000, price_target=14_000, downside_price=8_500,
        catalysts=[Catalyst(event="실적", expected_by="2026-07")], invalidation=list(invalidation),
        rating=rating, conviction=0.6, sizing_pct=5, exit_plan="목표가 절반 매도.", time_horizon="6-12 months")


def _review(action, met=(False, False), reasons=(), trim=None):
    conds = ["영업이익률 2분기 연속 12% 미만", "HBM 점유율 40% 아래"]
    return SellReview(checks=[InvalidationCheck(condition=c, met=m, evidence=f"근거 {i}") for i, (c, m) in enumerate(zip(conds, met, strict=True))],
                      reasons=list(reasons), reasoning=f"{action.value} 판단", action=action, trim_to_pct=trim)


def _rec(ticker, day, output, market="KRX"):
    kind = HOLDING if isinstance(output, SellReview) else CANDIDATE
    return to_record(Outcome(kind, ticker, output, None, {}, 1.0), market=market, day=day, screen_run_id=None, evidence=[])


@pytest.mark.unit
def test_read_thesis_reads_the_rendered_memo_and_review_back():
    m = read_thesis(_rec("A.KS", "2026-06-01", _memo()))
    assert m["kind"] == "memo" and m["invalidation"] == ["영업이익률 2분기 연속 12% 미만", "HBM 점유율 40% 아래"]
    assert (m["price"], m["target"], m["downside"]) == (10_000, 14_000, 8_500)
    assert m["thesis"].startswith("HBM 공급") and m["horizon"] == "6-12 months" and m["exit_plan"] == "목표가 절반 매도."
    r = read_thesis(_rec("A.KS", "2026-06-04", _review(SellAction.TRIM, (True, False), [SellReason.THESIS_BROKEN, SellReason.OVERWEIGHT], 2.5)))
    assert (r["kind"], r["action"], r["trim_to_pct"]) == ("review", "Trim", 2.5)
    assert r["reasons"] == ["thesis_broken", "overweight"] and r["reasoning"] == "Trim 판단"
    assert r["checks"] == [{"condition": "영업이익률 2분기 연속 12% 미만", "met": True, "evidence": "근거 0"},
                           {"condition": "HBM 점유율 40% 아래", "met": False, "evidence": "근거 1"}]
    # a value over several lines is read whole, and a check-like line inside it is not a check (review 1R #2)
    two = _review(SellAction.HOLD).model_copy(update={"reasoning": "첫 줄\n- [x] 가짜 — 근거"})
    r2 = read_thesis(_rec("A.KS", "2026-06-04", two))
    assert r2["reasoning"] == "첫 줄\n- [x] 가짜 — 근거" and [c["condition"] for c in r2["checks"]] == [
        "영업이익률 2분기 연속 12% 미만", "HBM 점유율 40% 아래"]
    # the Trim note to_record adds after a blank line is not part of the reasoning
    assert read_thesis(_rec("A.KS", "2026-06-04", _review(SellAction.TRIM, reasons=[SellReason.OVERWEIGHT], trim=2)))["reasoning"] == "Trim 판단"
    h = read_thesis(_rec("A.KS", "2026-06-05", _review(SellAction.HOLD)))
    assert (h["action"], h["trim_to_pct"], h["reasons"]) == ("Hold", None, [])
    # a rule decision and a REVIEW (no answer) are not read as either
    assert read_thesis(DecisionRecord(ticker="A.KS", trade_date="2026-06-01", rating="Buy", thesis="**Rating**: Buy\n\n근거: 모멘텀")) is None
    assert read_thesis(to_record(Outcome(HOLDING, "A.KS", None, "schema", {}, 1.0), market="KRX", day="2026-06-01",
                                 screen_run_id=None, evidence=[])) is None


@pytest.fixture
def world(tmp_path, monkeypatch):
    """KRX: 005930.KS bought on a memo (06-01, filled 06-02), trimmed by a review on 06-04 (filled 06-05), a Hold review
    on 06-05 recorded before the Trim; 000660.KS a Hold memo only; 005380.KS a rule Buy. US: AAPL a memo and an Exit
    review, never held. Earnings cache: AAPL dated, MSFT failed with an old date; exchange cache AAPL → NAS."""
    from agent_trading_core.execution import Prices, execute_date
    from agent_trading_core.risk import RiskConfig

    data = tmp_path / "data"
    _cache(data, "^KS11", [(d, 100 + i, 100 + i) for i, d in enumerate(DAYS)])
    _cache(data, "005930.KS", [(d, 10_000, 10_000) for d in DAYS])
    krx = market_audit(data / "audit", "KRX")
    krx.record_decision(_rec("005930.KS", "2026-06-01", _memo()))
    krx.record_decision(_rec("000660.KS", "2026-06-01", _memo(PortfolioRating.HOLD)))
    krx.record_decision(DecisionRecord(ticker="005380.KS", trade_date="2026-06-01", rating="Buy", thesis="**Rating**: Buy"))
    prices, risk = Prices(data / "cache"), RiskConfig(regime_ma_days=3)
    execute_date(krx, "2026-06-01", prices, risk=risk)
    krx.record_decision(_rec("005930.KS", "2026-06-05", _review(SellAction.HOLD)))
    krx.record_decision(_rec("005930.KS", "2026-06-04", _review(SellAction.TRIM, (True, False), [SellReason.THESIS_BROKEN], 2)))
    execute_date(krx, "2026-06-04", prices, risk=risk)
    us = market_audit(data / "audit", "US")
    us.record_decision(_rec("AAPL", "2026-06-01", _memo(PortfolioRating.HOLD), "US"))
    us.record_decision(_rec("AAPL", "2026-06-04", _review(SellAction.EXIT, (True, True), [SellReason.THESIS_BROKEN]), "US"))
    us.record_decision(_rec("MSFT", "2026-06-04", _memo(PortfolioRating.HOLD), "US"))
    (data / "cache" / "_earnings_us.json").write_text(json.dumps({
        "AAPL": {"date": "2026-07-30", "date_to": None, "source": "yfinance calendar", "fetched": "2026-06-03"},
        "MSFT": {"date": "2026-07-20", "date_to": None, "source": "yfinance calendar", "fetched": "2026-05-01",
                 "error": "HTTPError: 404", "failed": "2026-06-03"}}), encoding="utf-8")
    (data / "cache" / "_kis_us_exchange.json").write_text(json.dumps({"AAPL": "NAS"}), encoding="utf-8")
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from backend import main
    importlib.reload(main)
    monkeypatch.setattr(main, "sector_of", {"005930.KS": "Technology", "AAPL": "Technology"}.get)
    return TestClient(main.app)


@pytest.mark.unit
def test_thesis_checks_run_oldest_first_with_the_action_as_the_verdict(world):
    rows = world.get("/api/tickers/005930.KS").json()["thesis_checks"]
    assert [(r["trade_date"], r["kind"], r["verdict"], r["verdict_ko"]) for r in rows] == [
        ("2026-06-01", "memo", "memo", "투자 메모"), ("2026-06-04", "review", "trim", "축소"),
        ("2026-06-05", "review", "keep", "논지 유지")]
    memo, trim, hold = rows
    assert memo["met"] is None and memo["total"] == 2 and [c["met"] for c in memo["checks"]] == [None, None]
    assert (trim["met"], trim["total"], trim["trim_to_pct"], trim["rating"]) == (1, 2, 2, "Underweight")
    assert trim["reasons_ko"] == ["논지 깨짐"]
    assert (hold["met"], hold["reasons"], hold["rating_ko"]) == (0, [], "보유")
    us = world.get("/api/tickers/AAPL", params={"market": "US"}).json()["thesis_checks"]
    assert [(r["verdict"], r["met"]) for r in us] == [("memo", None), ("exit", 2)]
    # a rule decision has no memo to check; a memo-only ticker has its memo and no verdict
    assert world.get("/api/tickers/005380.KS").json()["thesis_checks"] == []
    (only,) = world.get("/api/tickers/000660.KS").json()["thesis_checks"]
    assert only["verdict"] == "memo"


@pytest.mark.unit
def test_holding_is_the_book_row_with_its_partial_sales(world):
    body = world.get("/api/tickers/005930.KS").json()
    h = body["holding"]
    sells = [o for o in body["orders"] if o["side"] == "Sell"]
    (sell,) = sells
    bought = next(o for o in body["orders"] if o["side"] == "Buy")["filled_quantity"]
    assert 0 < sell["filled_quantity"] < bought
    assert h["quantity"] == bought - sell["filled_quantity"] and h["partial_sold"] == sell["filled_quantity"]
    assert h["partial_sales"] == [{"date": "2026-06-05", "quantity": sell["filled_quantity"], "price": sell["filled_price"],
                                   "order_id": sell["order_id"]}]
    assert h["entry_date"] == "2026-06-02" and h["as_of"] == "2026-06-05"
    for k in ("avg_price", "stop_price", "trailing_high", "holding_days", "unrealized", "unrealized_pct", "weight"):
        assert k in h
    assert world.get("/api/tickers/000660.KS").json()["holding"] is None


@pytest.mark.unit
def test_sector_exchange_and_earnings_come_from_files_and_markets_stay_apart(world, monkeypatch, tmp_path):
    earnings = tmp_path / "data" / "cache" / "_earnings_us.json"
    connect = socket.socket.connect

    def no_network(sock, addr):        # loopback stays open: Windows' event loop makes its self-pipe through it
        if addr[0] not in ("127.0.0.1", "::1"):
            raise AssertionError(f"a screen request opened a connection to {addr}")
        return connect(sock, addr)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    krx = world.get("/api/tickers/005930.KS").json()
    assert (krx["market"], krx["sector"], krx["exchange"], krx["earnings"]) == ("KRX", "Technology", "KRX 유가증권", None)
    assert krx["earnings_note_ko"].startswith("국내 실적 발표일 미제공")
    nosector = world.get("/api/tickers/000660.KS").json()
    assert nosector["sector"] is None
    aapl = world.get("/api/tickers/AAPL", params={"market": "US"}).json()
    assert (aapl["market"], aapl["sector"], aapl["exchange"]) == ("US", "Technology", "나스닥")
    assert aapl["earnings"] == {"date": "2026-07-30", "date_to": None, "source": "yfinance calendar", "fetched": "2026-06-03"}
    assert aapl["earnings_note_ko"] == "참고 · 결정 · 주문에 쓰지 않음" and aapl["holding"] is None
    past = json.loads(earnings.read_text(encoding="utf-8")) | {"AAPL": {"date": "2026-06-02", "date_to": None,
                                                                       "source": "yfinance calendar", "fetched": "2026-06-03"}}
    earnings.write_text(json.dumps(past), encoding="utf-8")
    body = world.get("/api/tickers/AAPL", params={"market": "US"}).json()
    assert body["earnings"]["date"] == "2026-06-02" and body["earnings_note_ko"].startswith("지난 실적일")
    # a range still running on the fetch day is not past (review 1R #1)
    earnings.write_text(json.dumps(past | {"AAPL": past["AAPL"] | {"date_to": "2026-06-09"}}), encoding="utf-8")
    body = world.get("/api/tickers/AAPL", params={"market": "US"}).json()
    assert body["earnings_note_ko"] == "참고 · 결정 · 주문에 쓰지 않음"
    msft = world.get("/api/tickers/MSFT", params={"market": "US"}).json()
    assert msft["earnings"]["date"] == "2026-07-20" and msft["exchange"] is None
    assert msft["earnings_note_ko"] == "마지막 갱신 실패(2026-06-03: HTTPError: 404) — 이전 값"
    # US records are not in the KRX book and the other way round
    assert world.get("/api/tickers/AAPL").status_code == 404
    assert world.get("/api/tickers/005930.KS", params={"market": "US"}).status_code == 404


@pytest.mark.unit
def test_earnings_missing_cache_and_unreadable_cache(world, tmp_path):
    path = tmp_path / "data" / "cache" / "_earnings_us.json"
    path.unlink()
    body = world.get("/api/tickers/AAPL", params={"market": "US"}).json()
    assert body["earnings"] is None and body["earnings_note_ko"].startswith("실적 일정 캐시에 없음")
    path.write_text("{not json", encoding="utf-8")
    body = world.get("/api/tickers/AAPL", params={"market": "US"}).json()
    assert body["earnings"] is None and body["earnings_note_ko"] == "실적 일정 캐시를 읽지 못했습니다"


@pytest.mark.unit
def test_refresh_earnings_writes_dates_none_and_failures_without_making_one_up():
    cal = {"AAPL": {"Earnings Date": [date(2026, 7, 30), date(2026, 7, 28)]}, "KO": {"Earnings Date": [date(2026, 7, 21)]},
           "NONE": {}, "EMPTY": {"Earnings Date": []}}

    def fetch(s):
        if s not in cal:
            raise RuntimeError("rate limited")
        return cal[s]
    old = {"BAD": {"date": "2026-04-01", "date_to": None, "source": "yfinance calendar", "fetched": "2026-03-01"}}
    book, counts = refresh_earnings.refresh(["AAPL", "KO", "NONE", "EMPTY", "BAD", "NEW"], old, fetch, today="2026-06-03")
    assert counts == {"calls": 6, "dated": 2, "none": 2, "failed": 2}
    assert book["AAPL"] == {"date": "2026-07-28", "date_to": "2026-07-30", "source": "yfinance calendar", "fetched": "2026-06-03"}
    assert book["KO"]["date"] == "2026-07-21" and book["KO"]["date_to"] is None
    assert book["NONE"]["date"] is None and book["EMPTY"]["date"] is None and "error" not in book["NONE"]
    assert book["BAD"] == {"date": "2026-04-01", "date_to": None, "source": "yfinance calendar", "fetched": "2026-03-01",
                           "error": "RuntimeError: rate limited", "failed": "2026-06-03"}
    assert book["NEW"]["date"] is None and book["NEW"]["error"] == "RuntimeError: rate limited"
