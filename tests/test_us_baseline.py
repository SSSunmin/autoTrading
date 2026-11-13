"""scripts/baseline_krx.py --market: each market's book, index, preset and record paths (no network: the screen and
the backtest are replaced by fakes that call the script's own decide)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from agent_trading_core.audit import CandidateRecord, DecisionRecord, ScreenRun, baseline_summary_path
from agent_trading_core.screening import ScreenConfig, us_preset

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bk = _load("baseline_krx")
pb = _load("paper_backtest")
mock = _load("build_mockup")


@pytest.mark.unit
def test_summary_path_keeps_krx_where_it_was_and_puts_us_under_its_audit_dir(tmp_path):
    assert baseline_summary_path(tmp_path, "KRX") == tmp_path / "baseline_summary.json"
    assert baseline_summary_path(tmp_path, "US") == tmp_path / "audit" / "US" / "baseline_summary.json"


@pytest.mark.unit
def test_setups_krx_is_the_pre_market_axis_run_and_us_is_the_paper_book_with_the_us_preset():
    krx, us = bk.SETUPS["KRX"], bk.SETUPS["US"]
    # the values the script ran with before --market
    assert krx["universe"] == bk.UNIVERSE and len(krx["universe"]) == 20 and krx["universe"][0] == "005930.KS"
    assert krx["screen"] == ScreenConfig(dollar_volume_top_n=15, top_k=5, sector_max=3, volatility_min=0.10)
    assert (krx["label"], krx["run_id"]) == ("KOSPI200 sample (20)", "baseline_krx_v2")
    assert us["universe"] == pb.US_UNIVERSE and len(us["universe"]) == 40
    assert us["screen"] == us_preset(krx["screen"]) and us["screen"].price_min != krx["screen"].price_min
    assert not any(t.endswith(".KS") for t in us["universe"])


def _fake_run(monkeypatch, captured):
    def fake_screen(universe, date, cfg, cache_dir, universe_label, sector_of, run_id):
        captured.setdefault("screen_cfg", cfg)
        captured.setdefault("label", universe_label)
        # first four ranked 1..4 (Buy, Overweight, Underweight, Sell over 4), the rest unranked (Hold)
        cands = [CandidateRecord(ticker=t, rank=i + 1 if i < 4 else None, selected=i < 4) for i, t in enumerate(universe)]
        return ScreenRun(run_id=run_id, as_of=date, universe=universe_label, universe_size=len(universe), rules=[],
                         candidates=cands)

    def fake_backtest(universe, dates, decide, cfg, reflector, run_id, progress):
        captured["cfg"], captured["run_id"] = cfg, run_id
        for d in dates:
            for t in universe:
                decide(t, d)
        return SimpleNamespace(run_id=run_id, log_path=Path(cfg["results_dir"]) / "log.md", cells_run=len(universe) * len(dates),
                               skipped=0, failures=[], settlement_failures=[])

    summary = SimpleNamespace(resolved=0, pending=0, unscored=0, by_rating={}, holding="5 trading days", render=lambda: "")
    monkeypatch.setattr(bk, "screen", fake_screen)
    monkeypatch.setattr(bk, "run_backtest", fake_backtest)
    monkeypatch.setattr(bk, "summarize", lambda result: summary)
    monkeypatch.setattr(bk, "korean_name", lambda t: None)
    monkeypatch.setattr(bk, "name_of", lambda t: None)


@pytest.mark.unit
@pytest.mark.parametrize("market, benchmark, run_id, currency", [
    ("US", "^GSPC", "baseline_us_v1", "USD"),
    ("KRX", "^KS11", "baseline_krx_v2", "KRW"),
])
def test_a_run_writes_only_its_own_book(tmp_path, monkeypatch, market, benchmark, run_id, currency):
    captured: dict = {}
    _fake_run(monkeypatch, captured)
    # the KRX book from before the market axis, straight under data/audit/: read as KRX, never written
    audit = tmp_path / "audit"
    audit.mkdir()
    old = DecisionRecord(ticker="005930.KS", trade_date="2026-06-05", screen_run_id="old_run", rating="Buy",
                         action="Buy", thesis="old").model_dump_json() + "\n"
    (audit / "decisions.jsonl").write_text(old, encoding="utf-8")
    (audit / "screens.jsonl").write_text("", encoding="utf-8")
    legacy = {p.name: p.read_bytes() for p in audit.glob("*.jsonl")}
    assert bk.main(["--market", market, "--dates", "2"], data=tmp_path) == 0

    assert {p.name: p.read_bytes() for p in audit.glob("*.jsonl")} == legacy
    setup = bk.SETUPS[market]
    assert captured["cfg"]["benchmark_ticker"] == benchmark                 # alpha against MARKETS[market]
    assert captured["screen_cfg"] == setup["screen"] and captured["label"] == setup["label"]
    assert captured["run_id"] == run_id
    # records only in data/audit/<market>/: nothing in the other book
    other = "KRX" if market == "US" else "US"
    assert not (audit / other).exists()
    decisions = [json.loads(line) for line in (audit / market / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(decisions) == 2 * 4 and {d["market"] for d in decisions} == {market}  # 4 non-Hold names x 2 dates
    assert all(d["screen_run_id"].startswith(run_id) for d in decisions)
    summary = json.loads(baseline_summary_path(tmp_path, market).read_text(encoding="utf-8"))
    assert (summary["market"], summary["currency"], summary["benchmark"]) == (market, currency, benchmark)
    assert summary["universe"] == setup["universe"]
    assert summary["cells_run"] == len(setup["universe"]) * 2


@pytest.mark.unit
def test_build_mockup_reads_the_markets_book(tmp_path, monkeypatch):
    captured: dict = {}
    _fake_run(monkeypatch, captured)
    bk.main(["--market", "US", "--dates", "1"], data=tmp_path)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "log.md").write_text("", encoding="utf-8")
    payload = mock.build_payload(tmp_path, "US")
    assert payload["generated_from"]["run_id"] == "baseline_us_v1"
    assert {d["market"] for d in payload["decisions"]} == {"US"} and len(payload["decisions"]) == 4
    with pytest.raises(FileNotFoundError):                 # no KRX summary here: KRX does not fall back to the US one
        mock.build_payload(tmp_path, "KRX")


@pytest.mark.unit
def test_the_llm_decider_asks_only_about_survivors_up_to_the_cap_then_falls_back_to_rule(tmp_path, monkeypatch):
    from anthropic.types import Message

    _fake_run(monkeypatch, {})
    days = pd.bdate_range("2026-01-02", "2026-12-31")
    monkeypatch.setattr(bk, "load_ohlcv", lambda t, d, cache_dir, fill_gaps: pd.DataFrame(
        {"Date": days, "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1e6}))
    asked = []
    memo = {"business_summary": "b", "thesis": "t", "bull_case": "u", "bear_case": "d", "valuation_basis": "v",
            "current_price": 100, "price_target": 130, "downside_price": 90, "catalysts": [{"event": "e"}],
            "invalidation": ["i"], "rating": "Overweight", "conviction": 0.5, "exit_plan": "x", "time_horizon": "1y"}

    def create(**kw):
        asked.append(kw["messages"][0]["content"])
        return Message.model_validate({"id": "m", "type": "message", "role": "assistant", "model": kw["model"],
                                       "content": [{"type": "text", "text": json.dumps(memo)}], "stop_reason": "end_turn",
                                       "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(bk.agent, "make_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setitem(bk.LLM, "max_calls_per_run", 5)
    assert bk.main(["--market", "US", "--dates", "2", "--decider", "llm"], data=tmp_path) == 0
    decisions = [DecisionRecord.model_validate_json(line) for line in
                 (tmp_path / "audit" / "US" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(d.screen_run_id.startswith("baseline_us_v1_llm_") for d in decisions)    # apart from the rule run
    agent_made = [d for d in decisions if d.settings["decider"] == "claude-opus-5-5"]
    fallback = [d for d in decisions if d.settings.get("fallback") == "rule"]
    # 4 survivors x 2 dates: 5 asked, 3 by rule; the unranked rest stay rule Holds (not recorded)
    assert len(asked) == len(agent_made) == 5 and len(fallback) == 3 and len(decisions) == 8
    assert all(d.rating == "Overweight" and d.source == "agent" for d in agent_made)
    assert all(d.settings["fallback_reason"] == "max_calls_per_run(5)" for d in fallback)
