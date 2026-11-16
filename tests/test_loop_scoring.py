"""The daily loop's decisions scored on the performance screen (loop-scoring): settled five sessions on as
scripts/llm_trial_report.py settles them (the same figures, checked on the same book), a buy and a sell by hand, a
holiday inside the window, pending reasons, a person's decision and REVIEW kept out of the scores, the market split,
the source and date filters, the cache, the trial line — under both stores."""
from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent_trading_core import kis
from agent_trading_core.audit import (
    BASELINE_DECIDER,
    CandidateRecord,
    DecisionRecord,
    FilterResult,
    ScreenRun,
    market_audit,
)
from agent_trading_core.memory import TradingMemoryLog, settlement
from tests.test_llm_trial import MODEL, R1, R2, R3, report
from tests.test_llm_trial import _book as trial_book
from tests.test_llm_trial import _cache as trial_cache


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_STORE", request.param)
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("TRADING_LLM_TRIAL", raising=False)
    return tmp_path


def _client():
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), main


def _loop(api, **q):
    r = api.get("/api/performance/loop", params={"market": "KRX"} | q)
    assert r.status_code == 200, r.text
    return r.json()


# --- the same figures as the report script ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_screen_settles_as_the_trial_report_does(store, monkeypatch):
    """test_llm_trial's three-day book: the script's report and the API's agent column agree number for number."""
    def yahoo(*a):
        pytest.fail("KIS only, never Yahoo")
    monkeypatch.setattr(settlement, "get_closes", yahoo)
    trial_cache(store)
    audit = trial_book(store)
    script = report.trial_report(audit, "KRX", store / "cache")
    api, _ = _client()
    out = _loop(api)
    agent = out["columns"]["agent"]
    keys = ("decisions", "ratings", "review", "settled", "pending", "settle_failed", "buy_settled")
    assert {k: agent[k] for k in keys} == {k: script[k] for k in keys}
    assert (agent["mean_raw"], agent["mean_alpha"]) == (pytest.approx(script["mean_raw"]), pytest.approx(script["mean_alpha"]))
    assert agent["alpha_by_rating"].keys() == script["alpha_by_rating"].keys()
    for k, v in script["alpha_by_rating"].items():
        assert (agent["alpha_by_rating"][k]["n"], agent["alpha_by_rating"][k]["mean_alpha"]) == (v["n"], pytest.approx(v["mean_alpha"]))
    assert (out["agreement"]["compared"], out["agreement"]["agree"]) == (script["rule_compared"], script["rule_agree"])
    assert out["agreement"]["rate"] == pytest.approx(0.5)
    # the hand count of that test, once more: 3 settled · C.KS has no KIS file (pending, no_price) · Buy +10% / +8%
    assert (agent["settled"], agent["pending"], agent["mean_raw"]) == (3, 1, pytest.approx(0.10))
    assert {r["id"]: r["count"] for r in agent["pending_by_reason"]} == {"window": 0, "no_price": 1, "no_bench": 0, "price_ended": 0}
    # REVIEW (A R2) settles but scores nothing; C R3 Underweight is still pending
    assert (agent["review"], agent["directional"]["n"]) == (1, 1)
    # the rule column is the rule's call on the model's four (ticker, day): the same names, the same days
    rule = out["columns"]["rule"]
    assert (rule["decisions"], rule["first"], rule["last"]) == (4, agent["first"], agent["last"])
    assert out["rule_recorded"]["stand_ins"] == 1                                  # B R2: counted, in no column
    assert settlement.get_closes is yahoo                                          # put back after the settlement


@pytest.mark.unit
def test_the_trial_line_is_the_models(store, monkeypatch):
    trial_cache(store)
    trial_book(store)
    api, _ = _client()
    assert _loop(api)["trial"] is None
    monkeypatch.setenv("TRADING_LLM_TRIAL", "1")
    assert _loop(api)["trial"] == {"sessions": 5, "days": [R1, R2, R3], "remaining": 2}
    model = api.get("/api/model", params={"market": "KRX"}).json()
    assert (model["trial_mode"], model["trial"]) == (True, {"sessions": 5, "days": [R1, R2, R3], "remaining": 2})


# --- by hand -------------------------------------------------------------------------------------------------------

D = "2026-07-06"                                  # Monday; entry 07-07; 07-09 a holiday (no bar); exit 07-15
HOLIDAY = "2026-07-09"
YESTERDAY = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()


def _kis(cache: Path, symbol: str, before: float, after: float, since: str = "2026-07-15", until: str = "2026-08-31"):
    days = [d for d in pd.bdate_range("2026-06-22", until) if d != pd.Timestamp(HOLIDAY)]
    close = [after if d >= pd.Timestamp(since) else before for d in days]
    cache.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"Date": days, "Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0}) \
        .to_csv(kis.cache_path(symbol, cache), index=False)


def _screen(audit, day, tickers, market="KRX"):
    """The loop's screen for the day: the rule calls rank 1 Buy, 2 Overweight, 3 Underweight, 4 Sell, the rest Hold."""
    prefix = {"KRX": "daily_krx", "US": "daily_us"}[market]
    audit.record_screen(ScreenRun(run_id=f"{prefix}_{day}", as_of=day, universe="u", universe_size=len(tickers),
                                  rules=["momentum_top_k"], candidates=[
                                      CandidateRecord(ticker=t, rank=i + 1, selected=i < 2, score=1.0 - i / 10,
                                                      filters=[FilterResult(rule="momentum_top_k", passed=True)])
                                      for i, t in enumerate(tickers)]))


def _decide(audit, ticker, rating, day=D, market="KRX", **kw):
    prefix = {"KRX": "daily_krx", "US": "daily_us"}[market]
    settings = {"decider": kw.pop("decider", MODEL)} | kw.pop("settings", {})
    audit.record_decision(DecisionRecord(ticker=ticker, trade_date=day, rating=rating, thesis="t", market=market,
                                         screen_run_id=kw.pop("screen", f"{prefix}_{day}"), settings=settings, **kw))


@pytest.fixture
def hand(store):
    """KRX index 1000 → 1020 at the exit. X.KS 100 → 112, Y.KS 100 → 95, V.KS and U.KS flat, W.KS a Yahoo file only
    (+50% there), Z.KS's bars stop on 07-08. If the holiday counted as a session the exit would be 07-14, still before
    every jump (all 0%). The day's screen ranks X, W, V, Y: the rule's Buy, Overweight, Underweight, Sell; U, Z Hold."""
    cache = store / "cache"
    _kis(cache, "^KS11", 1000.0, 1020.0)
    _kis(cache, "X.KS", 100.0, 112.0)
    _kis(cache, "Y.KS", 100.0, 95.0)
    _kis(cache, "V.KS", 100.0, 100.0)
    _kis(cache, "U.KS", 100.0, 100.0)
    _kis(cache, "Z.KS", 100.0, 100.0, until="2026-07-08")
    _kis(cache, "W.KS", 100.0, 150.0)
    kis.cache_path("W.KS", cache).rename(cache / "W.KS-YFin-data.csv")
    a = market_audit(store / "audit", "KRX")
    _screen(a, D, ["X.KS", "W.KS", "V.KS", "Y.KS"])
    _screen(a, YESTERDAY, ["X.KS", "W.KS", "V.KS", "Y.KS"])
    for t, r in (("X.KS", "Buy"), ("Y.KS", "Sell"), ("W.KS", "Buy"), ("V.KS", "REVIEW"), ("U.KS", "Hold"), ("Z.KS", "Buy")):
        _decide(a, t, r)                                                          # the model's
    _decide(a, "X.KS", "Buy", day=YESTERDAY)                                      # its five sessions have not passed
    _decide(a, "X.KS", "Buy", day="2026-06-29", decider=BASELINE_DECIDER)         # the rule's own record: counted
    _decide(a, "T.KS", "Buy", decider=BASELINE_DECIDER, settings={"fallback": "rule"})   # a stand-in: counted
    _decide(a, "X.KS", "Buy", source="human", settings={"human_action": "hold"})  # a person's: out of every score
    _decide(a, "Y.KS", "Sell", source="rule", decider="rule_exit", screen=None)   # a rule exit: counted apart
    _decide(a, "X.KS", "Sell", decider="scratch_momentum")                        # another decider: neither column
    return a


@pytest.mark.unit
def test_a_buy_and_a_sell_by_hand_across_a_holiday(hand):
    """X Buy raw +12%, index +2% → alpha +10%; Y Sell raw −5%, alpha −7% → scored the other way, +5% / +7%, a hit.
    V and U flat: alpha −2% (V's Underweight in the rule column: +2%, a hit). W no KIS file, Z's prices ended,
    yesterday's X inside its window: pending, each for its reason."""
    api, _ = _client()
    out = _loop(api)
    agent, rule = out["columns"]["agent"], out["columns"]["rule"]
    why = {"window": 1, "no_price": 1, "no_bench": 0, "price_ended": 1}
    assert (agent["decisions"], agent["review"], agent["settled"], agent["pending"]) == (7, 1, 4, 3)
    assert {r["id"]: r["count"] for r in agent["pending_by_reason"]} == why
    assert (agent["buy_settled"], agent["mean_raw"], agent["mean_alpha"]) == (1, pytest.approx(0.12), pytest.approx(0.10))
    assert agent["directional"] == {"n": 2, "mean_return": pytest.approx(0.085), "mean_alpha": pytest.approx(0.085),
                                    "hit_rate": 1.0}                              # REVIEW (V) is in no score
    assert agent["hold"] == {"n": 1, "mean_alpha": pytest.approx(-0.02)}
    assert agent["alpha_by_rating"]["REVIEW"] == {"n": 1, "mean_alpha": pytest.approx(-0.02)}   # settled, as the script
    # the rule on the same seven (ticker, day): X Buy · W Overweight · V Underweight · Y Sell · U, Z Hold · X Buy
    assert rule["ratings"] == {"Buy": 2, "Overweight": 1, "Underweight": 1, "Sell": 1, "Hold": 2}
    assert (rule["decisions"], rule["first"], rule["last"]) == (7, D, YESTERDAY)
    assert {r["id"]: r["count"] for r in rule["pending_by_reason"]} == why
    assert (rule["buy_settled"], rule["mean_raw"], rule["mean_alpha"]) == (1, pytest.approx(0.12), pytest.approx(0.10))
    assert rule["directional"] == {"n": 3, "mean_return": pytest.approx(0.17 / 3), "mean_alpha": pytest.approx(0.19 / 3),
                                   "hit_rate": 1.0}
    assert out["agreement"] == {"compared": 7, "agree": 4, "rate": pytest.approx(4 / 7), "no_screen": 0}
    assert out["rule_recorded"] == {"decisions": 1, "first": "2026-06-29", "last": "2026-06-29", "stand_ins": 1}
    assert (out["human_excluded"], out["rule_exits_excluded"]) == (1, 1)


@pytest.mark.unit
def test_source_and_dates_filter_and_the_market_is_its_own(hand, store):
    api, main = _client()
    assert list(_loop(api, source="rule")["columns"]) == ["rule"]
    assert list(_loop(api, source="agent")["columns"]) == ["agent"]
    july = _loop(api, **{"from": "2026-07-01", "to": "2026-07-31"})
    assert (july["columns"]["rule"]["decisions"], july["columns"]["agent"]["pending"]) == (6, 2)   # yesterday's out
    assert july["rule_recorded"]["decisions"] == 0                                # 06-29 is out of range too
    assert _loop(api, **{"from": "2026-08-01", "to": "2026-08-31"})["columns"]["agent"]["decisions"] == 0
    for q in ({"source": "baseline"}, {"from": "2026-7-1"}, {"from": "2026-08-01", "to": "2026-07-01"}):
        assert api.get("/api/performance/loop", params={"market": "KRX"} | q).status_code == 422
    us = _loop(api, market="US")
    assert (us["columns"]["agent"]["decisions"], us["agreement"]["compared"], us["human_excluded"]) == (0, 0, 0)
    a = market_audit(store / "audit", "US")
    _kis(store / "cache", "AAPL", 200.0, 210.0)
    _screen(a, D, ["AAPL", "MSFT", "NVDA", "AMZN"], market="US")
    _decide(a, "AAPL", "Buy", market="US")
    main.PRICES._checked.clear()
    us = _loop(api, market="US")["columns"]["agent"]                              # no ^GSPC file yet
    assert (us["decisions"], us["pending"]) == (1, 1)
    assert {r["id"]: r["count"] for r in us["pending_by_reason"]}["no_bench"] == 1
    _kis(store / "cache", "^GSPC", 4000.0, 4000.0)
    main.PRICES._checked.clear()
    us = _loop(api, market="US")
    assert (us["columns"]["agent"]["mean_raw"], us["columns"]["agent"]["mean_alpha"]) == (pytest.approx(0.05), pytest.approx(0.05))
    assert us["agreement"] == {"compared": 1, "agree": 1, "rate": 1.0, "no_screen": 0}
    assert _loop(api)["columns"]["agent"]["decisions"] == 7                       # KRX unchanged


@pytest.mark.unit
def test_the_settlements_are_read_again_after_a_record_or_a_price_file(hand, store, monkeypatch):
    api, main = _client()
    calls, rules = [], []
    real = main._loop_settle
    monkeypatch.setattr(main, "_loop_settle", lambda pairs, bench: calls.append(list(pairs)) or real(pairs, bench))
    loop = main._daily_loop()
    real_rule = loop.rule_record
    monkeypatch.setattr(loop, "rule_record", lambda *a: rules.append(a) or real_rule(*a))
    first = _loop(api)
    assert (len(calls), len(rules)) == (1, 7)
    assert _loop(api) == first and (len(calls), len(rules)) == (1, 7)             # cached, both
    _decide(hand, "Y.KS", "Underweight", day="2026-07-07")                        # no screen for 07-07
    after = _loop(api)
    assert after["columns"]["agent"]["decisions"] == 8 and calls[-1] == [("Y.KS", "2026-07-07")]  # only the new pair
    assert (after["columns"]["rule"]["decisions"], after["agreement"]["no_screen"], len(rules)) == (7, 1, 7)
    _kis(store / "cache", "X.KS", 100.0, 120.0)                                   # a refresh rewrites a price file
    os.utime(kis.cache_path("X.KS", store / "cache"), ns=(1, 1))
    main.PRICES._checked.clear()
    assert _loop(api)["columns"]["agent"]["mean_raw"] == pytest.approx(0.20)
    assert sorted(calls[-1]) == [("X.KS", D), ("X.KS", YESTERDAY)]


def _baseline(store):
    """test_api's baseline summary and memory log, under this store's data dir."""
    log_path = store / "m.md"
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    log.store_decision("005930.KS", "2026-06-05", "**Rating**: Buy\n\nx", rating="Buy")
    log.update_with_outcome("005930.KS", "2026-06-05", 0.03, 0.012, 5, "규칙 전용 베이스라인.", "2026-06-12")
    (store / "baseline_summary.json").write_text(json.dumps({
        "run_id": "r1", "log_path": str(log_path), "cells_run": 1, "resolved": 1, "pending": 0, "unscored": 0,
        "by_rating": {"Buy": {"count": 1, "hit_rate": 1.0, "mean_alpha": 0.012}}, "holding": "5 trading days",
        "dates": ["2026-06-05"], "universe": ["005930.KS"]}), encoding="utf-8")


@pytest.mark.unit
def test_the_performance_answer_carries_the_loop_and_keeps_the_baseline_apart(hand, store, monkeypatch):
    """/api/performance adds ``loop`` (the same as /api/performance/loop); the baseline's scores come from its log
    alone — a loop decision changes the loop's figures, never the baseline's. A failing loop score stays in its key."""
    api, main = _client()
    assert api.get("/api/performance", params={"market": "KRX"}).status_code == 404   # no baseline summary: as before
    assert api.get("/api/performance", params={"market": "KRX", "source": "x"}).status_code == 422
    _baseline(store)
    before = api.get("/api/performance", params={"market": "KRX"}).json()
    assert before["loop"] == _loop(api)
    _decide(hand, "Y.KS", "Buy", day="2026-07-07")
    after = api.get("/api/performance", params={"market": "KRX"}).json()
    assert after["loop"]["columns"]["agent"]["decisions"] == before["loop"]["columns"]["agent"]["decisions"] + 1
    assert {k: after[k] for k in ("rating_bars", "by_rating", "resolved", "pending")} == \
           {k: before[k] for k in ("rating_bars", "by_rating", "resolved", "pending")}
    monkeypatch.setattr(main, "_loop_scoring", lambda *a: 1 / 0)
    broken = api.get("/api/performance", params={"market": "KRX"})
    assert broken.status_code == 200 and broken.json()["loop"] == {"error": "ZeroDivisionError: division by zero"}
    assert broken.json()["rating_bars"] == before["rating_bars"]
