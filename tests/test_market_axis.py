"""KRX and US are separate books (docs/08): their own logs, calendars, currencies, caps and trading state."""

from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import DecisionRecord, OperationRecord, OrderRecord, market_audit, trading_state
from agent_trading_core.execution import (
    ExecutionRefused,
    Prices,
    execute_date,
    fmt_money,
    paper_config,
    positions_view,
)
from agent_trading_core.risk import RiskConfig

RISK = RiskConfig(regime_ma_days=3)
SLIP = 0.0005


def _frame(rows: dict[str, tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame({"Open": [o for o, _ in rows.values()], "Close": [c for _, c in rows.values()]},
                        index=pd.Index(list(rows), dtype=object))


def _rising(days):
    return _frame({d: (100.0 + i, 100.0 + i) for i, d in enumerate(days)})


def _flat(days, price):
    return _frame({d: (price, price) for d in days})


def _decide(audit, ticker, day, rating="Buy", market="KRX"):
    audit.record_decision(DecisionRecord(ticker=ticker, trade_date=day, rating=rating, thesis=rating, market=market))


@pytest.fixture
def root(tmp_path):
    return tmp_path / "audit"


# --- the books do not mix -------------------------------------------------------------------------

@pytest.mark.unit
def test_a_record_of_one_market_cannot_be_written_to_or_read_from_the_other_book(jsonl_store, root):
    us = market_audit(root, "US")
    with pytest.raises(ValueError, match="KRX 기록이 US 장부"):
        us.record_order(OrderRecord(order_id="x", ticker="AAPL", trade_date="2026-06-01", side="Buy", quantity=1,
                                    status="open"))                       # market left at its KRX default
    assert not (root / "US" / "orders.jsonl").exists()
    # a KRX line that reached the US file by hand is a broken book, not a silent KRX order
    (root / "US" / "orders.jsonl").write_text(json.dumps({"order_id": "y", "ticker": "AAPL", "trade_date": "2026-06-01",
                                                          "side": "Buy", "quantity": 1, "status": "open"}) + "\n",
                                              encoding="utf-8")
    with pytest.raises(ValueError, match="KRX 기록이 US 장부"):
        us.orders()


@pytest.mark.unit
def test_krx_reads_the_files_from_before_the_market_axis_as_krx_and_writes_beside_them(jsonl_store, root):
    root.mkdir(parents=True)
    old = json.dumps({"order_id": "paper-2026-06-01-005930.KS", "ticker": "005930.KS", "trade_date": "2026-06-01",
                      "side": "Buy", "quantity": 3, "status": "open"}) + "\n"   # no market field: written before the axis
    (root / "orders.jsonl").write_text(old, encoding="utf-8")
    krx, us = market_audit(root, "KRX"), market_audit(root, "US")
    (o,) = krx.orders()
    assert o.market == "KRX" and us.orders() == []
    krx.record_order(o.model_copy(update={"status": "cancelled"}))
    assert (root / "orders.jsonl").read_text(encoding="utf-8") == old             # never rewritten
    assert [x.status for x in krx.orders()] == ["open", "cancelled"]              # old first, then the new file
    assert (root / "KRX" / "orders.jsonl").exists()


@pytest.mark.unit
def test_a_us_decision_fills_in_the_us_book_only(root):
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    prices = Prices(frames={"^KS11": _rising(days), "^GSPC": _rising(days), "AAPL": _flat(days, 100.0)})
    krx, us = market_audit(root, "KRX"), market_audit(root, "US")
    _decide(us, "AAPL", "2026-06-01", market="US")
    (o,) = execute_date(us, "2026-06-01", prices, risk=RISK)
    # $100,000 book, 10% name cap: 10,000 // (100 x 1.0005) = 99 shares, no commission on the US book
    assert (o.market, o.status, o.filled_quantity) == ("US", "filled", 99)
    assert o.filled_price == pytest.approx(100 * (1 + SLIP)) and "$100.00" in o.reason
    assert execute_date(krx, "2026-06-01", prices, risk=RISK) == [] and krx.orders() == []
    view = positions_view(us.orders(), prices, as_of="2026-06-02", risk=RISK, cfg=paper_config("US"))
    assert (view["market"], view["currency"], view["initial_cash"]) == ("US", "USD", 100_000.0)
    with pytest.raises(ValueError, match="US 기록에 KRX 설정"):
        execute_date(us, "2026-06-02", prices, risk=RISK, cfg=paper_config("KRX"))


@pytest.mark.unit
def test_a_us_sell_pays_the_sec_fee_and_finra_taf_and_no_sell_tax(root):
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
    prices = Prices(frames={"^GSPC": _rising(days), "AAPL": _flat(days, 100.0)})
    us = market_audit(root, "US")
    _decide(us, "AAPL", "2026-06-01", market="US")
    execute_date(us, "2026-06-01", prices, risk=RISK)                      # 99 @ 100.05 on 06-02
    _decide(us, "AAPL", "2026-06-02", "Sell", market="US")
    (s,) = execute_date(us, "2026-06-02", prices, risk=RISK)               # 99 @ 99.95 on 06-03
    assert s.status == "filled" and s.filled_price == pytest.approx(99.95)
    gross = 99 * 99.95
    proceeds = gross - gross * 20.60 / 1_000_000 - 99 * 0.000195            # SEC $20.60 per $1M; TAF per share, under $9.79
    view = positions_view(us.orders(), prices, as_of="2026-06-04", risk=RISK, cfg=paper_config("US"))
    assert view["cash"] == pytest.approx(100_000 - 99 * 100.05 + proceeds)


@pytest.mark.unit
def test_us_trading_days_come_from_the_sp500_calendar_not_the_kospi_one(root):
    # 2026-06-19 is Juneteenth: KRX trades, NYSE does not
    krx_days = ["2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22"]
    us_days = ["2026-06-16", "2026-06-17", "2026-06-18", "2026-06-22"]
    prices = Prices(frames={"^KS11": _rising(krx_days), "^GSPC": _rising(us_days),
                            "A.KS": _flat(krx_days, 10_000.0), "AAPL": _flat(us_days, 100.0)})
    krx, us = market_audit(root, "KRX"), market_audit(root, "US")
    _decide(krx, "A.KS", "2026-06-18")
    _decide(us, "AAPL", "2026-06-18", market="US")
    (k,) = execute_date(krx, "2026-06-18", prices, risk=RISK)
    (u,) = execute_date(us, "2026-06-18", prices, risk=RISK)
    assert (k.fill_date, u.fill_date) == ("2026-06-19", "2026-06-22")
    # past each market's own index data: refused per market
    _decide(us, "AAPL", "2026-06-23", market="US")
    with pytest.raises(ExecutionRefused, match="2026-06-22까지"):
        execute_date(us, "2026-06-23", prices, risk=RISK)


@pytest.mark.unit
def test_the_us_caps_are_dollars_and_an_order_over_20000_is_an_exception(root):
    days = ["2026-06-01", "2026-06-02"]
    prices = Prices(frames={"^GSPC": _rising(days), "AAPL": _flat(days, 100.0)})
    us = market_audit(root, "US")
    _decide(us, "AAPL", "2026-06-01", market="US")
    big = replace(paper_config("US"), initial_cash=1_000_000.0)            # 10% = $100,000 target
    (o,) = execute_date(us, "2026-06-01", prices, risk=RISK, cfg=big)
    assert o.status == "exception" and o.rules_applied[-1] == "auto_order_max"
    assert o.reason == "주문 금액 $99,900.00 > 주문당 상한 $20,000.00"          # 999 shares x $100, against $20,000


@pytest.mark.unit
def test_a_us_daily_loss_halts_the_us_loop_only(root):
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
    aapl = _frame({"2026-06-01": (100, 100), "2026-06-02": (100, 100), "2026-06-03": (70, 70), "2026-06-04": (70, 70)})
    prices = Prices(frames={"^GSPC": _rising(days), "^KS11": _rising(days), "AAPL": aapl})
    krx, us = market_audit(root, "KRX"), market_audit(root, "US")
    _decide(us, "AAPL", "2026-06-01", market="US")
    execute_date(us, "2026-06-01", prices, risk=RISK)
    execute_date(us, "2026-06-03", prices, risk=RISK)                      # 99 x -30 = -$2,970: about -3% of the book
    (op,) = us.operations()
    assert (op.operator, op.target, op.market, op.scope) == ("system", "daily_loss", "US", "US")
    assert trading_state(us.operations())["running"] is False
    assert krx.operations() == [] and trading_state(krx.operations())["running"] is True


@pytest.mark.unit
def test_money_is_written_in_the_markets_currency():
    assert (fmt_money(30_000_000, "KRX"), fmt_money(20_000, "US"), fmt_money(1234.567, "US")) == \
        ("30,000,000원", "$20,000.00", "$1,234.57")


# --- the API ----------------------------------------------------------------------------------------

def _cache(data, ticker, rows):
    cache = data / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"] + [f"{d} 00:00:00,{o},{o},{c},{c},1000" for d, o, c in rows]
    (cache / f"{ticker}-YFin-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A US book with one filled buy, an empty KRX book."""
    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    for index in ("^KS11", "^GSPC"):
        _cache(data, index, [(d, 100 + i, 100 + i) for i, d in enumerate(days)])
    _cache(data, "AAPL", [(d, 100, 100) for d in days])
    us = market_audit(data / "audit", "US")
    _decide(us, "AAPL", "2026-06-01", market="US")
    execute_date(us, "2026-06-01", Prices(data / "cache"), risk=RISK)
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    from backend import main
    importlib.reload(main)
    return TestClient(main.app)


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/api/orders", "/api/positions", "/api/decisions", "/api/paper", "/api/health",
                                  "/api/trading/mode", "/api/settings/rules", "/api/exceptions", "/api/tickers"])
def test_an_unknown_market_is_400_not_the_krx_book(api, path):
    for bad in ("JP", "us", ""):
        r = api.get(path, params={"market": bad})
        assert r.status_code == 400 and "market은 KRX, US" in r.json()["detail"]


@pytest.mark.unit
def test_market_us_serves_the_us_book_and_krx_stays_empty(api, tmp_path):
    (o,) = api.get("/api/orders", params={"market": "US"}).json()
    assert (o["ticker"], o["name"], o["market"], o["status"]) == ("AAPL", "애플", "US", "filled")
    assert api.get("/api/orders").json() == []                               # KRX is the default and has none
    pos = api.get("/api/positions", params={"market": "US"}).json()
    assert (pos["currency"], pos["initial_cash"], pos["limits"]["auto_daily_order_cap"]) == ("USD", 100_000.0, 70_000)
    assert [p["ticker"] for p in pos["positions"]] == ["AAPL"] and pos["mode"]["label_ko"] == "미국 모의투자 모드"
    assert api.get("/api/positions").json()["currency"] == "KRW"

    r = api.get("/api/paper", params={"market": "US"})
    assert r.status_code == 404 and "paper_backtest.py --market US" in r.json()["detail"]
    out = tmp_path / "data" / "results" / "paper_us"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps({"market": "US", "currency": "USD", "variants": {
        "rules_on": {"metrics": {"max_drawdown": -0.12}, "exits_by_reason": {"trailing": 2}}}}), encoding="utf-8")
    body = api.get("/api/paper", params={"market": "US"}).json()
    assert body["market"] == "US" and body["variants"]["rules_on"]["exits_by_reason_ko"] == {"트레일링": 2}
    assert api.get("/api/paper").status_code == 404                          # the KRX summary is its own file


@pytest.mark.unit
def test_markets_summarises_each_book_in_its_own_currency_without_a_total(api):
    rows = api.get("/api/markets").json()
    assert [r["market"] for r in rows] == ["KRX", "US"]
    krx, us = rows
    assert (krx["currency"], krx["equity"], krx["positions"]) == ("KRW", 100_000_000.0, 0)
    assert (us["currency"], us["currency_symbol"], us["positions"], us["benchmark_name_ko"]) == ("USD", "$", 1, "S&P 500")
    assert us["state"]["running"] is True and us["next_run_label_ko"].endswith(" KST")
    assert set(us) >= {"as_of", "day_return", "drawdown", "session_kst", "next_run_kst"}
    assert not any(k.startswith("total") for r in rows for k in r)


@pytest.mark.unit
def test_halt_all_stops_both_books_and_resume_us_leaves_krx_halted(api):
    r = api.post("/api/trading/halt", json={"operator": "운영자A", "reason": "점검"}).json()   # scope defaults to all
    assert r["scope"] == "all" and [x["market"] for x in r["results"]] == ["KRX", "US"] and r["skipped"] == []
    states = {m: api.get("/api/trading/mode", params={"market": m}).json() for m in ("KRX", "US")}
    assert not states["KRX"]["running"] and not states["US"]["running"]
    assert states["KRX"]["halted_at"] == states["US"]["halted_at"]         # one request, one time on both lines
    history = api.get("/api/settings/rules").json()["change_history"]
    assert sorted((h["market"], h["scope"]) for h in history) == [("KRX", "all"), ("US", "all")]

    assert api.post("/api/trading/halt", json={"operator": "x", "reason": "y"}).status_code == 409   # nothing left to stop
    r = api.post("/api/trading/resume", json={"operator": "관리자", "reason": "미국만", "scope": "US"}).json()
    assert r["resumed"] == ["US"]
    assert api.get("/api/trading/mode", params={"market": "US"}).json()["running"] is True
    assert api.get("/api/trading/mode").json()["running"] is False                               # KRX still halted
    r = api.post("/api/trading/halt", json={"operator": "x", "reason": "y", "scope": "all"}).json()
    assert [x["market"] for x in r["results"]] == ["US"] and r["skipped"][0]["market"] == "KRX"
    assert r["skipped"][0]["keep_exits"] is True                      # its exits still run: shown, not hidden
    assert api.post("/api/trading/halt", json={"operator": "x", "reason": "y", "scope": "JP"}).status_code == 400


@pytest.mark.unit
def test_a_broken_us_operations_log_does_not_block_the_halt_of_krx(jsonl_store, api, tmp_path):
    ops = tmp_path / "data" / "audit" / "US" / "operations.jsonl"
    ops.write_text('{"kind": "halt", "at": "2026-06-0\n', encoding="utf-8")                  # a cut-short line
    r = api.post("/api/trading/halt", json={"operator": "운영자A", "reason": "장애"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["market"] for x in body["results"]] == ["KRX", "US"]
    assert body["results"][0]["error"] is None and "operations.jsonl" in body["results"][1]["error"]
    assert api.get("/api/trading/mode").json()["running"] is False                               # KRX stopped
    last = json.loads(ops.read_text(encoding="utf-8").splitlines()[-1])
    assert (last["kind"], last["market"], last["after"]["running"]) == ("halt", "US", False)      # US line written anyway
    assert last["before"] == {"running": None, "unreadable": True}                               # not claimed running


@pytest.mark.unit
def test_a_broken_us_book_leaves_the_krx_card_and_krx_settings_readable(jsonl_store, api, tmp_path):
    with open(tmp_path / "data" / "audit" / "US" / "orders.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    cards = {c["market"]: c for c in api.get("/api/markets").json()}
    assert cards["KRX"]["error"] is None and cards["KRX"]["equity"] == 100_000_000
    assert cards["US"]["equity"] is None and "기록을 읽지 못함" in cards["US"]["error"]
    r = api.get("/api/settings/rules", params={"market": "KRX"})
    assert r.status_code == 200 and len(r.json()["history_errors"]) == 1


@pytest.mark.unit
def test_halt_with_exits_off_also_stops_the_exits_of_a_market_halted_with_exits_on(api):
    api.post("/api/trading/halt", json={"operator": "a", "reason": "국내만", "scope": "KRX", "keep_exits": True})
    r = api.post("/api/trading/halt", json={"operator": "b", "reason": "청산도 중단", "keep_exits": False}).json()
    assert [x["market"] for x in r["results"]] == ["KRX", "US"] and r["skipped"] == []
    assert all(x["effects"]["exits_active"] is False for x in r["results"])
    assert api.get("/api/trading/mode").json()["keep_exits"] is False
    r = api.post("/api/trading/halt", json={"operator": "c", "reason": "다시", "keep_exits": True})
    assert r.status_code == 409 and "이미 정지" in r.json()["detail"]      # both already halted, nothing to tighten


@pytest.mark.unit
def test_labels_give_each_market_its_currency_index_and_session_in_kst_with_one_colour_convention(api):
    body = api.get("/api/labels").json()
    us, krx = body["markets"]["US"], body["markets"]["KRX"]
    assert (us["currency_symbol"], us["benchmark_name_ko"], us["timezone"], us["name_style"]) == \
        ("$", "S&P 500", "America/New_York", "name_ticker")
    assert (krx["session_kst"], krx["name_style"]) == ("09:00-15:30", "name")
    assert us["session_kst"] in ("22:30-05:00", "23:30-06:00")
    assert body["color_convention"]["up"] == "red" and "시장 무관" in body["color_convention"]["note"]


@pytest.mark.unit
@pytest.mark.parametrize("day, expected", [
    (date(2026, 7, 1), ("2026-07-01 22:30", "2026-07-02 05:00")),          # daylight saving: ET = UTC-4
    (date(2026, 12, 1), ("2026-12-01 23:30", "2026-12-02 06:00")),         # standard time: ET = UTC-5
])
def test_the_us_session_in_kst_follows_daylight_saving(day, expected):
    from backend.main import session_window
    assert tuple(f"{t:%Y-%m-%d %H:%M}" for t in session_window("US", day)) == expected
    assert tuple(f"{t:%H:%M}" for t in session_window("KRX", day)) == ("09:00", "15:30")


@pytest.mark.unit
@pytest.mark.parametrize("now, market, expected", [
    # Friday 16:00 KST: the KRX run (15:35) has passed → Monday; the US Friday session closes 06:00 KST Saturday
    (datetime(2026, 11, 13, 7, 0, tzinfo=UTC), "KRX", "2026-11-16 15:35"),
    (datetime(2026, 11, 13, 7, 0, tzinfo=UTC), "US", "2026-11-14 06:05"),
    # Tuesday 20:00 ET in summer: after that day's run → Wednesday 16:05 ET = Thursday 05:05 KST
    (datetime(2026, 7, 1, 0, 0, tzinfo=UTC), "US", "2026-07-02 05:05"),
])
def test_the_next_run_is_five_minutes_after_the_next_close_in_kst(now, market, expected):
    from backend.main import next_run
    assert f"{next_run(market, now):%Y-%m-%d %H:%M}" == expected


@pytest.mark.unit
def test_the_cli_runs_the_us_book_under_audit_us(jsonl_store, tmp_path, monkeypatch, capsys):
    from scripts import paper_execute
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    prices = Prices(frames={"^GSPC": _rising(days), "^KS11": _rising(days), "AAPL": _flat(days, 100.0)})
    monkeypatch.setattr(paper_execute, "Prices", lambda _cache: prices)
    _decide(market_audit(tmp_path / "audit", "US"), "AAPL", "2026-06-01", market="US")
    assert paper_execute.main(["--market", "US", "--all", "--data-dir", str(tmp_path)]) == 0
    assert "US · 주문 1건" in capsys.readouterr().out
    assert (tmp_path / "audit" / "US" / "orders.jsonl").exists() and not (tmp_path / "audit" / "orders.jsonl").exists()
    assert not (tmp_path / "audit" / "KRX" / "orders.jsonl").exists()
    assert paper_execute.main(["--market", "US", "--all", "--data-dir", str(tmp_path)]) == 0
    assert "US · 주문 1건" in capsys.readouterr().out                       # rerun: nothing new
    # an operation record of the other market in the US log stops the run as a broken book (exit 4)
    with open(tmp_path / "audit" / "US" / "operations.jsonl", "a", encoding="utf-8") as f:
        f.write(OperationRecord(kind="halt", operator="x", role="operator", reason="r").model_dump_json() + "\n")
    assert paper_execute.main(["--market", "US", "--all", "--data-dir", str(tmp_path)]) == 4
