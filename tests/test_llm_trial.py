"""The LLM trial (config.LLM trial · trial_sessions, user decision 2026-11-16 "1주 시험 후 판단"): with TRADING_LLM_TRIAL=1
the daily loop's default decider is the model for a market's first five decide days the model decided, replayed from
the decision records, then the rule with a line saying so; off, or with an explicit --decider, nothing changes. And
scripts/llm_trial_report.py over a hand-made book of three days, checked against a hand count — under both stores.
No network: the fake KIS and model clients of tests/test_daily_loop.py, the report's prices from a written cache."""

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from agent_trading_core import kis
from agent_trading_core.audit import DecisionRecord, OrderPlan, OrderRecord, PlanSide, market_audit
from agent_trading_core.memory import settlement
from tests.test_daily_loop import FakeManager, NoManager, _market, at, dl
from tests.test_exception_rerun import _screen

_spec = importlib.util.spec_from_file_location(
    "llm_trial_report", Path(__file__).resolve().parents[1] / "scripts" / "llm_trial_report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

DAYS = ["2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11"]   # 5 trial days + 1
MODEL = dl.LLM["model"]


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    return request.param


@pytest.fixture
def trial(store, monkeypatch):
    monkeypatch.setenv("TRADING_LLM_TRIAL", "1")


def _decide(client, market, day, data, llm, **kw):
    """The day's decide as the scheduler runs it: no --decider unless given."""
    client.today = day
    now = at(day, "15:40") if market == "KRX" else at((pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                                                      "06:10")
    return dl.run_stage(market, "decide", day, data, client, now, llm=llm, **kw)


def _trial_lines(r):
    return [n for n in r.notes if n.startswith("LLM 시험")]


@pytest.mark.unit
def test_the_trial_is_off_by_default_and_the_rule_decides(store, tmp_path):
    assert dl.LLM["enabled"] is False and dl.LLM["trial"] is False and dl.LLM["trial_sessions"] == 5
    assert not dl.trial_on()
    r = _decide(_market(), "KRX", DAYS[0], tmp_path, NoManager())
    decisions = market_audit(tmp_path / "audit", "KRX").decisions()
    assert r.status == "ok" and decisions and all(d.settings["decider"] == "rule_momentum_baseline" for d in decisions)
    assert "llm_calls" not in r.counts and not _trial_lines(r)


@pytest.mark.parametrize("value", ["0", "", "true"])
@pytest.mark.unit
def test_only_the_value_1_turns_the_trial_on(store, tmp_path, monkeypatch, value):
    monkeypatch.setenv("TRADING_LLM_TRIAL", value)
    assert not dl.trial_on() and dl.default_decider(market_audit(tmp_path / "audit", "KRX"), "KRX", DAYS[0]) == "rule"


@pytest.mark.unit
def test_five_model_days_then_the_rule_with_the_ending_line(trial, tmp_path):
    client, llm = _market(), FakeManager()
    audit = market_audit(tmp_path / "audit", "KRX")
    for i, day in enumerate(DAYS[:5]):
        assert dl.default_decider(audit, "KRX", day) == "llm"
        r = _decide(client, "KRX", day, tmp_path, llm)
        assert r.status == "ok" and r.counts["llm_calls"] > 0, r.notes
        assert _trial_lines(r) == [f"LLM 시험 {i + 1}/5회째"]
        assert dl.llm_trial_days(audit, "KRX") == DAYS[: i + 1]
    asked = len(llm.asked)
    r = _decide(client, "KRX", DAYS[5], tmp_path, NoManager())               # the model client is never touched
    assert r.status == "ok" and "llm_calls" not in r.counts
    assert _trial_lines(r) == ["LLM 시험 종료 → 규칙 (5/5회 끝남)"]
    sixth = [d for d in audit.decisions() if d.trade_date == DAYS[5]]
    assert sixth and all(d.settings["decider"] == "rule_momentum_baseline" for d in sixth)
    assert len(llm.asked) == asked and dl.llm_trial_days(audit, "KRX") == DAYS[:5]


@pytest.mark.unit
def test_each_market_counts_its_own_days(trial, tmp_path):
    client, llm = _market(), FakeManager()
    for day in DAYS[:5]:
        _decide(client, "KRX", day, tmp_path, llm)
    krx, us = market_audit(tmp_path / "audit", "KRX"), market_audit(tmp_path / "audit", "US")
    assert dl.default_decider(krx, "KRX", DAYS[5]) == "rule"
    assert dl.default_decider(us, "US", DAYS[5]) == "llm" and dl.llm_trial_days(us, "US") == []
    r = _decide(_market(market="US"), "US", DAYS[0], tmp_path, llm)
    assert r.status == "ok" and r.counts["llm_calls"] > 0 and _trial_lines(r) == ["LLM 시험 1/5회째"]
    assert dl.llm_trial_days(us, "US") == [DAYS[0]] and dl.llm_trial_days(krx, "KRX") == DAYS[:5]


@pytest.mark.unit
def test_an_explicit_decider_always_wins(trial, tmp_path):
    client, llm = _market(), FakeManager()
    r = _decide(client, "KRX", DAYS[0], tmp_path, NoManager(), decider="rule")         # in the trial: still the rule
    assert r.status == "ok" and "llm_calls" not in r.counts and not _trial_lines(r)
    audit = market_audit(tmp_path / "audit", "KRX")
    assert dl.llm_trial_days(audit, "KRX") == []                                       # a rule day is no trial day
    for day in DAYS[1:6]:
        _decide(client, "KRX", day, tmp_path, llm)
    assert dl.llm_trial_days(audit, "KRX") == DAYS[1:6]
    assert dl.default_decider(audit, "KRX", "2026-08-12") == "rule"
    client.frames = _market(days=pd.bdate_range("2025-06-02", "2026-08-12")).frames
    r = _decide(client, "KRX", "2026-08-12", tmp_path, llm, decider="llm")             # past the trial: the model
    assert r.status == "ok" and r.counts["llm_calls"] > 0 and not _trial_lines(r)


@pytest.mark.unit
def test_a_day_run_again_is_counted_once(trial, tmp_path):
    client, llm = _market(), FakeManager()
    for day in DAYS[:5]:
        _decide(client, "KRX", day, tmp_path, llm)
    audit = market_audit(tmp_path / "audit", "KRX")
    lines = len(audit.runs())
    assert _decide(client, "KRX", DAYS[4], tmp_path, llm) is None and len(audit.runs()) == lines   # done: nothing new
    # the fifth day itself stays the model's (a retry of a failed run of it), the next one is the rule's
    assert dl.default_decider(audit, "KRX", DAYS[4]) == "llm" and dl.trial_note(audit, "KRX", DAYS[4], "llm") == \
        "LLM 시험 5/5회째"
    assert dl.default_decider(audit, "KRX", DAYS[5]) == "rule"


@pytest.mark.unit
def test_rule_stand_ins_and_grid_calls_are_no_trial_days(trial, tmp_path):
    audit = market_audit(tmp_path / "audit", "KRX")
    run = _screen(DAYS[0])
    audit.record_decision(dl.rule_fallback("KRX", run, "A.KS", DAYS[0], "호출 상한"))        # the model never asked
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=DAYS[1], screen_run_id=f"grid_{DAYS[1]}",
                                         rating="Buy", thesis="t", settings={"decider": MODEL}))
    assert dl.llm_trial_days(audit, "KRX") == []


@pytest.mark.unit
def test_no_end_when_trial_sessions_is_none(trial, tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "trial_sessions", None)
    audit = market_audit(tmp_path / "audit", "KRX")
    for i in range(7):
        audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=f"2026-08-{i + 1:02d}", rating="Buy",
                                             screen_run_id=f"daily_krx_2026-08-{i + 1:02d}", thesis="t",
                                             settings={"decider": MODEL}))
    assert dl.default_decider(audit, "KRX", "2026-08-10") == "llm"
    assert dl.trial_note(audit, "KRX", "2026-08-10", "llm") == "LLM 시험 (기한 없음)"


# ---- the report over three hand-made days -------------------------------------------------------------------------

R1, R2, R3, R4 = "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"
USAGE = {"input_tokens": 1000, "output_tokens": 800, "cache_read_input_tokens": 1500, "cache_creation_input_tokens": 0}


def _cache(data: Path):
    """A.KS 100 → 110 from R3; B.KS flat 100; the index 1000 → 1020 from R3; C.KS has no file (never settles)."""
    days = pd.bdate_range("2026-07-20", "2026-08-31")
    cache = data / "cache"
    cache.mkdir(parents=True)
    for symbol, before, after in (("A.KS", 100.0, 110.0), ("B.KS", 100.0, 100.0), ("^KS11", 1000.0, 1020.0)):
        close = [after if d >= pd.Timestamp(R3) else before for d in days]
        pd.DataFrame({"Date": days, "Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0}) \
            .to_csv(kis.cache_path(symbol, cache), index=False)


def _book(data: Path):
    audit = market_audit(data / "audit", "KRX")
    for day in (R1, R2, R3):
        audit.record_screen(_screen(day))                    # A Buy · B Overweight · C Underweight · D Sell by rule

    def llm(ticker, day, rating, usage=USAGE, thesis="t", **settings):
        audit.record_decision(DecisionRecord(ticker=ticker, trade_date=day, screen_run_id=f"daily_krx_{day}",
                                             rating=rating, thesis=thesis, usage=usage,
                                             settings={"decider": MODEL, **settings}))
    llm("A.KS", R1, "Buy")                                                                  # rule Buy: agrees
    llm("B.KS", R1, "Hold")                                                                 # rule Overweight
    llm("A.KS", R2, "REVIEW", usage={"input_tokens": 500}, thesis="**사유**: 거부(refusal)\n원문")   # rule Buy
    llm("C.KS", R3, "Underweight", rerun_of="paper-x")                                      # rule Underweight: agrees
    audit.record_decision(dl.rule_fallback("KRX", _screen(R2), "B.KS", R2, "호출 상한"))     # a stand-in, not the model's
    grid = _screen(R2).model_copy(update={"run_id": f"baseline_krx_{R2}_llm"})
    audit.record_decision(dl.rule_fallback("KRX", grid, "C.KS", R2, "호출 상한"))           # a grid's stand-in: not the loop's
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=R4, screen_run_id=f"grid_{R4}", rating="Buy",
                                         thesis="t", usage=USAGE, settings={"decider": MODEL}))   # a grid: not the trial
    for oid, ticker, day, qty, status in (
            ("o1", "A.KS", R1, 1, "filled"),
            ("o2", "B.KS", R1, 1, "open"),
            ("o2", "B.KS", R1, 1, "filled"),
            ("o3", "A.KS", R2, 0, "exception"),
            ("o4", "C.KS", R3, 0, "blocked"),
            ("o5", "D.KS", R4, 1, "filled"),
    ):
        audit.record_order(OrderRecord(order_id=oid, ticker=ticker, trade_date=day, side="Buy", quantity=qty,
                                       status=status))
    return audit


@pytest.mark.unit
def test_the_report_matches_a_hand_count(store, tmp_path, monkeypatch, capsys):
    def yahoo(*a):
        pytest.fail("the report reads the KIS cache, not Yahoo")
    monkeypatch.setattr(settlement, "get_closes", yahoo)
    _cache(tmp_path)
    r = report.trial_report(_book(tmp_path), "KRX", tmp_path / "cache")
    # cost: three calls of 1000·4 + 800·20 + 1500·0.2 = 20,300 µ$ and one of 500·4 = 2,000 µ$ → $0.0629
    assert r | {"alpha_by_rating": None, "whatif": None} == {
        "market": "KRX", "trial_sessions": 5, "runs": 3, "first": R1, "last": R3, "days": [R1, R2, R3],
        "decisions": 4, "ratings": {"Buy": 1, "Hold": 1, "REVIEW": 1, "Underweight": 1}, "review": 1,
        "review_reasons": [f"{R2} A.KS: 거부(refusal)"], "rule_stand_ins": 1, "reruns": 1,
        "usage": {"input_tokens": 3500, "output_tokens": 2400, "cache_read_input_tokens": 4500,
                  "cache_creation_input_tokens": 0}, "cost_usd": 0.0629,
        "rule_compared": 4, "rule_agree": 2, "rule_ratings": {"Buy": 2, "Overweight": 1, "Underweight": 1},
        # A R1: in at R2 100, out five sessions on (R2+5 = 08-11) 110 → +10%, index 1000 → 1020 → alpha +8%;
        # B R1: 0 / −2%; A R2: in at R3 110 → 110, index 1020 → 1020 → 0 / 0; C R3: no prices → pending
        # the overall mean is the buy side's: A R1 Buy alone (+10% / +8%); Hold and REVIEW only by rating
        "settled": 3, "pending": 1, "settle_failed": [], "buy_settled": 1,
        "mean_raw": pytest.approx(0.10), "mean_alpha": pytest.approx(0.08),
        "orders": 4, "orders_by_status": {"filled": 2, "exception": 1, "blocked": 1}, "blocked": 1, "filled": 2,
        "alpha_by_rating": None, "whatif": None}
    # o4 C.KS: a blocked buy written without a plan — kept as a row that cannot be priced, not dropped
    assert [(w["ticker"], w["why"]) for w in r["whatif"]["rows"]] == [("C.KS", "제안 수량 없음(원안 기록 없음)")]
    assert r["alpha_by_rating"] == {"Buy": {"n": 1, "mean_alpha": pytest.approx(0.08)},
                                    "Hold": {"n": 1, "mean_alpha": pytest.approx(-0.02)},
                                    "REVIEW": {"n": 1, "mean_alpha": pytest.approx(0.0)}}
    text = report.render(r)
    assert "KRX LLM 시험 — 3/5회 (2026-08-03 ~ 2026-08-05)" in text and "비용 $0.0629 (하루 $0.0210)" in text
    assert "규칙 결정과 일치 2/4" in text and "정산 완료 3 · 대기 1" in text
    assert "매수 쪽(Buy·Overweight) 1건 평균 수익 +10.00% · 평균 알파 +8.00%" in text and "    - Hold: 1건 -2.00%" in text
    assert "하드 룰 차단(blocked) 1" in text
    assert "| C.KS | (규칙 없음) | – | 평가 불가: 제안 수량 없음(원안 기록 없음) |" in text
    assert "평가된 표본 0건(전체 1건) < 10건" in text
    assert report.main(["--market", "all", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "## KRX LLM 시험 — 3/5회" in out and "## US LLM 시험 — 0/5회 (– ~ –)\n- 시험 기록 없음" in out
    assert settlement.get_closes is yahoo                                          # put back after the report


# ---- the blocked buys, as if they had gone through (blocked_whatif) ------------------------------------------------

END = "2026-08-31"
UNIT = 1.0005 * 1.00015                     # KRX paper: 0.05% slippage, 0.015% commission per side
SELL = 0.9995 * (1 - 0.00015 - 0.002)       # − slippage, commission, 0.20% sell tax


def _whatif_cache(data: Path, *, stop_at: str | None = None):
    """X.KS opens and closes 100, then closes 110 from R2 (opens 101 on R2: not the decision day's close). Y.KS 200 to
    R2, 180 on R3 (a −10% close: the stop), opens 185 and closes 190 from R4. W.KS 20,000,000 a share, flat. The index
    opens 1000 every day, closes 1000 then 1050 from R2. Z.KS and V.KS have no file. ``stop_at``: X.KS's file ends
    there (the index goes on)."""
    days = pd.bdate_range("2026-07-20", END)
    cache = data / "cache"
    cache.mkdir(parents=True)
    r2, r3, r4 = pd.Timestamp(R2), pd.Timestamp(R3), pd.Timestamp(R4)
    frames = {
        "X.KS": ([101.0 if d == r2 else 100.0 for d in days], [110.0 if d >= r2 else 100.0 for d in days]),
        "Y.KS": ([185.0 if d >= r4 else 180.0 if d == r3 else 200.0 for d in days],
                 [190.0 if d >= r4 else 180.0 if d == r3 else 200.0 for d in days]),
        "W.KS": ([2e7] * len(days), [2e7] * len(days)),
        "^KS11": ([1000.0] * len(days), [1050.0 if d >= r2 else 1000.0 for d in days]),
    }
    for symbol, (opens, closes) in frames.items():
        f = pd.DataFrame({"Date": days, "Open": opens, "High": closes, "Low": closes, "Close": closes, "Volume": 1.0})
        if stop_at and symbol == "X.KS":
            f = f[f["Date"] <= pd.Timestamp(stop_at)]
        f.to_csv(kis.cache_path(symbol, cache), index=False)


def _blocked(audit, oid, ticker, day, rules, plan=None, side="Buy", status="blocked"):
    audit.record_order(OrderRecord(order_id=oid, ticker=ticker, trade_date=day, side=side, quantity=0, status=status,
                                   rules_applied=rules, market=audit.market, plan=plan))


def _plan(qty):
    return OrderPlan(proposed=PlanSide(side="Buy", qty=qty, weight_pct=0.2, amount=0.0), final=PlanSide(side="Hold"))


def _whatif_book(data: Path):
    """R1's buy list X · Y · Z · V · W (five names: 20% each, cut to the 10% per-name cap); every buy blocked."""
    audit = market_audit(data / "audit", "KRX")
    for t in ("X.KS", "Y.KS", "Z.KS", "V.KS", "W.KS"):
        audit.record_decision(DecisionRecord(ticker=t, trade_date=R1, screen_run_id=f"daily_krx_{R1}", rating="Buy",
                                             thesis="t", settings={"decider": MODEL}))
    _blocked(audit, "x", "X.KS", R1, ["regime"], _plan(49_000))                   # the plan's 20%: not what it sizes
    _blocked(audit, "y", "Y.KS", R1, ["regime"], _plan(24_000))
    _blocked(audit, "z", "Z.KS", R1, ["regime"], _plan(5))                       # no prices at all
    _blocked(audit, "v", "V.KS", R1, ["already_held"])                           # not sized: no plan
    _blocked(audit, "w", "W.KS", R1, ["max_position_pct"], _plan(1))             # 10% < one share: a size cap blocked it
    _blocked(audit, "s", "S.KS", R1, ["no_short"], side="Sell")                  # a blocked sell: not a buy
    audit.record_order(OrderRecord(order_id="f", ticker="F.KS", trade_date=R1, side="Buy", quantity=1, status="filled",
                                   filled_quantity=1, filled_price=100.0, fill_date=R2))   # it went through
    _blocked(audit, "late", "X.KS", R4, ["regime"], _plan(10))                   # R4 is no trial day
    us = market_audit(data / "audit", "US")
    _blocked(us, "u", "AAPL", R1, ["regime"], _plan(3))                          # the other market's book
    return audit, us


@pytest.mark.unit
def test_the_blocked_buys_as_if_filled_match_a_hand_count(store, tmp_path):
    _whatif_cache(tmp_path)
    audit, us = _whatif_book(tmp_path)
    days = dl.llm_trial_days(audit, "KRX")
    assert days == [R1]
    w = report.blocked_whatif(audit, "KRX", tmp_path / "cache", days)
    assert w["until"] == END and w["benchmark"] == "^KS11" and w["provisional"] is False
    assert [r["ticker"] for r in w["rows"]] == ["X.KS", "Y.KS", "Z.KS", "V.KS", "W.KS"]   # the day's decision order;
    rows = {r["ticker"]: r for r in w["rows"]}                                  # no sell, fill, other day, US name
    # the executor's size: 1억 × 10% (the per-name cap over 20%) ÷ (R1's close × UNIT), not the plan's 49,000
    x_qty, y_qty = int(1e7 // (100 * UNIT)), int(1e7 // (200 * UNIT))
    assert (x_qty, y_qty) == (99_935, 49_967)
    # in at R2's open × (1 + slippage), commission on top; marked at END's close less slippage, commission, sell tax
    x_cost, x_value = x_qty * 101 * UNIT, x_qty * 110 * SELL
    x = rows["X.KS"]
    assert (x["qty"], x["sizing"], x["entry"], x["entry_price"]) == (x_qty, "실행기 크기", R2, pytest.approx(101.0505))
    assert x["cost"] == pytest.approx(x_cost) and x["hold_pnl"] == pytest.approx(x_value - x_cost)
    # +8.86% from the fill price: under the trailing's +10% start, no stop, 20 sessions not up → held by the rules too
    assert x["rule_pnl"] == pytest.approx(x_value - x_cost) and x["exit"] is None
    assert x["horizon"] == {5: pytest.approx(x_value / x_cost - 1), 10: pytest.approx(x_value / x_cost - 1)}
    assert x["bench"] == pytest.approx(0.05) and x["marked"] == END             # 1000 at R2's open → 1050
    y_cost, y_hold, y_exit = y_qty * 200 * UNIT, y_qty * 190 * SELL, y_qty * 185 * SELL   # R3's 180: −10.0% ≤ −8%
    y = rows["Y.KS"]
    assert y["hold_pnl"] == pytest.approx(y_hold - y_cost)
    assert y["rule_pnl"] == pytest.approx(y_exit - y_cost) and y["exit"] == f"손절 {R4} 시가"
    assert rows["Z.KS"]["why"] == f"결정일 {R1} 종가 없음 — 수량을 정할 수 없음" and rows["Z.KS"]["cost"] is None
    assert rows["V.KS"]["why"] == "제안 수량 없음(원안 기록 없음)" and rows["V.KS"]["cost"] is None
    # W: 10% of 1억 buys no 2천만 share — the cap was the block, so the plan's own 1 share, marked as the plan's
    wr = rows["W.KS"]
    w_cost, w_value = 1 * 2e7 * UNIT, 1 * 2e7 * SELL
    assert (wr["qty"], wr["sizing"]) == (1, "원안(규칙 전)") and wr["hold_pnl"] == pytest.approx(w_value - w_cost)
    t = w["total"]
    assert (t["n"], t["priced"], t["unpriced"], t["unbenched"]) == (5, 3, 2, 0)
    assert t["cost"] == pytest.approx(x_cost + y_cost + w_cost)
    assert t["hold_pnl"] == pytest.approx(x_value + y_hold + w_value - x_cost - y_cost - w_cost)
    assert t["rule_pnl"] == pytest.approx(x_value + y_exit + w_value - x_cost - y_cost - w_cost)
    assert t["hold_pct"] == pytest.approx((x_value + y_hold + w_value) / (x_cost + y_cost + w_cost) - 1)
    assert t["bench_pct"] == pytest.approx(0.05)
    regime = w["by_rule"]["regime"]
    assert (regime["n"], regime["priced"], regime["unpriced"]) == (3, 2, 1)
    assert regime["hold_pct"] == pytest.approx((x_value + y_hold) / (x_cost + y_cost) - 1)
    assert w["by_rule"]["already_held"]["cost"] is None and w["by_rule"]["already_held"]["hold_pct"] is None
    text = "\n".join(report.render_whatif(w, "KRX"))
    assert f"평가일 {END} 종가 · 시험 진행 중이면 중간값" in text and "잠정" not in text
    assert "평가된 표본 3건(전체 5건) < 10건" in text and "실제 장부는 막혀서 현금 그대로(0 변동)" in text
    hold = x_value + y_hold - x_cost - y_cost
    assert (f"| regime | 3 | 2 | 1 | {x_cost + y_cost:,.0f}원 | +{hold:,.0f}원 "
            f"({(x_value + y_hold) / (x_cost + y_cost) - 1:+.2%}) |") in text
    assert "| already_held | 1 | 0 | 1 | – | – (–) | – (–) | – | 0 |" in text
    assert f"| {R1} | X.KS | regime | {x_qty} (실행기 크기) | {R2} 101원 | +8.55% | +8.55% |" in text
    assert f"| {R1} | Y.KS | regime | {y_qty} (실행기 크기) | {R2} 200원 | -5.31% | -7.80% (손절 {R4} 시가) |" in text
    assert f"| {R1} | W.KS | max_position_pct | 1 (원안(규칙 전)) |" in text
    assert f"| {R1} | Z.KS | regime | – | 평가 불가: 결정일 {R1} 종가 없음 — 수량을 정할 수 없음 |" in text
    for line in text.splitlines():                        # facts only: no verdict sentence
        assert not any(word in line for word in ("결론", "했어야", "좋았", "나빴"))
    # the US book has its own row, priced on its own calendar (no ^GSPC file here: no entry day yet)
    u = report.blocked_whatif(us, "US", tmp_path / "cache", [R1])
    assert [r["ticker"] for r in u["rows"]] == ["AAPL"] and u["rows"][0]["cost"] is None


@pytest.mark.unit
def test_the_session_buy_cap_and_the_per_order_cap_apply(store, tmp_path, monkeypatch):
    caps = {"auto_order_max": 5_000_000, "auto_daily_order_cap": 9_000_000}
    monkeypatch.setattr(report, "auto_execution", lambda market: caps)
    _whatif_cache(tmp_path)
    audit, _ = _whatif_book(tmp_path)
    rows = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}
    # X: 10% = 99,935 shares × 100 > 5,000,000 → int(5,000,000 / 100) = 50,000 (D1 shrink); Y the same → 25,000.
    # The session: X 5,000,000, then Y 5,000,000 more > 9,000,000 → the executor's exception, not a fill
    assert rows["X.KS"]["qty"] == 50_000 and rows["X.KS"]["cost"] == pytest.approx(50_000 * 101 * UNIT)
    assert rows["Y.KS"]["qty"] == 25_000 and rows["Y.KS"]["cost"] is None
    assert rows["Y.KS"]["why"] == "일일 매수 상한 9,000,000원 초과 — 실행기라면 예외(체결 안 됨)"
    # W (a size-cap block): the plan's one share of 2천만 is over 5,000,000 a order too — the executor's cap_zero
    assert rows["W.KS"]["cost"] is None and rows["W.KS"]["why"] == "원안 1주도 주문당 상한 5,000,000원 초과(주문 안 됨)"


@pytest.mark.unit
def test_a_cache_one_session_short_is_a_data_delay(store, tmp_path):
    _whatif_cache(tmp_path, stop_at=R1)                    # X.KS ends at R1, the index has R2
    audit, _ = _whatif_book(tmp_path)
    x = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}["X.KS"]
    assert x["why"] == f"진입일 {R2} 시세가 캐시에 아직 없음(마지막 {R1}) — 데이터 지연" and x["entry"] == R2


@pytest.mark.unit
def test_the_sample_warning_counts_the_priced_rows(store, tmp_path):
    _whatif_cache(tmp_path)
    audit = market_audit(tmp_path / "audit", "KRX")
    audit.record_decision(DecisionRecord(ticker="X.KS", trade_date=R1, screen_run_id=f"daily_krx_{R1}", rating="Buy",
                                         thesis="t", settings={"decider": MODEL}))
    for k in range(11):                                    # eleven blocked buys, eleven of them with no plan
        _blocked(audit, f"v{k}", f"V{k}.KS", R1, ["already_held"])
    _blocked(audit, "x", "X.KS", R1, ["regime"], _plan(10))
    w = report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])
    assert (w["total"]["n"], w["total"]["priced"]) == (12, 1)
    assert "- 평가된 표본 1건(전체 12건) < 10건 — 몇 건의 결과라 우연의 폭이 크다" in report.render_whatif(w, "KRX")


@pytest.mark.unit
def test_a_row_left_out_of_the_benchmark_is_counted(store, tmp_path):
    _whatif_cache(tmp_path)
    f = pd.read_csv(kis.cache_path("^KS11", tmp_path / "cache"))
    f.loc[f["Date"] == R2, "Open"] = float("nan")          # the index has no open on the entry day
    f.to_csv(kis.cache_path("^KS11", tmp_path / "cache"), index=False)
    audit, _ = _whatif_book(tmp_path)
    w = report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])
    assert w["total"]["unbenched"] == 3 and w["total"]["bench_pct"] is None
    assert "| – (3건 제외 — 지수 진입일 시가 없음) | 0 |" in "\n".join(report.render_whatif(w, "KRX"))


@pytest.mark.unit
def test_no_blocked_buy_says_zero(store, tmp_path):
    _whatif_cache(tmp_path)
    audit = market_audit(tmp_path / "audit", "KRX")
    w = report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])
    assert w["rows"] == [] and w["total"]["n"] == 0
    text = report.render_whatif(w, "KRX")
    assert text[-1] == "- 시험 기간 막힌 매수 0건" and not any("표본" in line for line in text)


@pytest.mark.unit
def test_a_provisional_close_on_the_valuation_day_is_marked(store, tmp_path):
    _whatif_cache(tmp_path)
    (tmp_path / "cache" / "_provisional.json").write_text(f'{{"{END}": ["^KS11"]}}', encoding="utf-8")
    audit, _ = _whatif_book(tmp_path)
    w = report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])
    assert w["provisional"] is True and "잠정(장 마감 전) 값 포함" in report.render_whatif(w, "KRX")[1]


@pytest.mark.unit
def test_a_regime_block_the_sector_cap_sizes_to_nothing_is_not_priced(store, tmp_path, monkeypatch):
    """Three regime-blocked names of one sector: 10% · 10% fill the sector's 20%, the third sizes to 0 — no buy even
    without the regime, so not the plan's quantity. And a size-cap block whose plan is over the per-order cap is cut."""
    monkeypatch.setattr(report, "sector_of", lambda t: "금융" if t in ("X.KS", "Y.KS", "W.KS") else None)
    _whatif_cache(tmp_path)
    audit, _ = _whatif_book(tmp_path)
    _blocked(audit, "w", "W.KS", R1, ["regime"], _plan(1))                       # W now blocked by the regime
    rows = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}
    assert rows["X.KS"]["qty"] == 99_935 and rows["Y.KS"]["qty"] == 49_967
    assert rows["W.KS"]["cost"] is None and rows["W.KS"]["qty"] == 0
    assert rows["W.KS"]["why"] == ("막은 규칙 없이도 실행기 크기 0주 — max_position_pct: W.KS 20.0% → 10.0% · "
                                   "sector_cap: W.KS (금융) 10.0% → 0.0%(주문 안 됨)")


@pytest.mark.unit
def test_a_size_cap_block_takes_the_plan_cut_to_the_per_order_cap(store, tmp_path):
    _whatif_cache(tmp_path)
    audit, _ = _whatif_book(tmp_path)
    _blocked(audit, "w", "W.KS", R1, ["max_position_pct"], _plan(3))             # 3 × 2천만 > 3천만 per order
    w = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}["W.KS"]
    assert (w["qty"], w["sizing"]) == (1, "원안(규칙 전) → 주문당 상한")       # int(30,000,000 / 20,000,000)
    _blocked(audit, "w", "W.KS", R1, ["auto_order_max", "cap_zero"], _plan(3))   # one share over the cap blocked it
    w = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}["W.KS"]
    assert (w["qty"], w["sizing"]) == (3, "원안(규칙 전)")


@pytest.mark.unit
def test_the_buy_list_is_the_days_last_agent_decision_per_name(store, tmp_path):
    _whatif_cache(tmp_path)
    audit, _ = _whatif_book(tmp_path)
    for t in ("Y.KS", "Z.KS", "V.KS"):                    # re-recorded Hold: off the buy list, as execute_date reads it
        audit.record_decision(DecisionRecord(ticker=t, trade_date=R1, screen_run_id=f"daily_krx_{R1}", rating="Hold",
                                             thesis="t", settings={"decider": MODEL}))
    rows = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}
    assert rows["X.KS"]["qty"] == 99_935
    assert rows["Y.KS"]["cost"] is None and rows["Y.KS"]["why"] == "그날 마지막 결정이 매수 등급이 아님(실행기 매수 목록에 없음)"


@pytest.mark.unit
def test_a_retried_order_blocked_on_its_retry_day_says_so(store, tmp_path):
    _whatif_cache(tmp_path)
    audit, _ = _whatif_book(tmp_path)
    audit.record_order(OrderRecord(order_id="x", ticker="X.KS", trade_date=R1, side="Buy", quantity=0, status="blocked",
                                   rules_applied=["regime"], market="KRX", plan=_plan(10), retry_on=R2, retries=1))
    x = {r["ticker"]: r for r in report.blocked_whatif(audit, "KRX", tmp_path / "cache", [R1])["rows"]}["X.KS"]
    assert x["cost"] is None and x["why"] == f"재시도 주문(원 결정 {R1}, 재시도 {R2}) — 재시도 크기는 재생하지 않음"
