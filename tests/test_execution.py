"""Paper execution: one order per decision, filled at the next trading day's open, refused while halted,
written once, and a book that replays to the numbers worked out by hand here."""

import json
import threading
import time

import pandas as pd
import pytest

from agent_trading_core.audit import AuditLog, DecisionRecord, OperationRecord, latest_orders
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
    OperationRecord(kind="halt", operator="운영자", role="operator", reason="급락"),
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
def test_a_daily_loss_past_the_limit_blocks_the_next_sessions_buys(audit):
    # 10% position falls 30% on the decision day: equity −3% ≤ −2%
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (10_000, 7_000),
                "2026-06-05": (7_000, 7_000), "2026-06-08": (7_000, 7_000)})
    prices = _prices(A_KS=a, B_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    _decide(audit, "B.KS", "2026-06-04")
    (o,) = execute_date(audit, "2026-06-04", prices, risk=RISK)
    assert o.status == "blocked" and o.rules_applied == ["daily_loss_limit"] and "일일 손실 한도" in o.reason


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
def test_a_drawdown_past_the_stop_blocks_new_buys(audit):
    # the whole book in one name that halves: equity -50% <= -20%
    risk, cfg = RiskConfig(regime_ma_days=3, max_position_pct=1.0), PaperConfig(initial_cash=2.5e7)
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (10_000, 5_000),
                "2026-06-05": (5_000, 5_000), "2026-06-08": (5_000, 5_000)})
    prices = _prices(A_KS=a, B_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=risk, cfg=cfg)
    _decide(audit, "B.KS", "2026-06-05")
    (o,) = execute_date(audit, "2026-06-05", prices, risk=risk, cfg=cfg)
    assert o.status == "blocked" and "dd_stop" in o.rules_applied and "MaxDrawdown" in o.reason


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
        audit.record_operation(OperationRecord(kind="halt", operator="운영자", role="operator", reason="급락"))
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
