"""The sensitivity grid sweeps exactly its three axes, hands each cell's RiskConfig to the simulation,
and its default cell is the docs/05 variant C run."""

import importlib.util
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest

from agent_trading_core import simulate
from agent_trading_core.risk import EPS, RiskConfig
from agent_trading_core.simulate import run_simulation
from tests.test_simulate import _cfg, _index, _paths

_spec = importlib.util.spec_from_file_location("param_grid", Path(__file__).resolve().parents[1] / "scripts" / "param_grid.py")
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

SWEPT = {"stop_loss_pct", "max_position_pct", "trailing_pct"}


@pytest.mark.unit
def test_grid_is_the_full_product_and_leaves_other_fields_at_default():
    cells = pg.grid_configs()
    assert len(cells) == 3 * 3 * 3
    combos = {(c.stop_loss_pct, c.max_position_pct, c.trailing_pct) for c in cells}
    assert len(combos) == 27
    assert combos == {(s, p, t) for s in (-0.05, -0.08, -0.10) for p in (0.05, 0.10, 0.15) for t in (0.03, 0.05, 0.07)}
    default = RiskConfig()
    for c in cells:
        for f in fields(RiskConfig):
            if f.name not in SWEPT:
                assert getattr(c, f.name) == getattr(default, f.name), f.name
    # the draft values are one cell, exactly once
    assert sum(1 for c in cells if c == default) == 1
    assert len(pg.grid_configs((-0.05,), (0.05, 0.10), (0.03,))) == 2


@pytest.mark.unit
def test_paper_checks_by_hand():
    # PAPER_TRADING: 8 weeks, maxDD ≥ −10%, ≤ 2 daily-loss breaches, 0 stop-outs
    ok = pg.paper_checks(-0.10, 2, 0, 40)
    assert ok == {"min_weeks": True, "max_drawdown": True, "max_daily_loss_breaches": True, "max_stop_outs": True,
                  "max_review_rate": None, "max_unexplained_orders": None}
    bad = pg.paper_checks(-0.1001, 3, 1, 39)
    # on the line, as risk.EPS: float noise below −10% still passes
    assert pg.paper_checks(0.9 - 1, 0, 0, 40)["max_drawdown"] is True
    assert pg.paper_checks(-0.1 - 1e-12, 0, 0, 40)["max_drawdown"] is True
    assert [k for k, v in bad.items() if v is False] == ["min_weeks", "max_drawdown", "max_daily_loss_breaches",
                                                         "max_stop_outs"]


@pytest.mark.unit
def test_each_cell_reaches_the_simulation(monkeypatch):
    seen = []
    real = simulate.run_simulation

    def spy(frames, index, cfg, **kw):
        seen.append(cfg)
        return real(frames, index, cfg, **kw)

    monkeypatch.setattr(pg, "run_simulation", spy)
    configs = pg.grid_configs((-0.05, -0.10), (0.05,), (0.03, 0.07))
    rows = pg.run_grid(_paths(), _index(), _cfg(), configs)
    assert [c.risk for c in seen] == configs                 # not a default RiskConfig, not a copy that lost fields
    assert all(c.regime_filter for c in seen)
    assert [(r["stop_loss_pct"], r["trailing_pct"]) for r in rows] == [(-0.05, 0.03), (-0.05, 0.07), (-0.10, 0.03),
                                                                       (-0.10, 0.07)]


@pytest.mark.unit
def test_default_cell_reproduces_rules_on_regime():
    frames, idx = _paths(), _index()
    rows = pg.run_grid(frames, idx, _cfg(), [RiskConfig()])
    direct = run_simulation(frames, idx, _cfg(label="rules_on_regime", risk=RiskConfig(), regime_filter=True))
    row = rows[0]
    assert row["max_drawdown"] == direct.metrics["max_drawdown"]
    assert row["total_return"] == direct.metrics["total_return"]
    assert row["volatility"] == direct.metrics["volatility"]
    assert row["trades"] == len(direct.trades)
    assert row["worst_trade_pct"] == min(t.pnl_pct for t in direct.trades)
    assert row["exits_by_reason"] == direct.exits_by_reason()
    assert row["stop_outs"] == sum(1 for e in direct.events if e["kind"] == "guard" and e["text"].startswith("MaxDrawdown"))
    assert row["paper_measurable"] == 4 and 0 <= row["paper_passed"] <= 4
    assert asdict(RiskConfig()) == asdict(pg.grid_configs()[13])   # middle cell of the 3×3×3 is the default


@pytest.mark.unit
def test_guard_counts_match_hand_count():
    # tight guards so the CRASH gap trips both: −0.5% daily loss limit, −2% stop-out
    risk = replace(RiskConfig(), daily_loss_limit=-0.005, dd_halve=-0.01, dd_stop=-0.02)
    frames, idx = _paths(), _index()
    row = pg.run_grid(frames, idx, _cfg(), [risk])[0]
    direct = run_simulation(frames, idx, _cfg(risk=risk, regime_filter=True))
    day_ret = direct.equity.pct_change().dropna()
    assert row["daily_loss_breaches"] == int((day_ret <= risk.daily_loss_limit + EPS).sum()) > 0
    # a stop-out liquidates the book: at least one forced exit, and the check fails
    assert row["stop_outs"] > 0 and row["stopped_out"] is True
    assert row["exits_by_reason"].get("force", 0) > 0
    assert row["paper_checks"]["max_stop_outs"] is False
