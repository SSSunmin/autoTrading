"""Rebound-speed measures (screening.Rebound, docs/11): A · B · C by hand on small series, nothing after the decision
day read, the screen unchanged when it is off, no benchmark → None; the score and filter modes; the grid's
down-market windows (scripts/rebound_grid.py)."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agent_trading_core.screening import (
    Rebound,
    ScreenConfig,
    capture_spread,
    momentum_scores,
    ranking_scores,
    rebound_beta,
    rebound_from_low,
    screen,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rebound_grid

AS_OF = "2026-11-12"
BENCH = "^IDX"


def _series(values, end=AS_OF):
    return pd.Series([float(v) for v in values], index=pd.bdate_range(end=end, periods=len(values)))


def _capture_pair():
    """40 benchmark days alternating up (+1% · +2%) and down (−1% · −2%); the stock moves 2× the benchmark on up days
    and 0.5× on down days, plus a constant 0.1%."""
    b = np.array([0.01, -0.01, 0.02, -0.02] * 10)
    s = np.where(b > 0, 2 * b, 0.5 * b) + 0.001
    return _series(100 * np.cumprod(np.r_[1, 1 + s])), _series(100 * np.cumprod(np.r_[1, 1 + b]))


# ---- by hand -------------------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_a_is_the_excess_return_since_the_benchmarks_low():
    bench, stock = _series([100, 90, 80, 85, 88]), _series([50, 45, 40, 46, 50])
    # low day = the 3rd (80): stock 50/40 − 1 = +25%, benchmark 88/80 − 1 = +10% → +15%p
    assert rebound_from_low(stock, bench, window=5) == pytest.approx(0.15)
    # a window that starts after the low: its own low is the 4th bar (85)
    assert rebound_from_low(stock, bench, window=2) == pytest.approx((50 / 46 - 1) - (88 / 85 - 1))
    assert rebound_from_low(stock.drop(stock.index[2]), bench, window=5) is None     # no stock close on the low day
    assert rebound_from_low(stock, bench, window=6) is None                          # too few benchmark bars


@pytest.mark.unit
def test_b_is_up_capture_minus_down_capture():
    stock, bench = _capture_pair()
    up_b, down_b = 0.015, -0.015                                 # benchmark means on up / down days
    up_s, down_s = 2 * up_b + 0.001, 0.5 * down_b + 0.001
    assert capture_spread(stock, bench, window=40) == pytest.approx(up_s / up_b - down_s / down_b)   # 2.0667 − 0.4333
    assert capture_spread(stock, bench, window=41) is None       # the window needs one bar more than it has


@pytest.mark.unit
def test_c_is_the_up_day_beta_minus_the_down_day_beta():
    stock, bench = _capture_pair()
    assert rebound_beta(stock, bench, window=40) == pytest.approx(2.0 - 0.5)          # the constant moves no beta
    few = _series(100 * np.cumprod(np.r_[1, 1 + np.array([0.01, 0.02] * 15 + [-0.01, -0.02] * 4 + [-0.01])]))
    assert (np.diff(few.to_numpy()) < 0).sum() == 9                                 # 9 down days: under 10
    assert rebound_beta(few, few, window=39) is None and capture_spread(few, few, window=39) is None
    ten = _series(np.r_[few.to_numpy(), few.iloc[-1] * 0.99])                        # one more down day: 10
    assert capture_spread(ten, ten, window=40) == pytest.approx(0.0)


# ---- inside the screen ---------------------------------------------------------------------------------------------

def _frame(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"Date": close.index, "Open": close.values, "High": close.values * 1.01,
                         "Low": close.values * 0.99, "Close": close.values, "Volume": 1e6})


def _market(n_names=10, days=800, end="2026-12-31"):
    """A benchmark with a fall and a rebound, and names that follow it with their own drift and rebound speed. The
    frames run past AS_OF, so a measure that read ahead would change when the later rows do."""
    t = np.arange(days)
    dates = pd.bdate_range(end=end, periods=days)
    wave = 1 + 0.02 * np.sin(2 * np.pi * t / 7)
    bench = 1000 * np.exp(0.0003 * t) * wave * (1 - 0.15 * np.exp(-((t - 700) / 25) ** 2))
    frames = {BENCH: _frame(pd.Series(bench, index=dates))}
    for k in range(n_names):
        speed = 0.5 + 0.2 * k
        own = np.exp((0.0002 + 0.0001 * k) * t) * (1 - 0.15 * speed * np.exp(-((t - 700) / 25) ** 2))
        frames[f"N{k}.KS"] = _frame(pd.Series(10000 * own * wave ** (1 + 0.1 * k), index=dates))
    return frames


def _loader(frames, asked=None):
    def load(ticker, as_of, cache_dir=None, fill_gaps=True):
        if asked is not None:
            asked.append(ticker)
        if ticker not in frames:
            raise KeyError(ticker)
        f = frames[ticker]
        return f[f["Date"] <= pd.Timestamp(as_of)].copy()
    return load


def _cfg(rebound=None, rules=None):
    cfg = ScreenConfig(dollar_volume_top_n=20, top_k=4, sector_max=3, volatility_min=0.0, volatility_max=5.0,
                       rebound=rebound)
    if rules == "filter":
        k = cfg.rules.index("sector_quota")
        cfg.rules = (*cfg.rules[:k], "rebound_filter", *cfg.rules[k:])
    return cfg


def _shape(run):
    return [c.model_dump() for c in run.candidates]


@pytest.mark.unit
@pytest.mark.parametrize("metric", ["A", "B", "C"])
@pytest.mark.parametrize("mode", ["score", "filter"])
def test_nothing_after_the_decision_day_moves_a_measure(metric, mode):
    frames = _market()
    names = [t for t in frames if t != BENCH]
    cfg = _cfg(Rebound(metric=metric, benchmark=BENCH, mode=mode, weight=0.5), rules=mode)
    before = screen(names, AS_OF, cfg, load=_loader(frames), run_id="r")
    later = {t: f.assign(Close=np.where(f["Date"] > pd.Timestamp(AS_OF), f["Close"] * 3, f["Close"]))
             for t, f in frames.items()}                                    # every row after AS_OF tripled
    assert _shape(screen(names, AS_OF, cfg, load=_loader(later), run_id="r")) == _shape(before)


@pytest.mark.unit
def test_off_it_is_the_screen_as_it_was_and_never_reads_the_benchmark():
    frames = _market()
    names = [t for t in frames if t != BENCH]
    asked = []
    off = screen(names, AS_OF, _cfg(), load=_loader(frames, asked), run_id="r")
    assert BENCH not in asked and ScreenConfig().rebound is None
    assert off.rules == list(ScreenConfig().rules)                     # no rebound_filter in the default rules
    # weight 0 scores the momentum alone: the same records as off
    zero = screen(names, AS_OF, _cfg(Rebound(metric="A", benchmark=BENCH, weight=0.0)), load=_loader(frames), run_id="r")
    assert _shape(zero) == _shape(off)


@pytest.mark.unit
def test_the_score_mode_blends_the_measures_z_and_reorders():
    frames = _market()
    names = [t for t in frames if t != BENCH]
    off = screen(names, AS_OF, _cfg(), load=_loader(frames), run_id="r")
    on = screen(names, AS_OF, _cfg(Rebound(metric="A", benchmark=BENCH, weight=0.5)), load=_loader(frames), run_id="r")
    assert [c.score for c in on.candidates] != [c.score for c in off.candidates]
    from agent_trading_core import screening

    feats = {t: screening._features(frames[t][frames[t]["Date"] <= AS_OF], AS_OF, _cfg(), None) for t in names}
    feats["N0.KS"].rebound, feats["N1.KS"].rebound = 1.0, -1.0            # two measured, the rest None
    mom = momentum_scores(feats)
    mixed = ranking_scores(feats, _cfg(Rebound(metric="A", benchmark=BENCH, weight=0.25)))
    assert mixed["N0.KS"] == pytest.approx(0.75 * mom["N0.KS"] + 0.25 * 1.0)   # z of {1, −1} is ±1
    assert mixed["N1.KS"] == pytest.approx(0.75 * mom["N1.KS"] - 0.25)
    assert all(mixed[t] == mom[t] for t in names[2:])                          # no measure: momentum alone


@pytest.mark.unit
def test_the_filter_drops_the_bottom_30_percent_of_the_measured():
    frames = _market()
    names = [t for t in frames if t != BENCH]
    rb = Rebound(metric="A", benchmark=BENCH, mode="filter")
    run = screen(names, AS_OF, _cfg(rb, rules="filter"), load=_loader(frames), run_id="r")
    verdicts = {c.ticker: next(f for f in c.filters if f.rule == "rebound_filter") for c in run.candidates}
    dropped = sorted((v.value, t) for t, v in verdicts.items() if not v.passed)
    kept = [v.value for v in verdicts.values() if v.passed]
    assert len(dropped) == 3 and max(v for v, _ in dropped) <= min(kept)       # ⌊0.3 × 10⌋, the lowest three
    assert all(v.threshold == "하위 30%" for v in verdicts.values())


@pytest.mark.unit
def test_no_benchmark_means_no_measure_and_nothing_dropped_or_reordered():
    frames = _market()
    names = [t for t in frames if t != BENCH]
    no_bench = {t: f for t, f in frames.items() if t != BENCH}
    off = screen(names, AS_OF, _cfg(), load=_loader(no_bench), run_id="r")
    scored = screen(names, AS_OF, _cfg(Rebound(metric="B", benchmark=BENCH, weight=0.5)), load=_loader(no_bench),
                    run_id="r")
    assert _shape(scored) == _shape(off)
    rb = Rebound(metric="B", benchmark=BENCH, mode="filter")
    run = screen(names, AS_OF, _cfg(rb, rules="filter"), load=_loader(no_bench), run_id="r")
    verdicts = [next(f for f in c.filters if f.rule == "rebound_filter") for c in run.candidates]
    assert all(v.passed and v.value is None and v.note == "반등 지표 없음: 제외 안 함" for v in verdicts)


@pytest.mark.unit
def test_a_bad_setting_is_refused():
    with pytest.raises(ValueError):
        Rebound(metric="D", benchmark=BENCH)
    with pytest.raises(ValueError):
        Rebound(metric="A", benchmark=BENCH, mode="both")
    frames = _market(n_names=3)
    with pytest.raises(ValueError, match="needs ScreenConfig.rebound"):
        screen(list(frames)[1:], AS_OF, _cfg(rules="filter"), load=_loader(frames), run_id="r")
    # a mode and rules that do not go together: the measure would be computed and do nothing, or filter unasked
    with pytest.raises(ValueError, match="mode filter goes with"):
        screen(list(frames)[1:], AS_OF, _cfg(Rebound(metric="A", benchmark=BENCH, mode="filter")), load=_loader(frames))
    with pytest.raises(ValueError, match="mode filter goes with"):
        screen(list(frames)[1:], AS_OF, _cfg(Rebound(metric="A", benchmark=BENCH), rules="filter"), load=_loader(frames))


# ---- the grid's windows --------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_drawdown_episodes_and_their_60_sessions_from_the_low():
    bench = _series([100, 95, 89, 85, 92, 101, 99, 88, 90, 95, 97])
    eps = rebound_grid.drawdown_episodes(bench, after=3)
    d = bench.index.strftime("%Y-%m-%d")
    # 1st: −11% on the 3rd bar, low 85 (4th), high back on the 6th; 2nd: from 101, −12.9% on the 8th, never back
    assert [(e["crossed"], e["low"], e["rebound_end"]) for e in eps] == [(d[2], d[3], d[6]), (d[7], d[7], d[10])]
    assert eps[0]["low_drawdown"] == pytest.approx(-0.15)


@pytest.mark.unit
def test_a_segment_counts_buys_and_exits_on_the_same_days_as_its_returns():
    from agent_trading_core.simulate import SimResult, Trade

    days = pd.bdate_range("2026-01-05", periods=6)
    equity = pd.Series([100.0, 101, 102, 103, 104, 105], index=days)
    ds = days.strftime("%Y-%m-%d")
    trades = [Trade("X", ds[0], d, 1.0, 1.0, 10, "stop_loss", 0.0, 1) for d in (ds[1], ds[3], ds[5])]
    events = [{"date": d, "kind": "rebalance", "text": f"선별 5→3순위, 목표 2종목, 신규 매수 {n}"}
              for d, n in ((ds[0], 1), (ds[3], 2), (ds[5], 4))]
    r = SimResult("t", equity, equity, equity * 0, trades, events, 3, {}, {})
    full = rebound_grid.segment(r, days[0], days[5], first=True)
    front = rebound_grid.segment(r, days[0], days[3], first=True)
    back = rebound_grid.segment(r, days[3], days[5])
    assert (full["buys"], full["exits"]) == (7, 3)
    assert (front["buys"], front["exits"]) == (3, 2) and (back["buys"], back["exits"]) == (4, 1)   # the cut day: front
    assert front["buys"] + back["buys"] == full["buys"] and front["exits"] + back["exits"] == full["exits"]


@pytest.mark.unit
def test_the_split_and_the_regime_off_days():
    dates = pd.bdate_range("2026-01-01", periods=9)
    assert rebound_grid.split_dates(dates) == (dates[0], dates[6], dates[8])
    closes = [100.0] * 199 + [100.0, 90.0, 95.0, 120.0]                 # 200-day average just under 100
    index = pd.DataFrame({"Date": pd.bdate_range(end="2026-11-12", periods=len(closes)), "Close": closes})
    days = pd.DatetimeIndex(index["Date"].iloc[-4:])
    # off when the day before closed under its average: after 90 and after 95, not after 100 or 120
    assert list(rebound_grid.regime_off_days(index, days)) == [days[2], days[3]]
