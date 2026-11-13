"""Screening: every rule's verdict is recorded, relative rules see only survivors, momentum ranks the rest."""

import itertools

import numpy as np
import pandas as pd
import pytest

from agent_trading_core.audit import stage_counts
from agent_trading_core.screening import ScreenConfig, screen

AS_OF = "2026-11-12"


def _frame(days=800, start_price=10000.0, drift=0.0, wobble=0.02, volume=1_000_000, first_date=None):
    """A deterministic price path: exponential drift plus a 7-bar sine wobble, so
    momentum and volatility are controlled rather than sampled."""
    dates = pd.bdate_range(end=AS_OF, periods=days)
    if first_date is not None:
        dates = dates[dates >= pd.Timestamp(first_date)]
    t = np.arange(len(dates))
    close = start_price * np.exp(drift * t) * (1 + wobble * np.sin(2 * np.pi * t / 7))
    return pd.DataFrame({"Date": dates, "Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": float(volume)})


UNIVERSE = {
    "WIN.KS":   _frame(drift=0.001),                                 # +29% a year: strong momentum
    "FLAT.KS":  _frame(drift=0.0, wobble=0.0003),                     # too quiet: volatility band fails
    "PENNY.KS": _frame(start_price=500.0),                           # price_min fails
    "NEW.KS":   _frame(first_date="2026-09-01"),                     # listed_days fails
    "THIN.KS":  _frame(volume=10),                                   # dollar volume rank last
    "LOSE.KS":  _frame(drift=-0.001),                                # negative momentum
    "MID.KS":   _frame(drift=0.0005),
}


def _load(ticker, as_of, cache_dir=None, fill_gaps=True):
    return UNIVERSE[ticker].copy()


def _cfg(**over):
    base = {"dollar_volume_top_n": 4, "top_k": 3, "sector_max": 2}
    base.update(over)
    return ScreenConfig(**base)


@pytest.mark.unit
def test_every_ticker_has_a_record_and_failed_ones_name_the_rule():
    run = screen(list(UNIVERSE), AS_OF, _cfg(), load=_load, run_id="t")
    by = {c.ticker: c for c in run.candidates}
    assert set(by) == set(UNIVERSE)
    assert by["PENNY.KS"].failed_rules == ["price_min"]
    assert by["NEW.KS"].failed_rules == ["listed_days_min"]
    assert "dollar_volume_top_n" in by["THIN.KS"].failed_rules
    assert any(r in by["FLAT.KS"].failed_rules for r in ("volatility_band", "range_stability_min"))


@pytest.mark.unit
def test_a_ticker_that_failed_early_is_not_evaluated_by_later_rules():
    run = screen(list(UNIVERSE), AS_OF, _cfg(), load=_load, run_id="t")
    penny = next(c for c in run.candidates if c.ticker == "PENNY.KS")
    assert [f.rule for f in penny.filters] == ["price_min"]


@pytest.mark.unit
def test_momentum_ranks_survivors_and_selects_top_k():
    run = screen(list(UNIVERSE), AS_OF, _cfg(top_k=2), load=_load, run_id="t")
    selected = [c.ticker for c in sorted(run.selected, key=lambda c: c.rank)]
    assert len(selected) == 2
    assert selected[0] == "WIN.KS"
    assert "LOSE.KS" not in selected
    win = next(c for c in run.candidates if c.ticker == "WIN.KS")
    assert win.score is not None and win.rank == 1


@pytest.mark.unit
def test_sector_quota_cuts_the_weakest_in_a_crowded_sector():
    sector = {"WIN.KS": "Tech", "MID.KS": "Tech", "LOSE.KS": "Tech", "THIN.KS": "Tech"}
    run = screen(list(UNIVERSE), AS_OF, _cfg(sector_max=2, top_k=5), load=_load,
                 sector_of=lambda t: sector.get(t), run_id="t")
    by = {c.ticker: c for c in run.candidates}
    assert "sector_quota" in by["LOSE.KS"].failed_rules       # weakest Tech by momentum
    assert by["WIN.KS"].selected and by["MID.KS"].selected


@pytest.mark.unit
def test_stage_counts_shrink_monotonically():
    run = screen(list(UNIVERSE), AS_OF, _cfg(), load=_load, run_id="t")
    counts = stage_counts(run)
    assert len(counts) == len(run.rules)
    assert all(a >= b for a, b in itertools.pairwise(counts))
    assert counts[-1] == len(run.selected)


@pytest.mark.unit
def test_a_vendor_failure_is_recorded_not_raised():
    from agent_trading_core.errors import NoMarketDataError

    def load(ticker, as_of, cache_dir=None, fill_gaps=True):
        if ticker == "DEAD.KS":
            raise NoMarketDataError(ticker, ticker, "delisted")
        return _load(ticker, as_of)

    run = screen(["WIN.KS", "DEAD.KS"], AS_OF, _cfg(), load=load, run_id="t")
    dead = next(c for c in run.candidates if c.ticker == "DEAD.KS")
    assert not dead.selected and dead.filters[0].rule == "data" and "delisted" in dead.filters[0].note


@pytest.mark.unit
def test_performance_filter_drops_a_ticker_with_bad_settled_alpha(tmp_path):
    from agent_trading_core.memory import TradingMemoryLog

    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    for i in range(3):
        log.store_decision("WIN.KS", f"2026-01-0{i + 1}", "Rating: Buy\n\nx")
        log.update_with_outcome("WIN.KS", f"2026-01-0{i + 1}", -0.05, -0.06, 5, "bad", "2026-02-01")
    run = screen(list(UNIVERSE), AS_OF, _cfg(), load=_load, memory_log=log, run_id="t")
    win = next(c for c in run.candidates if c.ticker == "WIN.KS")
    assert "performance_filter" in win.failed_rules
