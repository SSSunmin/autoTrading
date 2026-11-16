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
from agent_trading_core.audit import DecisionRecord, OrderRecord, market_audit
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
    assert r | {"alpha_by_rating": None} == {
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
        "alpha_by_rating": None}
    assert r["alpha_by_rating"] == {"Buy": {"n": 1, "mean_alpha": pytest.approx(0.08)},
                                    "Hold": {"n": 1, "mean_alpha": pytest.approx(-0.02)},
                                    "REVIEW": {"n": 1, "mean_alpha": pytest.approx(0.0)}}
    text = report.render(r)
    assert "KRX LLM 시험 — 3/5회 (2026-08-03 ~ 2026-08-05)" in text and "비용 $0.0629 (하루 $0.0210)" in text
    assert "규칙 결정과 일치 2/4" in text and "정산 완료 3 · 대기 1" in text
    assert "매수 쪽(Buy·Overweight) 1건 평균 수익 +10.00% · 평균 알파 +8.00%" in text and "    - Hold: 1건 -2.00%" in text
    assert "하드 룰 차단(blocked) 1" in text
    assert report.main(["--market", "all", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "## KRX LLM 시험 — 3/5회" in out and "## US LLM 시험 — 0/5회 (– ~ –)\n- 시험 기록 없음" in out
    assert settlement.get_closes is yahoo                                          # put back after the report
