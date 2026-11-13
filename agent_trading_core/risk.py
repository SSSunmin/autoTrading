"""Hard rules: the risk team's guidelines, as code the agent cannot talk its way around.

Everything here is deterministic and pure so it can be tested cell by cell and replayed in a
backtest. The pieces, in the order the loop applies them each day (docs/01 §3, §5; docs/02 v0.3):

- ``check_exit``       per position: stop-loss → trailing stop → holding-period expiry (freqtrade should_exit order)
- ``drawdown_scale``   portfolio drawdown vs peak: −10% halves new sizing, −20% stops out (ai-hedge-fund clamps)
- ``daily_loss_blocks`` a day worse than the limit blocks new buys on the next session
- ``Protections``      freqtrade's four: StoplossGuard (global lock after N stops), MaxDrawdown (via drawdown_scale),
                       Cooldown (no re-entry right after an exit), LowProfit (lock a ticker that keeps losing)
- ``regime_on``        index above its 200-day average; below it no new buys are opened
- ``size_targets``     equal weight capped per name, per sector and by gross exposure, scaled by the guards

The functions return the reason they fired as text so ``DecisionRecord.hard_rule_overrides`` and the
exception queue can show what the rule did and why.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

ExitReason = Literal["stop_loss", "trailing", "expired", "exit_signal", "force"]

# Thresholds are hit "at" the boundary; 90/100 - 1 is -0.0999…, so compare with a tolerance.
EPS = 1e-9


@dataclass(frozen=True)
class RiskConfig:
    """Risk-team parameters. Defaults are the docs/01 draft for a KRX large-cap book."""

    # sizing
    max_position_pct: float = 0.10       # of equity, per name
    max_sector_pct: float = 0.20         # of equity, per sector
    max_gross_pct: float = 1.00          # total long exposure
    # per-position exits, vs entry price (close-to-close; fills happen next open, so gaps add to these)
    stop_loss_pct: float = -0.08
    trailing_activate_pct: float = 0.10  # trailing starts once the position is up this much
    trailing_pct: float = 0.05           # ...and exits this far below the high-water mark
    max_holding_days: int = 20           # trading days; re-selection at a rebalance does not reset the clock
    # portfolio guards
    daily_loss_limit: float = -0.02      # equity change in one session
    dd_halve: float = -0.10              # drawdown from peak → new sizing × 0.5
    dd_stop: float = -0.20               # drawdown from peak → liquidate, lock
    dd_stop_lock_days: int = 20          # trading days with no new buys after the stop
    # after the lock: "reset_peak" = the executor's rule (execution.py, admin resume after a −20% stop): the peak
    # becomes that close's equity and sizing is normal from it. "half_size" = the backtest rule before 2026-11-13
    # (reset, then half weight until back within dd_halve of the old peak), kept to reproduce earlier numbers.
    dd_stop_recovery: Literal["reset_peak", "half_size"] = "reset_peak"
    # Protections (freqtrade names)
    stoploss_guard_n: int = 3
    stoploss_guard_lookback_days: int = 10
    stoploss_guard_lock_days: int = 5
    cooldown_days: int = 5
    low_profit_n: int = 2
    low_profit_lookback_days: int = 30
    low_profit_lock_days: int = 10
    # regime
    regime_ma_days: int = 200


@dataclass
class Position:
    ticker: str
    entry_day: int                      # trading-day index of the fill
    entry_price: float
    quantity: int
    sector: str | None = None
    high_water: float = 0.0             # highest close since entry

    def __post_init__(self) -> None:
        self.high_water = max(self.high_water, self.entry_price)


@dataclass(frozen=True)
class ExitSignal:
    ticker: str
    reason: ExitReason
    note: str


def check_exit(pos: Position, close: float, day: int, cfg: RiskConfig) -> ExitSignal | None:
    """freqtrade's should_exit order: stop-loss first, then trailing stop, then time expiry.

    ``close`` is today's close; the caller updates ``pos.high_water`` from it after this check so the
    trailing stop measures from the high seen *before* today's bar, as a live loop would.
    """
    ret = close / pos.entry_price - 1
    if ret <= cfg.stop_loss_pct + EPS:
        return ExitSignal(pos.ticker, "stop_loss", f"손절: 진입가 대비 {ret:+.1%} ≤ {cfg.stop_loss_pct:+.0%}")
    high_ret = pos.high_water / pos.entry_price - 1
    if high_ret >= cfg.trailing_activate_pct - EPS:
        from_high = close / pos.high_water - 1
        if from_high <= -cfg.trailing_pct + EPS:
            return ExitSignal(pos.ticker, "trailing",
                              f"트레일링: 고점 {pos.high_water:,.0f} 대비 {from_high:+.1%} ≤ −{cfg.trailing_pct:.0%} (고점 수익 {high_ret:+.1%})")
    if day - pos.entry_day >= cfg.max_holding_days:
        return ExitSignal(pos.ticker, "expired", f"보유기간 만료: {day - pos.entry_day}거래일 ≥ {cfg.max_holding_days}")
    return None


def drawdown_scale(equity: float, peak: float, cfg: RiskConfig) -> tuple[float, str | None]:
    """Sizing multiplier from the portfolio drawdown: 1.0 normal, 0.5 past dd_halve, 0.0 (stop-out) past dd_stop."""
    if peak <= 0:
        return 1.0, None
    dd = equity / peak - 1
    if dd <= cfg.dd_stop + EPS:
        return 0.0, f"MaxDrawdown: 피크 대비 {dd:+.1%} ≤ {cfg.dd_stop:+.0%} → 전량 청산·정지"
    if dd <= cfg.dd_halve + EPS:
        return 0.5, f"드로다운 {dd:+.1%} ≤ {cfg.dd_halve:+.0%} → 신규 비중 절반"
    return 1.0, None


def daily_loss_blocks(day_return: float, cfg: RiskConfig) -> str | None:
    if day_return <= cfg.daily_loss_limit + EPS:
        return f"일일 손실 한도: {day_return:+.2%} ≤ {cfg.daily_loss_limit:+.0%} → 다음 세션 신규 매수 중단"
    return None


@dataclass(frozen=True)
class TradeOutcome:
    ticker: str
    exit_day: int
    reason: ExitReason
    pnl_pct: float


@dataclass
class Protections:
    """freqtrade Protections over the closed-trade history. ``day`` is a trading-day index."""

    cfg: RiskConfig
    history: deque[TradeOutcome] = field(default_factory=deque)
    global_lock_until: int = -1
    ticker_lock_until: dict[str, int] = field(default_factory=dict)
    _reasons: dict[str, str] = field(default_factory=dict)

    def record(self, outcome: TradeOutcome) -> list[str]:
        """Add a closed trade; returns the locks it triggered (as text), if any."""
        self.history.append(outcome)
        fired: list[str] = []
        c = self.cfg
        # Cooldown: never buy back what was just sold.
        self.ticker_lock_until[outcome.ticker] = max(self.ticker_lock_until.get(outcome.ticker, -1),
                                                     outcome.exit_day + c.cooldown_days)
        self._reasons[outcome.ticker] = f"Cooldown: {outcome.ticker} 청산 후 {c.cooldown_days}거래일 재진입 금지"
        # StoplossGuard: too many stops recently → nothing new for a while.
        recent_stops = [o for o in self.history
                        if o.reason == "stop_loss" and outcome.exit_day - o.exit_day < c.stoploss_guard_lookback_days]
        if len(recent_stops) >= c.stoploss_guard_n and outcome.exit_day + c.stoploss_guard_lock_days > self.global_lock_until:
            self.global_lock_until = outcome.exit_day + c.stoploss_guard_lock_days
            self._reasons["__global__"] = (f"StoplossGuard: {c.stoploss_guard_lookback_days}거래일 내 손절 {len(recent_stops)}건 ≥ "
                                           f"{c.stoploss_guard_n} → {c.stoploss_guard_lock_days}거래일 신규 매수 잠금")
            fired.append(self._reasons["__global__"])
        # LowProfit: a name that keeps losing is locked.
        recent = [o for o in self.history
                  if o.ticker == outcome.ticker and outcome.exit_day - o.exit_day < c.low_profit_lookback_days]
        if len(recent) >= c.low_profit_n and sum(o.pnl_pct for o in recent) / len(recent) < 0:
            until = outcome.exit_day + c.low_profit_lock_days
            if until > self.ticker_lock_until.get(outcome.ticker, -1):
                self.ticker_lock_until[outcome.ticker] = until
                self._reasons[outcome.ticker] = (f"LowProfit: {outcome.ticker} 최근 {len(recent)}건 평균 "
                                                 f"{sum(o.pnl_pct for o in recent) / len(recent):+.1%} < 0 → {c.low_profit_lock_days}거래일 잠금")
                fired.append(self._reasons[outcome.ticker])
        return fired

    def locked_global(self, day: int) -> str | None:
        return self._reasons.get("__global__") if day < self.global_lock_until else None

    def locked_ticker(self, ticker: str, day: int) -> str | None:
        return self._reasons.get(ticker) if day < self.ticker_lock_until.get(ticker, -1) else None


def regime_on(index_closes: pd.Series, cfg: RiskConfig) -> bool:
    """True when the index's last close is at or above its ``regime_ma_days`` simple average.
    With fewer bars than the window the regime is treated as on (no filter)."""
    s = index_closes.dropna()
    if len(s) < cfg.regime_ma_days:
        return True
    return float(s.iloc[-1]) >= float(s.iloc[-cfg.regime_ma_days:].mean())


def size_targets(targets: list[str], cfg: RiskConfig, *, scale: float = 1.0,
                 sector_of=None) -> tuple[dict[str, float], list[str]]:
    """Equal weight across ``targets``, then the caps: per name, per sector (first come, first served in
    target order), gross. ``scale`` is the guards' multiplier. Returns (weights, overrides as text)."""
    overrides: list[str] = []
    if not targets or scale <= 0:
        return {}, (["신규 매수 비중 0 (가드 발동)"] if targets else [])
    raw = 1.0 / len(targets)
    weights: dict[str, float] = {}
    sector_used: dict[str, float] = {}
    for t in targets:
        w = raw
        if w > cfg.max_position_pct:
            overrides.append(f"max_position_pct: {t} {w:.1%} → {cfg.max_position_pct:.1%}")
            w = cfg.max_position_pct
        sector = sector_of(t) if sector_of else None
        if sector:
            room = cfg.max_sector_pct - sector_used.get(sector, 0.0)
            if w > room + 1e-12:
                overrides.append(f"sector_cap: {t} ({sector}) {w:.1%} → {max(room, 0):.1%}")
                w = max(room, 0.0)
            sector_used[sector] = sector_used.get(sector, 0.0) + w
        if w > 0:
            weights[t] = w
    gross = sum(weights.values())
    if gross > cfg.max_gross_pct + 1e-12:
        overrides.append(f"max_gross_pct: {gross:.1%} → {cfg.max_gross_pct:.0%}")
        weights = {t: w * cfg.max_gross_pct / gross for t, w in weights.items()}
    if scale < 1.0:
        overrides.append(f"가드 스케일 ×{scale:g}")
        weights = {t: w * scale for t, w in weights.items()}
    return weights, overrides
