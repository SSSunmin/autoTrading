"""Paper execution: one order per decision, filled at the next trading day's open, refused while halted,
written once, and a book that replays to the numbers worked out by hand here."""

import json
import threading
import time

import pandas as pd
import pytest

from agent_trading_core.audit import (
    AuditLog,
    DecisionRecord,
    OperationRecord,
    latest_orders,
    market_audit,
    trading_state,
)
from agent_trading_core.execution import (
    ExecutionRefused,
    PaperConfig,
    Prices,
    cancel_orders,
    execute_date,
    execution_lock,
    next_session,
    open_orders,
    positions_view,
    unexplained_orders,
)
from agent_trading_core.risk import RiskConfig

SLIP, COMM, TAX = 0.0005, 0.00015, 0.0020
# 2026-06-03 is a market holiday: the calendar skips it.
DAYS = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05", "2026-06-08"]
RISK = RiskConfig(regime_ma_days=3)          # short window so a test can switch the regime with a few bars


def _frame(rows: dict[str, tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame({"Open": [o for o, _ in rows.values()], "Close": [c for _, c in rows.values()]},
                        index=pd.Index(list(rows), dtype=object))


def _index(days=DAYS, closes=None):
    closes = closes or [100.0 + i for i in range(len(days))]          # rising: regime on
    return _frame({d: (c, c) for d, c in zip(days, closes, strict=True)})


def _prices(**tickers) -> Prices:
    frames = {"^KS11": tickers.pop("index", None) if "index" in tickers else _index()}
    frames.update({t.replace("_", ".") : f for t, f in tickers.items()})
    return Prices(frames=frames)


def _flat(price=10_000.0, days=DAYS) -> pd.DataFrame:
    return _frame({d: (price, price) for d in days})


def _decide(audit, ticker, date, rating="Buy"):
    audit.record_decision(DecisionRecord(ticker=ticker, trade_date=date, rating=rating, thesis=f"{rating} {ticker}"))


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit")


def _orders(audit):
    return latest_orders(audit.orders())


@pytest.mark.unit
def test_next_session_skips_the_holiday_and_is_none_past_the_data():
    assert next_session(DAYS, "2026-06-02") == "2026-06-04"
    assert next_session(DAYS, "2026-06-03") == "2026-06-04"     # a decision dated on the holiday itself
    assert next_session(DAYS, "2026-06-08") is None


@pytest.mark.unit
def test_buy_fills_at_the_next_sessions_open_not_the_decision_days_price(audit):
    # decision-day close 10,000; the holiday-skipped next session opens at 10,400
    a = _frame({"2026-06-01": (9_900, 10_000), "2026-06-02": (9_950, 10_000), "2026-06-04": (10_400, 10_500),
                "2026-06-05": (10_500, 10_600), "2026-06-08": (10_600, 10_700)})
    _decide(audit, "A.KS", "2026-06-02")
    written = execute_date(audit, "2026-06-02", _prices(A_KS=a), risk=RISK)
    (o,) = written
    assert o.status == "filled" and o.fill_date == "2026-06-04" and o.order_id == "paper-2026-06-02-A.KS"
    assert o.filled_price == pytest.approx(10_400 * (1 + SLIP))
    # sized from the decision-day close: 10% of 100,000,000 / (10,000 × slippage × commission), whole shares
    assert o.quantity == o.filled_quantity == int(10_000_000 // (10_000 * (1 + SLIP) * (1 + COMM))) == 999
    assert o.rules_applied == ["max_position_pct"] and "100.0% → 10.0%" in o.reason


@pytest.mark.unit
def test_an_order_whose_next_session_is_not_in_the_data_stays_open_then_fills_once(audit):
    _decide(audit, "A.KS", "2026-06-08")
    (o,) = execute_date(audit, "2026-06-08", _prices(A_KS=_flat()), risk=RISK)
    assert o.status == "open" and o.filled_price is None

    later = DAYS + ["2026-06-09"]
    prices = _prices(index=_index(later), A_KS=_flat(days=later))
    (f,) = execute_date(audit, "2026-06-08", prices, risk=RISK)
    assert f.order_id == o.order_id and f.status == "filled" and f.fill_date == "2026-06-09"
    assert execute_date(audit, "2026-06-08", prices, risk=RISK) == []           # idempotent: nothing more
    assert [x.status for x in audit.orders()] == ["open", "filled"]             # append-only history
    assert len(_orders(audit)) == 1


@pytest.mark.unit
def test_rerunning_a_date_or_a_recorded_twice_decision_never_orders_twice(audit):
    _decide(audit, "A.KS", "2026-06-02")
    _decide(audit, "A.KS", "2026-06-02")                                        # same decision recorded again
    prices = _prices(A_KS=_flat())
    assert len(execute_date(audit, "2026-06-02", prices, risk=RISK)) == 1
    assert execute_date(audit, "2026-06-02", prices, risk=RISK) == []
    assert len(audit.orders()) == 1


@pytest.mark.unit
@pytest.mark.parametrize("op", [
    OperationRecord(kind="halt", operator="운영자", role="operator", reason="급락", after={"keep_exits": False}),
    OperationRecord(kind="mode_change", operator="관리자", role="admin", reason="실거래", after={"mode": "auto"}),
    OperationRecord(kind="mode_change", operator="관리자", role="admin", reason="수동", after={"mode": "manual"}),
])
def test_halted_or_not_paper_mode_writes_nothing(audit, op):
    _decide(audit, "A.KS", "2026-06-02")
    audit.record_operation(op)
    with pytest.raises(ExecutionRefused):
        execute_date(audit, "2026-06-02", _prices(A_KS=_flat()), risk=RISK)
    assert audit.orders() == []


@pytest.mark.unit
def test_halt_cancels_open_orders_and_resume_does_not_revive_them(audit):
    _decide(audit, "A.KS", "2026-06-08")
    execute_date(audit, "2026-06-08", _prices(A_KS=_flat()), risk=RISK)
    (c,) = cancel_orders(audit, open_orders(audit), "긴급 정지")
    assert c.status == "cancelled" and "긴급 정지" in c.reason
    assert open_orders(audit) == []

    later = DAYS + ["2026-06-09"]
    assert execute_date(audit, "2026-06-08", _prices(index=_index(later), A_KS=_flat(days=later)), risk=RISK) == []
    assert _orders(audit)["paper-2026-06-08-A.KS"].status == "cancelled"


@pytest.mark.unit
def test_sell_without_a_holding_and_buy_of_a_held_name_are_blocked(audit):
    _decide(audit, "A.KS", "2026-06-01")
    prices = _prices(A_KS=_flat(), B_KS=_flat())
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    _decide(audit, "A.KS", "2026-06-04", "Overweight")
    _decide(audit, "B.KS", "2026-06-04", "Underweight")
    _decide(audit, "C.KS", "2026-06-04", "Hold")
    execute_date(audit, "2026-06-04", prices, risk=RISK)
    by = {o.ticker: o for o in _orders(audit).values() if o.trade_date == "2026-06-04"}
    assert by["A.KS"].status == "blocked" and by["A.KS"].rules_applied == ["already_held"]
    assert by["B.KS"].status == "blocked" and by["B.KS"].rules_applied == ["no_short"] and by["B.KS"].side == "Sell"
    assert "C.KS" not in by                                                     # Hold places no order


@pytest.mark.unit
def test_regime_below_its_average_blocks_new_buys(audit):
    _decide(audit, "A.KS", "2026-06-04")
    falling = _index(closes=[110.0, 105.0, 90.0, 89.0, 88.0])                   # 90 < mean(110,105,90)
    (o,) = execute_date(audit, "2026-06-04", _prices(index=falling, A_KS=_flat()), risk=RISK)
    assert o.status == "blocked" and o.rules_applied == ["regime"] and o.quantity == 0


@pytest.mark.unit
def test_a_daily_loss_past_the_limit_halts_the_loop_once_and_keeps_exits_running(audit):
    # 10% position falls 30% on 06-04: equity -3% <= -2%
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (10_000, 7_000),
                "2026-06-05": (7_000, 7_000), "2026-06-08": (7_000, 7_000)})
    prices = _prices(A_KS=a, B_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    _decide(audit, "B.KS", "2026-06-04")
    (exit_,) = execute_date(audit, "2026-06-04", prices, risk=RISK)        # the stop-loss sell, no buy of B
    assert exit_.order_id == "paper-2026-06-04-A.KS-exit" and exit_.status == "filled"

    (halt,) = audit.operations()
    assert (halt.kind, halt.operator, halt.role, halt.target) == ("halt", "system", "auto", "daily_loss")
    assert halt.after["keep_exits"] is True and halt.after["date"] == "2026-06-04"
    assert halt.after["day_return"] == pytest.approx(-0.03, abs=0.002) and "일일 손실 한도 초과" in halt.reason
    state = trading_state(audit.operations())
    assert state["running"] is False and state["halted_by"] == "system"

    assert execute_date(audit, "2026-06-04", prices, risk=RISK) == []          # halted, exits kept: nothing new
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="확인"))
    (b,) = execute_date(audit, "2026-06-04", prices, risk=RISK)              # resumed the same day: no second halt
    assert len(audit.operations()) == 2
    assert b.ticker == "B.KS" and b.status == "blocked" and b.rules_applied == ["daily_loss_limit"]


@pytest.mark.unit
def test_review_and_missing_open_are_exceptions(audit):
    hole = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-05": (10_000, 10_000)})
    _decide(audit, "R.KS", "2026-06-02", "REVIEW")
    _decide(audit, "H.KS", "2026-06-02")                                        # halted on 06-04: no open
    execute_date(audit, "2026-06-02", _prices(H_KS=hole), risk=RISK)
    by = {o.ticker: o for o in _orders(audit).values()}
    assert by["R.KS"].status == "exception" and by["R.KS"].exception_kind == "review" and by["R.KS"].quantity == 0
    assert by["H.KS"].status == "exception" and by["H.KS"].exception_kind == "vendor_unavailable"
    assert "2026-06-04 시가 없음" in by["H.KS"].reason and by["H.KS"].filled_quantity == 0


@pytest.mark.unit
def test_auto_execution_caps_make_exceptions_not_smaller_orders(audit):
    # 1e9 equity × 10% = 100M per order > 30M per-order cap
    _decide(audit, "A.KS", "2026-06-02")
    (o,) = execute_date(audit, "2026-06-02", _prices(A_KS=_flat()), risk=RISK, cfg=PaperConfig(initial_cash=1e9))
    assert o.status == "exception" and o.exception_kind == "cap_exceeded" and o.rules_applied[-1] == "auto_order_max"
    assert o.quantity > 0 and o.filled_quantity == 0


@pytest.mark.unit
def test_the_daily_order_cap_stops_the_order_that_crosses_it(audit, tmp_path):
    # 250M × 10% ≈ 25M each: four fit under 100M, the fifth crosses it
    tickers = [f"T{i}.KS" for i in range(5)]
    for t in tickers:
        _decide(audit, t, "2026-06-02")
    prices = _prices(**{t.replace(".", "_"): _flat() for t in tickers})
    out = execute_date(audit, "2026-06-02", prices, risk=RISK, cfg=PaperConfig(initial_cash=2.5e8))
    assert [o.status for o in out] == ["filled"] * 4 + ["exception"]
    assert out[-1].rules_applied[-1] == "auto_daily_order_cap"


@pytest.mark.unit
def test_a_gap_up_open_fills_partly_or_is_rejected_for_cash(audit):
    risk = RiskConfig(regime_ma_days=3, max_position_pct=0.5)
    gap = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (12_000, 12_000),
                  "2026-06-05": (12_000, 12_000), "2026-06-08": (12_000, 12_000)})
    _decide(audit, "A.KS", "2026-06-02")
    _decide(audit, "B.KS", "2026-06-02")
    # 50M book: 25M per name stays under the per-order cap; the +20% gap leaves too little cash for the second
    out = execute_date(audit, "2026-06-02", _prices(A_KS=gap, B_KS=gap), risk=risk, cfg=PaperConfig(initial_cash=5e7))
    assert [o.status for o in out] == ["filled", "partial"]
    assert out[1].filled_quantity < out[1].quantity and "잔량 취소" in out[1].reason

    audit2 = AuditLog(audit.directory.parent / "audit2")
    one = PaperConfig(initial_cash=1_000_000)
    big = _frame({"2026-06-01": (900_000, 900_000), "2026-06-02": (900_000, 900_000), "2026-06-04": (1_100_000, 1_100_000),
                  "2026-06-05": (1_100_000, 1_100_000), "2026-06-08": (1_100_000, 1_100_000)})
    _decide(audit2, "A.KS", "2026-06-02")
    (r,) = execute_date(audit2, "2026-06-02", _prices(A_KS=big), risk=RiskConfig(regime_ma_days=3, max_position_pct=1.0), cfg=one)
    assert r.quantity == 1 and r.status == "rejected" and r.exception_kind == "order_rejected" and "현금 부족" in r.reason


@pytest.mark.unit
def test_an_earlier_date_after_a_later_one_is_refused(audit):
    _decide(audit, "A.KS", "2026-06-04")
    _decide(audit, "B.KS", "2026-06-02")
    prices = _prices(A_KS=_flat(), B_KS=_flat())
    execute_date(audit, "2026-06-04", prices, risk=RISK)
    with pytest.raises(ExecutionRefused, match="결정일 순서"):
        execute_date(audit, "2026-06-02", prices, risk=RISK)


@pytest.mark.unit
def test_a_broken_orders_line_refuses_before_anything_is_appended(audit):
    _decide(audit, "A.KS", "2026-06-02")
    path = audit.directory / "orders.jsonl"
    path.write_text('{"order_id": "x", "ticker"', encoding="utf-8")            # a write cut short
    with pytest.raises(json.JSONDecodeError):
        execute_date(audit, "2026-06-02", _prices(A_KS=_flat()), risk=RISK)
    assert path.read_text(encoding="utf-8") == '{"order_id": "x", "ticker"'


@pytest.mark.unit
def test_a_bad_date_is_refused():
    with pytest.raises(ValueError):
        execute_date(AuditLog.__new__(AuditLog), "2026/06/02", _prices())


@pytest.mark.unit
def test_the_book_replays_to_the_hand_worked_numbers(audit):
    # buy 06-01 → fills 06-02 open 10,000; sell decided 06-04 → fills 06-05 open 12,000; marked at closes.
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 11_000), "2026-06-04": (11_000, 11_500),
                "2026-06-05": (12_000, 12_500), "2026-06-08": (12_500, 12_500)})
    prices = _prices(A_KS=a)
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)

    q = 999                                                                     # 10M // (10,000 × 1.0005 × 1.00015)
    buy_px = 10_000 * (1 + SLIP)
    cash = 100_000_000 - q * buy_px * (1 + COMM)
    view = positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)
    (p,) = view["positions"]
    assert p["quantity"] == q and p["avg_price"] == pytest.approx(buy_px) and p["entry_date"] == "2026-06-02"
    assert p["last_price"] == 11_500 and p["unrealized"] == pytest.approx((11_500 - buy_px) * q)
    assert p["stop_price"] == pytest.approx(buy_px * 0.92) and p["holding_days"] == 1
    assert p["trailing_high"] == 11_000 and p["trailing_active"] is False      # +9.9% < +10% activation
    assert view["cash"] == pytest.approx(cash) and view["equity"] == pytest.approx(cash + 11_500 * q)
    assert view["gross_exposure"] == pytest.approx(11_500 * q / (cash + 11_500 * q))

    _decide(audit, "A.KS", "2026-06-04", "Sell")
    (s,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    sell_px = 12_000 * (1 - SLIP)
    assert s.side == "Sell" and s.status == "filled" and s.filled_quantity == q and s.filled_price == pytest.approx(sell_px)
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=RISK)
    assert view["positions"] == []
    assert view["cash"] == pytest.approx(cash + q * sell_px * (1 - COMM - TAX))
    assert view["realized_pnl"] == pytest.approx((sell_px - buy_px) * q) and view["realized_today"] == 0
    assert view["exits_by_reason"] == {"exit_signal": 1} and view["closed"][0]["pnl_pct"] == pytest.approx(sell_px / buy_px - 1)
    # the sale at 12,000 is above every earlier close-marked equity, so the book ends at its peak
    assert view["equity"] == pytest.approx(view["cash"]) and view["peak"] == pytest.approx(view["equity"])
    assert view["drawdown"] == pytest.approx(0.0)


@pytest.mark.unit
def test_positions_show_a_stop_loss_verdict_without_placing_an_order(audit):
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (9_000, 9_000),
                "2026-06-05": (9_000, 9_000), "2026-06-08": (9_000, 9_000)})
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", _prices(A_KS=a), risk=RISK)
    (p,) = positions_view(audit.orders(), _prices(A_KS=a), as_of="2026-06-04", risk=RISK)["positions"]
    assert p["exit_signal"]["reason"] == "stop_loss"
    assert len(audit.orders()) == 1                                            # shown, not acted on


@pytest.mark.unit
def test_a_holiday_dated_decision_sees_the_buy_filling_at_the_same_session(audit):
    # 06-02 and 06-03 (holiday) both fill at the 06-04 open: the second must not buy the name again
    prices = _prices(A_KS=_flat())
    _decide(audit, "A.KS", "2026-06-02")
    (first,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert first.status == "filled" and first.fill_date == "2026-06-04"
    _decide(audit, "A.KS", "2026-06-03")
    (second,) = execute_date(audit, "2026-06-03", prices, risk=RISK)
    assert second.status == "blocked" and second.rules_applied == ["already_held"]


@pytest.mark.unit
def test_the_daily_cap_counts_buys_filling_at_the_same_session_across_decision_dates(audit):
    # 250M book, ~25M per buy: three on 06-02 and two on the holiday 06-03 all fill at the 06-04 open
    names = [f"T{i}.KS" for i in range(5)]
    prices = _prices(**{t.replace(".", "_"): _flat() for t in names})
    cfg = PaperConfig(initial_cash=2.5e8)
    for t in names[:3]:
        _decide(audit, t, "2026-06-02")
    execute_date(audit, "2026-06-02", prices, risk=RISK, cfg=cfg)
    for t in names[3:]:
        _decide(audit, t, "2026-06-03")
    out = execute_date(audit, "2026-06-03", prices, risk=RISK, cfg=cfg)
    assert [o.status for o in out] == ["filled", "exception"] and out[1].rules_applied[-1] == "auto_daily_order_cap"


@pytest.mark.unit
def test_cooldown_blocks_buying_back_a_name_just_sold(audit):
    prices = _prices(A_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)                     # fills 06-02
    _decide(audit, "A.KS", "2026-06-02", "Sell")
    execute_date(audit, "2026-06-02", prices, risk=RISK)                     # sold 06-04
    _decide(audit, "A.KS", "2026-06-04")
    (o,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    assert o.status == "blocked" and o.rules_applied == ["Cooldown"] and "재진입 금지" in o.reason


@pytest.mark.unit
def test_a_drawdown_past_the_stop_sells_everything_and_halts_the_loop_by_system(audit):
    # the whole book in one name that halves: equity -50% <= -20%
    risk, cfg = RiskConfig(regime_ma_days=3, max_position_pct=1.0), PaperConfig(initial_cash=2.5e7)
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (10_000, 5_000),
                "2026-06-05": (5_000, 5_000), "2026-06-08": (5_000, 5_000)})
    prices = _prices(A_KS=a, B_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=risk, cfg=cfg)
    _decide(audit, "B.KS", "2026-06-05")
    by = {o.ticker: o for o in execute_date(audit, "2026-06-05", prices, risk=risk, cfg=cfg)}
    assert by["A.KS"].rules_applied == ["stop_loss"]                         # its own stop comes before the DD stop
    assert "B.KS" not in by                                                  # halted: no new buy is even placed
    (halt,) = [op for op in audit.operations() if op.kind == "halt"]
    assert (halt.operator, halt.role, halt.target, halt.after["keep_exits"]) == ("system", "auto", "dd_stop", True)
    assert trading_state(audit.operations())["halt_target"] == "dd_stop"


@pytest.mark.unit
def test_a_sell_is_not_stopped_by_the_buy_caps(audit):
    # 25M bought under the 30M cap, then the name doubles: the 50M sale is over the cap but reduces risk
    cfg, risk = PaperConfig(initial_cash=1e9), RiskConfig(regime_ma_days=3, max_position_pct=0.025)
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 20_000), "2026-06-04": (20_000, 20_000),
                "2026-06-05": (20_000, 20_000), "2026-06-08": (20_000, 20_000)})
    prices = _prices(A_KS=a)
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=risk, cfg=cfg)
    _decide(audit, "A.KS", "2026-06-02", "Sell")
    (s,) = execute_date(audit, "2026-06-02", prices, risk=risk, cfg=cfg)
    assert s.side == "Sell" and s.status == "filled" and s.amount > 30_000_000


@pytest.mark.unit
def test_a_pending_sell_is_not_placed_twice(audit):
    prices = _prices(A_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    _decide(audit, "A.KS", "2026-06-02", "Sell")
    execute_date(audit, "2026-06-02", prices, risk=RISK)                     # fills 06-04
    _decide(audit, "A.KS", "2026-06-03", "Underweight")                      # holiday: same 06-04 session
    (o,) = execute_date(audit, "2026-06-03", prices, risk=RISK)
    assert o.status == "blocked" and o.rules_applied == ["already_selling"]


@pytest.mark.unit
def test_the_executor_waits_for_the_lock_a_halt_holds(audit):
    _decide(audit, "A.KS", "2026-06-02")
    errors: list = []

    def run():
        try:
            execute_date(audit, "2026-06-02", _prices(A_KS=_flat()), risk=RISK)
        except ExecutionRefused as exc:
            errors.append(exc)

    with execution_lock(audit.directory):
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        assert t.is_alive() and audit.orders() == []                          # waiting on the lock
        audit.record_operation(OperationRecord(kind="halt", operator="운영자", role="operator", reason="급락",
                                               after={"keep_exits": False}))
    t.join(timeout=15)
    assert len(errors) == 1 and audit.orders() == []                           # it saw the halt and wrote nothing


@pytest.mark.unit
def test_the_cli_exits_4_on_a_broken_orders_line(tmp_path, capsys):
    from scripts.paper_execute import main
    audit = AuditLog(tmp_path / "audit")
    _decide(audit, "A.KS", "2026-06-02")
    (audit.directory / "orders.jsonl").write_text('{"order_id": "x"', encoding="utf-8")
    assert main(["--all", "--data-dir", str(tmp_path)]) == 4
    assert "기록 파손" in capsys.readouterr().err


@pytest.mark.unit
def test_a_holiday_sell_behind_an_unsettled_buy_says_so(audit):
    prices = _prices(A_KS=_flat())
    _decide(audit, "A.KS", "2026-06-02")
    execute_date(audit, "2026-06-02", prices, risk=RISK)                     # fills 06-04
    _decide(audit, "A.KS", "2026-06-03", "Sell")
    (o,) = execute_date(audit, "2026-06-03", prices, risk=RISK)
    assert o.status == "blocked" and o.rules_applied == ["pending_buy"]


@pytest.mark.unit
def test_a_decision_past_the_price_data_is_refused(audit):
    _decide(audit, "A.KS", "2026-06-09")
    with pytest.raises(ExecutionRefused, match="캐시 갱신"):
        execute_date(audit, "2026-06-09", _prices(A_KS=_flat()), risk=RISK)
    assert audit.orders() == []



def _held(audit, prices, risk=RISK, cfg=None):
    """A.KS bought on 06-01, filled at the 06-02 open (10,000 x 1.0005)."""
    _decide(audit, "A.KS", "2026-06-01")
    (o,) = execute_date(audit, "2026-06-01", prices, risk=risk, cfg=cfg)
    assert o.status == "filled" and o.fill_date == "2026-06-02"
    return o


def _rule_decision(audit, ticker, date):
    (d,) = [d for d in audit.decisions() if d.source == "rule" and d.ticker == ticker and d.trade_date == date]
    return d


@pytest.mark.unit
def test_a_stop_loss_at_the_close_records_a_rule_decision_then_sells_at_the_next_open(audit):
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 9_900), "2026-06-04": (9_800, 9_100),
                "2026-06-05": (9_000, 9_000), "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=a)
    buy = _held(audit, prices)
    (o,) = execute_date(audit, "2026-06-04", prices, risk=RISK)              # 9,100 / 10,005 - 1 = -9.0% <= -8%
    assert o.order_id == "paper-2026-06-04-A.KS-exit" and o.side == "Sell" and o.rules_applied == ["stop_loss"]
    assert o.status == "filled" and o.fill_date == "2026-06-05" and o.filled_quantity == buy.filled_quantity
    assert o.filled_price == pytest.approx(9_000 * (1 - SLIP))
    d = _rule_decision(audit, "A.KS", "2026-06-04")
    assert (d.rating, d.action, d.hard_rule_overrides) == ("Sell", "Sell", []) and d.thesis.startswith("손절")
    assert [(e.source, e.as_of, e.ref) for e in d.evidence] == [("portfolio", "2026-06-04", "stop_loss")]
    # the decision line is written before the order line, and rerunning the day writes neither again
    assert execute_date(audit, "2026-06-04", prices, risk=RISK) == []
    assert len([d for d in audit.decisions() if d.source == "rule"]) == 1
    assert unexplained_orders(audit.orders(), audit.decisions()) == []
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=RISK)
    assert view["exits_by_reason"] == {"stop_loss": 1} and view["closed"][0]["reason"] == "stop_loss"


@pytest.mark.unit
def test_a_trailing_stop_sells_after_the_high_gives_back_its_band(audit):
    # +15% high on 06-02, then 10,900 on 06-04 is -5.2% from it: trailing (and not a stop-loss)
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 11_500), "2026-06-04": (11_000, 10_900),
                "2026-06-05": (10_800, 10_800), "2026-06-08": (10_800, 10_800)})
    prices = _prices(A_KS=a)
    _held(audit, prices)
    (o,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    assert o.rules_applied == ["trailing"] and _rule_decision(audit, "A.KS", "2026-06-04").thesis.startswith("트레일링")


@pytest.mark.unit
def test_an_expired_holding_is_sold_after_max_holding_days(audit):
    risk = RiskConfig(regime_ma_days=3, max_holding_days=2)                # entry 06-02 (index 1) + 2 sessions = 06-05
    prices = _prices(A_KS=_flat())
    _held(audit, prices, risk)
    assert execute_date(audit, "2026-06-04", prices, risk=risk) == []
    (o,) = execute_date(audit, "2026-06-05", prices, risk=risk)
    assert o.rules_applied == ["expired"] and o.status == "filled" and o.fill_date == "2026-06-08"


@pytest.mark.unit
def test_the_drawdown_stop_liquidates_what_its_own_rules_keep(audit):
    # the whole book in one name, stop-loss off: -25% from the peak <= -20% -> force; halted with exits kept
    risk = RiskConfig(regime_ma_days=3, max_position_pct=1.0, stop_loss_pct=-0.99)
    cfg = PaperConfig(initial_cash=2.5e7)
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (10_000, 7_500),
                "2026-06-05": (7_500, 7_500), "2026-06-08": (7_500, 7_500)})
    prices = _prices(A_KS=a)
    _held(audit, prices, risk, cfg)
    (o,) = execute_date(audit, "2026-06-04", prices, risk=risk, cfg=cfg)
    assert o.rules_applied == ["force"] and o.status == "filled"
    assert "MaxDrawdown" in _rule_decision(audit, "A.KS", "2026-06-04").thesis
    assert audit.operations()[-1].operator == "system"                     # the same day's -25% also halted the loop


@pytest.mark.unit
def test_three_stop_losses_lock_new_buys_through_stoploss_guard(audit):
    names = ["A.KS", "B.KS", "C.KS"]
    drop = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 9_000), "2026-06-04": (9_000, 9_000),
                   "2026-06-05": (9_000, 9_000), "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=drop, B_KS=drop, C_KS=drop, D_KS=_flat())
    for t in names:
        _decide(audit, t, "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)                    # bought at the 06-02 open
    # 06-02 close 9,000: -10% each -> three stop-losses (book -3%: the auto halt keeps exits running)
    out = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert sorted(o.rules_applied[0] for o in out) == ["stop_loss"] * 3
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="확인"))
    _decide(audit, "D.KS", "2026-06-05")
    (d,) = execute_date(audit, "2026-06-05", prices, risk=RISK)
    assert d.status == "blocked" and d.rules_applied == ["StoplossGuard"]
    guard = positions_view(audit.orders(), prices, as_of="2026-06-05", risk=RISK)["protections"][0]
    assert guard["id"] == "StoplossGuard" and guard["active"] is True


@pytest.mark.unit
def test_an_agent_sell_the_same_day_is_not_placed_twice(audit):
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (9_000, 9_000),
                "2026-06-05": (9_000, 9_000), "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=a)
    _held(audit, prices)
    _decide(audit, "A.KS", "2026-06-04", "Sell")
    by = {o.order_id: o for o in execute_date(audit, "2026-06-04", prices, risk=RISK)}
    assert by["paper-2026-06-04-A.KS-exit"].status == "filled"
    assert by["paper-2026-06-04-A.KS"].status == "blocked" and by["paper-2026-06-04-A.KS"].rules_applied == ["already_selling"]
    assert unexplained_orders(audit.orders(), audit.decisions()) == []


@pytest.mark.unit
@pytest.mark.parametrize("keep_exits, sells", [(True, 1), (False, 0)])
def test_a_human_halt_runs_rule_exits_only_when_it_kept_exits(audit, keep_exits, sells):
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (9_000, 9_000),
                "2026-06-05": (9_000, 9_000), "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=a, B_KS=_flat())
    _held(audit, prices)
    audit.record_operation(OperationRecord(kind="halt", operator="운영자", role="operator", reason="점검",
                                           after={"keep_exits": keep_exits}))
    _decide(audit, "B.KS", "2026-06-05")
    if not keep_exits:
        with pytest.raises(ExecutionRefused, match="청산도 중단"):
            execute_date(audit, "2026-06-05", prices, risk=RISK)
        assert len(audit.orders()) == 1
        return
    out = execute_date(audit, "2026-06-05", prices, risk=RISK)
    assert [(o.ticker, o.side, o.rules_applied) for o in out] == [("A.KS", "Sell", ["stop_loss"])]  # no buy of B


@pytest.mark.unit
def test_an_order_without_its_decision_is_counted_unexplained(audit):
    from agent_trading_core.audit import OrderRecord
    rule_only = OrderRecord(order_id="paper-2026-06-04-A.KS-exit", ticker="A.KS", trade_date="2026-06-04", side="Sell",
                            quantity=1, status="open")
    _decide(audit, "A.KS", "2026-06-04", "Sell")                            # an agent decision does not explain it
    assert unexplained_orders([rule_only], audit.decisions()) == [rule_only]


@pytest.mark.unit
def test_a_rule_decision_left_without_its_order_is_reused_not_recorded_twice(audit):
    # a crash between the decision line and the order line: the next run places the order on the decision it finds
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 9_900), "2026-06-04": (9_800, 9_100),
                "2026-06-05": (9_000, 9_000), "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=a)
    _held(audit, prices)
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date="2026-06-04", rating="Sell", action="Sell",
                                         source="rule", thesis="손절 (먼저 기록됨)"))
    (o,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    assert o.order_id == "paper-2026-06-04-A.KS-exit" and o.status == "filled"
    assert _rule_decision(audit, "A.KS", "2026-06-04").thesis == "손절 (먼저 기록됨)"
    assert unexplained_orders(audit.orders(), audit.decisions()) == []


@pytest.mark.unit
def test_a_day_without_its_own_bar_does_not_judge_a_stop_on_the_last_close(audit):
    # no A.KS bar on 06-04 (suspended): the 06-02 close of 9,100 is -9% but it is not today's close
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 9_100), "2026-06-05": (9_000, 9_000),
                "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=a)
    _held(audit, prices)
    assert execute_date(audit, "2026-06-04", prices, risk=RISK) == []
    assert positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)["positions"][0]["exit_signal"] is None
    (o,) = execute_date(audit, "2026-06-05", prices, risk=RISK)              # its own bar again: 9,000 is -10%
    assert o.rules_applied == ["stop_loss"] and o.fill_date == "2026-06-08"


@pytest.mark.unit
def test_the_cli_all_runs_a_holiday_dated_decision_on_its_own_date(tmp_path, monkeypatch, capsys):
    from scripts import paper_execute
    prices = _prices(A_KS=_flat())
    monkeypatch.setattr(paper_execute, "Prices", lambda _cache: prices)
    audit = AuditLog(tmp_path / "audit")
    _decide(audit, "A.KS", "2026-06-03")                                    # a holiday: not in DAYS
    assert paper_execute.main(["--all", "--data-dir", str(tmp_path)]) == 0
    (o,) = _orders(market_audit(tmp_path / "audit", "KRX")).values()        # new KRX lines: audit/KRX/
    assert (o.order_id, o.status, o.fill_date) == ("paper-2026-06-03-A.KS", "filled", "2026-06-04")
    assert "2026-06-03: 1건 기록" in capsys.readouterr().out
