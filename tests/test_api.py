"""The read API serves the audit log, memory log and summary as recorded, with Korean labels attached.

Runs against a small fixture data dir built here, so it does not depend on a baseline run.
"""

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import (
    AuditLog,
    CandidateRecord,
    DecisionRecord,
    EvidenceItem,
    FilterResult,
    ScreenRun,
    market_audit,
)
from agent_trading_core.memory import TradingMemoryLog


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    audit = AuditLog(data / "audit")
    audit.record_screen(ScreenRun(
        run_id="r1", as_of="2026-06-05", universe="테스트", universe_size=2, rules=["price_min", "momentum_top_k"],
        candidates=[
            CandidateRecord(ticker="005930.KS", selected=True, rank=1, score=1.2,
                            filters=[FilterResult(rule="price_min", value=300000, threshold=1000, passed=True),
                                     FilterResult(rule="momentum_top_k", value=1, threshold=5, passed=True, note="12-1개월 +40%")]),
            CandidateRecord(ticker="000660.KS", selected=False,
                            filters=[FilterResult(rule="price_min", value=500, threshold=1000, passed=False)]),
        ]))
    audit.record_decision(DecisionRecord(ticker="005930.KS", trade_date="2026-06-05", screen_run_id="r1", rating="Buy",
                                         action="Buy", confidence=0.8, thesis="**Rating**: Buy\n\n근거: 모멘텀 1위",
                                         evidence=[EvidenceItem(source="market", summary="모멘텀 순위 1/2", as_of="2026-06-05")]))
    audit.record_decision(DecisionRecord(ticker="000660.KS", trade_date="2026-06-05", rating="REVIEW", thesis="?"))
    log_path = data / "m.md"
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    log.store_decision("005930.KS", "2026-06-05", "**Rating**: Buy\n\nx", rating="Buy")
    log.update_with_outcome("005930.KS", "2026-06-05", 0.03, 0.012, 5, "규칙 전용 베이스라인.", "2026-06-12")
    (data / "baseline_summary.json").write_text(json.dumps({
        "run_id": "r1", "log_path": str(log_path), "cells_run": 2, "resolved": 1, "pending": 0, "unscored": 1,
        "by_rating": {"Buy": {"count": 1, "hit_rate": 1.0, "mean_alpha": 0.012}}, "holding": "5 trading days",
        "dates": ["2026-06-05"], "universe": ["005930.KS", "000660.KS"]}), encoding="utf-8")

    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from backend import main
    importlib.reload(main)
    return TestClient(main.app)


@pytest.mark.unit
def test_health_counts_the_records(client):
    body = client.get("/api/health").json()
    assert body["ok"] and body["screens"] == 1 and body["decisions"] == 2 and body["memory_entries"] == 1


@pytest.mark.unit
def test_screen_detail_carries_korean_labels_and_failed_rules(client):
    rows = client.get("/api/screens").json()
    assert rows[0]["stage_counts"] == [1, 1] and rows[0]["rules_ko"] == ["가격 하한", "모멘텀 상위"]
    detail = client.get("/api/screens/r1").json()
    by = {c["ticker"]: c for c in detail["candidates"]}
    assert by["005930.KS"]["name"] == "삼성전자"
    assert by["000660.KS"]["failed_rules_ko"] == ["가격 하한"]
    assert client.get("/api/screens/nope").status_code == 404


@pytest.mark.unit
def test_decisions_join_their_outcome_and_flag_review(client):
    rows = client.get("/api/decisions", params={"date": "2026-06-05"}).json()
    by = {d["ticker"]: d for d in rows}
    assert by["005930.KS"]["rating_ko"] == "매수" and by["005930.KS"]["tradeable"]
    assert by["005930.KS"]["outcome"]["alpha"] == pytest.approx(0.012)
    assert by["000660.KS"]["rating_ko"] == "검토 필요" and not by["000660.KS"]["tradeable"]
    assert [d["ticker"] for d in client.get("/api/decisions", params={"review_only": True}).json()] == ["000660.KS"]


@pytest.mark.unit
def test_ticker_detail_has_screening_decisions_and_outcomes(client):
    body = client.get("/api/tickers/005930.KS").json()
    assert body["name"] == "삼성전자"
    assert body["screening"][0]["selected"] and body["screening"][0]["filters"][1]["rule_ko"] == "모멘텀 상위"
    assert body["outcomes"][0]["reflection"].startswith("규칙 전용")
    assert client.get("/api/tickers/XXX").status_code == 404


@pytest.mark.unit
def test_performance_and_rules_and_stubs(client):
    perf = client.get("/api/performance").json()
    assert perf["by_rating"]["Buy"]["rating_ko"] == "매수" and perf["per_date"][0]["long_mean_alpha"] == pytest.approx(0.012)
    rules = client.get("/api/settings/rules").json()
    assert rules["market"]["price_limit"] == 0.30 and rules["screen_rules"][0]["label_ko"] == "가격 하한"
    # no orders yet: an empty paper book at the initial cash, not 501 and not invented numbers
    pos = client.get("/api/positions").json()
    assert pos["positions"] == [] and pos["equity"] == pos["cash"] == pos["initial_cash"] and pos["drawdown"] == 0.0
    auto = {r["id"]: r for r in rules["auto_rules"]}
    assert (auto["auto_daily_order_cap"]["value"], auto["auto_daily_order_cap"]["currency"]) == (100_000_000, "KRW")
    assert auto["auto_daily_loss_limit"]["currency"] is None and rules["auto_by_market"]["US"]["auto_order_max"] == 20_000
    assert rules["mode"]["mode"] == "paper" and rules["change_history"] == []


@pytest.mark.unit
def test_rules_list_hard_rules_and_paper_criteria(client):
    rules = client.get("/api/settings/rules").json()
    risk = {r["id"]: r for r in rules["risk_rules"]}
    assert risk["dd_stop"]["value"] == -0.20 and risk["dd_stop"]["layer_ko"] == "하드룰" and risk["stop_loss_pct"]["label_ko"].startswith("손절")
    assert {c["id"] for c in rules["paper_criteria"]} >= {"min_weeks", "max_drawdown", "max_stop_outs"}


@pytest.mark.unit
def test_paper_summary_is_served_with_korean_variant_labels(client, tmp_path, monkeypatch):
    assert client.get("/api/paper").status_code == 404
    from backend import main
    out = main.DATA_DIR / "results" / "paper"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps({
        "start": "2024-01-02", "end": "2026-11-12", "universe": ["005930.KS"], "benchmark": {"metrics": {}, "windows": {}},
        "variants": {"rules_on": {"metrics": {"max_drawdown": -0.193}, "exits_by_reason": {"stop_loss": 3},
                                  "events": [{"date": "2024-08-05", "kind": "guard", "text": "일일 손실 한도"}] * 50}},
        "caveats": []}), encoding="utf-8")
    body = client.get("/api/paper").json()
    v = body["variants"]["rules_on"]
    assert v["label_ko"] == "하드 룰 켬" and v["exits_by_reason_ko"] == {"손절": 3}
    assert v["event_counts"] == {"guard": 50} and len(v["events_tail"]) == 40 and "events" not in v
    assert body["criteria"][0]["id"] == "min_weeks"


@pytest.mark.unit
def test_approve_exists_only_in_manual_mode(client):
    # v0.3: auto/paper modes have no approval step; the endpoint says so instead of pretending.
    r = client.post("/api/decisions/005930.KS/2026-06-05/approve", json={"approver": "tester"})
    assert r.status_code == 409
    client.post("/api/trading/mode", json={"operator": "admin", "mode": "manual", "reason": "LLM 연결 전 수동 승인", "market": "KRX"})
    r = client.post("/api/decisions/005930.KS/2026-06-05/approve", json={"approver": "tester"})
    assert r.status_code == 501 and r.json()["implemented"] is False
    assert client.post("/api/decisions/XXX/2026-06-05/approve", json={"approver": "tester"}).status_code == 404


@pytest.mark.unit
def test_exception_queue_lists_review_decisions_for_the_latest_date(client):
    rows = client.get("/api/exceptions").json()
    assert [(r["kind"], r["ticker"]) for r in rows] == [("review", "000660.KS")]
    assert rows[0]["kind_ko"] == "등급 읽기 실패 (REVIEW)" and rows[0]["detail"] == "?"
    assert client.get("/api/exceptions", params={"date": "2026-01-01"}).json() == []
    assert client.get("/api/orders").json() == []  # no execution layer yet: empty, not fabricated


@pytest.mark.unit
def test_halt_resume_and_mode_change_are_replayed_from_the_operations_log(client):
    assert client.get("/api/trading/mode").json() == {
        "mode": "paper", "mode_ko": "모의투자", "running": True, "halted_at": None, "halted_by": None, "halted_by_ko": None, "halt_reason": None,
        "keep_exits": True, "mode_changed_at": None, "label_ko": "국내 모의투자 모드", "admin_token_required": False,
        "market": "KRX", "market_ko": "국내", "halt_target": None, "halt_target_ko": None, "dd_stop": False}

    r = client.post("/api/trading/halt", json={"operator": "운영자A", "reason": "벤더 지연", "keep_exits": False, "scope": "KRX"})
    assert r.status_code == 200
    (res,) = r.json()["results"]
    assert res["market"] == "KRX" and res["state"]["running"] is False and res["state"]["keep_exits"] is False
    assert res["effects"]["open_orders_cancelled"] == 0 and res["state"]["label_ko"].startswith("국내 정지됨 · ")
    assert client.get("/api/trading/mode", params={"market": "US"}).json()["running"] is True   # this market only
    assert client.post("/api/trading/halt", json={"operator": "x", "reason": "y", "scope": "KRX"}).status_code == 409
    assert client.post("/api/trading/halt", json={"operator": "x", "reason": ""}).status_code == 422

    r = client.post("/api/trading/resume", json={"operator": "관리자", "reason": "복구 확인", "scope": "KRX"}).json()
    assert r["resumed"] == ["KRX"] and r["states"]["KRX"]["running"] is True
    assert client.post("/api/trading/resume", json={"operator": "관리자", "reason": "again"}).status_code == 409

    r = client.post("/api/trading/mode", json={"operator": "관리자", "mode": "auto", "reason": "모의투자 4주 통과", "market": "KRX"})
    assert r.json()["state"]["mode"] == "auto" and r.json()["state"]["label_ko"] == "국내 자동 매매 실행 중"
    assert client.get("/api/trading/mode", params={"market": "US"}).json()["mode"] == "paper"  # modes are per market
    assert client.post("/api/trading/mode", json={"operator": "관리자", "mode": "auto", "reason": "dup", "market": "KRX"}).status_code == 409
    # no default market for a mode change: a missing one is refused, not read as KRX
    assert client.post("/api/trading/mode", json={"operator": "관리자", "mode": "manual", "reason": "r"}).status_code == 422
    assert client.post("/api/trading/mode", json={"operator": "관리자", "mode": "manual", "reason": "r", "market": "JP"}).status_code == 400

    history = client.get("/api/settings/rules").json()["change_history"]
    assert [h["kind"] for h in history] == ["mode_change", "resume", "halt"]  # newest first
    assert history[0]["before"] == {"mode": "paper"} and history[0]["after"] == {"mode": "auto"}
    assert history[2]["role"] == "operator" and history[1]["role"] == "admin(dev, no token)"
    assert {(h["market"], h["market_ko"]) for h in history} == {("KRX", "국내")}


@pytest.mark.unit
def test_admin_token_guards_resume_and_mode_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_ADMIN_TOKEN", "s3cret")
    from backend import main
    importlib.reload(main)
    c = TestClient(main.app)
    body = {"operator": "관리자", "mode": "auto", "reason": "r", "market": "KRX"}
    assert c.post("/api/trading/mode", json=body).status_code == 403
    assert c.post("/api/trading/mode", json=body, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.post("/api/trading/mode", json=body, headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert c.get("/api/trading/mode").json()["admin_token_required"] is True
    # halt stays operator-level: no token needed, so a human can always stop the loop.
    assert c.post("/api/trading/halt", json={"operator": "운영자", "reason": "급락"}).status_code == 200
    assert c.post("/api/trading/resume", json={"operator": "운영자", "reason": "r"}).status_code == 403


def _cache(data, ticker, rows):
    cache = data / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"] + [f"{d} 00:00:00+09:00,{o},{o},{c},{c},1000" for d, o, c in rows]
    (cache / f"{ticker}-YFin-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def executed(tmp_path, monkeypatch):
    """A paper book: one buy filled at the next open, one buy still open (its next session is not in the data)."""
    from agent_trading_core.execution import Prices, execute_date
    from agent_trading_core.risk import RiskConfig

    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05"]
    _cache(data, "^KS11", [(d, 100 + i, 100 + i) for i, d in enumerate(days)])
    _cache(data, "005930.KS", [(d, 10_000, 11_000) for d in days])
    audit = AuditLog(data / "audit")
    audit.record_decision(DecisionRecord(ticker="005930.KS", trade_date="2026-06-01", rating="Buy", thesis="b"))
    audit.record_decision(DecisionRecord(ticker="000660.KS", trade_date="2026-06-05", rating="REVIEW", thesis="?"))
    audit.record_decision(DecisionRecord(ticker="005380.KS", trade_date="2026-06-05", rating="Buy", thesis="b"))
    _cache(data, "005380.KS", [(d, 20_000, 20_000) for d in days])
    prices, risk = Prices(data / "cache"), RiskConfig(regime_ma_days=3)
    execute_date(audit, "2026-06-01", prices, risk=risk)
    execute_date(audit, "2026-06-05", prices, risk=risk)
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from backend import main
    importlib.reload(main)
    return TestClient(main.app)


@pytest.mark.unit
def test_orders_and_positions_serve_the_paper_book(executed):
    rows = {o["ticker"]: o for o in executed.get("/api/orders").json()}
    assert rows["005930.KS"]["status"] == "filled" and rows["005930.KS"]["fill_date"] == "2026-06-02"
    assert rows["005930.KS"]["status_ko"] == "체결" and rows["005930.KS"]["amount"] == pytest.approx(908 * 10_005)  # sized from the 11,000 close, filled at the 10,000 open
    assert rows["005380.KS"]["status"] == "open" and rows["000660.KS"]["exception_kind"] == "review"
    assert executed.get("/api/health").json()["orders"] == 3

    pos = executed.get("/api/positions").json()
    assert pos["as_of"] == "2026-06-05" and pos["mode"]["mode"] == "paper"
    (p,) = pos["positions"]
    assert (p["ticker"], p["name"], p["quantity"], p["last_price"]) == ("005930.KS", "삼성전자", 908, 11_000)
    assert {g["id"] for g in pos["protections"]} == {"StoplossGuard", "MaxDrawdown", "Cooldown", "LowProfit"}
    # the decision card carries its order as the execution strip
    d = executed.get("/api/decisions", params={"date": "2026-06-01"}).json()[0]
    assert [o["status"] for o in d["orders"]] == ["filled"]
    # the REVIEW decision is one exception item, not two (decision + its exception order)
    assert [(x["kind"], x["ticker"]) for x in executed.get("/api/exceptions").json()] == [("review", "000660.KS")]


@pytest.mark.unit
def test_bad_query_parameters_are_400(executed):
    assert executed.get("/api/orders", params={"date": "2026-13-01"}).status_code == 400
    assert executed.get("/api/orders", params={"date": "2026-6-1"}).status_code == 400   # dates compare as strings
    assert executed.get("/api/orders", params={"status": "done"}).status_code == 400
    assert executed.get("/api/positions", params={"as_of": "yesterday"}).status_code == 400
    assert executed.get("/api/exceptions", params={"date": "06/05/2026"}).status_code == 400
    assert executed.get("/api/orders", params={"status": "cancelled"}).json() == []


@pytest.mark.unit
def test_halt_cancels_the_open_orders_in_the_orders_log(executed):
    r = executed.post("/api/trading/halt", json={"operator": "운영자A", "reason": "점검"}).json()
    krx = next(x for x in r["results"] if x["market"] == "KRX")
    assert krx["effects"]["open_orders_cancelled"] == 1 and "취소 기록" in krx["effects"]["note"]
    rows = {o["ticker"]: o for o in executed.get("/api/orders").json()}
    assert rows["005380.KS"]["status"] == "cancelled" and rows["005380.KS"]["status_ko"] == "취소"
    assert rows["005930.KS"]["status"] == "filled"                             # fills are not touched
    history = executed.get("/api/settings/rules").json()["change_history"]
    assert history[0]["after"]["open_orders_cancelled"] == ["paper-2026-06-05-005380.KS"]


@pytest.mark.unit
def test_halt_with_keep_exits_leaves_open_sells_and_a_broken_orders_log_still_halts(executed, tmp_path):
    from agent_trading_core.audit import OrderRecord
    audit = market_audit(tmp_path / "data" / "audit", "KRX")
    audit.record_order(OrderRecord(order_id="s1", ticker="005930.KS", trade_date="2026-06-05", side="Sell",
                                   quantity=1, status="open"))
    r = executed.post("/api/trading/halt", json={"operator": "운영자A", "reason": "점검", "keep_exits": True,
                                                 "scope": "KRX"}).json()
    assert r["results"][0]["effects"]["open_orders_cancelled"] == 1          # the buy, not the exit
    status = {o["order_id"]: o["status"] for o in executed.get("/api/orders").json()}
    assert status["s1"] == "open" and status["paper-2026-06-05-005380.KS"] == "cancelled"

    executed.post("/api/trading/resume", json={"operator": "관리자", "reason": "r"})
    with open(audit.directory / "orders.jsonl", "a", encoding="utf-8") as f:
        f.write('{"order_id": "cut')                                            # a write cut short
    r = executed.post("/api/trading/halt", json={"operator": "운영자B", "reason": "파손 중 정지", "scope": "KRX"})
    (res,) = r.json()["results"]
    assert r.status_code == 200 and res["state"]["running"] is False
    assert res["effects"]["open_orders_cancelled"] is None and "건너뜀" in res["effects"]["note"]
    assert audit.operations()[-1].kind == "halt"


@pytest.mark.unit
def test_leaving_paper_mode_cancels_the_open_paper_orders(executed):
    r = executed.post("/api/trading/mode", json={"operator": "관리자", "mode": "manual", "reason": "수동 전환", "market": "KRX"})
    assert r.status_code == 200
    rows = {o["ticker"]: o["status"] for o in executed.get("/api/orders").json()}
    assert rows["005380.KS"] == "cancelled"
    history = executed.get("/api/settings/rules").json()["change_history"]
    assert history[0]["after"]["open_orders_cancelled"] == ["paper-2026-06-05-005380.KS"]


@pytest.mark.unit
def test_resume_cancels_open_orders_a_broken_log_kept_through_the_halt(executed, tmp_path):
    path = tmp_path / "data" / "audit" / "orders.jsonl"
    good = path.read_text(encoding="utf-8")
    path.write_text(good + '{"order_id": "cut', encoding="utf-8")
    r = executed.post("/api/trading/halt", json={"operator": "운영자", "reason": "파손", "scope": "KRX"}).json()
    assert r["results"][0]["effects"]["open_orders_cancelled"] is None
    path.write_text(good, encoding="utf-8")                                    # the admin repairs the log
    assert executed.post("/api/trading/resume", json={"operator": "관리자", "reason": "복구", "scope": "KRX"}).status_code == 200
    rows = {o["ticker"]: o["status"] for o in executed.get("/api/orders").json()}
    assert rows["005380.KS"] == "cancelled"


@pytest.mark.unit
def test_a_rule_exit_is_served_as_its_own_decision_with_its_own_order(tmp_path, monkeypatch):
    from agent_trading_core.execution import Prices, execute_date
    from agent_trading_core.risk import RiskConfig

    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05"]
    _cache(data, "^KS11", [(d, 100 + i, 100 + i) for i, d in enumerate(days)])
    # bought at the 06-02 open (10,000), -30% close on 06-04: stop-loss, and a -3% day that halts the loop
    _cache(data, "005930.KS", [("2026-06-01", 10_000, 10_000), ("2026-06-02", 10_000, 10_000),
                               ("2026-06-04", 7_000, 7_000), ("2026-06-05", 7_000, 7_000)])
    audit = AuditLog(data / "audit")
    audit.record_decision(DecisionRecord(ticker="005930.KS", trade_date="2026-06-01", rating="Buy", thesis="b"))
    audit.record_decision(DecisionRecord(ticker="005930.KS", trade_date="2026-06-04", rating="Sell", thesis="에이전트 매도"))
    prices, risk = Prices(data / "cache"), RiskConfig(regime_ma_days=3)
    execute_date(audit, "2026-06-01", prices, risk=risk)
    execute_date(audit, "2026-06-04", prices, risk=risk)
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from backend import main
    importlib.reload(main)
    c = TestClient(main.app)

    by = {d["source"]: d for d in c.get("/api/decisions", params={"date": "2026-06-04"}).json()}
    assert by["rule"]["source_ko"] == "규칙 청산" and by["rule"]["rating"] == "Sell" and by["rule"]["outcome"] is None
    assert [o["order_id"] for o in by["rule"]["orders"]] == ["paper-2026-06-04-005930.KS-exit"]
    assert by["rule"]["orders"][0]["rules_applied"] == ["stop_loss"]
    # the agent's sell of the same day is its own card; the loop halted that day (daily loss), so it has no order
    assert [(o["order_id"], o["status"]) for o in by["agent"]["orders"]] == []   # halted that day: no agent order
    sells = c.get("/api/orders", params={"date": "2026-06-04", "ticker": "005930.KS"}).json()
    assert [o["side"] for o in sells] == ["Sell"]

    st = c.get("/api/trading/mode").json()
    assert st["running"] is False and st["halted_by"] == "system" and st["halted_by_ko"] == "시스템 자동 정지"
    assert st["label_ko"].endswith("· 시스템 · 일일 손실 정지") and "일일 손실 한도 초과" in st["halt_reason"]
    assert st["halt_target"] == "daily_loss" and st["dd_stop"] is False
    assert c.get("/api/labels").json()["decision_source_ko"]["rule"] == "규칙 청산"
    # the agent's own calls can be read without the rule exits, and the health count says which is which
    assert [d["source"] for d in c.get("/api/decisions", params={"source": "agent", "rating": "Sell"}).json()] == ["agent"]
    assert c.get("/api/decisions", params={"source": "robot"}).status_code == 422
    assert c.get("/api/health").json()["decisions_by_source"] == {"agent": 2, "rule": 1}


# --- screen 1 · 5 fields (ui-foundation): the numbers the screens show, counted by the server -------------------

SLIP, COMM = 0.0005, 0.00015


@pytest.fixture
def same_session(tmp_path, monkeypatch):
    """005930 bought 06-01 (fills 06-02). On 06-04 one sell (005930), one buy (005380) and one REVIEW: the sell and the
    buy both fill at the 06-05 open. 005380 closes 06-05 at 18,500, 0.5% above its stop line."""
    from agent_trading_core.execution import Prices, execute_date
    from agent_trading_core.risk import RiskConfig

    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05"]
    _cache(data, "^KS11", [(d, 100 + i, 100 + i) for i, d in enumerate(days)])
    _cache(data, "005930.KS", [(d, 10_000, 11_000) for d in days])
    _cache(data, "005380.KS", [(d, 20_000, 20_000) for d in days[:-1]] + [("2026-06-05", 20_000, 18_500)])
    audit = AuditLog(data / "audit")
    audit.record_decision(DecisionRecord(ticker="005930.KS", trade_date="2026-06-01", rating="Buy", thesis="b"))
    for t, r in (("005930.KS", "Sell"), ("005380.KS", "Buy"), ("000660.KS", "REVIEW")):
        audit.record_decision(DecisionRecord(ticker=t, trade_date="2026-06-04", rating=r, thesis=r))
    prices, risk = Prices(data / "cache"), RiskConfig(regime_ma_days=3)
    for d in ("2026-06-01", "2026-06-04", "2026-06-05"):
        execute_date(audit, d, prices, risk=risk)
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), prices


def _hand():
    q1 = 908                                                       # 10M // (11,000 × 1.0005 × 1.00015)
    cash = 100_000_000 - q1 * 10_000 * (1 + SLIP) * (1 + COMM)
    equity_0604 = cash + q1 * 11_000
    q2 = int(0.10 * equity_0604 // (20_000 * (1 + SLIP) * (1 + COMM)))
    return q1, q2


@pytest.mark.unit
def test_the_session_amounts_split_buys_and_sells_and_only_buys_count_against_the_cap(same_session):
    client, _ = same_session
    from agent_trading_core.config import auto_execution

    q1, q2 = _hand()
    assert q2 == 504                                               # 0.1 × 100,902,097 // 20,013.0
    pos = client.get("/api/positions", params={"as_of": "2026-06-04"}).json()
    # both orders decided 06-04 fill at 06-05's open: buy at 20,000 × 1.0005, sell at 10,000 × 0.9995
    assert pos["today_session"] == "2026-06-05"
    assert pos["today_buy_amount"] == pytest.approx(q2 * 20_010)
    assert pos["today_sell_amount"] == pytest.approx(q1 * 9_995)
    cap = auto_execution("KRX")["auto_daily_order_cap"]
    assert pos["today_buy_cap_used"] == pytest.approx(q2 * 20_010 / cap)
    # the 06-01 buy filled at another session (06-02): not today's
    early = client.get("/api/positions", params={"as_of": "2026-06-01"}).json()
    assert (early["today_session"], early["today_buy_amount"], early["today_sell_amount"]) == ("2026-06-02", pytest.approx(q1 * 10_005), 0)


@pytest.mark.unit
def test_near_stop_max_weight_and_active_guards_are_counted_by_the_server(same_session):
    client, _ = same_session
    _, q2 = _hand()
    pos = client.get("/api/positions").json()                       # as of 06-05: only 005380 is held
    (p,) = pos["positions"]
    stop = 20_010 * 0.92                                           # the fill average, not the decision close (docs/10 D3)
    assert p["stop_price"] == pytest.approx(stop) and p["stop_gap"] == pytest.approx(18_500 / stop - 1)
    assert p["near_stop"] is True and pos["near_stop_count"] == 1 and pos["limits"]["near_stop_pct"] == 0.02
    assert pos["max_position_ticker"] == "005380.KS"
    assert pos["max_position_weight"] == pytest.approx(q2 * 18_500 / pos["equity"])
    # 005930 was sold at the 06-05 open: its Cooldown is the one active guard; nothing was locked on 06-04
    assert pos["protections_active"] == 1 and [g["id"] for g in pos["protections"] if g["active"]] == ["Cooldown"]
    assert client.get("/api/positions", params={"as_of": "2026-06-04"}).json()["protections_active"] == 0
    # 4 bars < the 200-day window: no distance to show (the filter reads the regime as on)
    assert pos["regime_gap"] is None and pos["limits"]["regime_ma_days"] == 200


@pytest.mark.unit
def test_the_regime_gap_is_the_last_index_close_against_its_average(same_session):
    from agent_trading_core.execution import positions_view
    from agent_trading_core.risk import RiskConfig

    _, prices = same_session
    view = positions_view([], prices, as_of="2026-06-05", risk=RiskConfig(regime_ma_days=3))
    assert view["regime_gap"] == pytest.approx(103 / 102 - 1)       # closes 101, 102, 103
    assert view["regime_on"] is True
    assert positions_view([], prices, as_of="2026-06-02", risk=RiskConfig(regime_ma_days=3))["regime_gap"] is None


@pytest.mark.unit
def test_the_home_flow_cards_are_counted_per_market(same_session, tmp_path):
    client, _ = same_session
    # a rule exit on the same day is not an agent judgment: counted apart, not as a Sell
    AuditLog(tmp_path / "data" / "audit").record_decision(
        DecisionRecord(ticker="000270.KS", trade_date="2026-06-04", rating="Sell", thesis="손절", source="rule"))
    krx, us = client.get("/api/markets").json()
    flow = krx["flow"]
    assert flow["screen"] is None                                   # no screening run in this book
    assert flow["decisions"] == {"date": "2026-06-04", "total": 3, "by_rating": {"Buy": 1, "Sell": 1, "REVIEW": 1},
                                 "by_rating_ko": {"매수": 1, "매도": 1, "검토 필요": 1}, "rule": 1}
    assert flow["exceptions"] == {"date": "2026-06-04", "total": 1, "by_kind": {"review": 1},
                                  "by_kind_ko": {"등급 읽기 실패 (REVIEW)": 1}}
    assert flow["holdings"] == {"count": 1, "near_stop": 1, "protections_active": 1}   # 005930's Cooldown
    # the US book is empty and separate
    assert us["flow"]["decisions"]["total"] == 0 and us["flow"]["holdings"]["count"] == 0


@pytest.mark.unit
def test_a_screen_summary_counts_its_rules(client):
    (s,) = client.get("/api/screens").json()[:1]
    assert s["rule_count"] == len(s["rules"]) > 0


# --- /api/performance per market (the US baseline summary under data/audit/US/) -------------------------------

@pytest.mark.unit
def test_us_performance_is_404_without_its_summary_and_reads_its_own_summary_and_log(client, tmp_path):
    data = tmp_path / "data"
    krx_before = client.get("/api/performance").content
    r = client.get("/api/performance", params={"market": "US"})
    assert r.status_code == 404 and "baseline_krx.py --market US" in r.json()["detail"]

    log_path = data / "us_memory.md"
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    log.store_decision("AAPL", "2026-06-05", "**Rating**: Sell\n\nx", rating="Sell")
    log.update_with_outcome("AAPL", "2026-06-05", -0.04, -0.021, 5, "미국 규칙 베이스라인.", "2026-06-12")
    (data / "audit" / "US").mkdir(parents=True, exist_ok=True)
    (data / "audit" / "US" / "baseline_summary.json").write_text(json.dumps({
        "run_id": "us1", "log_path": str(log_path), "cells_run": 1, "resolved": 1, "pending": 0, "unscored": 0,
        "by_rating": {"Sell": {"count": 1, "hit_rate": 1.0, "mean_alpha": -0.021}}, "holding": "5 trading days",
        "dates": ["2026-06-05"], "universe": ["AAPL"], "market": "US", "currency": "USD", "benchmark": "^GSPC"}),
        encoding="utf-8")

    us = client.get("/api/performance", params={"market": "US"})
    assert us.status_code == 200
    body = us.json()
    assert set(body) == {"run_id", "cells_run", "resolved", "pending", "unscored", "holding", "dates", "universe",
                         "by_rating", "per_date", "reflections", "caveat"}
    assert body["run_id"] == "us1" and body["by_rating"]["Sell"]["rating_ko"] == "매도"
    # a Sell's alpha counts on the short side; the reflection is the US log's, not the KRX one
    assert body["per_date"] == [{"date": "2026-06-05", "resolved": 1, "long_mean_alpha": None, "short_mean_alpha": pytest.approx(-0.021)}]
    assert [(x["ticker"], x["reflection"]) for x in body["reflections"]] == [("AAPL", "미국 규칙 베이스라인.")]
    # the KRX answer is byte for byte what it was before the US summary existed
    assert client.get("/api/performance").content == krx_before


@pytest.mark.unit
def test_the_memory_log_override_stays_krx_only(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MEMORY_LOG", str(tmp_path / "nowhere.md"))
    from backend import main
    assert main._Store("US").memory_log_path() is None                 # no US summary: no log, override ignored
    assert main._Store("KRX").memory_log_path() == tmp_path / "nowhere.md"
