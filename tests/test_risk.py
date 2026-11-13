"""Hard rules fire in freqtrade's order, guards scale sizing, Protections lock, the regime filter reads the 200-day line."""

import pandas as pd
import pytest

from agent_trading_core.risk import (
    Position,
    Protections,
    RiskConfig,
    TradeOutcome,
    check_exit,
    daily_loss_blocks,
    drawdown_scale,
    regime_on,
    size_targets,
)

CFG = RiskConfig()


@pytest.mark.unit
def test_exit_order_is_stop_then_trailing_then_expiry():
    pos = Position("A", entry_day=0, entry_price=100.0, quantity=1)
    assert check_exit(pos, 95.0, 1, CFG) is None
    assert check_exit(pos, 92.0, 1, CFG).reason == "stop_loss"          # −8% exactly
    # trailing only after +10% from entry; then 5% off the high
    pos.high_water = 112.0
    assert check_exit(pos, 107.0, 2, CFG) is None                        # −4.5% from high: hold
    assert check_exit(pos, 106.0, 2, CFG).reason == "trailing"           # −5.4% from high
    # a trailing hit that is also past the stop is reported as the stop (stop checked first)
    assert check_exit(pos, 91.0, 2, CFG).reason == "stop_loss"
    # expiry after max_holding_days, only when nothing else fired
    fresh = Position("B", entry_day=0, entry_price=100.0, quantity=1)
    assert check_exit(fresh, 101.0, 19, CFG) is None
    assert check_exit(fresh, 101.0, 20, CFG).reason == "expired"


@pytest.mark.unit
def test_trailing_is_not_active_before_the_activation_gain():
    pos = Position("A", entry_day=0, entry_price=100.0, quantity=1, high_water=109.0)
    assert check_exit(pos, 103.0, 1, CFG) is None                        # −5.5% from a high that never reached +10%


@pytest.mark.unit
def test_drawdown_scale_steps_and_daily_loss():
    assert drawdown_scale(100, 100, CFG) == (1.0, None)
    assert drawdown_scale(91, 100, CFG)[0] == 1.0
    scale, why = drawdown_scale(90, 100, CFG)
    assert scale == 0.5 and "절반" in why
    scale, why = drawdown_scale(80, 100, CFG)
    assert scale == 0.0 and why.startswith("MaxDrawdown")
    assert drawdown_scale(50, 0, CFG) == (1.0, None)                     # no peak yet
    assert daily_loss_blocks(-0.019, CFG) is None
    assert "다음 세션 신규 매수 중단" in daily_loss_blocks(-0.02, CFG)


@pytest.mark.unit
def test_protections_stoploss_guard_cooldown_and_low_profit():
    p = Protections(CFG)
    assert p.record(TradeOutcome("A", 10, "stop_loss", -0.08)) == []
    assert p.locked_ticker("A", 14) and p.locked_ticker("A", 15) is None  # cooldown 5 days
    assert p.locked_global(11) is None
    p.record(TradeOutcome("B", 12, "stop_loss", -0.09))
    fired = p.record(TradeOutcome("C", 15, "stop_loss", -0.08))          # 3 stops within 10 days
    assert any(f.startswith("StoplossGuard") for f in fired)
    assert p.locked_global(19) and p.locked_global(20) is None           # lock 5 days from day 15
    # an old stop outside the lookback does not count
    q = Protections(CFG)
    q.record(TradeOutcome("A", 0, "stop_loss", -0.08)); q.record(TradeOutcome("B", 1, "stop_loss", -0.08))
    assert q.record(TradeOutcome("C", 11, "stop_loss", -0.08)) == []
    # LowProfit: two losing trades on the same name inside 30 days → 10-day lock
    r = Protections(CFG)
    r.record(TradeOutcome("Z", 5, "expired", -0.02))
    fired = r.record(TradeOutcome("Z", 20, "exit_signal", -0.01))
    assert any(f.startswith("LowProfit") for f in fired)
    assert r.locked_ticker("Z", 29) and r.locked_ticker("Z", 30) is None
    # winners do not lock beyond the cooldown
    s = Protections(CFG)
    s.record(TradeOutcome("W", 5, "trailing", 0.12)); assert s.record(TradeOutcome("W", 20, "trailing", 0.05)) == []


@pytest.mark.unit
def test_regime_reads_the_200_day_average():
    up = pd.Series([100.0] * 199 + [101.0])
    assert regime_on(up, CFG)
    down = pd.Series([100.0] * 199 + [99.0])
    assert not regime_on(down, CFG)
    assert regime_on(pd.Series([50.0, 40.0]), CFG)                       # too short: no filter


@pytest.mark.unit
def test_size_targets_applies_name_sector_gross_caps_and_scale():
    w, ov = size_targets(["A", "B"], CFG)
    assert w == {"A": 0.10, "B": 0.10} and len(ov) == 2 and ov[0].startswith("max_position_pct: A 50.0% → 10.0%")
    sectors = {"A": "반도체", "B": "반도체", "C": "반도체", "D": "금융"}
    w, ov = size_targets(list(sectors), CFG, sector_of=sectors.get)
    assert w == {"A": 0.10, "B": 0.10, "D": 0.10} and any(o.startswith("sector_cap: C") for o in ov)
    w, ov = size_targets(["A", "B"], CFG, scale=0.5)
    assert w == {"A": 0.05, "B": 0.05} and "가드 스케일 ×0.5" in ov
    assert size_targets(["A"], CFG, scale=0.0) == ({}, ["신규 매수 비중 0 (가드 발동)"])
    assert size_targets([], CFG) == ({}, [])
    loose = RiskConfig(max_position_pct=0.6, max_sector_pct=1.0)
    w, ov = size_targets(["A", "B"], loose)
    assert sum(w.values()) == pytest.approx(1.0) and ov == []
    w, ov = size_targets(["A", "B"], RiskConfig(max_position_pct=0.6, max_sector_pct=1.0, max_gross_pct=0.8))
    assert sum(w.values()) == pytest.approx(0.8) and ov[0].startswith("max_gross_pct")
