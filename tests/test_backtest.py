"""Backtesting any decide(ticker, date) over a grid, scored by the memory log.

Adapted from TradingAgents' test_backtest.py: the graph stand-in is replaced by a
plain decision function, which is what the extracted runner takes.
"""

from __future__ import annotations

import pytest

from agent_trading_core.backtest import Decision, iter_grid, run_backtest, summarize
from agent_trading_core.memory import TradingMemoryLog

DECISION = "Rating: Buy\n\nbuy it"


@pytest.mark.unit
def test_grid_spacing_and_canonical_dates():
    assert iter_grid("2026-01-05", "2026-01-20", every_n_days=7) == ["2026-01-05", "2026-01-12", "2026-01-19"]


@pytest.mark.unit
def test_grid_stops_at_today(monkeypatch):
    import agent_trading_core.backtest as bt

    monkeypatch.setattr(bt, "get_current_date", lambda: "2026-01-10")
    assert iter_grid("2026-01-05", "2026-02-20", every_n_days=5) == ["2026-01-05", "2026-01-10"]


@pytest.mark.unit
def test_grid_rejects_a_non_canonical_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        iter_grid("2026-1-5", "2026-01-20")


def _config(tmp_path):
    return {"results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "live" / "trading_memory.md"),
            "holding_period_days": 5, "benchmark_map": {"": "SPY"}}


class _Decider:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, ticker, date):
        self.calls.append((ticker, date))
        if (ticker, date) in self.fail_on:
            raise RuntimeError("vendor exploded")
        return Decision(DECISION, "Buy")


@pytest.mark.unit
def test_every_cell_is_decided_and_logged_in_the_run_log(tmp_path):
    decide = _Decider()
    result = run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], decide, _config(tmp_path))

    assert result.cells_run == 4 and result.failures == []
    assert sorted(decide.calls) == sorted([("NVDA", "2026-01-05"), ("NVDA", "2026-01-12"),
                                           ("AAPL", "2026-01-05"), ("AAPL", "2026-01-12")])
    entries = TradingMemoryLog({"memory_log_path": str(result.log_path)}).load_entries()
    assert {(e["ticker"], e["date"]) for e in entries} == set(decide.calls)
    assert all(e["rating"] == "Buy" and e["pending"] for e in entries)


@pytest.mark.unit
def test_the_live_memory_log_is_never_written(tmp_path):
    config = _config(tmp_path)
    run_backtest(["NVDA"], ["2026-01-05"], _Decider(), config)
    assert not (tmp_path / "live" / "trading_memory.md").exists()


@pytest.mark.unit
def test_a_failed_cell_is_reported_and_the_sweep_goes_on(tmp_path):
    decide = _Decider(fail_on={("NVDA", "2026-01-05")})
    result = run_backtest(["NVDA", "AAPL"], ["2026-01-05"], decide, _config(tmp_path))
    assert result.cells_run == 1
    assert result.failures == [("NVDA", "2026-01-05", "vendor exploded")]


@pytest.mark.unit
def test_a_resumed_sweep_skips_the_cells_already_in_its_log(tmp_path):
    config = _config(tmp_path)
    first = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _Decider(), config)
    decide = _Decider()
    second = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12", "2026-01-19"], decide, config,
                          run_id=first.run_id)
    assert second.skipped == 2 and second.cells_run == 1
    assert decide.calls == [("NVDA", "2026-01-19")]


@pytest.mark.unit
def test_a_ticker_or_date_given_twice_runs_once(tmp_path):
    decide = _Decider()
    result = run_backtest(["NVDA", "NVDA"], ["2026-01-05", "2026-01-05"], decide, _config(tmp_path))
    assert result.cells_run == 1 and decide.calls == [("NVDA", "2026-01-05")]


@pytest.mark.unit
def test_progress_is_reported_before_each_cell(tmp_path):
    seen = []
    run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _Decider(), _config(tmp_path),
                 progress=lambda done, total, t, d: seen.append((done, total, t, d)))
    assert seen == [(1, 2, "NVDA", "2026-01-05"), (2, 2, "NVDA", "2026-01-12")]


@pytest.mark.unit
def test_a_decision_without_a_rating_is_logged_as_review(tmp_path):
    result = run_backtest(["NVDA"], ["2026-01-05"], lambda t, d: Decision("I cannot say."), _config(tmp_path))
    entries = TradingMemoryLog({"memory_log_path": str(result.log_path)}).load_entries()
    assert entries[0]["rating"] == "REVIEW"


def _settled_log(tmp_path, rows):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    for i, (rating, alpha) in enumerate(rows):
        date = f"2026-01-{i + 1:02d}"
        log.store_decision("NVDA", date, f"Rating: {rating}\n\nx")
        if alpha is not None:
            log.update_with_outcome("NVDA", date, alpha, alpha, 5, "note", "2026-02-01")
    return tmp_path / "m.md"


@pytest.mark.unit
def test_summary_scores_resolved_cells_and_keeps_pending_out(tmp_path):
    path = _settled_log(tmp_path, [("Buy", 0.10), ("Buy", -0.02), ("Buy", None)])
    s = summarize(path)
    assert s.resolved == 2 and s.pending == 1
    assert s.by_rating["Buy"].count == 2
    assert s.by_rating["Buy"].hit_rate == 0.5
    assert abs(s.by_rating["Buy"].mean_alpha - 0.04) < 1e-9


@pytest.mark.unit
def test_a_bearish_call_that_rose_counts_as_wrong(tmp_path):
    path = _settled_log(tmp_path, [("Sell", 0.10), ("Underweight", -0.10)])
    s = summarize(path)
    assert s.by_rating["Sell"].hit_rate == 0.0
    assert s.by_rating["Underweight"].hit_rate == 1.0


@pytest.mark.unit
def test_hold_claims_no_direction_so_it_gets_no_hit_rate(tmp_path):
    path = _settled_log(tmp_path, [("Hold", 0.10)])
    s = summarize(path)
    assert s.by_rating["Hold"].hit_rate is None
    assert "no direction claimed" in s.render()


@pytest.mark.unit
def test_the_report_names_the_window_the_scores_cover(tmp_path):
    path = _settled_log(tmp_path, [("Buy", 0.10)])
    assert "5 trading days" in summarize(path).render()


@pytest.mark.unit
def test_a_log_path_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        summarize(tmp_path / "nope.md")
