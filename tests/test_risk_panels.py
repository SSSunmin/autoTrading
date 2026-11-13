"""Risk panels (docs/10 §3 단계 3): the halt state the home badge and the resume modal branch on, the resume preview
(the same peak-reset calculation the resume writes), the guard / lock history, the Protections lookback counts, the
fill time in both zones and the data_delayed exception."""

import importlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import AuditLog, DecisionRecord, OperationRecord, OrderRecord, market_audit
from agent_trading_core.execution import PaperConfig, Prices, book_events, execute_date, positions_view
from agent_trading_core.risk import RiskConfig

KST = ZoneInfo("Asia/Seoul")
CAL = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", "2026-06-30")]


def _frame(closes: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({"Open": list(closes.values()), "Close": list(closes.values())}, index=pd.Index(list(closes), dtype=object))


def _cache(data, ticker, closes):
    cache = data / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"] + [f"{d} 00:00:00+09:00,{c},{c},{c},{c},1000" for d, c in closes.items()]
    (cache / f"{ticker}-YFin-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _filled(ticker, side, fill_date, q, px, *, exit_rule=None, rules=()):
    oid = f"paper-{fill_date}-{ticker}" + ("-exit" if exit_rule else "")
    return OrderRecord(order_id=oid, ticker=ticker, trade_date=fill_date, side=side, quantity=q, filled_quantity=q,
                       filled_price=px, status="filled", fill_date=fill_date,
                       rules_applied=[exit_rule] if exit_rule else list(rules))


def _halt(target, date, market="KRX", operator="system"):
    return OperationRecord(kind="halt", operator=operator, role="auto" if operator == "system" else "operator",
                           reason=f"{target} 정지", target=target, market=market, scope=market,
                           after={"running": False, "keep_exits": True, "date": date})


def _client(data, monkeypatch, token=None, now=None):
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    if token:
        monkeypatch.setenv("TRADING_ADMIN_TOKEN", token)
    else:
        monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    from backend import main
    importlib.reload(main)
    if now:
        monkeypatch.setattr(main, "_clock", lambda: now)
    return TestClient(main.app), main


# a KRX book of 9,000 shares bought at 10,000 (commission 0.015%) that closes at 7,000 from 06-08:
# cash 100,000,000 − 90,000,000 × 1.00015 = 9,986,500; equity 9,986,500 + 63,000,000 = 72,986,500 (−27.0% from 1억)
EQUITY, PEAK = 72_986_500.0, 100_000_000.0


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    data = tmp_path / "data"
    days = [d for d in CAL if d <= "2026-06-12"]
    _cache(data, "^KS11", {d: 100.0 + i for i, d in enumerate(days)})
    _cache(data, "AAA.KS", {d: 10_000.0 if d < "2026-06-08" else 7_000.0 for d in days})
    audit = market_audit(data / "audit", "KRX")
    audit.record_order(_filled("AAA.KS", "Buy", "2026-06-02", 9000, 10_000.0))
    audit.record_operation(_halt("dd_stop", "2026-06-08"))
    c, _ = _client(data, monkeypatch, token="t", now=datetime(2026, 6, 12, 20, 0, tzinfo=KST))
    return c, audit


# --- state per halt variant ------------------------------------------------------------------------------

@pytest.mark.unit
def test_a_persons_halt_names_its_scope_and_has_no_release_time(tmp_path, monkeypatch):
    c, _ = _client(tmp_path / "data", monkeypatch)
    assert c.post("/api/trading/halt", json={"operator": "운영자A", "reason": "점검", "scope": "KRX"}).status_code == 200
    st = c.get("/api/trading/mode").json()
    assert (st["running"], st["halt_target"], st["halt_scope"], st["halt_scope_ko"]) == (False, "manual", "KRX", "국내만")
    assert st["auto_resume_at"] is None and st["auto_resume_label_ko"] is None
    c.post("/api/trading/halt", json={"operator": "운영자A", "reason": "점검", "scope": "all"})       # US now, scope all
    assert c.get("/api/trading/mode", params={"market": "US"}).json()["halt_scope_ko"] == "전체 시장"


@pytest.mark.unit
@pytest.mark.parametrize(("market", "halted_on", "label", "opens"), [
    ("KRX", "2026-06-03", "06-04(목) 09:00 KST", "2026-06-04T09:00:00+09:00"),
    ("US", "2026-07-08", "07-09(목) 22:30 KST", "2026-07-09T22:30:00+09:00"),     # EDT: 09:30 New York = 22:30 KST
    ("US", "2026-12-02", "12-03(목) 23:30 KST", "2026-12-03T23:30:00+09:00"),     # EST: 23:30 KST
])
def test_a_daily_loss_halt_shows_the_market_open_it_ends_at(tmp_path, monkeypatch, market, halted_on, label, opens):
    data = tmp_path / "data"
    market_audit(data / "audit", market).record_operation(_halt("daily_loss", halted_on, market))
    c, _ = _client(data, monkeypatch, now=datetime.fromisoformat(f"{halted_on}T12:00:00+09:00"))
    st = c.get("/api/trading/mode", params={"market": market}).json()
    assert (st["running"], st["halt_target"], st["halt_scope"]) == (False, "daily_loss", market)
    assert (st["auto_resume_open_kst"], st["auto_resume_label_ko"]) == (opens, label)


@pytest.mark.unit
def test_a_dd_stop_state_and_after_release_no_scope(stopped, tmp_path, monkeypatch):
    c, _ = stopped
    st = c.get("/api/trading/mode").json()
    assert (st["halt_target"], st["dd_stop"], st["halt_scope"], st["auto_resume_label_ko"]) == ("dd_stop", True, "KRX", None)


# --- resume preview -----------------------------------------------------------------------------------

@pytest.mark.unit
def test_the_preview_by_hand_with_the_reset_on_and_off(stopped):
    c, _ = stopped
    on = c.get("/api/trading/resume-preview").json()
    assert (on["variant"], on["can_resume"], on["reset_peak"], on["restops"]) == ("dd_stop", True, True, False)
    assert (on["equity"], on["peak_before"], on["base"]) == (pytest.approx(EQUITY), pytest.approx(PEAK), pytest.approx(EQUITY))
    assert [x["id"] for x in on["lines"]] == ["peak", "dd_halve", "dd_stop"]
    assert [x["label_ko"] for x in on["lines"]] == ["피크 · DD 0%", "반감 −10%", "정지 −20%"]
    assert [x["amount"] for x in on["lines"]] == pytest.approx([EQUITY, EQUITY * 0.9, EQUITY * 0.8])
    assert [x["vs_equity"] for x in on["lines"]] == pytest.approx([0.0, 1 / 0.9 - 1, 1 / 0.8 - 1])
    off = c.get("/api/trading/resume-preview", params={"reset_peak": "false"}).json()
    assert (off["reset_peak"], off["base"], off["restops"]) == (False, pytest.approx(PEAK), True)   # the old peak stops again
    assert [x["amount"] for x in off["lines"]] == pytest.approx([1e8, 9e7, 8e7])
    assert [x["vs_equity"] for x in off["lines"]] == pytest.approx([EQUITY / 1e8 - 1, EQUITY / 9e7 - 1, EQUITY / 8e7 - 1])
    assert c.get("/api/trading/resume-preview", params={"reset_peak": "maybe"}).status_code == 422
    assert c.get("/api/trading/resume-preview", params={"market": "JP"}).status_code == 400


@pytest.mark.unit
def test_the_resume_writes_the_previewed_peak(stopped):
    c, _ = stopped
    preview = c.get("/api/trading/resume-preview").json()
    body = {"operator": "관리자", "reason": "점검 후 재개", "scope": "KRX"}
    assert c.post("/api/trading/resume", json=body).status_code == 403
    r = c.post("/api/trading/resume", json=body, headers={"X-Admin-Token": "t"}).json()
    assert r["peak_resets"]["KRX"] == {"to": preview["base"], "at": preview["as_of"], "before": preview["peak_before"]}
    assert c.get("/api/trading/resume-preview").json() | {"market": "KRX"} == {
        "market": "KRX", "variant": "running", "variant_ko": "실행 중", "can_resume": False, "reset_peak": False,
        "equity": None, "as_of": None, "peak_before": None, "base": None, "restops": False, "lines": []}


@pytest.mark.unit
@pytest.mark.parametrize(("op", "variant", "can"), [(_halt("daily_loss", "2026-06-11"), "daily_loss", False),
                                                    (_halt("manual", "2026-06-11", operator="운영자A"), "manual", True)])
def test_a_daily_loss_halt_offers_no_resume_and_a_persons_halt_no_peak_lines(tmp_path, monkeypatch, op, variant, can):
    data = tmp_path / "data"
    market_audit(data / "audit", "KRX").record_operation(op)
    c, _ = _client(data, monkeypatch, now=datetime(2026, 6, 11, 20, 0, tzinfo=KST))
    p = c.get("/api/trading/resume-preview").json()
    assert (p["variant"], p["can_resume"], p["lines"], p["reset_peak"]) == (variant, can, [], False)


# --- the guard / lock history -------------------------------------------------------------------------

@pytest.mark.unit
def test_book_events_one_of_each_kind_newest_first():
    days = [d for d in CAL if d <= "2026-06-12"]
    # AAA: 9,000 shares; 06-08 closes at 8,800 (−10.8%: halve), 06-09 at 7,000 (the stop)
    aaa = {d: 10_000.0 if d < "2026-06-08" else 8_800.0 if d == "2026-06-08" else 7_000.0 for d in days}
    prices = Prices(frames={"IDX": _frame({d: 100.0 for d in days}), "AAA.KS": _frame(aaa),
                            "DDD.KS": _frame(dict.fromkeys(days, 10_000.0))})
    cfg = PaperConfig(index="IDX")
    orders = [
        _filled("AAA.KS", "Buy", "2026-06-02", 9000, 10_000.0),
        _filled("DDD.KS", "Buy", "2026-06-02", 10, 10_000.0),
        _filled("DDD.KS", "Sell", "2026-06-05", 10, 9_000.0, exit_rule="stop_loss"),                   # → Cooldown
        OrderRecord(order_id="paper-2026-06-01-CCC.KS", ticker="CCC.KS", trade_date="2026-06-01", side="Buy", quantity=5,
                    status="open", rules_applied=["max_position_pct"],
                    reason="모의 주문: 목표 비중 10.0% × 평가 ₩100,000,000 · max_position_pct: CCC.KS 50.0% → 10.0% · 다음 거래일 시가"),
        OrderRecord(order_id="paper-2026-06-09-BBB.KS", ticker="BBB.KS", trade_date="2026-06-09", side="Buy", quantity=0,
                    status="blocked", rules_applied=["dd_stop"], reason="MaxDrawdown: 피크 대비 −27.0%"),
    ]
    ops = [_halt("dd_stop", "2026-06-09"),
           _halt("manual", "2026-06-10", operator="운영자A"),                                  # a person's halt: not a guard
           OperationRecord(kind="resume", operator="관리자", role="admin", reason="재개", target="manual",
                           after={"running": True, "peak_reset_to": 72_986_500.0, "peak_reset_at": "2026-06-10",
                                  "peak_before": 100_000_000.0})]
    ev = book_events(orders, ops, prices, cfg=cfg)
    assert [(e["date"], e["kind"]) for e in ev] == [
        ("2026-06-10", "peak_reset"), ("2026-06-09", "guard"), ("2026-06-09", "lock"), ("2026-06-08", "guard"),
        ("2026-06-05", "protections"), ("2026-06-01", "intervention")]
    assert ev[0]["text"] == "피크 100,000,000원 → 72,986,500원 · 관리자: 재개"
    assert ev[2]["text"] == "신규 매수 1건 차단 (BBB.KS) · MaxDrawdown: 피크 대비 −27.0%"
    assert ev[3]["text"].startswith("드로다운 ") and "신규 비중 절반" in ev[3]["text"]
    assert (ev[4]["ticker"], ev[4]["text"]) == ("DDD.KS", "Cooldown: DDD.KS 청산 후 5거래일 재진입 금지")
    assert ev[5]["text"] == "CCC.KS 비중 축소 · max_position_pct: CCC.KS 50.0% → 10.0%"


@pytest.mark.unit
def test_the_events_endpoint_labels_counts_and_limit(stopped):
    c, _ = stopped
    # only the stop's halt (06-08): no close (no Cooldown), and 06-08 drops straight past −20% (no halve line)
    out = c.get("/api/events").json()
    assert (out["market"], out["total"], out["counts"]) == ("KRX", 1, {"guard": 1})
    assert out["events"] == [{"date": "2026-06-08", "kind": "guard", "ticker": None, "text": "dd_stop 정지",
                              "kind_ko": "가드", "name": None}]
    c.post("/api/trading/resume", json={"operator": "관리자", "reason": "재개", "scope": "KRX"}, headers={"X-Admin-Token": "t"})
    out = c.get("/api/events", params={"limit": 1}).json()
    assert (out["total"], len(out["events"]), out["events"][0]["kind_ko"]) == (2, 1, "피크 재설정")      # newest first
    assert c.get("/api/events", params={"limit": 0}).status_code == 422
    assert c.get("/api/events", params={"market": "JP"}).status_code == 400


# --- Protections: lookback hits -------------------------------------------------------------------------

@pytest.mark.unit
def test_lookback_hits_by_hand():
    prices = Prices(frames={"IDX": _frame({d: 100.0 for d in CAL}),
                            **{t: _frame(dict.fromkeys(CAL, 10_000.0)) for t in ("T1.KS", "T2.KS", "T3.KS")}})
    orders = [_filled("T1.KS", "Buy", "2026-06-01", 10, 10_000.0), _filled("T1.KS", "Sell", "2026-06-02", 10, 9_000.0, exit_rule="stop_loss"),
              _filled("T2.KS", "Buy", "2026-06-01", 10, 10_000.0), _filled("T2.KS", "Sell", "2026-06-12", 10, 9_500.0, exit_rule="stop_loss"),
              _filled("T1.KS", "Buy", "2026-06-10", 10, 10_000.0), _filled("T1.KS", "Sell", "2026-06-15", 10, 9_000.0, exit_rule="stop_loss"),
              _filled("T3.KS", "Buy", "2026-06-01", 10, 10_000.0), _filled("T3.KS", "Sell", "2026-06-17", 10, 11_000.0, exit_rule="trailing")]
    ops = [_halt("dd_stop", "2026-06-05"), _halt("dd_stop", "2026-05-01")]       # the second is before the window
    view = positions_view(orders, prices, as_of="2026-06-19", cfg=PaperConfig(index="IDX"), operations=ops)
    g = {x["id"]: (x["lookback_days"], x["lookback_hits"]) for x in view["protections"]}
    # last 10 trading days 06-08..06-19: stops 06-12, 06-15 (06-02 is out); last 5 (06-15..06-19): closes 06-15, 06-17;
    # LowProfit fired once (T1's two closes, 06-02 and 06-15, average −10%); one dd_stop halt within 20 days
    assert g == {"StoplossGuard": (10, 2), "MaxDrawdown": (20, 1), "Cooldown": (5, 2), "LowProfit": (30, 1)}


# --- fill time in both zones ----------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("market", "status", "fill_date", "trade_date", "local", "kst", "planned"), [
    ("US", "filled", "2026-07-01", "2026-06-30", "2026-07-01T09:30:00-04:00", "2026-07-01T22:30:00+09:00", False),   # EDT
    ("US", "filled", "2026-12-01", "2026-11-30", "2026-12-01T09:30:00-05:00", "2026-12-01T23:30:00+09:00", False),   # EST
    ("US", "open", None, "2026-11-06", "2026-11-09T09:30:00-05:00", "2026-11-09T23:30:00+09:00", True),              # Fri → Mon
    ("KRX", "filled", "2026-07-01", "2026-06-30", "2026-07-01T09:00:00+09:00", "2026-07-01T09:00:00+09:00", False),
    ("KRX", "blocked", None, "2026-07-01", None, None, False),
])
def test_fill_at_in_market_time_and_kst(tmp_path, monkeypatch, market, status, fill_date, trade_date, local, kst, planned):
    _, main = _client(tmp_path / "data", monkeypatch)
    o = OrderRecord(order_id="x", ticker="AAA", trade_date=trade_date, side="Buy", quantity=1, status=status,
                    fill_date=fill_date, market=market)
    assert main._fill_at(o) == {"fill_at_local": local, "fill_at_kst": kst, "fill_planned": planned}


# --- data_delayed ---------------------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("aaa_days", "kind"), [(["2026-06-01"], "data_delayed"),                     # one session short
                                                 (["2026-05-27"], "vendor_unavailable"),                 # sessions earlier: a halt
                                                 (["2026-06-01", "2026-06-03"], "vendor_unavailable")])  # a gap on the day
def test_a_cache_behind_the_fill_day_is_data_delayed_and_a_gap_is_not(tmp_path, aaa_days, kind):
    days = ["2026-05-27", "2026-05-28", "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03"]
    prices = Prices(frames={"^KS11": _frame({d: 100.0 + i for i, d in enumerate(days)}),
                            "AAA.KS": _frame(dict.fromkeys(aaa_days, 10_000.0))})
    audit = AuditLog(tmp_path / "audit")
    audit.record_decision(DecisionRecord(ticker="AAA.KS", trade_date="2026-06-01", rating="Buy", thesis="Buy AAA"))
    risk = RiskConfig(regime_ma_days=3)
    execute_date(audit, "2026-06-01", prices, risk=risk)
    execute_date(audit, "2026-06-02", prices, risk=risk)
    (o,) = [o for o in audit.orders() if o.ticker == "AAA.KS"][-1:]
    assert (o.status, o.exception_kind) == ("exception", kind)
    assert ("데이터 지연" in o.reason) == (kind == "data_delayed")
