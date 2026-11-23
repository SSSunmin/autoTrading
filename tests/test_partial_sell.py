"""Partial sells (feature/partial-sell): a sell review's Trim sells down to its target weight, a memo's Underweight
sells AUTO_EXECUTION's share of the holding — the whole of it since 2026-11-23 (fix/uw-full, docs/13), half before:
the half tests set 0.5 (``half``) — Sell / Exit and the rule exits sell it all. The rest keeps its entry, average
price, stop line and trailing high; only a full close locks the name (Cooldown). Numbers worked out by hand here."""

import pytest

from agent_trading_core.audit import AuditLog, DecisionRecord, trim_target
from agent_trading_core.config import AUTO_EXECUTION
from agent_trading_core.execution import _sell_size, execute_date, positions_view
from tests.test_execution import RISK, SLIP, _decide, _flat, _frame, _held, _prices

BUY_PX = 10_000 * (1 + SLIP)                         # _held: 999 shares filled at the 06-02 open
CASH = 100_000_000 - 999 * BUY_PX * 1.00015          # 90,003,505.75 after the buy and its commission


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit")


@pytest.fixture
def half(monkeypatch):
    """A memo's Underweight sells half the holding, the setting from 2026-11-16 to 11-23."""
    monkeypatch.setitem(AUTO_EXECUTION, "underweight_sell_fraction", 0.5)


def _trim(audit, ticker, date, pct):
    """A sell review's Trim as agent.to_record writes it: rating Underweight, the target in position_sizing."""
    audit.record_decision(DecisionRecord(ticker=ticker, trade_date=date, rating="Underweight", action="Sell",
                                         position_sizing=f"trim to {pct:g}% of equity", thesis=f"Trim {ticker}"))


def _guard(view, gid):
    return next(g for g in view["protections"] if g["id"] == gid)


@pytest.mark.unit
def test_trim_target_reads_only_the_trim_sizing():
    def rec(sizing):
        return DecisionRecord(ticker="A.KS", trade_date="2026-06-02", rating="Underweight", thesis="x",
                              position_sizing=sizing)
    assert trim_target(rec("trim to 10% of equity")) == pytest.approx(0.10)
    assert trim_target(rec("trim to 0% of equity")) == 0.0
    assert trim_target(rec("trim to 2.5% of equity")) == pytest.approx(0.025)
    assert trim_target(rec("10% of equity")) is None             # an InvestmentMemo's sizing, not a Trim
    assert trim_target(rec(None)) is None


@pytest.mark.unit
def test_sell_size_by_hand():
    # Trim: 120 × 10,000 of 6.6M is 18.2%; 10% keeps 6.6M × 0.1 / 10,000 = 66 → sell 54
    assert _sell_size(120, 0.10, 0.5, 10_000, 6_600_000, "KRX") == (
        54, "Trim 비중 18.2% → 10.0% (120주 → 66주) · 평가 120만 → 66만")
    # keep 65.4 → the shares over it rounded down: 54.6 → sell 54, keep 66 (never under the target by a share)
    assert _sell_size(120, 0.10, 0.5, 10_000, 6_540_000, "KRX")[0] == 54
    # keep 0.07 × 7M / 1,000 = 490 exactly, as float 490.00000000000006: sell 510, not 509
    assert _sell_size(1000, 0.07, 0.5, 1_000, 7_000_000, "KRX")[0] == 510
    assert _sell_size(120, 0.0, 0.5, 10_000, 6_600_000, "KRX")[0] == 120             # 0%: all of it
    assert _sell_size(120, 0.20, 0.5, 10_000, 6_600_000, "KRX")[0] == 0              # 20% ≥ 18.2%: nothing
    assert _sell_size(120, 0.1818, 0.5, 10_000, 6_600_000, "KRX")[0] == 0            # 119.99 kept: under a share
    assert _sell_size(10, 0.05, 0.5, 200.0, 20_000, "US") == (5, "Trim 비중 10.0% → 5.0% (10주 → 5주) · 평가 $2,000 → $1,000")
    # Underweight: half rounded down, one share held → that share
    assert _sell_size(999, None, 0.5, None, 0.0, "KRX") == (499, "Underweight 보유의 50% 매도 (999주 → 500주)")
    assert _sell_size(3, None, 0.5, None, 0.0, "KRX")[0] == 1
    assert _sell_size(100, None, 0.29, None, 0.0, "KRX")[0] == 29            # 100 × 0.29 = 28.999999999999996
    # a whole-holding Underweight's words: _place orders it as a Sell instead
    # (test_an_underweight_of_one_share_sells_that_share)
    assert _sell_size(1, None, 0.5, None, 0.0, "KRX") == (1, "Underweight 보유 1주 전량 매도 (1주 보유)")


@pytest.mark.unit
@pytest.mark.parametrize("store", ["sqlite", "jsonl"])
def test_a_trim_sells_down_to_its_target_and_keeps_the_plan(audit, monkeypatch, store):
    monkeypatch.setenv("TRADING_STORE", store)
    prices = _prices(A_KS=_flat())
    _held(audit, prices)
    # 06-02 close: equity = 90,003,505.75 + 999 × 10,000 = 99,993,505.75; A is 9.99%. 5% keeps 499.97 → sell 499
    _trim(audit, "A.KS", "2026-06-02", 5)
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert (o.side, o.quantity, o.status, o.filled_quantity, o.fill_date) == ("Sell", 499, "filled", 499, "2026-06-04")
    assert o.reason.startswith("모의 주문: Trim 비중 10.0% → 5.0% (999주 → 500주) · 평가 999만 → 500만 · 다음 거래일 시가")
    p = o.plan
    assert (p.proposed.side, p.proposed.qty, p.proposed.amount) == ("Sell", 999, 9_990_000)
    assert (p.final.side, p.final.qty, p.final.amount) == ("Sell", 499, 4_990_000) and p.interventions == []
    assert p.final.weight_pct == pytest.approx(4_990_000 / (CASH + 9_990_000))
    (row,) = positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)["positions"]
    assert (row["quantity"], row["partial_sold"]) == (500, 499)


@pytest.mark.unit
def test_a_trim_at_or_over_the_current_weight_places_nothing(audit):
    prices = _prices(A_KS=_flat())
    _held(audit, prices)
    _trim(audit, "A.KS", "2026-06-02", 20)                                   # 20% ≥ 9.99%
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert (o.status, o.quantity, o.rules_applied) == ("blocked", 0, ["trim_at_target"])
    assert o.reason == "Trim 비중 10.0% → 20.0% (999주 → 999주) · 평가 999만 → 999만 — 줄일 수량 없음 (보유 999주 그대로)"
    assert (o.plan.proposed.qty, o.plan.final.side, o.plan.final.qty) == (999, "Hold", 0)
    assert positions_view(audit.orders(), prices, as_of="2026-06-08", risk=RISK)["positions"][0]["quantity"] == 999


@pytest.mark.unit
def test_a_trim_to_zero_and_a_sell_close_the_whole_holding(audit):
    prices = _prices(A_KS=_flat(), B_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    _decide(audit, "B.KS", "2026-06-01")                                     # 999 each: 50% capped at 10%
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    _trim(audit, "A.KS", "2026-06-02", 0)
    _decide(audit, "B.KS", "2026-06-02", "Sell")
    by = {o.ticker: o for o in execute_date(audit, "2026-06-02", prices, risk=RISK)}
    assert by["A.KS"].quantity == by["A.KS"].filled_quantity == 999 and by["A.KS"].plan.final.qty == 999
    assert by["A.KS"].reason.startswith("모의 주문: Trim 비중 10.0% → 0.0% (999주 → 0주)")
    # Sell is the order it always was: the whole holding, no plan
    assert (by["B.KS"].quantity, by["B.KS"].plan) == (999, None)
    assert by["B.KS"].reason.startswith("모의 주문: 보유 999주 전량 매도 · 다음 거래일 시가")
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=RISK)
    assert view["positions"] == [] and [c["full"] for c in view["closed"]] == [True, True]


@pytest.mark.unit
def test_an_underweight_sells_half_rounded_down(audit, half):
    prices = _prices(A_KS=_flat())
    _held(audit, prices)
    _decide(audit, "A.KS", "2026-06-02", "Underweight")
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert (o.quantity, o.filled_quantity) == (499, 499)                     # 999 × 0.5 = 499.5 → 499
    assert o.reason.startswith("모의 주문: Underweight 보유의 50% 매도 (999주 → 500주) · 다음 거래일 시가")
    assert (o.plan.proposed.qty, o.plan.final.qty) == (999, 499)
    (row,) = positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)["positions"]
    assert row["quantity"] == 500


@pytest.mark.unit
def test_an_underweight_of_one_share_sells_that_share(audit, half):
    # 10% of 100M at 6,003,900 a share (with slippage and commission) is one share
    prices = _prices(A_KS=_flat(6_000_000))
    assert _held(audit, prices).filled_quantity == 1
    _decide(audit, "A.KS", "2026-06-02", "Underweight")
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert (o.quantity, o.filled_quantity, o.plan) == (1, 1, None) and o.reason.startswith("모의 주문: 보유 1주 전량 매도")
    assert positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)["positions"] == []


@pytest.mark.unit
def test_the_rest_keeps_its_entry_price_trailing_high_and_stop(audit, half):
    # +15% close on 06-02 turns the trailing on; the Trim fills at the 06-04 open; 06-04 closes inside the band
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 11_500), "2026-06-04": (11_400, 11_400),
                "2026-06-05": (11_400, 11_400), "2026-06-08": (11_400, 11_400)})
    prices = _prices(A_KS=a)
    _held(audit, prices)
    unsold = audit.orders()
    _decide(audit, "A.KS", "2026-06-02", "Underweight")
    (o,) = execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert o.filled_quantity == 499 and o.fill_date == "2026-06-04"
    (before,) = positions_view(unsold, prices, as_of="2026-06-04", risk=RISK)["positions"]   # the same day, unsold
    (after,) = positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)["positions"]
    assert after["quantity"] == 500
    keep = ("avg_price", "entry_date", "stop_price", "trailing_high", "trailing_active", "trailing_stop_price")
    assert {k: after[k] for k in keep} == {k: before[k] for k in keep}
    assert after["avg_price"] == pytest.approx(BUY_PX) and after["stop_price"] == pytest.approx(BUY_PX * 0.92)
    assert after["trailing_high"] == 11_500 and after["trailing_active"] is True
    assert after["holding_days"] == before["holding_days"] == 1              # counted from 06-02, not the sale


@pytest.mark.unit
def test_a_stop_loss_after_a_partial_sell_sells_the_rest_at_the_original_stop_line(audit, half):
    # stop line 10,005 × 0.92 = 9,204.6; 06-05 closes at 9,100 → rule exit, filled at the 06-08 open
    a = _frame({"2026-06-01": (10_000, 10_000), "2026-06-02": (10_000, 10_000), "2026-06-04": (10_000, 9_900),
                "2026-06-05": (9_500, 9_100), "2026-06-08": (9_000, 9_000)})
    prices = _prices(A_KS=a)
    _held(audit, prices)
    _decide(audit, "A.KS", "2026-06-02", "Underweight")
    execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert execute_date(audit, "2026-06-04", prices, risk=RISK) == []        # 9,900 is above 9,204.6
    (x,) = execute_date(audit, "2026-06-05", prices, risk=RISK)
    assert (x.order_id, x.rules_applied, x.quantity, x.filled_quantity) == ("paper-2026-06-05-A.KS-exit", ["stop_loss"], 500, 500)
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=RISK)
    assert view["positions"] == [] and view["exits_by_reason"] == {"stop_loss": 1}     # the partial sell is no exit
    assert [(c["quantity"], c["reason"], c["full"]) for c in view["closed"]] == [(500, "stop_loss", True),
                                                                                  (499, "exit_signal", False)]


@pytest.mark.unit
def test_a_partial_sell_locks_nothing_and_the_full_close_after_it_does(audit, half):
    prices = _prices(A_KS=_flat())
    _held(audit, prices)
    _decide(audit, "A.KS", "2026-06-02", "Underweight")
    execute_date(audit, "2026-06-02", prices, risk=RISK)                     # 499 sold at the 06-04 open
    view = positions_view(audit.orders(), prices, as_of="2026-06-04", risk=RISK)
    assert _guard(view, "Cooldown")["active"] is False and _guard(view, "Cooldown")["tickers"] == []
    _decide(audit, "A.KS", "2026-06-04", "Sell")
    (s,) = execute_date(audit, "2026-06-04", prices, risk=RISK)              # the other 500, at the 06-05 open
    assert (s.quantity, s.filled_quantity) == (500, 500)
    view = positions_view(audit.orders(), prices, as_of="2026-06-05", risk=RISK)
    assert _guard(view, "Cooldown")["active"] is True
    _decide(audit, "A.KS", "2026-06-05")
    (b,) = execute_date(audit, "2026-06-05", prices, risk=RISK)
    assert b.status == "blocked" and b.rules_applied == ["Cooldown"]


@pytest.mark.unit
def test_an_underweight_at_the_default_sells_the_whole_holding_as_a_sell_does(audit):
    """fraction 1.0 (the default since 2026-11-23): a memo's Underweight is the order a Sell places — the whole
    holding, the Sell's words, no plan — and its close is a full one that locks the name (Cooldown), as every
    Underweight was before partial sells (develop 4bd1181^: SELL_RATINGS = Sell, Underweight)."""
    assert AUTO_EXECUTION["underweight_sell_fraction"] == 1.0
    prices = _prices(A_KS=_flat(), B_KS=_flat())
    _decide(audit, "A.KS", "2026-06-01")
    _decide(audit, "B.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    _decide(audit, "A.KS", "2026-06-02", "Underweight")
    _decide(audit, "B.KS", "2026-06-02", "Sell")
    by = {o.ticker: o for o in execute_date(audit, "2026-06-02", prices, risk=RISK)}
    keep = ("side", "quantity", "status", "reason", "plan", "rules_applied", "filled_quantity", "fill_date", "filled_price")
    assert {k: getattr(by["A.KS"], k) for k in keep} == {k: getattr(by["B.KS"], k) for k in keep}
    assert (by["A.KS"].quantity, by["A.KS"].plan) == (999, None)
    assert by["A.KS"].reason.startswith("모의 주문: 보유 999주 전량 매도 · 다음 거래일 시가 → 모의 체결")
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=RISK)
    assert view["positions"] == [] and sorted((c["ticker"], c["full"]) for c in view["closed"]) == [("A.KS", True), ("B.KS", True)]
    assert _guard(view, "Cooldown")["active"] is True and sorted(x["ticker"] for x in _guard(view, "Cooldown")["tickers"]) == ["A.KS", "B.KS"]
