"""The portfolio simulation keeps its books, fills at the next open, and lets the hard rules cut a crash."""

import numpy as np
import pandas as pd
import pytest

from agent_trading_core.risk import RiskConfig
from agent_trading_core.screening import ScreenConfig
from agent_trading_core.simulate import SimConfig, metrics, run_simulation, window_metrics

DATES = pd.bdate_range("2023-01-02", periods=700)
START, END = "2025-01-02", DATES[-1].strftime("%Y-%m-%d")


def _frame(path: np.ndarray, volume=1e6) -> pd.DataFrame:
    return pd.DataFrame({"Date": DATES, "Open": path, "High": path * 1.01, "Low": path * 0.99, "Close": path,
                         "Volume": float(volume)})


def _paths():
    t = np.arange(len(DATES))
    wob = 1 + 0.02 * np.sin(2 * np.pi * t / 7)
    up = 10000 * np.exp(0.0008 * t) * wob                      # steady winner
    up2 = 12000 * np.exp(0.0006 * t) * wob
    crash = 10000 * np.exp(0.0009 * t) * wob
    k = np.searchsorted(DATES, pd.Timestamp("2025-06-02"))
    crash[k:] = crash[k:] * 0.55                                # −45% gap one day, stays down
    flat = 10000 * np.exp(0.0001 * t) * wob
    down = 10000 * np.exp(-0.0005 * t) * wob
    return {"UP.KS": _frame(up), "UP2.KS": _frame(up2), "CRASH.KS": _frame(crash), "FLAT.KS": _frame(flat), "DOWN.KS": _frame(down)}


def _index():
    t = np.arange(len(DATES))
    return _frame(2500 * np.exp(0.0003 * t))


def _cfg(**over):
    base = {"start": START, "end": END, "screen": ScreenConfig(dollar_volume_top_n=5, top_k=4, sector_max=5, volatility_min=0.0,
                                                                volatility_max=5.0, range_stability_min=0.0)}
    base.update(over)
    return SimConfig(**base)


@pytest.mark.unit
def test_books_balance_and_fills_are_next_open():
    frames, idx = _paths(), _index()
    r = run_simulation(frames, idx, _cfg(label="off"))
    assert len(r.equity) == len(idx.set_index("Date").loc[START:END])
    assert r.equity.iloc[0] == pytest.approx(100_000_000)            # nothing bought before the first fill
    assert r.rebalances > 30 and r.exposure.max() > 0.9                # rules off: fully invested
    # every trade entered at an open that exists in the frame, strictly after a rebalance close
    for tr in r.trades:
        f = frames[tr.ticker].set_index("Date")
        assert tr.entry_price == pytest.approx(float(f.at[pd.Timestamp(tr.entry_date), "Open"]) * (1 + 0.0005))
    assert set(r.metrics) >= {"total_return", "max_drawdown", "worst_day", "dd_peak", "dd_trough"}


@pytest.mark.unit
def test_hard_rules_cut_the_crash_and_report_why():
    frames, idx = _paths(), _index()
    off = run_simulation(frames, idx, _cfg(label="off"))
    on = run_simulation(frames, idx, _cfg(label="on", risk=RiskConfig()))
    # the crashing name is a top-momentum name before June, so both books hold it. Rules off: half the book is in it
    # and the −45% gap is taken in full. Rules on: 10% cap bounds the hit, and the stop records why it was sold.
    crash = [t for t in on.trades if t.ticker == "CRASH.KS" and t.reason == "stop_loss"]
    assert crash and crash[0].pnl_pct < -0.30                          # a gap goes straight through the −8% stop
    assert all(t.reason == "exit_signal" for t in off.trades)         # no stops without rules
    assert off.metrics["max_drawdown"] < -0.20
    assert on.metrics["max_drawdown"] > -0.10                          # the position cap, not the stop, bounded the loss
    assert on.exposure.max() <= 0.21                                   # 2 targets × 10%
    kinds = {e["kind"] for e in on.events}
    assert {"rebalance", "exit"} <= kinds
    assert any("손절" in e["text"] for e in on.events)
    # expiry recycles positions: the holding period is bounded
    assert max(t.holding_days for t in on.trades) <= RiskConfig().max_holding_days + 1


@pytest.mark.unit
def test_regime_filter_blocks_buys_below_the_200_day_line():
    frames = _paths()
    t = np.arange(len(DATES))
    falling = _frame(3000 * np.exp(-0.0008 * t))                     # index always below its own average
    r = run_simulation(frames, falling, _cfg(label="regime", risk=RiskConfig(max_position_pct=0.5, max_sector_pct=1.0),
                                             regime_filter=True))
    assert not r.trades and r.exposure.max() == 0                    # never bought
    assert any(e["kind"] == "lock" and "국면 필터" in e["text"] for e in r.events)


@pytest.mark.unit
def test_metrics_drawdown_dates_and_windows():
    e = pd.Series([100, 110, 99, 95, 100, 112, 108], index=pd.bdate_range("2024-01-01", periods=7), dtype=float)
    m = metrics(e)
    assert m["max_drawdown"] == pytest.approx(95 / 110 - 1)
    assert m["dd_peak"] == "2024-01-02" and m["dd_trough"] == "2024-01-04" and m["dd_recovery"] == "2024-01-08"
    assert m["dd_length_days"] == 2 and m["dd_recovery_days"] == 2
    assert m["worst_day"] == pytest.approx(99 / 110 - 1)
    w = window_metrics(e, {"dip": ("2024-01-02", "2024-01-04")})
    assert w["dip"]["return"] == pytest.approx(95 / 110 - 1) and w["dip"]["max_drawdown"] == pytest.approx(95 / 110 - 1)
    assert metrics(pd.Series([1.0])) == {}
