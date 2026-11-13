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
    assert client.get("/api/positions").status_code == 501
    assert [r["id"] for r in rules["auto_rules"]][:2] == ["auto_daily_order_cap", "auto_daily_loss_limit"]
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
    client.post("/api/trading/mode", json={"operator": "admin", "mode": "manual", "reason": "LLM 연결 전 수동 승인"})
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
        "mode": "paper", "mode_ko": "모의투자", "running": True, "halted_at": None, "halted_by": None, "halt_reason": None,
        "keep_exits": True, "mode_changed_at": None, "label_ko": "모의투자 모드", "admin_token_required": False}

    r = client.post("/api/trading/halt", json={"operator": "운영자A", "reason": "벤더 지연", "keep_exits": False})
    assert r.status_code == 200
    assert r.json()["state"]["running"] is False and r.json()["state"]["keep_exits"] is False
    assert r.json()["effects"]["open_orders_cancelled"] == 0 and r.json()["state"]["label_ko"].startswith("정지됨 · ")
    assert client.post("/api/trading/halt", json={"operator": "x", "reason": "y"}).status_code == 409
    assert client.post("/api/trading/halt", json={"operator": "x", "reason": ""}).status_code == 422

    assert client.post("/api/trading/resume", json={"operator": "관리자", "reason": "복구 확인"}).json()["state"]["running"] is True
    assert client.post("/api/trading/resume", json={"operator": "관리자", "reason": "again"}).status_code == 409

    r = client.post("/api/trading/mode", json={"operator": "관리자", "mode": "auto", "reason": "모의투자 4주 통과"})
    assert r.json()["state"]["mode"] == "auto" and r.json()["state"]["label_ko"] == "자동 매매 실행 중"
    assert client.post("/api/trading/mode", json={"operator": "관리자", "mode": "auto", "reason": "dup"}).status_code == 409

    history = client.get("/api/settings/rules").json()["change_history"]
    assert [h["kind"] for h in history] == ["mode_change", "resume", "halt"]  # newest first
    assert history[0]["before"] == {"mode": "paper"} and history[0]["after"] == {"mode": "auto"}
    assert history[2]["role"] == "operator" and history[1]["role"] == "admin(dev, no token)"


@pytest.mark.unit
def test_admin_token_guards_resume_and_mode_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_ADMIN_TOKEN", "s3cret")
    from backend import main
    importlib.reload(main)
    c = TestClient(main.app)
    body = {"operator": "관리자", "mode": "auto", "reason": "r"}
    assert c.post("/api/trading/mode", json=body).status_code == 403
    assert c.post("/api/trading/mode", json=body, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.post("/api/trading/mode", json=body, headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert c.get("/api/trading/mode").json()["admin_token_required"] is True
    # halt stays operator-level: no token needed, so a human can always stop the loop.
    assert c.post("/api/trading/halt", json={"operator": "운영자", "reason": "급락"}).status_code == 200
    assert c.post("/api/trading/resume", json={"operator": "운영자", "reason": "r"}).status_code == 403
