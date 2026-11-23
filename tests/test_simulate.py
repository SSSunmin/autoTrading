"""The portfolio simulation keeps its books, fills at the next open, and lets the hard rules cut a crash."""

import hashlib
import json
import sys
from pathlib import Path

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


def _jump_index(day: str) -> pd.DataFrame:
    """Falling 0.2% a day — always below its 200-day average — with a one-off +12% jump on ``day``: from then on above
    its 50-day average and 12% over its 60-day low, still below the 200-day average (~22% above the close)."""
    t = np.arange(len(DATES))
    path = 3000 * np.exp(-0.002 * t)
    path[DATES >= pd.Timestamp(day)] *= 1.12
    return _frame(path)


RISK_TWO = RiskConfig(max_position_pct=0.5, max_sector_pct=1.0)


@pytest.mark.unit
@pytest.mark.parametrize("rule", ["ma50", "low_rebound"])
def test_a_release_opens_buys_below_the_200_day_line_from_the_decision_close_only(rule):
    frames = _paths()
    dates = DATES[DATES >= pd.Timestamp(START)]
    jump = dates[5 * 20 + 1]                      # the day after a rebalance (rebalances on every 5th session)
    idx = _jump_index(jump.strftime("%Y-%m-%d"))
    kept = run_simulation(frames, idx, _cfg(label="kept", risk=RISK_TWO, regime_filter=True))
    assert not kept.trades and kept.exposure.max() == 0
    r = run_simulation(frames, idx, _cfg(label=rule, risk=RISK_TWO, regime_filter=True, regime_release=rule))
    # the first rebalance that sees the jump is 4 sessions after it; its orders fill the next open. Reading one close
    # ahead would decide on the rebalance before the jump and fill on the jump day itself.
    first_fill = dates[5 * 21 + 1]
    assert r.trades and min(pd.Timestamp(t.entry_date) for t in r.trades) == first_fill
    assert any(e["kind"] == "override" and f"국면 해제({rule})" in e["text"] for e in r.events)


@pytest.mark.unit
def test_the_half_release_buys_below_the_line_at_half_weight():
    frames = _paths()
    t = np.arange(len(DATES))
    falling = _frame(3000 * np.exp(-0.0008 * t))                     # always below its own average
    full = run_simulation(frames, falling, _cfg(label="off", risk=RISK_TWO))
    half = run_simulation(frames, falling, _cfg(label="half", risk=RISK_TWO, regime_filter=True, regime_release="half"))
    a, b = full.trades[0], half.trades[0]
    assert (a.ticker, a.entry_date) == (b.ticker, b.entry_date)
    assert b.quantity == pytest.approx(a.quantity / 2, abs=1)         # whole shares
    assert half.exposure.max() <= 0.5 * full.exposure.max() + 0.02


@pytest.mark.unit
def test_a_release_needs_the_filter_and_a_known_rule():
    with pytest.raises(ValueError):
        _cfg(risk=RiskConfig(), regime_release="ma50")                 # no regime_filter
    with pytest.raises(ValueError):
        _cfg(risk=RiskConfig(), regime_filter=True, regime_release="ma20")


@pytest.mark.unit
def test_without_a_release_the_regime_filter_runs_as_before():
    """A run whose index crosses its 200-day line both ways: equity, trades and events hash to the digest the code
    before the release option gave (develop ddf1426, measured 2026-11-18) — the default path is unchanged."""
    t = np.arange(len(DATES))
    wave = _frame(2500 * (1 + 0.15 * np.sin(2 * np.pi * t / 260)))
    r = run_simulation(_paths(), wave, _cfg(label="g", risk=RISK_TWO, regime_filter=True))
    blob = json.dumps({"equity": [round(float(x), 4) for x in r.equity],
                       "trades": [[tr.ticker, tr.entry_date, tr.exit_date, round(tr.entry_price, 6), tr.quantity,
                                   tr.reason] for tr in r.trades],
                       "events": r.events}, ensure_ascii=False, default=str)
    assert len(r.trades) == 8 and sum(e["kind"] == "lock" and "국면" in e["text"] for e in r.events) == 11
    assert hashlib.sha256(blob.encode()).hexdigest() == "86d36327e6460cd956ff0507b2d2c00068d8c74c1a5638fab585fa39e28599f7"


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import regime_release_grid


@pytest.mark.unit
def test_the_index_only_table_weighs_each_day_by_the_closes_before_it():
    """docs/12 §5-4: the weight of day k is decided from closes up to day k-1 — the +12% jump's own day still holds
    nothing under the releases, the day after holds the index."""
    days = pd.bdate_range("2023-01-02", periods=300)
    path = 3000 * np.exp(-0.002 * np.arange(len(days)))
    path[250:] *= 1.12                                            # still ~20% under the 200-day average
    closes = pd.Series(path, index=days)
    for rule in ("ma50", "low_rebound"):
        w = regime_release_grid.index_weights(closes, True, rule)
        assert w.loc[days[249]] == 0.0 and w.loc[days[250]] == 0.0      # the jump day: decided on the close before it
        assert w.loc[days[251]] == 1.0
    assert regime_release_grid.index_weights(closes, True, None).loc[days[251]] == 0.0
    assert regime_release_grid.index_weights(closes, True, "half").loc[days[251]] == 0.5
    assert regime_release_grid.index_weights(closes, False, None).loc[days[251]] == 1.0


def _fading():
    """Seven names on steady drifts and FADE, which leads until 2024-06 and then drifts down: from 2025-01 its
    momentum ranks sixth of eight — Underweight (n_ranked 8: ranks 5-6) at every rebalance while it is held."""
    t = np.arange(len(DATES))
    wob = 1 + 0.02 * np.sin(2 * np.pi * t / 7)
    k = np.searchsorted(DATES, pd.Timestamp("2024-06-03"))
    frames = {f"{n}.KS": _frame(10000 * np.exp(d * t) * wob)
              for n, d in zip("ABCDEGH", (0.0010, 0.0008, 0.0006, 0.0004, 0.0002, -0.0002, -0.0004))}
    frames["FADE.KS"] = _frame(10000 * np.exp(np.cumsum(np.where(t < k, 0.0020, -0.0001))) * wob)
    return frames


SCREEN8 = ScreenConfig(dollar_volume_top_n=8, top_k=8, sector_max=8, volatility_min=0.0, volatility_max=5.0,
                       range_stability_min=0.0)


def _digest(r) -> str:
    blob = json.dumps({"equity": [round(float(x), 4) for x in r.equity],
                       "trades": [[tr.ticker, tr.entry_date, tr.exit_date, round(tr.entry_price, 6), tr.quantity,
                                   tr.reason] for tr in r.trades],
                       "events": r.events}, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@pytest.mark.unit
def test_an_underweight_sells_the_executors_share_of_the_position_each_time():
    """execution._sell_size: an Underweight sells underweight_sell_fraction (0.5) of the shares held, rounded down,
    at least one; the last share goes as a full exit. Each partial sell fills at the next open and keeps the rest."""
    r = run_simulation(_fading(), _index(), _cfg(screen=SCREEN8, label="u"))
    fade = [t for t in r.trades if t.ticker == "FADE.KS"]
    left = sum(t.quantity for t in fade)
    for t in fade[:-1]:
        assert not t.full and t.quantity == int(left * 0.5) and t.reason == "exit_signal"
        left -= t.quantity
    assert fade[-1].full and fade[-1].quantity == left == 1 and len(fade) == 12
    assert sum("부분 매도" in e["text"] for e in r.events) == 11
    opens = _fading()["FADE.KS"].set_index("Date")["Open"]
    assert all(t.exit_price == pytest.approx(opens[pd.Timestamp(t.exit_date)] * (1 - 0.0005)) for t in fade)
    # the whole position at once (1.0): one full exit of every share
    whole = run_simulation(_fading(), _index(), _cfg(screen=SCREEN8, label="u", underweight_sell_fraction=1.0))
    assert [(t.ticker, t.quantity, t.full) for t in whole.trades] == [("FADE.KS", sum(t.quantity for t in fade), True)]


@pytest.mark.unit
def test_a_whole_underweight_sale_runs_as_the_backtests_before_partial_sells():
    """underweight_sell_fraction 1.0 hashes to the digests develop 25311d5 (before the option) gave on the fading
    fixture, rules off and on (measured 2026-11-23): the old numbers of docs/09 · 11 · 12 reproduce with it."""
    for risk, n, digest in ((None, 1, "f735003c2deb0b92efd4716c9d0d7731e89f1d932b4b4120d2591feb1ad1f5f1"),
                            (RISK_TWO, 24, "42978511e2eae3309019f11374165f1b31b186123474b0824002f4f75c2f4e15")):
        r = run_simulation(_fading(), _index(), SimConfig(start="2025-01-02", end="2025-09-05", screen=SCREEN8,
                                                          label="on" if risk else "off", risk=risk,
                                                          underweight_sell_fraction=1.0))
        assert len(r.trades) == n and _digest(r) == digest


@pytest.mark.unit
def test_the_underweight_share_is_a_fraction_of_the_position():
    for bad in (0.0, -0.5, 1.5):
        with pytest.raises(ValueError, match="underweight_sell_fraction"):
            _cfg(underweight_sell_fraction=bad)
