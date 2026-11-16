"""Decision cards (docs/10 §3 단계 4): the per-order cap shrinks a buy (D1), every sized buy keeps its 원안 vs 최종
(``OrderRecord.plan``), the exception queue's write actions (D7) with their records, caps and replays, the next-session
retry the executor places again, the decision source badge, a person's Hold kept out of the scoring, and a
daily-loss halt the resume refuses."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent_trading_core.audit import AuditLog, DecisionRecord, OrderRecord, market_audit
from agent_trading_core.execution import PaperConfig, execute_date, unexplained_orders
from agent_trading_core.memory import TradingMemoryLog
from tests.test_execution import RISK, _decide, _flat, _frame, _prices
from tests.test_risk_panels import CAL, _cache, _client, _halt

KST = ZoneInfo("Asia/Seoul")
UNIT = 10_000 * 1.0005 * 1.00015          # one share at 10,000 with slippage and commission = 10,006.50075


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit")


# --- D1: the per-order cap shrinks the buy --------------------------------------------------------------------

@pytest.mark.unit
def test_a_buy_over_the_per_order_cap_is_shrunk_to_the_most_whole_shares_under_it(audit):
    _decide(audit, "A.KS", "2026-06-02")
    (o,) = execute_date(audit, "2026-06-02", _prices(A_KS=_flat()), risk=RISK, cfg=PaperConfig(initial_cash=1e9))
    # 1e9 × 10% = 1e8 → 1e8 // 10,006.50075 = 9,993 shares = 99,930,000 > 30,000,000 → 30M // 10,000 = 3,000 shares
    assert (o.status, o.quantity, o.filled_quantity) == ("filled", 3000, 3000)
    assert o.rules_applied == ["max_position_pct", "auto_order_max"]
    assert "auto_order_max: 9,993만 → 3,000만 (9993주 → 3000주)" in o.reason
    p = o.plan
    # 원안: the agent's equal weight before any rule, 100% of one name → 1e9 // 10,006.50075 = 99,935 shares
    assert (p.proposed.side, p.proposed.weight_pct, p.proposed.qty, p.proposed.amount) == ("Buy", 1.0, 99935, 999_350_000)
    assert (p.final.side, p.final.qty, p.final.amount, p.final.weight_pct) == ("Buy", 3000, 30_000_000, 0.03)
    assert [(i.rule, i.before, i.after) for i in p.interventions] == [
        ("max_position_pct", "100.0%", "10.0%"), ("auto_order_max", "9,993만", "3,000만")]


@pytest.mark.unit
def test_a_share_dearer_than_the_cap_is_blocked_cap_zero_with_its_plan(audit):
    _decide(audit, "A.KS", "2026-06-02")
    # 40,000,000 a share: 1e8 // 40,026,003 = 2 shares = 80M > 30M → 30M // 40M = 0 shares
    (o,) = execute_date(audit, "2026-06-02", _prices(A_KS=_flat(40_000_000.0)), risk=RISK, cfg=PaperConfig(initial_cash=1e9))
    assert (o.status, o.quantity) == ("blocked", 0)
    assert o.rules_applied == ["max_position_pct", "auto_order_max", "cap_zero"]
    assert o.plan.final.side == "Hold" and o.plan.final.qty == 0 and o.plan.interventions[-1].rule == "auto_order_max"


@pytest.mark.unit
def test_the_daily_cap_stays_an_exception_and_its_plan_names_it(audit):
    tickers = [f"T{i}.KS" for i in range(5)]
    for t in tickers:
        _decide(audit, t, "2026-06-02")
    # 2.5e8 × 10% = 25M → 2,498 shares = 24,980,000 each (under 30M, not shrunk); four make 99,920,000, the fifth crosses 1e8
    out = execute_date(audit, "2026-06-02", _prices(**{t.replace(".", "_"): _flat() for t in tickers}), risk=RISK,
                       cfg=PaperConfig(initial_cash=2.5e8))
    assert [o.status for o in out] == ["filled"] * 4 + ["exception"]
    last = out[-1]
    assert (last.exception_kind, last.quantity, last.rules_applied[-1]) == ("cap_exceeded", 2498, "auto_daily_order_cap")
    assert "auto_order_max" not in last.rules_applied
    assert last.plan.final.side == "Hold" and last.plan.interventions[-1].rule == "auto_daily_order_cap"


@pytest.mark.unit
def test_a_sell_is_never_capped(audit):
    audit.record_order(OrderRecord(order_id="paper-2026-06-01-A.KS", ticker="A.KS", trade_date="2026-06-01", side="Buy",
                                   quantity=9000, filled_quantity=9000, filled_price=10_000.0, status="filled",
                                   fill_date="2026-06-02"))
    _decide(audit, "A.KS", "2026-06-04", rating="Sell")
    (o,) = execute_date(audit, "2026-06-04", _prices(A_KS=_flat()), risk=RISK)
    # 9,000 × 10,000 = 90M > the 30M per-order cap: a sell reduces risk, the whole holding goes
    assert (o.side, o.status, o.quantity, o.rules_applied, o.plan) == ("Sell", "filled", 9000, [], None)


@pytest.mark.unit
def test_an_old_order_line_reads_without_the_new_fields():
    o = OrderRecord.model_validate({"order_id": "x", "ticker": "A.KS", "trade_date": "2026-06-01", "side": "Buy",
                                    "quantity": 1, "status": "exception", "exception_kind": "cap_exceeded"})
    assert (o.plan, o.exception_action, o.exception_resolved, o.reruns, o.retries, o.retry_on) == (None, None, False, 0, 0, None)
    assert o.placed_on == "2026-06-01"
    assert DecisionRecord.model_validate({"ticker": "A", "trade_date": "2026-06-01", "rating": "Hold", "thesis": ""}).source == "agent"


# --- the next-session retry the executor places again ----------------------------------------------------------

@pytest.mark.unit
def test_a_retried_order_goes_again_at_the_next_run_under_its_own_id_and_fills_a_session_later(audit):
    _decide(audit, "B.KS", "2026-06-02")
    no_open_0604 = _frame({d: (10_000.0, 10_000.0) for d in ["2026-06-01", "2026-06-02", "2026-06-05", "2026-06-08"]})
    prices = _prices(B_KS=no_open_0604)
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert (o.status, o.exception_kind) == ("exception", "vendor_unavailable")      # placed, then no 06-04 open
    q = o.quantity
    audit.record_order(o.model_copy(update={"exception_action": "retry_next", "retries": 1}))     # as the API writes it
    assert execute_date(audit, "2026-06-02", prices, risk=RISK) == []    # the session it failed at (06-04) has not run
    (again,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    # 06-04's run places it again (retry_on) and it fills at the next session, 06-05, at the first quantity
    assert (again.order_id, again.trade_date, again.retry_on, again.fill_date) == (o.order_id, "2026-06-02", "2026-06-04", "2026-06-05")
    assert (again.status, again.quantity, again.retries, again.exception_action) == ("filled", q, 1, None)
    assert "다음 세션 재시도 1/2" in again.reason
    assert execute_date(audit, "2026-06-04", prices, risk=RISK) == []    # done once
    assert unexplained_orders(audit.orders(), audit.decisions()) == []


# --- the exception queue's write actions ------------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path, monkeypatch):
    """A KRX book with a REVIEW exception (RRR), a vendor exception (VVV) and a filled order (FFF), all decided 06-10."""
    data = tmp_path / "data"
    _cache(data, "^KS11", {d: 100.0 for d in CAL if d <= "2026-06-12"})
    a = market_audit(data / "audit", "KRX")
    for t, rating in (("RRR.KS", "REVIEW"), ("VVV.KS", "Buy"), ("FFF.KS", "Buy")):
        a.record_decision(DecisionRecord(ticker=t, trade_date="2026-06-10", rating=rating, thesis=f"{rating} {t}"))
    a.record_order(OrderRecord(order_id="paper-2026-06-10-RRR.KS", ticker="RRR.KS", trade_date="2026-06-10", side="Buy",
                               quantity=0, status="exception", exception_kind="review", reason="등급을 읽을 수 없어 주문하지 않았습니다"))
    a.record_order(OrderRecord(order_id="paper-2026-06-10-VVV.KS", ticker="VVV.KS", trade_date="2026-06-10", side="Buy",
                               quantity=10, status="exception", exception_kind="vendor_unavailable", reason="시가 없음"))
    a.record_order(OrderRecord(order_id="paper-2026-06-10-FFF.KS", ticker="FFF.KS", trade_date="2026-06-10", side="Buy",
                               quantity=5, filled_quantity=5, filled_price=10_000.0, status="filled", fill_date="2026-06-11"))
    c, _ = _client(data, monkeypatch, token="t")
    return c, a, data


H = {"X-Admin-Token": "t"}


def _act(c, order_id, action, reason="사유", headers=H):
    return c.post(f"/api/exceptions/{order_id}/action", json={"action": action, "operator": "운영자A", "reason": reason},
                  headers=headers)


@pytest.mark.unit
def test_the_queue_lists_each_items_actions_and_an_action_needs_the_token_and_a_reason(queue):
    c, a, _ = queue
    items = {x["ticker"]: x for x in c.get("/api/exceptions", params={"date": "2026-06-10"}).json()}
    assert set(items) == {"RRR.KS", "VVV.KS"}
    assert [x["action"] for x in items["RRR.KS"]["actions"]] == ["rerun", "hold", "dismiss"]
    assert [x["action"] for x in items["VVV.KS"]["actions"]] == ["retry_next", "dismiss_order"]
    assert items["RRR.KS"]["order_id"] == "paper-2026-06-10-RRR.KS" and items["RRR.KS"]["pending_action"] is None
    assert _act(c, "paper-2026-06-10-RRR.KS", "hold", headers={}).status_code == 403
    assert _act(c, "paper-2026-06-10-RRR.KS", "hold", reason="").status_code == 422
    assert a.operations() == [] and len(a.orders()) == 3                  # refused: nothing written


@pytest.mark.unit
def test_hold_writes_a_human_hold_decision_first_then_the_operation_then_closes_the_order(queue):
    c, a, _ = queue
    r = _act(c, "paper-2026-06-10-RRR.KS", "hold", reason="원문이 유지 쪽").json()
    assert (r["action"], r["replayed"], r["order"]["exception_resolved"]) == ("hold", False, True)
    human = [d for d in a.decisions() if d.source == "human"]
    assert [(d.ticker, d.trade_date, d.rating, d.action, d.thesis) for d in human] == [
        ("RRR.KS", "2026-06-10", "Hold", None, "사람 처리: 보유")]
    (op,) = a.operations()
    assert (op.kind, op.target, op.operator, op.reason) == ("exception_action", "paper-2026-06-10-RRR.KS", "운영자A", "원문이 유지 쪽")
    assert op.after == {"action": "hold", "scored": False, "decision": {"rating": "Hold", "source": "human"}}
    # written in that order: the decision's line is older than the operation's, and the order line comes last
    assert human[0].recorded_at <= op.at
    last = a.orders()[-1]                                                  # the log's last order row
    assert (last.order_id, last.exception_action, last.exception_resolved) == ("paper-2026-06-10-RRR.KS", "hold", True)
    assert [x["ticker"] for x in c.get("/api/exceptions", params={"date": "2026-06-10"}).json()] == ["VVV.KS"]
    # the same request again: nothing more written
    again = _act(c, "paper-2026-06-10-RRR.KS", "hold", reason="원문이 유지 쪽").json()
    assert again["replayed"] is True and len(a.operations()) == 1 and len([d for d in a.decisions() if d.source == "human"]) == 1
    assert _act(c, "paper-2026-06-10-RRR.KS", "dismiss", reason="다른 사유").status_code == 409          # closed already
    # the card of the person's call has no order; the agent's REVIEW card keeps the closed exception
    cards = {d["source"]: d for d in c.get("/api/decisions", params={"ticker": "RRR.KS"}).json()}
    assert cards["human"]["orders"] == [] and cards["human"]["source_kind_ko"] == "사람 처리"
    assert cards["agent"]["orders"][0]["exception_action"] == "hold"
    assert unexplained_orders(a.orders(), a.decisions()) == []


@pytest.mark.unit
def test_a_rerun_is_reserved_up_to_twice_and_a_repeat_is_a_replay(queue):
    c, a, _ = queue
    oid = "paper-2026-06-10-RRR.KS"
    r1 = _act(c, oid, "rerun", reason="형식 오류").json()
    assert (r1["attempt"], r1["limit"], r1["replayed"]) == (1, 2, False) and "예약" in r1["result_ko"]
    assert _act(c, oid, "rerun", reason="형식 오류").json()["replayed"] is True       # double click
    assert len(a.operations()) == 1
    item = next(x for x in c.get("/api/exceptions", params={"date": "2026-06-10"}).json() if x["ticker"] == "RRR.KS")
    assert (item["pending_action"], item["reruns"]) == ("rerun", 1)                    # still on the queue
    assert _act(c, oid, "rerun", reason="다시 한 번").json()["attempt"] == 2
    third = _act(c, oid, "rerun", reason="세 번째")
    assert third.status_code == 409 and third.json()["detail"].startswith("재실행은 예외마다 2회까지")
    assert [op.after["attempt"] for op in a.operations()] == [1, 2]
    assert not [d for d in a.decisions() if d.source == "human"]                        # a rerun decides nothing


@pytest.mark.unit
def test_an_order_exception_takes_retry_or_dismiss_only_and_dismiss_closes_it(queue):
    c, a, _ = queue
    oid = "paper-2026-06-10-VVV.KS"
    assert _act(c, oid, "rerun").status_code == 422 and _act(c, oid, "hold").status_code == 422
    r = _act(c, oid, "retry_next", reason="일시 장애").json()
    assert (r["attempt"], r["order"]["exception_action"], r["order"]["retries"]) == (1, "retry_next", 1)
    d = _act(c, oid, "dismiss_order", reason="결정 무효").json()
    assert d["order"]["exception_resolved"] is True and d["order"]["exception_action"] == "dismiss_order"
    assert [op.after["action"] for op in a.operations()] == ["retry_next", "dismiss_order"]
    assert _act(c, oid, "retry_next", reason="또").status_code == 409
    assert c.get("/api/exceptions", params={"date": "2026-06-10"}).json()[0]["ticker"] == "RRR.KS"


@pytest.mark.unit
def test_dismiss_closes_a_review_without_a_decision(queue):
    c, a, _ = queue
    r = _act(c, "paper-2026-06-10-RRR.KS", "dismiss", reason="만기 청산 예정").json()
    assert r["order"]["exception_resolved"] is True
    assert not [d for d in a.decisions() if d.source == "human"]
    assert a.operations()[0].after == {"action": "dismiss", "scored": False}


@pytest.mark.unit
def test_an_order_not_in_the_queue_is_409_and_an_unknown_one_404(queue):
    c, _, _ = queue
    assert _act(c, "paper-2026-06-10-FFF.KS", "dismiss_order").status_code == 409
    assert _act(c, "paper-2026-06-10-XXX.KS", "dismiss_order").status_code == 404
    assert _act(c, "paper-2026-06-10-RRR.KS", "hold", headers=H).status_code == 200
    assert c.post("/api/exceptions/paper-2026-06-10-VVV.KS/action", params={"market": "JP"}, headers=H,
                  json={"action": "dismiss_order", "operator": "a", "reason": "b"}).status_code == 400


# --- source badge, intervention tab, scoring ---------------------------------------------------------------------

@pytest.mark.unit
def test_the_source_badge_and_the_intervention_tab(queue):
    c, a, _ = queue
    a.record_decision(DecisionRecord(ticker="BBB.KS", trade_date="2026-06-10", rating="Buy", thesis="모멘텀",
                                     settings={"decider": "rule_momentum_baseline"}))
    a.record_order(OrderRecord(
        order_id="paper-2026-06-10-BBB.KS", ticker="BBB.KS", trade_date="2026-06-10", side="Buy", quantity=3000,
        status="open", rules_applied=["auto_order_max"],
        plan={"proposed": {"side": "Buy", "weight_pct": 0.1, "qty": 9993, "amount": 99_930_000},
              "final": {"side": "Buy", "weight_pct": 0.03, "qty": 3000, "amount": 30_000_000},
              "interventions": [{"rule": "auto_order_max", "before": "9,993만", "after": "3,000만", "note": "auto_order_max: …"}]}))
    cards = {d["ticker"]: d for d in c.get("/api/decisions", params={"date": "2026-06-10"}).json()}
    assert (cards["BBB.KS"]["source_kind"], cards["BBB.KS"]["source_kind_ko"]) == ("baseline", "규칙 베이스라인")
    assert (cards["FFF.KS"]["source_kind"], cards["FFF.KS"]["source_kind_ko"]) == ("agent", "에이전트 결정")
    assert cards["BBB.KS"]["orders"][0]["plan"]["final"]["qty"] == 3000
    assert [d["ticker"] for d in c.get("/api/decisions", params={"date": "2026-06-10", "overrides_only": True}).json()] == ["BBB.KS"]
    assert c.get("/api/labels").json()["decision_source_ko"]["human"] == "사람 처리"


@pytest.mark.unit
def test_a_persons_hold_is_counted_apart_and_moves_no_score(queue):
    c, _, data = queue
    log_path = data / "memory.md"
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    log.store_decision("FFF.KS", "2026-06-10", "**Rating**: Buy\n\nx", rating="Buy")
    log.update_with_outcome("FFF.KS", "2026-06-10", 0.03, 0.01, 5, "", "2026-06-17")
    (data / "baseline_summary.json").write_text(json.dumps({
        "run_id": "k1", "log_path": str(log_path), "cells_run": 1, "resolved": 1, "pending": 0, "unscored": 0,
        "by_rating": {"Buy": {"count": 1, "hit_rate": 1.0, "mean_alpha": 0.01}}, "holding": "5 trading days",
        "dates": ["2026-06-10"], "universe": ["FFF.KS"]}), encoding="utf-8")
    before = c.get("/api/performance").json()
    assert before["human_handled"] == 0
    _act(c, "paper-2026-06-10-RRR.KS", "hold")
    after = c.get("/api/performance").json()
    assert after["human_handled"] == 1
    assert (after["per_date"], after["by_rating"], after["resolved"]) == (before["per_date"], before["by_rating"], before["resolved"])
    assert after["per_date"] == [{"date": "2026-06-10", "resolved": 1, "long_mean_alpha": 0.01, "short_mean_alpha": None}]


# --- a daily-loss halt is not resumed early ------------------------------------------------------------------------

@pytest.mark.unit
def test_resume_refuses_a_daily_loss_halt_and_names_when_it_lifts(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _cache(data, "^KS11", {d: 100.0 for d in CAL if d <= "2026-06-12"})
    _cache(data, "^GSPC", {d: 100.0 for d in CAL if d <= "2026-06-12"})
    market_audit(data / "audit", "KRX").record_operation(_halt("daily_loss", "2026-06-11"))
    market_audit(data / "audit", "US").record_operation(_halt("manual", "2026-06-11", market="US", operator="운영자A"))
    c, _ = _client(data, monkeypatch, token="t", now=datetime(2026, 6, 11, 20, 0, tzinfo=KST))
    body = {"operator": "관리자", "reason": "조기 재개", "scope": "KRX"}
    r = c.post("/api/trading/resume", json=body, headers=H)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["auto_resume_at"].startswith("2026-06-12") and "자동 해제" in detail["message"]
    assert [op.kind for op in market_audit(data / "audit", "KRX").operations()] == ["halt"]          # nothing written
    # scope all: the person's US halt resumes, the KRX daily-loss halt waits for its open
    r = c.post("/api/trading/resume", json=body | {"scope": "all"}, headers=H).json()
    assert r["resumed"] == ["US"]
    assert [(s["market"], s["auto_resume_at"][:10]) for s in r["skipped"]] == [("KRX", "2026-06-12")]


# --- review round 1: retries go through the day's sizing, a rule exit has no retry, what counts as a shrink ---------

@pytest.mark.unit
def test_a_retry_is_cut_to_the_days_target_never_above_its_first_quantity(audit):
    _decide(audit, "B.KS", "2026-06-02")
    no_open_0604 = _frame({d: (10_000.0, 10_000.0) for d in ["2026-06-01", "2026-06-02", "2026-06-05", "2026-06-08"]})
    prices = _prices(B_KS=no_open_0604)
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    # as if it had been placed at 3,000 shares: on 06-04 the day's sizing allows 1e8 × 10% // 10,006.50075 = 999
    audit.record_order(o.model_copy(update={"quantity": 3000, "exception_action": "retry_next", "retries": 1}))
    (again,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    assert (again.status, again.quantity) == ("filled", 999)
    assert "처음 수량 3000주 → 그날 목표 999주" in again.reason and "max_position_pct" in again.rules_applied
    assert again.plan.proposed.qty == 3000 and again.plan.final.qty == 999


@pytest.mark.unit
def test_a_retry_with_no_agent_order_behind_it_goes_back_to_the_queue_once(audit):
    audit.record_order(OrderRecord(order_id="paper-2026-06-02-X.KS-exit", ticker="X.KS", trade_date="2026-06-02", side="Sell",
                                   quantity=10, status="exception", exception_kind="vendor_unavailable", reason="시가 없음",
                                   exception_action="retry_next", retries=1))
    _decide(audit, "X.KS", "2026-06-02", rating="Sell")         # an agent call on the same day: its own id, not the exit's
    prices = _prices(X_KS=_flat())
    out = execute_date(audit, "2026-06-04", prices, risk=RISK)
    back = [o for o in out if o.order_id.endswith("-exit")]
    assert len(back) == 1 and back[0].exception_action is None and "예외 큐로 돌아옴" in back[0].reason
    assert not any(o.order_id == "paper-2026-06-02-X.KS" for o in out)       # nothing written under the agent order id
    assert execute_date(audit, "2026-06-04", prices, risk=RISK) == []


@pytest.mark.unit
def test_a_rule_exit_exception_offers_dismiss_only_and_refuses_retry_next(queue):
    c, a, _ = queue
    a.record_order(OrderRecord(order_id="paper-2026-06-10-EEE.KS-exit", ticker="EEE.KS", trade_date="2026-06-10", side="Sell",
                               quantity=10, status="exception", exception_kind="vendor_unavailable", reason="시가 없음"))
    items = {x["order_id"]: x for x in c.get("/api/exceptions", params={"date": "2026-06-10"}).json()}
    assert [x["action"] for x in items["paper-2026-06-10-EEE.KS-exit"]["actions"]] == ["dismiss_order"]
    r = _act(c, "paper-2026-06-10-EEE.KS-exit", "retry_next")
    assert r.status_code == 422 and r.json()["detail"].startswith("규칙 청산 주문은 실행기가 다음 종가에")
    assert _act(c, "paper-2026-06-10-EEE.KS-exit", "dismiss_order").status_code == 200


@pytest.mark.unit
def test_an_old_cap_exception_is_no_shrink_event_and_cap_zero_is_a_lock():
    from agent_trading_core.execution import book_events
    orders = [OrderRecord(order_id="paper-2026-06-02-OLD.KS", ticker="OLD.KS", trade_date="2026-06-02", side="Buy", quantity=5000,
                          status="exception", exception_kind="cap_exceeded", rules_applied=["auto_order_max"],
                          reason="주문 금액 50,000,000원 > 주문당 상한 30,000,000원"),
              OrderRecord(order_id="paper-2026-06-02-BIG.KS", ticker="BIG.KS", trade_date="2026-06-02", side="Buy", quantity=0,
                          status="blocked", rules_applied=["auto_order_max", "cap_zero"], reason="auto_order_max: 8,000만 → 3,000만 (2주 → 0주)")]
    ev = book_events(orders, [], _prices(OLD_KS=_flat(), BIG_KS=_flat()))
    assert [(e["kind"], e["ticker"]) for e in ev] == [("lock", "BIG.KS")]
    assert ev[0]["text"].startswith("BIG.KS 신규 매수 차단 · auto_order_max: 8,000만")


@pytest.mark.unit
def test_a_persons_halt_over_a_daily_loss_halt_does_not_resume_before_the_next_open(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _cache(data, "^KS11", {d: 100.0 for d in CAL if d <= "2026-06-12"})
    a = market_audit(data / "audit", "KRX")
    a.record_operation(_halt("daily_loss", "2026-06-11"))
    a.record_operation(_halt("manual", "2026-06-11", operator="운영자A"))
    body = {"operator": "관리자", "reason": "조기 재개", "scope": "KRX"}
    c, _ = _client(data, monkeypatch, token="t", now=datetime(2026, 6, 11, 20, 0, tzinfo=KST))
    r = c.post("/api/trading/resume", json=body, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["auto_resume_at"].startswith("2026-06-12")
    # after the next session's 00:00 the person's halt still stands: the admin resume is the way out
    c, _ = _client(data, monkeypatch, token="t", now=datetime(2026, 6, 12, 9, 30, tzinfo=KST))
    assert c.post("/api/trading/resume", json=body, headers=H).json()["resumed"] == ["KRX"]


# --- review round 2: a retry is sized on its own decision day's denominator, at the retry day's scale ------------

def _twenty(audit):
    """06-02: twenty buy-rated names, so each is 1/20 = 5%; only A has prices, the others never get a quantity."""
    for i in range(19):
        _decide(audit, f"Z{i}.KS", "2026-06-02")


@pytest.mark.unit
def test_a_retry_is_halved_when_dd_halve_is_on_at_its_retry_day(audit, monkeypatch):
    import agent_trading_core.execution as ex
    _decide(audit, "A.KS", "2026-06-02")
    _twenty(audit)
    prices = _prices(A_KS=_frame({d: (10_000.0, 10_000.0) for d in ["2026-06-01", "2026-06-02", "2026-06-05", "2026-06-08"]}))
    a = next(o for o in execute_date(audit, "2026-06-02", prices, risk=RISK) if o.ticker == "A.KS")
    assert (a.status, a.quantity) == ("exception", 499)            # 1e8 × 5% // 10,006.50075 = 499, no 06-04 open
    audit.record_order(a.model_copy(update={"exception_action": "retry_next", "retries": 1}))
    real = ex.drawdown_scale
    monkeypatch.setattr(ex, "drawdown_scale", lambda e, p, r: (0.5, "드로다운 −12.0% → 신규 비중 절반") if e == 1e8 else real(e, p, r))
    (again,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    # 06-02's twenty names at 06-04's ×0.5: 2.5% × 1e8 = 2,500,000 // 10,006.50075 = 249 (not the first 499)
    assert (again.order_id, again.status, again.quantity) == (a.order_id, "filled", 249)
    assert "처음 수량 499주 → 그날 목표 249주" in again.reason
    assert again.plan.proposed.qty == 499 and again.plan.final.qty == 249


@pytest.mark.unit
def test_a_retry_of_a_zero_quantity_exception_buys_what_its_decision_day_would_have(audit):
    _decide(audit, "B.KS", "2026-06-02")
    _twenty(audit)
    prices = _prices(B_KS=_frame({d: (10_000.0, 10_000.0) for d in ["2026-06-04", "2026-06-05", "2026-06-08"]}))
    b = next(o for o in execute_date(audit, "2026-06-02", prices, risk=RISK) if o.ticker == "B.KS")
    assert (b.status, b.exception_kind, b.quantity) == ("exception", "vendor_unavailable", 0)     # no 06-02 close
    audit.record_order(b.model_copy(update={"exception_action": "retry_next", "retries": 1}))
    (again,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    # 1/20 = 5% of 1e8 → 5,000,000 // 10,006.50075 = 499 (not 10% as a one-name list would size it)
    assert (again.status, again.quantity) == ("filled", 499)
    assert again.plan.proposed.weight_pct == pytest.approx(0.05)
    assert "다음 세션 재시도 1/2 · 목표 비중 5.0%" in again.reason
