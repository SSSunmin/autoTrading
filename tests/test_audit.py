"""Screening and decision audit records: written once, read back whole, never silently a Hold."""

import pytest

from agent_trading_core.audit import (
    AuditLog,
    CandidateRecord,
    DecisionRecord,
    EvidenceItem,
    FilterResult,
    ScreenRun,
    decision_source,
)
from agent_trading_core.krx_names import DECISION_SOURCE_KO


def _screen():
    return ScreenRun(
        run_id="20261113_0900", as_of="2026-11-12", universe="KOSPI200", universe_size=3,
        rules=["dollar_volume_top_n", "listed_days_min"],
        candidates=[
            CandidateRecord(ticker="005930.KS", name="Samsung Electronics", selected=True, rank=1, score=0.9,
                            filters=[FilterResult(rule="dollar_volume_top_n", value=1, threshold=50, passed=True),
                                     FilterResult(rule="listed_days_min", value=9000, threshold=180, passed=True)]),
            CandidateRecord(ticker="NEWCO.KQ", selected=False,
                            filters=[FilterResult(rule="dollar_volume_top_n", value=12, threshold=50, passed=True),
                                     FilterResult(rule="listed_days_min", value=40, threshold=180, passed=False,
                                                  note="IPO 2026-10-03")]),
        ],
    )


@pytest.mark.unit
def test_a_rejected_candidate_names_the_rule_that_rejected_it():
    run = _screen()
    rejected = next(c for c in run.candidates if not c.selected)
    assert rejected.failed_rules == ["listed_days_min"]
    assert [c.ticker for c in run.selected] == ["005930.KS"]


@pytest.mark.unit
def test_records_round_trip_through_the_jsonl_log(tmp_path):
    log = AuditLog(tmp_path / "audit")
    log.record_screen(_screen())
    log.record_decision(DecisionRecord(
        ticker="005930.KS", trade_date="2026-11-12", screen_run_id="20261113_0900",
        rating="Overweight", action="Buy", entry_price=71000.0, stop_loss=67000.0,
        thesis="HBM demand", confidence=0.7,
        evidence=[EvidenceItem(source="fundamentals", summary="Q3 operating margin up", as_of="2026-10-31")],
        hard_rule_overrides=["max_position_pct"],
    ))
    log.record_decision(DecisionRecord(ticker="000660.KS", trade_date="2026-11-12", rating="REVIEW", thesis="?"))

    screens = log.screens()
    assert len(screens) == 1 and screens[0].universe_size == 3
    assert screens[0].candidates[1].filters[1].note == "IPO 2026-10-03"

    decisions = log.decisions()
    assert [d.ticker for d in decisions] == ["005930.KS", "000660.KS"]
    assert decisions[0].tradeable and decisions[0].evidence[0].source == "fundamentals"
    assert not decisions[1].tradeable
    assert [d.ticker for d in log.decisions("000660.KS")] == ["000660.KS"]


@pytest.mark.unit
def test_confidence_outside_the_unit_interval_is_refused():
    with pytest.raises(ValueError):
        DecisionRecord(ticker="X", trade_date="2026-01-01", rating="Buy", thesis="t", confidence=1.5)


@pytest.mark.unit
def test_trading_state_is_replayed_from_operations():
    from agent_trading_core.audit import OperationRecord, trading_state

    assert trading_state([])["mode"] == "paper" and trading_state([])["running"] is True
    ops = [
        OperationRecord(kind="mode_change", operator="a", role="admin", reason="r", target="mode",
                        before={"mode": "paper"}, after={"mode": "auto"}),
        OperationRecord(kind="halt", operator="op", role="operator", reason="급락", after={"keep_exits": False}),
    ]
    st = trading_state(ops)
    assert st["mode"] == "auto" and st["running"] is False and st["halted_by"] == "op" and st["keep_exits"] is False
    ops.append(OperationRecord(kind="resume", operator="a", role="admin", reason="복구"))
    st = trading_state(ops)
    assert st["running"] is True and st["halted_at"] is None and st["mode"] == "auto"
    # a rule change never touches the run state
    ops.append(OperationRecord(kind="rule_change", operator="a", role="admin", reason="x", target="sector_max",
                               before={"v": 2}, after={"v": 3}))
    assert trading_state(ops) == st


@pytest.mark.unit
def test_order_record_round_trips_and_amount(tmp_path):
    from agent_trading_core.audit import OrderRecord

    log = AuditLog(tmp_path)
    log.record_order(OrderRecord(order_id="o1", ticker="005930.KS", trade_date="2026-06-05", side="Buy", quantity=10,
                                 filled_quantity=10, filled_price=70000.0, status="filled"))
    log.record_order(OrderRecord(order_id="o2", ticker="000660.KS", trade_date="2026-06-05", side="Buy", quantity=5,
                                 status="rejected", reason="일일 주문 금액 상한 초과", rules_applied=["auto_daily_order_cap"]))
    rows = log.orders()
    assert [o.order_id for o in rows] == ["o1", "o2"]
    assert rows[0].amount == 700000.0 and rows[1].amount is None


@pytest.mark.unit
def test_decision_source_reads_the_rule_baseline_as_baseline():
    base = {"ticker": "005930.KS", "trade_date": "2026-07-24", "rating": "Buy", "thesis": "t"}
    old_line = DecisionRecord.model_validate_json(
        '{"ticker": "005930.KS", "trade_date": "2026-07-24", "rating": "Buy", "thesis": "t", '
        '"settings": {"decider": "rule_momentum_baseline"}}')                      # written before the value existed
    assert old_line.source == "agent" and decision_source(old_line) == "baseline"
    assert decision_source(DecisionRecord(**base)) == "agent"                     # an agent call, default kept
    assert decision_source(DecisionRecord(**base, source="rule")) == "rule"
    assert decision_source(DecisionRecord(**base, source="baseline")) == "baseline"
    assert decision_source(DecisionRecord(**base, settings={"decider": "llm"})) == "agent"
    assert DECISION_SOURCE_KO[decision_source(old_line)] == "규칙 베이스라인"
    with pytest.raises(ValueError):
        DecisionRecord(**base, source="human")                                    # not until stage 4
