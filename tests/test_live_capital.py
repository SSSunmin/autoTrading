"""Capital set by the admin (docs/10 D5, live-capital): replayed from ``capital_set`` operations — no record, the book
is the one it was; a change before any order is the starting cash, one after is a cash flow at that day's close and
the returns, the drawdown and the paper gate leave it out (time-weighted, by hand); the small-account mode holds one
name; the API's validation, token, replay and conflicts under both stores; the preview and the model settings."""
from __future__ import annotations

import importlib
import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import DecisionRecord, OperationRecord, OrderRecord, market_audit
from agent_trading_core.execution import (
    PaperConfig,
    Prices,
    execute_date,
    paper_config,
    paper_progress,
    positions_view,
    with_capital,
)
from agent_trading_core.risk import RiskConfig, drawdown_scale
from tests.test_market_axis import _cache

D = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]


def _frame(rows: dict[str, tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame({"Open": [o for o, _ in rows.values()], "Close": [c for _, c in rows.values()]},
                        index=pd.Index(list(rows), dtype=object))


def _capital(amount: float, change: str, day: str, flow: float | None = None, market: str = "US") -> OperationRecord:
    return OperationRecord(kind="capital_set", operator="관리자", role="admin", reason="자본", market=market,
                           target="paper", before={}, effective_from=day,
                           after={"account": "paper", "amount": amount, "change": change} | ({"flow": flow} if flow else {}))


CFG = PaperConfig(initial_cash=999.0, commission=0.0, slippage=0.0, sell_tax=0.0, regime_filter=False, index="^IDX",
                  market="US")
BUY = OrderRecord(order_id="paper-a", ticker="A", trade_date=D[0], side="Buy", quantity=50, filled_quantity=50,
                  filled_price=100.0, status="filled", fill_date=D[0], market="US")


def _book(closes: list[float]) -> Prices:
    return Prices(frames={"^IDX": _frame({d: (1.0, 1.0) for d in D}),
                          "A": _frame({d: (c, c) for d, c in zip(D, closes, strict=True)})})


# --- replay ----------------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_no_capital_record_leaves_the_config_as_it_was():
    ops = [OperationRecord(kind="halt", operator="a", role="operator", reason="r", market="US")]
    assert with_capital(CFG, ops) is CFG and with_capital(CFG, []) is CFG
    cfg = paper_config("KRX")
    assert with_capital(cfg, ops) is cfg and cfg.cash_flows == () and cfg.capital_on("2099-01-01") == cfg.initial_cash


@pytest.mark.unit
def test_a_deposit_while_running_is_neither_a_gain_nor_a_drawdown_by_hand():
    """10,000 start, 50 A at 100 on D1; A closes 100 · 120 · 90 · 108 · 99; +5,000 deposited at D3's close.
    By hand: equity 10,000 · 11,000 · 14,500 (9,500 + 5,000) · 15,400 · 14,950. Day returns leave D3's deposit out:
    +10% · 9,500/11,000 − 1 · 15,400/14,500 − 1 · 14,950/15,400 − 1. The unit value (start money) is the running
    product: 11,000 · 9,500 · then × 15,400/14,500 · × 14,950/15,400; its peak is D2's 11,000."""
    ops = [_capital(10_000.0, "start", D[0]), _capital(15_000.0, "flow", D[2], flow=5_000.0)]
    prices = _book([100.0, 120.0, 90.0, 108.0, 99.0])
    nav5 = 10_000 * 1.10 * (9_500 / 11_000) * (15_400 / 14_500) * (14_950 / 15_400)

    v = positions_view([BUY], prices, as_of=D[4], cfg=CFG, operations=ops)
    assert (v["initial_cash"], v["capital"], v["cash_flows"]) == (10_000.0, 15_000.0, [{"date": D[2], "amount": 5_000.0}])
    assert v["equity"] == pytest.approx(14_950.0) and v["cash"] == pytest.approx(10_000.0)
    assert v["drawdown"] == pytest.approx(nav5 / 11_000 - 1)                     # −10.96%, not 14,950/15,400 − 1
    assert v["day_return"] == pytest.approx(14_950 / 15_400 - 1)
    assert drawdown_scale(v["equity"], v["peak"], RiskConfig())[0] == 0.5         # past dd_halve: the deposit hid none of it
    v3 = positions_view([BUY], prices, as_of=D[2], cfg=CFG, operations=ops)
    assert v3["day_return"] == pytest.approx(9_500 / 11_000 - 1) and v3["drawdown"] == pytest.approx(9_500 / 11_000 - 1)

    p = paper_progress([BUY], [], ops, prices, CFG, as_of=D[4])
    assert p["paper"]["period_return"] == pytest.approx(nav5 / 10_000 - 1)        # −2.05%, not +49.5%
    assert p["drawdown"]["min"] == pytest.approx(9_500 / 11_000 - 1) and p["drawdown"]["min_date"] == D[2]
    assert [r["equity"] for r in p["series"]] == pytest.approx([10_000, 11_000, 14_500, 15_400, 14_950])
    assert p["paper"]["daily_loss_dates"] == [D[2], D[4]]                         # real losses only
    assert (p["initial_cash"], p["capital"]) == (10_000.0, 15_000.0)


@pytest.mark.unit
def test_a_withdrawal_on_a_flat_book_is_no_loss_and_no_breach():
    ops = [_capital(10_000.0, "start", D[0]), _capital(8_000.0, "flow", D[2], flow=-2_000.0)]
    prices = _book([100.0] * 5)
    v = positions_view([BUY], prices, as_of=D[2], cfg=CFG, operations=ops)
    assert (v["equity"], v["cash"], v["day_return"], v["drawdown"]) == pytest.approx((8_000.0, 3_000.0, 0.0, 0.0))
    p = paper_progress([BUY], [], ops, prices, CFG, as_of=D[4])
    assert p["paper"]["daily_loss_dates"] == [] and p["paper"]["period_return"] == pytest.approx(0.0)
    assert p["drawdown"]["min"] == pytest.approx(0.0)


@pytest.mark.unit
def test_a_flow_past_the_prices_counts_at_the_last_close():
    """A deposit dated after the cache's last day (the request came before today's refresh): cash and units at once."""
    ops = [_capital(10_000.0, "start", D[0]), _capital(12_000.0, "flow", "2026-06-08", flow=2_000.0)]
    v = positions_view([BUY], _book([100.0, 110.0, 110.0, 110.0, 110.0]), as_of="2026-06-08", cfg=CFG, operations=ops)
    assert v["equity"] == pytest.approx(10_500.0 + 2_000.0) and v["drawdown"] == pytest.approx(0.0)


@pytest.mark.unit
def test_a_flow_that_takes_the_book_to_zero_does_not_break_the_replay():
    """Review 1R #1: the API refuses it, but a recorded one must not divide by zero — units stay, the book reads."""
    sold = OrderRecord(order_id="paper-a-exit", ticker="A", trade_date=D[1], side="Sell", quantity=50, filled_quantity=50,
                       filled_price=100.0, status="filled", fill_date=D[1], market="US")
    ops = [_capital(10_000.0, "start", D[0]), _capital(0.01, "flow", D[2], flow=-10_000.0)]
    v = positions_view([BUY, sold], _book([100.0] * 5), as_of=D[4], cfg=CFG, operations=ops)
    assert abs(v["equity"]) < 1.0 and v["holdings_value"] == 0.0          # the US sell fees leave a few cents short
    p = paper_progress([BUY, sold], [], ops, _book([100.0] * 5), CFG, as_of=D[4])
    assert math.isfinite(p["paper"]["period_return"]) and math.isfinite(p["drawdown"]["min"])


# --- the executor ------------------------------------------------------------------------------------------------

def _krx_prices() -> Prices:
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    return Prices(frames={"^KS11": _frame({d: (100.0 + i, 100.0 + i) for i, d in enumerate(days)}),
                          "A.KS": _frame({d: (10_000.0, 10_000.0) for d in days}),
                          "B.KS": _frame({d: (10_000.0, 10_000.0) for d in days})})


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    return request.param


def _krx_book(tmp_path, start: float | None):
    a = market_audit(tmp_path / "audit", "KRX")
    if start is not None:
        a.record_operation(_capital(start, "start", "2026-06-01", market="KRX"))
    for t in ("A.KS", "B.KS"):
        a.record_decision(DecisionRecord(ticker=t, trade_date="2026-06-02", rating="Buy", thesis="Buy"))
    return a


@pytest.mark.unit
def test_the_executor_sizes_on_the_capital_the_admin_set(store, tmp_path):
    unit = 10_000.0 * 1.0005 * 1.00015
    for start, expect in ((None, int(0.10 * 100_000_000 // unit)), (50_000_000.0, int(0.10 * 50_000_000 // unit))):
        a = _krx_book(tmp_path / str(start), start)
        out = {o.ticker: o for o in execute_date(a, "2026-06-02", _krx_prices(), risk=RiskConfig(regime_ma_days=2))}
        assert out["A.KS"].quantity == expect and out["B.KS"].quantity == expect


@pytest.mark.unit
def test_small_account_buys_the_first_name_with_all_of_it_and_blocks_the_rest(store, tmp_path):
    a = _krx_book(tmp_path, 500_000.0)                                  # under 100만원
    out = {o.ticker: o for o in execute_date(a, "2026-06-02", _krx_prices(), risk=RiskConfig(regime_ma_days=2))}
    assert out["A.KS"].quantity == int(500_000 // (10_000.0 * 1.0005 * 1.00015)) == 49
    assert out["A.KS"].plan.proposed.weight_pct == 1.0
    assert (out["B.KS"].status, out["B.KS"].rules_applied) == ("blocked", ["small_account"])
    a.record_decision(DecisionRecord(ticker="B.KS", trade_date="2026-06-03", rating="Buy", thesis="Buy"))
    again = {o.ticker: o for o in execute_date(a, "2026-06-03", _krx_prices(), risk=RiskConfig(regime_ma_days=2))}
    assert again["B.KS"].rules_applied == ["small_account"] and "이미 A.KS 1종목" in again["B.KS"].reason


@pytest.mark.unit
def test_small_account_holds_a_retry_to_the_one_name_too(store, tmp_path):
    """Review 1R #4: B failed (no 06-02 open) and was set to retry; on 06-02 A is the day's buy — B stays blocked."""
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    prices = Prices(frames={"^KS11": _frame({d: (100.0 + i, 100.0 + i) for i, d in enumerate(days)}),
                            "A.KS": _frame({d: (10_000.0, 10_000.0) for d in days}),
                            "B.KS": _frame({d: (10_000.0, 10_000.0) for d in ("2026-06-01", "2026-06-03")})})
    a = market_audit(tmp_path / "audit", "KRX")
    a.record_operation(_capital(500_000.0, "start", "2026-06-01", market="KRX"))
    a.record_decision(DecisionRecord(ticker="B.KS", trade_date="2026-06-01", rating="Buy", thesis="Buy"))
    (b,) = execute_date(a, "2026-06-01", prices, risk=RiskConfig(regime_ma_days=2))
    assert (b.status, b.exception_kind) == ("exception", "vendor_unavailable")
    a.record_order(b.model_copy(update={"exception_action": "retry_next", "retries": 1}))
    a.record_decision(DecisionRecord(ticker="A.KS", trade_date="2026-06-02", rating="Buy", thesis="Buy"))
    out = {o.ticker: o for o in execute_date(a, "2026-06-02", prices, risk=RiskConfig(regime_ma_days=2))}
    assert out["A.KS"].status in ("filled", "open") and out["A.KS"].quantity > 0
    assert (out["B.KS"].status, out["B.KS"].rules_applied) == ("blocked", ["small_account"])


@pytest.mark.unit
def test_small_account_takes_one_of_two_retries_in_the_same_run(store, tmp_path):
    """Review 2R #7: B and C both failed on 06-01 (no 06-02 open, 5,000,000 — not small) and were set to retry; a
    withdrawal to 500,000 dated 06-02 makes the book small. Nothing held, no buy that day: the 06-02 run fills one
    retry and blocks the other (the retries' decisions are the day's buy list, cut to its first name)."""
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    gap = {d: (10_000.0, 10_000.0) for d in ("2026-06-01", "2026-06-03")}
    prices = Prices(frames={"^KS11": _frame({d: (100.0 + i, 100.0 + i) for i, d in enumerate(days)}),
                            "B.KS": _frame(gap), "C.KS": _frame(gap)})
    a = market_audit(tmp_path / "audit", "KRX")
    a.record_operation(_capital(5_000_000.0, "start", "2026-06-01", market="KRX"))
    for t in ("B.KS", "C.KS"):
        a.record_decision(DecisionRecord(ticker=t, trade_date="2026-06-01", rating="Buy", thesis="Buy"))
    failed = execute_date(a, "2026-06-01", prices, risk=RiskConfig(regime_ma_days=2))
    assert [(o.ticker, o.status) for o in failed] == [("B.KS", "exception"), ("C.KS", "exception")]
    for o in failed:
        a.record_order(o.model_copy(update={"exception_action": "retry_next", "retries": 1}))
    a.record_operation(_capital(500_000.0, "flow", "2026-06-02", flow=-4_500_000.0, market="KRX"))
    out = execute_date(a, "2026-06-02", prices, risk=RiskConfig(regime_ma_days=2))
    assert sorted((o.status, o.rules_applied == ["small_account"]) for o in out) == [("blocked", True), ("filled", False)]


# --- the API -------------------------------------------------------------------------------------------------------

def _client(data, monkeypatch, token: str | None = None):
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    if token:
        monkeypatch.setenv("TRADING_ADMIN_TOKEN", token)
    else:
        monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), main


@pytest.fixture
def data(store, tmp_path):
    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    for index in ("^KS11", "^GSPC"):
        _cache(data, index, [(d, 100 + i, 100 + i) for i, d in enumerate(days)])
    for t, px in (("005930.KS", 80_000), ("000660.KS", 1_200_000), ("AAPL", 200), ("BRK-A", 700_000)):
        _cache(data, t, [(d, px, px) for d in days])
    return data


def _post(api, market, **body):
    return api.post("/api/capital", params={"market": market},
                    json={"operator": "관리자", "reason": "자본 설정"} | body)


@pytest.mark.unit
def test_capital_input_is_validated_before_anything_is_written(data, monkeypatch):
    api, main = _client(data, monkeypatch)
    for market, amount in (("KRX", 0), ("KRX", -1), ("KRX", 1.5), ("US", 1.005), ("KRX", 100_000_000_001),
                           ("US", 100_000_001)):
        r = _post(api, market, account="paper", amount=amount)
        assert r.status_code == 422, (market, amount, r.text)
    assert _post(api, "KRX", account="cash", amount=1_000_000).status_code == 422
    assert _post(api, "KRX", account="paper", amount=1_000_000, reason="").status_code == 422
    assert _post(api, "XX", account="paper", amount=1_000_000).status_code == 400
    assert not any(op.kind == "capital_set" for m in ("KRX", "US") for op in main.STORES[m].refresh().operations)


@pytest.mark.unit
def test_the_admin_token_guards_the_write(data, monkeypatch):
    api, main = _client(data, monkeypatch, token="t0k")
    assert _post(api, "KRX", account="paper", amount=50_000_000).status_code == 403
    r = api.post("/api/capital", params={"market": "KRX"}, headers={"X-Admin-Token": "wrong"},
                 json={"operator": "관리자", "reason": "자본", "account": "paper", "amount": 50_000_000})
    assert r.status_code == 403 and main.STORES["KRX"].refresh().operations == []
    ok = api.post("/api/capital", params={"market": "KRX"}, headers={"X-Admin-Token": "t0k"},
                  json={"operator": "관리자", "reason": "자본", "account": "paper", "amount": 50_000_000})
    assert ok.status_code == 200 and ok.json()["capital"]["paper"]["set"]["role"] == "admin"


@pytest.mark.unit
def test_a_book_without_orders_takes_a_new_starting_cash_and_replays_a_repeat(data, monkeypatch):
    api, main = _client(data, monkeypatch)
    before = api.get("/api/capital", params={"market": "KRX"}).json()
    assert (before["paper"]["amount"], before["paper"]["source"], before["live"]["amount"]) == (100_000_000.0, "default", None)
    assert before["next_paper_change"] == "start"
    r = _post(api, "KRX", account="paper", amount=50_000_000)
    assert r.status_code == 200 and (r.json()["change"], r.json()["replayed"]) == ("start", False)
    again = _post(api, "KRX", account="paper", amount=50_000_000)
    assert again.json()["replayed"] is True
    assert sum(op.kind == "capital_set" for op in main.STORES["KRX"].refresh().operations) == 1
    assert _post(api, "KRX", account="paper", amount=50_000_000, reason="다른 사유").status_code == 409
    pos = api.get("/api/positions", params={"market": "KRX"}).json()
    assert (pos["initial_cash"], pos["capital"], pos["equity"], pos["small_account"]) == (50_000_000.0, 50_000_000.0,
                                                                                         50_000_000.0, False)
    cap = api.get("/api/capital", params={"market": "KRX"}).json()
    assert (cap["paper"]["start"], cap["paper"]["source"], cap["cash_flows"]) == (50_000_000.0, "record", [])
    us = api.get("/api/capital", params={"market": "US"}).json()
    assert us["paper"]["amount"] == 100_000.0                                        # the other book is its own


@pytest.mark.unit
def test_a_running_book_takes_a_change_as_a_cash_flow_and_refuses_a_withdrawal_past_its_cash(data, monkeypatch):
    api, _ = _client(data, monkeypatch)
    a = market_audit(data / "audit", "US")
    a.record_order(OrderRecord(order_id="paper-2026-06-01-AAPL", ticker="AAPL", trade_date="2026-06-01", side="Buy",
                               quantity=400, filled_quantity=400, filled_price=200.0, status="filled",
                               fill_date="2026-06-02", market="US"))
    assert api.get("/api/capital", params={"market": "US"}).json()["next_paper_change"] == "flow"
    r = _post(api, "US", account="paper", amount=150_000)
    body = r.json()
    assert r.status_code == 200 and (body["change"], body["flow"]) == ("flow", 50_000.0)
    flows = body["capital"]["cash_flows"]
    assert [(f["date"], f["amount"]) for f in flows] == [(body["effective_from"], 50_000.0)]
    assert body["capital"]["paper"]["start"] == 100_000.0 and body["capital"]["paper"]["amount"] == 150_000.0
    # cash: 100,000 − 80,000 (commission 0) at the last close, + the 50,000 dated today (pending: after the cache's
    # last day) = 70,000 — a 70,000.01 withdrawal is too much
    assert [f["pending"] for f in flows] == [True]
    assert _post(api, "US", account="paper", amount=79_999.99).status_code == 409
    assert _post(api, "US", account="paper", amount=80_000).json()["flow"] == -70_000.0


@pytest.mark.unit
def test_a_withdrawal_of_the_whole_book_or_past_the_pending_buys_is_refused(data, monkeypatch):
    """Review 1R #1 · #2. 100 AAPL bought at 200, sold at 150: all cash, about 95,000 less the US sell fees, capital
    100,000. An open buy of 100 at the 200 close reserves 20,000 × (1 + slippage 0.05%) = 20,010: a withdrawal to
    25,000 is past the cash left. With it cancelled, a withdrawal to the cent below 100,000 − cash takes the book
    under zero — refused; the cent above leaves it a little over zero — taken."""
    api, main = _client(data, monkeypatch)
    a = market_audit(data / "audit", "US")
    for side, day, px in (("Buy", "2026-06-01", 200.0), ("Sell", "2026-06-02", 150.0)):
        a.record_order(OrderRecord(order_id=f"paper-{day}-AAPL", ticker="AAPL", trade_date=day, side=side, quantity=100,
                                   filled_quantity=100, filled_price=px, status="filled", fill_date=day, market="US"))
    cash = api.get("/api/positions", params={"market": "US"}).json()["cash"]
    assert 94_990 < cash <= 95_000
    pending = OrderRecord(order_id="paper-2026-06-03-AAPL", ticker="AAPL", trade_date="2026-06-03", side="Buy",
                          quantity=100, status="open", market="US")
    a.record_order(pending)
    r = _post(api, "US", account="paper", amount=25_000)
    assert r.status_code == 409 and "체결 대기 매수 $20,010.00 제외" in r.json()["detail"]
    a.record_order(pending.model_copy(update={"status": "cancelled"}))
    below, above = math.floor((100_000 - cash) * 100) / 100, math.ceil((100_000 - cash) * 100) / 100
    assert _post(api, "US", account="paper", amount=below).status_code == 409
    assert not any(op.kind == "capital_set" for op in main.STORES["US"].refresh().operations)
    r = _post(api, "US", account="paper", amount=above)
    assert r.status_code == 200 and r.json()["flow"] == pytest.approx(above - 100_000)


@pytest.mark.unit
def test_the_live_capital_is_stored_shown_and_used_by_the_go_live_modal(data, monkeypatch):
    api, _ = _client(data, monkeypatch)
    r = _post(api, "KRX", account="live", amount=500_000)
    assert r.status_code == 200 and r.json()["change"] == "live"
    cap = api.get("/api/capital", params={"market": "KRX"}).json()
    assert (cap["live"]["amount"], cap["live"]["small_account"], cap["paper"]["small_account"]) == (500_000.0, True, False)
    g = api.get("/api/paper/progress", params={"market": "KRX"}).json()["go_live"]
    assert (g["capital"], g["capital_source"], g["daily_loss_amount"]) == (500_000.0, "live", 500_000.0 * -0.02)
    assert api.get("/api/trading/mode", params={"market": "KRX"}).json()["mode"] == "paper"   # no account switch


@pytest.mark.unit
def test_the_preview_counts_what_this_capital_can_buy(data, monkeypatch):
    api, _ = _client(data, monkeypatch)
    q = {"market": "KRX", "amount": 5_000_000, "universe": "custom", "tickers": "005930.KS, 000660.KS 035720.KS"}
    p = api.get("/api/capital/preview", params=q).json()
    # 10% of 5,000,000 = 500,000 per name: 005930 at 80,000 fits, 000660 at 1,200,000 does not, 035720 has no price
    assert (p["buyable"], p["universe"]["priced"], p["universe"]["missing"]) == (1, 2, ["035720.KS"])
    assert p["cheapest"]["ticker"] == "005930.KS" and p["over_order_cap"] == []
    rows = {r["id"]: r for r in p["rules"]}
    assert rows["max_position_pct"]["amount"] == 500_000 and rows["auto_order_max"]["amount"] == 5_000_000
    assert rows["dd_stop"]["amount"] == pytest.approx(4_000_000) and rows["buyable"]["value_ko"] == "1 / 2"
    small = api.get("/api/capital/preview", params=q | {"amount": 900_000}).json()
    assert small["small_account"] is True and {r["id"]: r for r in small["rules"]}["max_position_pct"]["amount"] == 900_000
    none = {r["id"]: r for r in api.get("/api/capital/preview", params=q | {"amount": 50_000}).json()["rules"]}
    assert (none["max_holdings"]["value_ko"], none["max_holdings"]["warn"]) == ("0종목", True)   # review 1R #5
    us = api.get("/api/capital/preview", params={"market": "US", "amount": 1_000_000, "universe": "custom",
                                                 "tickers": "AAPL,BRK-A"}).json()
    assert [x["ticker"] for x in us["over_order_cap"]] == ["BRK-A"] and us["buyable"] == 1   # $700,000 > $20,000
    assert api.get("/api/capital/preview", params={"market": "KRX", "universe": "custom"}).status_code == 422
    assert api.get("/api/capital/preview", params={"market": "KRX", "account": "live"}).status_code == 422
    assert api.get("/api/capital/preview", params=q | {"amount": -5}).status_code == 422


@pytest.mark.unit
def test_the_model_settings_are_the_config_and_say_only_whether_the_key_is_set(data, monkeypatch, tmp_path):
    from agent_trading_core.config import LLM

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-not-a-real-value\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ENV_FILE", str(env))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TRADING_LLM_TRIAL", raising=False)
    api, _ = _client(data, monkeypatch)
    r = api.get("/api/model", params={"market": "KRX"})
    m = r.json()
    assert (m["model"], m["effort"], m["max_calls_per_run"]) == (LLM["model"], LLM["effort"], LLM["max_calls_per_run"])
    assert m["decider"] == ("llm" if LLM["enabled"] else "rule") and m["keys"] == {"ANTHROPIC_API_KEY": True}
    assert m["trial_mode"] is bool(LLM["trial"]) and (m["trial"] is None) is (not LLM["trial"])
    assert "sk-not-a-real-value" not in r.text


@pytest.mark.unit
def test_the_model_settings_follow_the_loops_llm_trial(data, monkeypatch):
    """Review 1R #3: the trial is the loop's (TRADING_LLM_TRIAL=1 · daily_loop.default_decider), not LLM["trial"] alone —
    the model decides while trial days are left, then the rule."""
    from agent_trading_core.config import LLM

    monkeypatch.setitem(LLM, "enabled", False)
    monkeypatch.setenv("TRADING_LLM_TRIAL", "1")
    api, _ = _client(data, monkeypatch)
    m = api.get("/api/model", params={"market": "US"}).json()
    assert (m["trial_mode"], m["decider"], m["decider_id"]) == (True, "llm", LLM["model"])
    assert m["trial"] == {"sessions": LLM["trial_sessions"], "days": [], "remaining": LLM["trial_sessions"]}
    monkeypatch.setitem(LLM, "trial_sessions", 0)
    m = api.get("/api/model", params={"market": "US"}).json()
    assert (m["decider"], m["decider_id"], m["trial"]["remaining"]) == ("rule", "rule_momentum_baseline", 0)


@pytest.mark.unit
def test_the_status_label_names_both_axes(data, monkeypatch):
    api, _ = _client(data, monkeypatch)
    s = api.get("/api/trading/mode", params={"market": "US"}).json()
    assert (s["account_mode"], s["execution_mode"], s["label_ko"]) == ("paper", "auto", "미국 모의투자 · 자동 실행 중")
    rules = api.get("/api/settings/rules", params={"market": "KRX"}).json()
    assert rules["screen_params_by_market"]["US"]["price_min"] != rules["screen_params_by_market"]["KRX"]["price_min"]
    assert rules["regime_index"]["US"]["benchmark"] == "^GSPC"
