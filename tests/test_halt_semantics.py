"""Halt semantics (2026-11-13 decisions): a daily-loss halt ends with its session — the next trading day's run lifts
it by ``system``; a −20% drawdown stop halts by ``system`` until an admin resumes, and that resume may reset the
drawdown peak to the equity at the last close. A person's halt is never lifted by the executor."""

import importlib

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import AuditLog, DecisionRecord, OperationRecord, market_audit, trading_state
from agent_trading_core.execution import (
    ExecutionRefused,
    PaperConfig,
    Prices,
    execute_date,
    positions_view,
    unexplained_orders,
)
from agent_trading_core.risk import RiskConfig

SLIP, COMM, TAX = 0.0005, 0.00015, 0.0020
# 2026-06-03 is a holiday
DAYS = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
        "2026-06-11", "2026-06-12"]
RISK = RiskConfig(regime_ma_days=3)


def _frame(rows: dict[str, tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame({"Open": [o for o, _ in rows.values()], "Close": [c for _, c in rows.values()]},
                        index=pd.Index(list(rows), dtype=object))


def _series(opens_closes: list[tuple[float, float]]) -> pd.DataFrame:
    return _frame(dict(zip(DAYS, opens_closes, strict=True)))


def _prices(index="^KS11", **tickers) -> Prices:
    frames = {index: _series([(100.0 + i, 100.0 + i) for i in range(len(DAYS))])}     # rising: regime on
    frames.update({t.replace("_", "."): f for t, f in tickers.items()})
    return Prices(frames=frames)


def _decide(audit, ticker, date, rating="Buy", market="KRX"):
    audit.record_decision(DecisionRecord(ticker=ticker, trade_date=date, rating=rating, thesis=f"{rating} {ticker}",
                                         market=market))


def _halts(audit):
    return [op for op in audit.operations() if op.kind == "halt"]


def _resumes(audit):
    return [op for op in audit.operations() if op.kind == "resume"]


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit")


# a 10% position that falls 30% on 06-04: the book loses 3% that day (≤ −2%) → daily-loss halt on 06-04
FALL = _series([(10_000, 10_000), (10_000, 10_000), (10_000, 7_000)] + [(7_000, 7_000)] * (len(DAYS) - 3))


def _daily_loss_halt(audit, prices):
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    execute_date(audit, "2026-06-04", prices, risk=RISK)
    (halt,) = _halts(audit)
    assert (halt.operator, halt.target, halt.after["date"]) == ("system", "daily_loss", "2026-06-04")


# --- daily-loss halt: lifted by system on the next trading day ------------------------------------------

@pytest.mark.unit
def test_a_daily_loss_halt_is_lifted_by_system_on_the_next_trading_day_once(audit):
    prices = _prices(A_KS=FALL, B_KS=_series([(10_000, 10_000)] * len(DAYS)))
    _daily_loss_halt(audit, prices)
    execute_date(audit, "2026-06-04", prices, risk=RISK)                      # the same session again: still halted
    assert _resumes(audit) == []

    _decide(audit, "B.KS", "2026-06-05")
    (b,) = execute_date(audit, "2026-06-05", prices, risk=RISK)
    (resume,) = _resumes(audit)
    assert (resume.operator, resume.role, resume.target) == ("system", "auto", "daily_loss")
    assert resume.reason == "일일 손실 정지 자동 해제(정지일 2026-06-04)"
    assert trading_state(audit.operations())["running"] is True
    assert b.ticker == "B.KS" and b.side == "Buy" and b.status == "filled"   # the loop ordered again

    lines = len(audit.operations())
    execute_date(audit, "2026-06-05", prices, risk=RISK)                      # rerun: no second resume
    execute_date(audit, "2026-06-08", prices, risk=RISK)
    assert len(audit.operations()) == lines
    assert unexplained_orders(audit.orders(), audit.decisions()) == []


@pytest.mark.unit
def test_a_run_refused_for_its_mode_does_not_lift_the_halt(audit):
    prices = _prices(A_KS=FALL)
    _daily_loss_halt(audit, prices)
    audit.record_operation(OperationRecord(kind="mode_change", operator="관리자", role="admin", reason="수동 전환",
                                           target="mode", after={"mode": "manual"}))
    with pytest.raises(ExecutionRefused):
        execute_date(audit, "2026-06-05", prices, risk=RISK)
    assert _resumes(audit) == [] and trading_state(audit.operations())["running"] is False


@pytest.mark.unit
def test_a_holiday_dated_run_does_not_lift_the_halt(audit):
    # bought at 06-02's open, down 30% at its close: halted on 06-02; 06-03 is a holiday, 06-04 the next session
    early = _series([(10_000, 10_000), (10_000, 7_000)] + [(7_000, 7_000)] * (len(DAYS) - 2))
    prices = _prices(A_KS=early)
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=RISK)
    execute_date(audit, "2026-06-02", prices, risk=RISK)
    assert [h.after["date"] for h in _halts(audit)] == ["2026-06-02"]
    execute_date(audit, "2026-06-03", prices, risk=RISK)                      # a holiday, not a trading day
    assert _resumes(audit) == []
    execute_date(audit, "2026-06-04", prices, risk=RISK)
    assert [r.after["halted_on"] for r in _resumes(audit)] == ["2026-06-02"]


@pytest.mark.unit
@pytest.mark.parametrize("on_top", [False, True])
def test_a_persons_halt_is_never_lifted_by_the_executor(audit, on_top):
    prices = _prices(A_KS=FALL)
    if on_top:   # a person halts on top of the system's daily-loss halt
        _daily_loss_halt(audit, prices)
    else:
        _decide(audit, "A.KS", "2026-06-01")
        execute_date(audit, "2026-06-01", prices, risk=RISK)
    audit.record_operation(OperationRecord(kind="halt", operator="운영자A", role="operator", reason="점검",
                                           target="manual", after={"running": False, "keep_exits": True}))
    for day in ("2026-06-05", "2026-06-08", "2026-06-09"):
        execute_date(audit, day, prices, risk=RISK)
    assert _resumes(audit) == [] and trading_state(audit.operations())["running"] is False


@pytest.mark.unit
def test_an_admin_resume_the_same_day_is_not_followed_by_a_system_one(audit):
    prices = _prices(A_KS=FALL)
    _daily_loss_halt(audit, prices)
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="확인", target="manual"))
    execute_date(audit, "2026-06-05", prices, risk=RISK)
    assert [(op.operator, op.kind) for op in audit.operations()] == [("system", "halt"), ("관리자", "resume")]


@pytest.mark.unit
def test_lifting_a_daily_loss_halt_touches_that_market_only(tmp_path):
    root = tmp_path / "audit"
    krx, us = market_audit(root, "KRX"), market_audit(root, "US")
    frames = {"^KS11": _series([(100.0 + i, 100.0 + i) for i in range(len(DAYS))]),
              "^GSPC": _series([(100.0 + i, 100.0 + i) for i in range(len(DAYS))]), "A.KS": FALL}
    prices = Prices(frames=frames)
    _decide(krx, "A.KS", "2026-06-01")
    execute_date(krx, "2026-06-01", prices, risk=RISK)
    execute_date(krx, "2026-06-04", prices, risk=RISK)
    assert [op.target for op in _halts(krx)] == ["daily_loss"]
    execute_date(us, "2026-06-05", prices, risk=RISK)                         # the other market's next session
    assert us.operations() == [] and _resumes(krx) == []                     # KRX stays halted until its own run
    execute_date(krx, "2026-06-05", prices, risk=RISK)
    assert [op.market for op in _resumes(krx)] == ["KRX"] and us.operations() == []


# --- −20% stop: system halt, admin resume with a peak reset ----------------------------------------------

# the whole book in one name that halves on 06-05: equity about −50% → stop-loss sell + dd_stop halt that day
HALF = _series([(10_000, 10_000), (10_000, 10_000), (10_000, 10_000), (10_000, 5_000)] + [(5_000, 5_000)] * (len(DAYS) - 4))
BIG = RiskConfig(regime_ma_days=3, max_position_pct=1.0)
CFG = PaperConfig(initial_cash=2.5e7)


def _stopped_out(audit, prices):
    _decide(audit, "A.KS", "2026-06-01")
    execute_date(audit, "2026-06-01", prices, risk=BIG, cfg=CFG)             # buys at 06-02's open
    execute_date(audit, "2026-06-05", prices, risk=BIG, cfg=CFG)             # −50%: sell placed, loop halted
    execute_date(audit, "2026-06-08", prices, risk=BIG, cfg=CFG)             # exits only: the sell fills at 5,000
    # the −50% day is also a daily loss: that halt first, then the stop's own; the stop keeps it halted
    daily, halt = _halts(audit)
    assert daily.target == "daily_loss" and _resumes(audit) == []
    assert (halt.operator, halt.role, halt.target, halt.after["keep_exits"]) == ("system", "auto", "dd_stop", True)


def _hand_book():
    """By hand: 2,498 shares at 10,005 (10,000 + 0.05% slippage), commission 0.015%, sold at 4,997.5 less
    commission and tax. The peak is the initial cash: the 06-02 close is below it by the slippage and commission."""
    unit = 10_000 * (1 + SLIP) * (1 + COMM)
    q = int(2.5e7 // unit)
    cash = 2.5e7 - q * 10_000 * (1 + SLIP) * (1 + COMM)
    assert cash + q * 10_000 < 2.5e7
    peak = 2.5e7
    after = cash + q * 5_000 * (1 - SLIP) * (1 - COMM - TAX)
    return q, peak, after


@pytest.mark.unit
def test_a_dd_stop_halt_is_written_once_and_never_lifted_by_the_executor(audit):
    prices = _prices(A_KS=HALF)
    _stopped_out(audit, prices)
    for day in ("2026-06-09", "2026-06-10"):
        execute_date(audit, day, prices, risk=BIG, cfg=CFG)
    assert len(_halts(audit)) == 2 and _resumes(audit) == []               # no system resume of the daily loss either
    assert trading_state(audit.operations())["halt_target"] == "dd_stop"


@pytest.mark.unit
def test_the_peak_reset_makes_the_drawdown_zero_and_no_reset_keeps_the_old_peak(audit):
    prices = _prices(A_KS=HALF)
    _stopped_out(audit, prices)
    q, peak, after = _hand_book()
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=BIG, cfg=CFG, operations=audit.operations())
    assert q == 2498 and view["equity"] == pytest.approx(after)
    assert view["peak"] == pytest.approx(peak) and view["drawdown"] == pytest.approx(after / peak - 1)  # ≈ −50%

    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="재개", target="manual",
                                           after={"running": True, "peak_reset_to": after, "peak_reset_at": "2026-06-08"}))
    view = positions_view(audit.orders(), prices, as_of="2026-06-08", risk=BIG, cfg=CFG, operations=audit.operations())
    assert view["peak"] == pytest.approx(after) and view["drawdown"] == pytest.approx(0.0)
    assert view["peak_reset"] == {"at": "2026-06-08", "to": pytest.approx(after)}
    # before the reset date the old peak still counts
    early = positions_view(audit.orders(), prices, as_of="2026-06-05", risk=BIG, cfg=CFG, operations=audit.operations())
    assert early["peak"] == pytest.approx(peak)


@pytest.mark.unit
def test_rerunning_a_day_before_the_reset_does_not_stop_the_loop_again(audit):
    prices = _prices(A_KS=HALF)
    _stopped_out(audit, prices)
    _, _, after = _hand_book()
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="재개", target="manual",
                                           after={"running": True, "peak_reset_to": after, "peak_reset_at": "2026-06-10"}))
    for day in ("2026-06-05", "2026-06-08", "2026-06-10", "2026-06-11"):      # --all restarts at the last order's day
        execute_date(audit, day, prices, risk=BIG, cfg=CFG)
    assert len(_halts(audit)) == 2 and trading_state(audit.operations())["running"] is True


@pytest.mark.unit
def test_a_resume_without_the_reset_halts_again_on_the_next_run(audit):
    prices = _prices(A_KS=HALF)
    _stopped_out(audit, prices)
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="피크 유지",
                                           target="manual", after={"running": True}))
    execute_date(audit, "2026-06-09", prices, risk=BIG, cfg=CFG)             # still −50% from the old peak
    assert [op.target for op in _halts(audit)] == ["daily_loss", "dd_stop", "dd_stop"]
    assert trading_state(audit.operations())["running"] is False


@pytest.mark.unit
def test_a_rerun_of_the_stops_own_day_after_a_resume_without_the_reset_does_not_halt_twice(audit):
    prices = _prices(A_KS=HALF)
    _stopped_out(audit, prices)
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="피크 유지",
                                           target="manual", after={"running": True}))
    execute_date(audit, "2026-06-05", prices, risk=BIG, cfg=CFG)             # --all restarts at the stop's day
    assert [op.target for op in _halts(audit)] == ["daily_loss", "dd_stop"]


@pytest.mark.unit
def test_after_the_reset_the_halving_and_sizing_read_the_new_peak(audit):
    # after the reset B is bought with the whole book on 06-08, then falls 15% on 06-10 (a daily-loss halt, lifted
    # on 06-11): −15% from the new peak halves new buys; from the old peak (≈ −57%) it would be another stop
    b = _series([(10_000, 10_000)] * 6 + [(10_000, 8_500)] + [(8_500, 8_500)] * (len(DAYS) - 7))
    prices = _prices(A_KS=HALF, B_KS=b, C_KS=_series([(10_000, 10_000)] * len(DAYS)))
    _stopped_out(audit, prices)
    _, _, after = _hand_book()
    audit.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="재개", target="manual",
                                           after={"running": True, "peak_reset_to": after, "peak_reset_at": "2026-06-08"}))
    _decide(audit, "B.KS", "2026-06-08")
    (buy,) = [o for o in execute_date(audit, "2026-06-08", prices, risk=BIG, cfg=CFG) if o.ticker == "B.KS"]
    assert buy.status == "filled" and buy.rules_applied == []                 # full size against the new peak (dd 0)
    execute_date(audit, "2026-06-09", prices, risk=BIG, cfg=CFG)
    execute_date(audit, "2026-06-10", prices, risk=BIG, cfg=CFG)
    assert [h.target for h in _halts(audit)][2:] == ["daily_loss"]          # −15% in a day; no stop vs the new peak
    _decide(audit, "C.KS", "2026-06-11")
    (c,) = [o for o in execute_date(audit, "2026-06-11", prices, risk=BIG, cfg=CFG) if o.ticker == "C.KS"]
    assert _resumes(audit)[-1].operator == "system"                          # the daily loss lifted, the loop runs
    assert "dd_halve" in c.rules_applied                                     # and halves the new buy
    view = positions_view(audit.orders(), prices, as_of="2026-06-11", risk=BIG, cfg=CFG, operations=audit.operations())
    guard = {g["id"]: g for g in view["protections"]}["MaxDrawdown"]
    assert guard["active"] and "신규 비중 절반" in guard["detail"]
    assert view["drawdown"] == pytest.approx(view["equity"] / after - 1) and -0.16 < view["drawdown"] < -0.14


# --- API: resume with reset_peak ---------------------------------------------------------------------

def _cache(data, ticker, frame):
    cache = data / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"] + [f"{d} 00:00:00,{o},{o},{c},{c},1000"
                                                   for d, (o, c) in zip(frame.index, frame[["Open", "Close"]].values)]
    (cache / f"{ticker}-YFin-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def stopped_api(tmp_path, monkeypatch):
    """A KRX book stopped out at −20%, served with an admin token. The default risk sizes 10% a name, so the stop is
    hit with the default RiskConfig by ten names that all halve."""
    data = tmp_path / "data"
    _cache(data, "^KS11", _series([(100.0 + i, 100.0 + i) for i in range(len(DAYS))]))
    names = [f"{i:06d}.KS" for i in range(10)]
    for t in names:
        _cache(data, t, HALF)
    prices = Prices(data / "cache")
    krx = market_audit(data / "audit", "KRX")
    for t in names:
        _decide(krx, t, "2026-06-01")
    for day in ("2026-06-01", "2026-06-05", "2026-06-08"):
        execute_date(krx, day, prices, risk=RISK)
    assert [op.target for op in _halts(krx)] == ["daily_loss", "dd_stop"]
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    monkeypatch.setenv("TRADING_ADMIN_TOKEN", "test-token")
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), krx


@pytest.mark.unit
def test_the_api_shows_the_dd_stop_and_the_admin_resume_resets_the_peak(stopped_api):
    client, krx = stopped_api
    st = client.get("/api/trading/mode").json()
    assert (st["halt_target"], st["dd_stop"], st["halt_target_ko"]) == ("dd_stop", True, "시스템 · 드로다운 정지")
    assert st["label_ko"].endswith("· 시스템 · 드로다운 정지")
    before = client.get("/api/positions").json()
    assert before["drawdown"] < -0.2 and before["peak_reset"] is None

    body = {"operator": "관리자", "reason": "점검 후 재개", "scope": "KRX"}
    assert client.post("/api/trading/resume", json=body).status_code == 403                          # no token
    assert client.post("/api/trading/resume", json=body, headers={"X-Admin-Token": "x"}).status_code == 403
    assert client.post("/api/trading/resume", json=body | {"scope": "JP"},
                       headers={"X-Admin-Token": "test-token"}).status_code == 400
    assert client.post("/api/trading/resume", json=body | {"reset_peak": "yes please"},
                       headers={"X-Admin-Token": "test-token"}).status_code == 422                   # not a bool
    r = client.post("/api/trading/resume", json=body, headers={"X-Admin-Token": "test-token"})       # reset_peak true
    assert r.status_code == 200
    out = r.json()
    assert out["resumed"] == ["KRX"] and out["states"]["KRX"]["running"] is True
    reset = out["peak_resets"]["KRX"]
    assert reset["at"] == DAYS[-1] and reset["to"] == pytest.approx(before["equity"])
    assert reset["before"] == pytest.approx(before["peak"])
    (resume,) = _resumes(krx)
    assert (resume.operator, resume.role, resume.target) == ("관리자", "admin", "manual")
    assert resume.after["peak_reset_to"] == pytest.approx(before["equity"])
    after = client.get("/api/positions").json()
    assert after["drawdown"] == pytest.approx(0.0) and after["peak"] == pytest.approx(before["equity"])


@pytest.mark.unit
def test_reset_peak_false_keeps_the_peak_and_a_manual_halt_has_no_reset(stopped_api):
    client, krx = stopped_api
    r = client.post("/api/trading/resume", json={"operator": "관리자", "reason": "피크 유지", "scope": "all",
                                                 "reset_peak": False}, headers={"X-Admin-Token": "test-token"}).json()
    assert r["peak_resets"] == {} and "peak_reset_to" not in _resumes(krx)[0].after
    assert client.get("/api/positions").json()["drawdown"] < -0.2
    # a person's halt is not a dd stop: its resume never resets the peak
    client.post("/api/trading/halt", json={"operator": "운영자A", "reason": "점검", "scope": "KRX"})
    assert client.get("/api/trading/mode").json()["halt_target"] == "manual"
    r = client.post("/api/trading/resume", json={"operator": "관리자", "reason": "재개", "scope": "KRX"},
                    headers={"X-Admin-Token": "test-token"}).json()
    assert r["peak_resets"] == {}
