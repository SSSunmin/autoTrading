"""Portfolio simulation: the loss-bounding question, not the alpha question.

``run_backtest`` scores single decisions by alpha. This module runs a whole book day by day so the
hard rules (``risk.py``) can be judged by what they are for: the size of the worst drawdown, how long
it took to recover, and the worst single day. The same screen → rating → target pipeline runs with
the rules off, on, and on plus the regime filter, so the difference is the rules.

Mechanics (KRX): decisions are made at the close; fills happen at the *next* open, so gaps count
against the stops. Sells pay transaction tax; both sides pay commission and slippage. Positions are
whole shares.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from agent_trading_core.config import MARKETS
from agent_trading_core.risk import (
    ExitSignal,
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
from agent_trading_core.screening import ScreenConfig, screen, us_preset


def rating_for(rank: int | None, n_ranked: int) -> str:
    """Quartiles of the momentum ranking, as scripts/baseline_krx.py: top Buy, next Overweight,
    bottom Sell, next Underweight, middle Hold; unranked Hold."""
    if rank is None or n_ranked < 4:
        return "Hold"
    k = max(1, n_ranked // 4)
    if rank <= k:
        return "Buy"
    if rank <= 2 * k:
        return "Overweight"
    if rank > n_ranked - k:
        return "Sell"
    if rank > n_ranked - 2 * k:
        return "Underweight"
    return "Hold"


@dataclass
class SimConfig:
    start: str
    end: str
    label: str = "variant"
    initial_cash: float = 100_000_000.0
    rebalance_every: int = 5             # trading days
    commission: float = 0.00015          # per side
    sell_tax: float | None = None        # None → config.MARKETS[market]["sell_tax"] (KRX 2026: 0.20%)
    slippage: float = 0.0005             # per side
    screen: ScreenConfig | None = None   # None → the large-cap book below; US gets the $5 price floor. An explicit
                                         # screen is used as given: a US run must set price_min itself (us_preset)
    risk: RiskConfig | None = None       # None → no hard rules: equal weight, fully invested, exits only on Sell/Underweight
    regime_filter: bool = False          # needs risk
    market: str = "KRX"                  # config.MARKETS key: sell tax, sell-side regulatory fees, benchmark name

    def __post_init__(self) -> None:
        mkt = MARKETS[self.market]
        if self.sell_tax is None:
            self.sell_tax = mkt["sell_tax"]
        if self.screen is None:
            self.screen = ScreenConfig(dollar_volume_top_n=30, top_k=8, sector_max=3, volatility_min=0.10)
            if self.market == "US":
                self.screen = us_preset(self.screen)


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    reason: str
    pnl_pct: float
    holding_days: int


@dataclass
class SimResult:
    label: str
    equity: pd.Series                    # indexed by date
    benchmark: pd.Series                 # index normalized to initial cash
    exposure: pd.Series                  # invested fraction per day
    trades: list[Trade]
    events: list[dict]                   # {date, kind, text}
    rebalances: int
    metrics: dict
    benchmark_metrics: dict

    def exits_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.reason] = out.get(t.reason, 0) + 1
        return out


# --- metrics ------------------------------------------------------------------------------

def metrics(equity: pd.Series) -> dict:
    """Return, CAGR, max drawdown with its peak/trough/recovery dates and lengths, worst day, worst 20 days,
    annualized volatility and Sharpe (rf 0). Dates are ISO strings so the dict serializes."""
    e = equity.dropna()
    if len(e) < 2:
        return {}
    dd = e / e.cummax() - 1
    trough = dd.idxmin()
    peak = e.loc[:trough].idxmax()
    after = dd.loc[trough:]
    recovered = after[after >= -1e-12]
    recovery = recovered.index[0] if len(recovered) else None
    daily = e.pct_change().dropna()
    years = max((e.index[-1] - e.index[0]).days, 1) / 365.25
    total = float(e.iloc[-1] / e.iloc[0] - 1)
    roll20 = (e / e.shift(20) - 1).dropna()
    return {
        "total_return": total,
        "cagr": float((1 + total) ** (1 / years) - 1) if total > -1 else -1.0,
        "max_drawdown": float(dd.min()),
        "dd_peak": peak.strftime("%Y-%m-%d"), "dd_trough": trough.strftime("%Y-%m-%d"),
        "dd_recovery": recovery.strftime("%Y-%m-%d") if recovery is not None else None,
        "dd_length_days": int(e.index.get_loc(trough) - e.index.get_loc(peak)),
        "dd_recovery_days": int(e.index.get_loc(recovery) - e.index.get_loc(trough)) if recovery is not None else None,
        "worst_day": float(daily.min()), "worst_day_date": daily.idxmin().strftime("%Y-%m-%d"),
        "worst_20d": float(roll20.min()) if len(roll20) else None,
        "volatility": float(daily.std() * math.sqrt(252)),
        "sharpe": float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0.0,
        "days": len(e),
    }


def window_metrics(equity: pd.Series, windows: dict[str, tuple[str, str]]) -> dict[str, dict]:
    """Return and max drawdown inside named windows (crash episodes), measured from the window's own start."""
    out = {}
    for name, (a, b) in windows.items():
        e = equity.loc[a:b].dropna()
        if len(e) < 2:
            continue
        dd = e / e.cummax() - 1
        out[name] = {"start": a, "end": b, "return": float(e.iloc[-1] / e.iloc[0] - 1), "max_drawdown": float(dd.min())}
    return out


def sell_proceeds(quantity: int, px: float, cfg: SimConfig) -> float:
    """Cash from a sell at ``px`` (slippage already in it): commission, sell tax, and on US books the SEC
    Section 31 fee (fraction of the sale) and FINRA TAF (per share, capped per trade). KRX has neither fee."""
    mkt = MARKETS[cfg.market]
    sec_fee = mkt.get("sec_fee_rate", 0.0)
    taf = min(quantity * mkt.get("taf_per_share", 0.0), mkt.get("taf_max", 0.0))
    return quantity * px * (1 - cfg.commission - cfg.sell_tax - sec_fee) - taf


# --- simulation -----------------------------------------------------------------------------

def _panel(frames: dict[str, pd.DataFrame], dates: pd.DatetimeIndex, col: str) -> pd.DataFrame:
    cols = {}
    for t, f in frames.items():
        s = f.set_index("Date")[col]
        s = s[~s.index.duplicated(keep="last")]
        cols[t] = s.reindex(dates)
    return pd.DataFrame(cols, index=dates)


def run_simulation(frames: dict[str, pd.DataFrame], index_frame: pd.DataFrame, cfg: SimConfig, *,
                   sector_of: Callable[[str], str | None] | None = None,
                   progress: Callable[[int, int, str], None] | None = None,
                   universe_on: Callable[[pd.Timestamp], set[str]] | None = None) -> SimResult:
    """``frames``: full OHLCV history per ticker (Date naive, ascending). ``index_frame``: the benchmark index OHLCV,
    which also sets the trading calendar and feeds the regime filter.

    ``universe_on(date)``: the index members on that date (point-in-time universe, ``universe.members_on``). Each
    rebalance then screens only members whose prices still run; a name that leaves the index is kept until its own
    exit rules sell it, it just is not bought again. Without it every frame whose prices still run is a candidate.
    A held name whose prices stop (delisted, acquired) is closed at its last close on the first day without a bar."""
    risk = cfg.risk
    mkt = MARKETS[cfg.market]
    idx = index_frame.set_index("Date")["Close"].sort_index()
    idx = idx[~idx.index.duplicated(keep="last")]
    dates = idx.loc[cfg.start:cfg.end].index
    if len(dates) < 2:
        raise ValueError("시뮬레이션 구간에 거래일이 2일 미만")
    closes = _panel(frames, dates, "Close").ffill()
    opens = _panel(frames, dates, "Open").fillna(closes)
    universe = list(frames)
    last_bar = {t: f.loc[f["Close"].notna(), "Date"].max() for t, f in frames.items()}

    def load(ticker, as_of, cache_dir=None, fill_gaps=True):
        f = frames[ticker]
        return f[f["Date"] <= pd.Timestamp(as_of)]

    cash = cfg.initial_cash
    positions: dict[str, Position] = {}
    pending_sells: dict[str, ExitSignal] = {}
    pending_buys: dict[str, float] = {}             # ticker → target value (KRW)
    protections = Protections(risk) if risk else None
    peak = cfg.initial_cash
    old_peak: float | None = None                   # set while recovering from a stop-out
    stop_lock_until = -1
    block_buys_next = False
    trades: list[Trade] = []
    events: list[dict] = []
    eq_hist: list[float] = []
    exp_hist: list[float] = []
    rebalances = 0

    def event(date, kind, text):
        events.append({"date": date.strftime("%Y-%m-%d"), "kind": kind, "text": text})

    def buy_cost(px: float) -> float:
        return px * (1 + cfg.slippage) * (1 + cfg.commission)

    for i, date in enumerate(dates):
        if progress and i % 50 == 0:
            progress(i, len(dates), date.strftime("%Y-%m-%d"))
        # 0. a held name with no bar today and none after: its series ended, close at the last price we saw
        for t in [t for t in positions if last_bar[t] < date]:
            pos = positions.pop(t)
            pending_sells.pop(t, None)
            px = float(closes.at[date, t]) * (1 - cfg.slippage)     # forward-filled last close
            cash += sell_proceeds(pos.quantity, px, cfg)
            pnl = px / pos.entry_price - 1
            trades.append(Trade(t, dates[pos.entry_day].strftime("%Y-%m-%d"), date.strftime("%Y-%m-%d"), pos.entry_price,
                                px, pos.quantity, "delisted", pnl, i - pos.entry_day))
            event(date, "exit", f"{t} delisted {pnl:+.1%}: 마지막 시세 {last_bar[t]:%Y-%m-%d} 이후 거래 없음 → 마지막 종가로 청산")
            if protections:
                for fired in protections.record(TradeOutcome(t, i, "delisted", pnl)):
                    event(date, "protection", fired)
        # 1. fills at today's open, decided at yesterday's close
        for t, sig in list(pending_sells.items()):
            pos = positions.pop(t, None)
            if pos is None:
                continue
            px = float(opens.at[date, t]) * (1 - cfg.slippage)
            if math.isnan(px):
                positions[t] = pos
                continue
            proceeds = sell_proceeds(pos.quantity, px, cfg)
            cash += proceeds
            pnl = px / pos.entry_price - 1
            trades.append(Trade(t, dates[pos.entry_day].strftime("%Y-%m-%d"), date.strftime("%Y-%m-%d"), pos.entry_price,
                                px, pos.quantity, sig.reason, pnl, i - pos.entry_day))
            event(date, "exit", f"{t} {sig.reason} {pnl:+.1%}: {sig.note}")
            if protections:
                for fired in protections.record(TradeOutcome(t, i, sig.reason, pnl)):
                    event(date, "protection", fired)
        pending_sells.clear()
        for t, value in pending_buys.items():
            if t in positions or last_bar[t] < date:
                continue
            px = float(opens.at[date, t])
            if math.isnan(px) or px <= 0:
                continue
            qty = int(min(value, cash) // buy_cost(px))
            if qty <= 0:
                continue
            cash -= qty * buy_cost(px)
            positions[t] = Position(t, i, px * (1 + cfg.slippage), qty, sector_of(t) if sector_of else None)
        pending_buys.clear()

        # 2. mark to market
        holdings = sum(p.quantity * float(closes.at[date, t]) for t, p in positions.items())
        equity = cash + holdings
        prev = eq_hist[-1] if eq_hist else cfg.initial_cash
        day_ret = equity / prev - 1
        eq_hist.append(equity)
        exp_hist.append(holdings / equity if equity > 0 else 0.0)
        peak = max(peak, equity)
        scale = 1.0

        # 3. per-position exits first (so a stop is recorded as a stop), then the portfolio guards
        if risk:
            for t, pos in positions.items():
                close = float(closes.at[date, t])
                sig = check_exit(pos, close, i, risk)
                if sig:
                    pending_sells[t] = sig
                pos.high_water = max(pos.high_water, close)
            if risk.dd_stop_recovery == "reset_peak" and old_peak is not None and i >= stop_lock_until:
                # lock expired after a stop-out: the executor's admin resume (execution.peak_base / _peak: the last
                # close's equity, then the highest close after it), assumed unattended at the end of the lock. It
                # resets whatever is still held and wherever the liquidating open left the book; this close's buys
                # are sized at normal weight from the new peak and fill at the next open, like any decision
                events.append({"date": date.strftime("%Y-%m-%d"), "kind": "peak_reset",
                               "text": f"재가동: 기준 피크를 {old_peak:,.0f} → {equity:,.0f}(그날 종가 평가액)으로 재설정, "
                                       f"정상 비중", "peak_before": old_peak, "peak_after": equity})
                peak, old_peak = equity, None
            scale, why = drawdown_scale(equity, peak, risk)
            if scale == 0.0 and i >= stop_lock_until and positions:
                for t in positions:
                    pending_sells.setdefault(t, ExitSignal(t, "force", why or "MaxDrawdown"))
                stop_lock_until = i + risk.dd_stop_lock_days
                old_peak = peak
                event(date, "guard", f"{why} · {risk.dd_stop_lock_days}거래일 잠금 후 기준 피크 재설정, " +
                      ("절반 비중으로 재가동" if risk.dd_stop_recovery == "half_size" else "정상 비중으로 재가동"))
            elif scale == 0.0 and i >= stop_lock_until and not positions and old_peak is not None:
                # half_size (the rule before 2026-11-13): lock expired, reset to this close's equity, half size until
                # back within dd_halve of the old peak. Kept as it was so earlier numbers reproduce
                peak = equity
                scale = 0.5
                event(date, "guard", f"재가동: 기준 피크를 {equity:,.0f}으로 재설정, 이전 피크 {old_peak:,.0f} 대비 "
                                     f"{equity / old_peak - 1:+.1%} 회복까지 절반 비중")
            elif (old_peak is not None and risk.dd_stop_recovery == "half_size"
                  and equity >= old_peak * (1 + risk.dd_halve)):
                event(date, "guard", f"회복: 이전 피크 대비 {equity / old_peak - 1:+.1%}, 정상 비중 복귀")
                old_peak = None
            if old_peak is not None and scale > 0:
                scale = min(scale, 0.5)
            if i < stop_lock_until:
                scale = 0.0
            blocked = daily_loss_blocks(day_ret, risk)
            if blocked:
                event(date, "guard", blocked)
            block_buys_now, block_buys_next = block_buys_next, bool(blocked)

        # 4. rebalance: screen → rating → targets, orders for the next open
        if i % cfg.rebalance_every == 0 and i < len(dates) - 1:
            rebalances += 1
            # a name whose prices have stopped is never a candidate (fixed lists: none stop, so nothing changes)
            members = universe_on(date) if universe_on is not None else None
            candidates = [t for t in universe if last_bar[t] >= date and (members is None or t in members)]
            run = screen(candidates, date.strftime("%Y-%m-%d"), cfg.screen, load=load, sector_of=sector_of,
                         run_id=f"{cfg.label}_{date:%Y-%m-%d}")
            ranked = [c for c in run.candidates if c.rank is not None]
            ranked.sort(key=lambda c: c.rank)
            ratings = {c.ticker: rating_for(c.rank, len(ranked)) for c in run.candidates}
            targets = [c.ticker for c in ranked if ratings[c.ticker] in ("Buy", "Overweight")]
            for t in list(positions):
                if ratings.get(t) in ("Sell", "Underweight") and t not in pending_sells:
                    pending_sells[t] = ExitSignal(t, "exit_signal", f"등급 {ratings[t]}: 모멘텀 하위로 내려옴")
            if risk is None:
                weights = {t: 1.0 / len(targets) for t in targets} if targets else {}
                overrides: list[str] = []
                locks: list[str] = []
            else:
                weights, overrides = size_targets(targets, risk, scale=scale, sector_of=sector_of)
                locks = []
                if block_buys_now:
                    locks.append("일일 손실 한도 발동: 오늘 신규 매수 없음")
                g = protections.locked_global(i) if protections else None
                if g:
                    locks.append(g)
                if cfg.regime_filter and not regime_on(idx.loc[:date], risk):
                    locks.append(f"국면 필터: {mkt['benchmark_name_ko']} 종가가 {risk.regime_ma_days}일선 아래, 신규 매수 없음")
            new = []
            for t, w in weights.items():
                if t in positions or t in pending_sells:
                    continue
                tl = protections.locked_ticker(t, i) if protections else None
                if tl:
                    locks.append(tl)
                    continue
                if locks and risk is not None and any(not l.startswith(("Cooldown", "LowProfit")) for l in locks):
                    continue
                pending_buys[t] = w * equity
                new.append(t)
            event(date, "rebalance", f"선별 {run.universe_size}→{len(ranked)}순위, 목표 {len(targets)}종목, 신규 매수 {len(new)}"
                  + (f", 매도 {len(pending_sells)}" if pending_sells else "")
                  + (f" · 개입 {len(overrides)}" if overrides else "") + (f" · 잠금 {len(set(locks))}" if locks else ""))
            for o in overrides[:3]:
                event(date, "override", o)
            for l in dict.fromkeys(locks):
                event(date, "lock", l)

    equity_s = pd.Series(eq_hist, index=dates, name="equity")
    bench = (idx.loc[dates] / float(idx.loc[dates].iloc[0]) * cfg.initial_cash).rename(mkt["benchmark_key"])
    return SimResult(cfg.label, equity_s, bench, pd.Series(exp_hist, index=dates), trades, events, rebalances,
                     metrics(equity_s), metrics(bench))
