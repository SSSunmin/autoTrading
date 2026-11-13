"""Market axis: KRX and US share one settings shape, the simulator takes its sell costs, benchmark name and
regime wording from it, and the KRX path gives the same numbers it gave before the axis existed."""

from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from agent_trading_core.config import KRX_MARKET, MARKETS
from agent_trading_core.risk import RiskConfig
from agent_trading_core.screening import ScreenConfig, screen, us_preset
from agent_trading_core.simulate import SimConfig, run_simulation, sell_proceeds
from tests.test_simulate import DATES, _cfg, _frame, _index, _paths

COMMON = {"currency", "benchmark", "benchmark_key", "benchmark_name_ko", "timezone", "session", "settlement",
          "price_limit", "halt_rules", "sell_tax", "trade_unit", "fractional_shares", "pdt_rule", "data_source_index", "data_source_equity"}
# KRX keys before the axis (backend/main.py serves this dict as-is)
KRX_KEYS_BEFORE = {"price_limit", "vi_dynamic_kospi200", "vi_dynamic_other", "vi_dynamic_close_kospi200",
                   "vi_dynamic_close_other", "vi_static", "vi_halt_minutes", "sell_tax_kospi", "sell_tax_kosdaq",
                   "sell_tax_konex", "trade_unit", "short_selling"}


def _wavy_index():
    # ±15% yearly swing around a slow rise: the index spends stretches under its 200-day average
    t = np.arange(len(DATES))
    return _frame(2500 * np.exp(0.0003 * t) * (1 + 0.15 * np.sin(2 * np.pi * t / 250)))


@pytest.mark.unit
def test_markets_share_the_common_keys_and_keep_the_krx_alias():
    assert set(MARKETS) == {"KRX", "US"}
    for name, m in MARKETS.items():
        assert COMMON <= set(m), (name, COMMON - set(m))
    assert set(MARKETS["KRX"]) - COMMON == KRX_KEYS_BEFORE - COMMON        # KRX-only keys unchanged
    assert not {k for k in MARKETS["US"] if k.startswith("vi_")}
    assert KRX_MARKET is MARKETS["KRX"]
    assert KRX_KEYS_BEFORE <= set(KRX_MARKET)
    # the settings screen prints KRX values with String(v): scalars only
    assert all(isinstance(v, (int, float, str)) for v in KRX_MARKET.values())
    assert (MARKETS["KRX"]["benchmark"], MARKETS["US"]["benchmark"]) == ("^KS11", "^GSPC")
    assert (MARKETS["KRX"]["settlement"], MARKETS["US"]["settlement"]) == ("T+2", "T+1")
    assert MARKETS["US"]["price_limit"] is None and MARKETS["US"]["sell_tax"] == 0.0


@pytest.mark.unit
def test_sell_tax_and_screen_defaults_come_from_the_market():
    krx, us = SimConfig("2025-01-02", "2025-12-31"), SimConfig("2025-01-02", "2025-12-31", market="US")
    assert krx.sell_tax == 0.0020 and us.sell_tax == 0.0
    assert krx.screen.price_min == 1000.0 and us.screen.price_min == 5.0
    # the rest of the US screen is the KRX book's, unchanged
    assert {k: v for k, v in vars(us.screen).items() if k != "price_min"} == \
           {k: v for k, v in vars(krx.screen).items() if k != "price_min"}
    assert SimConfig("2025-01-02", "2025-12-31", market="US", sell_tax=0.001).sell_tax == 0.001
    # the KRX default book is the literal from develop 25580a4 (field default_factory before the market axis)
    assert asdict(krx.screen) == asdict(ScreenConfig(dollar_volume_top_n=30, top_k=8, sector_max=3, volatility_min=0.10))
    # one KRX tax number for the simulator and paper fills (execution.py reads sell_tax_kospi)
    assert MARKETS["KRX"]["sell_tax"] == MARKETS["KRX"]["sell_tax_kospi"] == 0.0020


@pytest.mark.unit
def test_sell_proceeds_by_hand():
    us = SimConfig("2025-01-02", "2025-12-31", market="US", commission=0.0)
    # 100 sh × $200 = $20,000; SEC $20.60/1M → $0.412; TAF 100 × $0.000195 = $0.0195
    assert sell_proceeds(100, 200.0, us) == pytest.approx(20_000 - 0.412 - 0.0195, abs=1e-9)
    # 100,000 sh: TAF 19.5 capped at 9.79
    assert sell_proceeds(100_000, 10.0, us) == pytest.approx(1_000_000 - 20.60 - 9.79, abs=1e-6)
    krx = SimConfig("2025-01-02", "2025-12-31")
    # 100 sh × 50,000원: commission 0.015% + tax 0.20%, no SEC/TAF
    assert sell_proceeds(100, 50_000.0, krx) == pytest.approx(5_000_000 * (1 - 0.00015 - 0.0020), abs=1e-6)


@pytest.mark.unit
def test_krx_path_matches_the_numbers_before_the_market_axis():
    # captured on develop 25580a4 (before MARKETS) with these exact inputs
    frames = _paths()
    off = run_simulation(frames, _index(), _cfg(label="off"))
    on = run_simulation(frames, _index(), _cfg(label="on", risk=RiskConfig()))
    wavy = run_simulation(frames, _wavy_index(), _cfg(label="regime", risk=RiskConfig(), regime_filter=True))
    assert float(off.equity.iloc[-1]) == 89050253.22120363
    assert off.metrics["max_drawdown"] == -0.24945679570388268 and len(off.trades) == 1
    assert float(on.equity.iloc[-1]) == 96919818.66849957
    assert on.metrics["max_drawdown"] == -0.05199611640310686
    assert on.exits_by_reason() == {"expired": 11, "stop_loss": 1}
    assert float(wavy.equity.iloc[-1]) == 96751735.85572746
    assert wavy.metrics["max_drawdown"] == -0.05020861492369533 and len(wavy.trades) == 8
    assert sum(1 for e in wavy.events if e["kind"] == "lock" and e["text"].startswith("국면 필터")) == 15
    assert float(off.benchmark.iloc[-1]) == 105421878.0277519


@pytest.mark.unit
def test_regime_wording_and_benchmark_name_follow_the_market():
    frames, idx = _paths(), _wavy_index()
    texts = {}
    for market in ("KRX", "US"):
        r = run_simulation(frames, idx, _cfg(label=market, risk=RiskConfig(), regime_filter=True, market=market))
        texts[market] = {e["text"] for e in r.events if e["kind"] == "lock" and e["text"].startswith("국면 필터")}
        assert r.benchmark.name == {"KRX": "kospi", "US": "sp500"}[market]
    assert texts["KRX"] == {"국면 필터: 코스피 종가가 200일선 아래, 신규 매수 없음"}
    assert texts["US"] == {"국면 필터: S&P 500 종가가 200일선 아래, 신규 매수 없음"}


@pytest.mark.unit
def test_us_preset_drops_names_under_five_dollars():
    dates = pd.bdate_range("2024-01-02", periods=300)
    t = np.arange(len(dates))

    def frame(level):
        p = level * np.exp(0.0005 * t) * (1 + 0.03 * np.sin(t / 3))
        return pd.DataFrame({"Date": dates, "Open": p, "High": p * 1.01, "Low": p * 0.99, "Close": p, "Volume": 1e7})

    frames = {"CHEAP": frame(3.0), "OK": frame(50.0), "OK2": frame(80.0)}

    def load(ticker, as_of, cache_dir=None, fill_gaps=True):
        return frames[ticker]

    as_of = dates[-1].strftime("%Y-%m-%d")
    base = ScreenConfig(volatility_min=0.0, volatility_max=5.0, range_stability_min=0.0, rules=("price_min",))
    us = screen(list(frames), as_of, us_preset(base), load=load)
    verdict = {c.ticker: next(f.passed for f in c.filters if f.rule == "price_min") for c in us.candidates}
    assert frames["CHEAP"]["Close"].iloc[-1] < 5.0
    assert verdict == {"CHEAP": False, "OK": True, "OK2": True}
    # the KRX floor (1,000) would drop all three: the preset is what makes a US book possible
    krx = screen(list(frames), as_of, base, load=load)
    assert not any(f.passed for c in krx.candidates for f in c.filters if f.rule == "price_min")
