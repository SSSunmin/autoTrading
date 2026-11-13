"""After a −20% stop: liquidate, lock dd_stop_lock_days, then reset the peak to that close's equity and size normally
(the executor's rule, RiskConfig.dd_stop_recovery="reset_peak"); "half_size" keeps the rule from before 2026-11-13."""

import numpy as np
import pandas as pd
import pytest

from agent_trading_core.risk import RiskConfig
from agent_trading_core.simulate import run_simulation
from tests.test_simulate import DATES, _cfg, _frame, _index, _paths

CRASH = pd.Timestamp("2025-06-02")


def _crash_book(rebound=None):
    """Five names gapping −65% together on 2025-06-02 and staying down: a book 25% per name can't absorb it.
    With ``rebound`` the next open (and everything after) is back at that fraction of the pre-crash path."""
    t = np.arange(len(DATES))
    wob = 1 + 0.02 * np.sin(2 * np.pi * t / 7)
    k = np.searchsorted(DATES, CRASH)
    out = {}
    for name, g in [("A.KS", 8), ("B.KS", 7), ("C.KS", 6), ("D.KS", 5), ("E.KS", 4)]:
        p = 10000 * np.exp(g * 1e-4 * t) * wob
        if rebound is None:
            p[k:] *= 0.35
        else:
            p[k] *= 0.35
            p[k + 1:] *= rebound
        out[name] = _frame(p)
    return out


def _run(mode, rebound=None):
    return run_simulation(_crash_book(rebound), _index(), _cfg(label=mode, risk=RiskConfig(max_position_pct=0.25,
                                                                                           dd_stop_recovery=mode)))


@pytest.mark.unit
def test_the_peak_is_reset_to_the_lock_end_close_and_sizing_is_normal_from_it():
    r = _run("reset_peak")
    stops = [e for e in r.events if e["kind"] == "guard" and e["text"].startswith("MaxDrawdown")]
    resets = [e for e in r.events if e["kind"] == "peak_reset"]
    assert [e["date"] for e in stops] == ["2025-06-02"] and "정상 비중으로 재가동" in stops[0]["text"]
    assert len(resets) == 1
    # 20 trading days after the stop's close, on the simulation's calendar
    days = r.equity.index
    reset_day = days[days.get_loc(CRASH) + RiskConfig().dd_stop_lock_days]
    assert resets[0]["date"] == reset_day.strftime("%Y-%m-%d") == "2025-06-30"
    # before: the highest close-of-day equity up to the stop; after: that day's close equity (known at that close)
    assert resets[0]["peak_before"] == pytest.approx(float(r.equity.loc[:CRASH].max()))
    assert resets[0]["peak_after"] == pytest.approx(float(r.equity.loc[reset_day]))
    assert float(r.exposure.loc[CRASH + pd.Timedelta(days=1):reset_day].max()) == 0.0   # locked: nothing held
    # after the reset the book is still more than 10% under the old peak — the old peak would halve sizing —
    # but the drawdown counts from the new peak, so the two buy-rated names come in at full weight (2 × 25%)
    after = slice(reset_day + pd.Timedelta(days=1), "2025-07-31")
    assert float(r.equity.loc[after].max()) / resets[0]["peak_before"] - 1 < RiskConfig().dd_halve
    assert float(r.exposure.loc[after].max()) == pytest.approx(0.5, abs=0.02)
    assert not [e for e in r.events if e["kind"] == "guard" and e["text"].startswith("회복")]


@pytest.mark.unit
def test_half_size_switch_keeps_the_old_recovery():
    old, new = _run("half_size"), _run("reset_peak")
    texts = [e["text"] for e in old.events if e["kind"] == "guard"]
    assert any(t.startswith("재가동: 기준 피크를 69,598,065으로 재설정") and t.endswith("회복까지 절반 비중") for t in texts)
    assert not [e for e in old.events if e["kind"] == "peak_reset"]
    reset_day = pd.Timestamp("2025-06-30")
    assert float(old.exposure.loc[reset_day + pd.Timedelta(days=1):"2025-07-31"].max()) == pytest.approx(0.25, abs=0.02)
    # identical up to and including the reset close: only the sizing after it differs
    assert old.equity.loc[:reset_day].equals(new.equity.loc[:reset_day])
    assert not old.equity.equals(new.equity)


@pytest.mark.unit
def test_the_peak_is_reset_even_when_the_liquidating_open_gaps_back_above_the_stop():
    # sold at the 2025-06-03 open back at 90%: the cash book is about −5% from the old peak, not −20%, yet an admin
    # resume would still reset the peak — so the lock end resets it, and nothing "recovers" during the lock
    r = _run("reset_peak", rebound=0.9)
    resets = [e for e in r.events if e["kind"] == "peak_reset"]
    assert [e["date"] for e in resets] == ["2025-06-30"]
    assert resets[0]["peak_after"] == pytest.approx(float(r.equity.loc["2025-06-30"]))
    assert -0.20 < resets[0]["peak_after"] / resets[0]["peak_before"] - 1 < -0.01
    assert not [e for e in r.events if e["kind"] == "guard" and e["text"].startswith("회복")]
    # half_size keeps its old path: no reset, a 회복 line the day after the stop
    old = _run("half_size", rebound=0.9)
    assert [e["date"] for e in old.events if e["kind"] == "guard" and e["text"].startswith("회복")] == ["2025-06-03"]
    assert not [e for e in old.events if e["kind"] == "guard" and e["text"].startswith("재가동")]


@pytest.mark.unit
def test_a_path_without_a_stop_is_the_same_under_both_rules():
    frames, idx = _paths(), _index()
    runs = {m: run_simulation(frames, idx, _cfg(label="f", risk=RiskConfig(dd_stop_recovery=m)))
            for m in ("reset_peak", "half_size")}
    for r in runs.values():
        assert not [e for e in r.events if e["kind"] == "guard" and e["text"].startswith("MaxDrawdown")]
        assert float(r.equity.iloc[-1]) == 96919818.66849957          # tests/test_markets.py literal
    assert runs["reset_peak"].equity.equals(runs["half_size"].equity)
    assert RiskConfig().dd_stop_recovery == "reset_peak"
