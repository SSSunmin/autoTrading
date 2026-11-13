"""Universe screening: deterministic rules applied in order, every verdict recorded.

The rules and their order come from docs/01 §2-4, the ranking score from the MSCI
Momentum Indexes methodology (§11-1): 12-1 and 6-1 month price momentum, each
divided by 3-year volatility, z-scored across the universe and blended 50:50.

Every ticker gets a ``CandidateRecord`` with one ``FilterResult`` per rule it was
evaluated against. A relative rule (top-N, quota) is evaluated only on the
tickers still alive, so a ticker that failed earlier simply has no entry for it;
the dashboard shows that as "not evaluated".
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np
import pandas as pd

from agent_trading_core.audit import CandidateRecord, FilterResult, ScreenRun
from agent_trading_core.errors import VendorError
from agent_trading_core.identity import resolve_instrument_identity
from agent_trading_core.universe import SECTORS, load_sectors
from agent_trading_core.yahoo import load_ohlcv

logger = logging.getLogger(__name__)


def sector_lookup(ticker: str, path=SECTORS) -> str | None:
    """A ``sector_of`` for the quota rule: the sector file when it lists the ticker (no network, None if it has no
    sector), else the Yahoo profile. The profile lookup is fail-open: an error counts as no sector."""
    known = load_sectors(path)
    if ticker in known:
        return known[ticker]
    try:
        return resolve_instrument_identity(ticker).get("sector")
    except Exception as exc:
        logger.warning("%s: sector lookup failed: %s", ticker, exc)
        return None


TRADING_DAYS = 252
MONTH = 21


@dataclass
class ScreenConfig:
    """Rule parameters. Defaults are the docs/01 §2-4 draft for KRX large caps."""

    price_min: float = 1000.0
    listed_days_min: int = 180
    dollar_volume_top_n: int = 100
    dollar_volume_window: int = 20
    volatility_min: float = 0.15
    volatility_max: float = 0.80
    volatility_window: int = 20
    range_stability_min: float = 0.05
    range_window: int = 60
    sector_max: int = 3
    performance_min_alpha: float = -0.03
    performance_n: int = 10
    top_k: int = 15

    rules: tuple[str, ...] = field(default=(
        "price_min", "listed_days_min", "dollar_volume_top_n", "volatility_band",
        "range_stability_min", "sector_quota", "performance_filter", "momentum_top_k",
    ))


# US preset: a $5 floor, the usual penny-stock cut (SEC Rule 3a51-1 treats stocks under $5 as penny stocks).
# Every other rule keeps the KRX default on purpose: the US book is measured once with the same rules, not tuned.
US_PRICE_MIN = 5.0


def us_preset(cfg: ScreenConfig | None = None) -> ScreenConfig:
    return replace(cfg or ScreenConfig(), price_min=US_PRICE_MIN)


@dataclass
class _Features:
    close: float
    listed_days: int
    dollar_volume: float
    volatility: float
    range_change: float
    mom_12_1: float | None
    mom_6_1: float | None
    vol_3y: float | None
    sector: str | None = None
    score: float | None = None


def _features(frame: pd.DataFrame, as_of: str, cfg: ScreenConfig, sector: str | None) -> _Features:
    close = frame["Close"].astype(float)
    _high, _low, volume = frame["High"].astype(float), frame["Low"].astype(float), frame["Volume"].astype(float)
    first = pd.to_datetime(frame["Date"].iloc[0])
    listed_days = (pd.to_datetime(as_of) - first).days

    dv = (close * volume).tail(cfg.dollar_volume_window).mean()
    log_ret = np.log(close).diff().dropna()
    vol = float(log_ret.tail(cfg.volatility_window).std(ddof=0) * math.sqrt(TRADING_DAYS)) if len(log_ret) >= 2 else 0.0
    window = frame.tail(cfg.range_window)
    rng = float((window["High"].max() - window["Low"].min()) / window["Low"].min()) if len(window) else 0.0

    def ret(skip: int, back: int) -> float | None:
        if len(close) <= back:
            return None
        return float(close.iloc[-1 - skip] / close.iloc[-1 - back] - 1)

    weekly = close.iloc[::-5][::-1].tail(156)   # every 5th bar from the end, up to 3 years
    weekly_ret = np.log(weekly).diff().dropna()
    vol_3y = float(weekly_ret.std(ddof=0) * math.sqrt(52)) if len(weekly_ret) >= 26 else None

    return _Features(
        close=float(close.iloc[-1]), listed_days=listed_days, dollar_volume=float(dv), volatility=vol,
        range_change=rng, mom_12_1=ret(MONTH, 13 * MONTH), mom_6_1=ret(MONTH, 7 * MONTH), vol_3y=vol_3y,
        sector=sector,
    )


def _zscore(values: dict[str, float]) -> dict[str, float]:
    if len(values) < 2:
        return {k: 0.0 for k in values}
    arr = np.array(list(values.values()), dtype=float)
    sd = arr.std(ddof=0)
    if sd == 0:
        return {k: 0.0 for k in values}
    mean = arr.mean()
    return {k: float(max(-3.0, min(3.0, (v - mean) / sd))) for k, v in values.items()}


def momentum_scores(feats: dict[str, _Features]) -> dict[str, float | None]:
    """MSCI-style risk-adjusted momentum: z(12-1 / vol) and z(6-1 / vol), blended 50:50, capped at ±3.

    A ticker lacking one window uses the other alone; lacking both scores None.
    """
    m12 = {t: f.mom_12_1 / f.vol_3y for t, f in feats.items()
           if f.mom_12_1 is not None and f.vol_3y}
    m6 = {t: f.mom_6_1 / f.vol_3y for t, f in feats.items()
          if f.mom_6_1 is not None and f.vol_3y}
    z12, z6 = _zscore(m12), _zscore(m6)
    scores: dict[str, float | None] = {}
    for t in feats:
        parts = [z for z in (z12.get(t), z6.get(t)) if z is not None]
        scores[t] = sum(parts) / len(parts) if parts else None
    return scores


def _recent_alpha(memory_log, ticker: str, n: int) -> float | None:
    """Mean settled alpha of the ticker's last ``n`` decisions, None without any."""
    if memory_log is None:
        return None
    settled = [e for e in memory_log.load_entries() if e["ticker"] == ticker and not e["pending"] and e.get("alpha")]
    settled.sort(key=lambda e: e["date"])
    alphas = []
    for e in settled[-n:]:
        try:
            alphas.append(float(e["alpha"].rstrip("%")) / 100)
        except ValueError:
            continue
    return sum(alphas) / len(alphas) if alphas else None


def screen(
    universe: list[str],
    as_of: str,
    cfg: ScreenConfig | None = None,
    *,
    cache_dir: str | None = None,
    universe_label: str = "custom",
    memory_log=None,
    sector_of: Callable[[str], str | None] | None = None,
    load: Callable[..., pd.DataFrame] = load_ohlcv,
    run_id: str | None = None,
) -> ScreenRun:
    """Apply the rules to ``universe`` as of ``as_of`` and return the full record.

    ``sector_of`` resolves a ticker's sector for the quota rule; None skips that
    rule's effect (every ticker counts as one unnamed sector, so nothing is cut).
    ``load`` is the OHLCV loader, replaceable in tests.
    """
    cfg = cfg or ScreenConfig()
    universe = list(dict.fromkeys(universe))
    records: dict[str, CandidateRecord] = {}
    feats: dict[str, _Features] = {}

    for ticker in universe:
        try:
            frame = load(ticker, as_of, cache_dir=cache_dir)
            sector = sector_of(ticker) if sector_of else None
            feats[ticker] = _features(frame, as_of, cfg, sector)
            records[ticker] = CandidateRecord(ticker=ticker, selected=False)
        except (VendorError, ValueError, KeyError, IndexError) as exc:
            logger.warning("%s: no usable data as of %s: %s", ticker, as_of, exc)
            records[ticker] = CandidateRecord(
                ticker=ticker, selected=False,
                filters=[FilterResult(rule="data", passed=False, note=f"데이터 없음 ({type(exc).__name__}): {exc}")],
            )

    alive = [t for t in universe if t in feats]

    def apply(rule: str, verdicts: dict[str, FilterResult]) -> None:
        nonlocal alive
        for t, v in verdicts.items():
            records[t].filters.append(v)
        alive = [t for t in alive if verdicts[t].passed]

    for rule in cfg.rules:
        if not alive:
            break
        if rule == "price_min":
            apply(rule, {t: FilterResult(rule=rule, value=round(feats[t].close, 2), threshold=cfg.price_min,
                                         passed=feats[t].close >= cfg.price_min) for t in alive})
        elif rule == "listed_days_min":
            apply(rule, {t: FilterResult(rule=rule, value=feats[t].listed_days, threshold=cfg.listed_days_min,
                                         passed=feats[t].listed_days >= cfg.listed_days_min) for t in alive})
        elif rule == "dollar_volume_top_n":
            ranked = sorted(alive, key=lambda t: feats[t].dollar_volume, reverse=True)
            rank = {t: i + 1 for i, t in enumerate(ranked)}
            apply(rule, {t: FilterResult(rule=rule, value=rank[t], threshold=cfg.dollar_volume_top_n,
                                         passed=rank[t] <= cfg.dollar_volume_top_n,
                                         # ponytail: KRW wording; a US run prints dollars ÷ 1e8 as 억원. Pass the
                                         # currency in when US screen records are stored (execution layer, api).
                                         note=f"{cfg.dollar_volume_window}일 평균 거래대금 {feats[t].dollar_volume / 1e8:,.0f}억원")
                         for t in alive})
        elif rule == "volatility_band":
            apply(rule, {t: FilterResult(rule=rule, value=round(feats[t].volatility, 4),
                                         threshold=f"{cfg.volatility_min}-{cfg.volatility_max}",
                                         passed=cfg.volatility_min <= feats[t].volatility <= cfg.volatility_max)
                         for t in alive})
        elif rule == "range_stability_min":
            apply(rule, {t: FilterResult(rule=rule, value=round(feats[t].range_change, 4),
                                         threshold=cfg.range_stability_min,
                                         passed=feats[t].range_change >= cfg.range_stability_min) for t in alive})
        elif rule == "sector_quota":
            scores = momentum_scores({t: feats[t] for t in alive})
            for t in alive:
                feats[t].score = scores[t]
            by_sector: dict[str, list[str]] = {}
            for t in sorted(alive, key=lambda t: (scores[t] is None, -(scores[t] or 0))):
                by_sector.setdefault(feats[t].sector or "", []).append(t)
            verdicts = {}
            for sector, members in by_sector.items():
                for i, t in enumerate(members):
                    keep = i < cfg.sector_max or not sector
                    verdicts[t] = FilterResult(rule=rule, value=i + 1, threshold=cfg.sector_max, passed=keep,
                                               note=sector or "섹터 미확인: 쿼터 미적용")
            apply(rule, verdicts)
        elif rule == "performance_filter":
            verdicts = {}
            for t in alive:
                alpha = _recent_alpha(memory_log, t, cfg.performance_n)
                verdicts[t] = FilterResult(
                    rule=rule, value=None if alpha is None else round(alpha, 4), threshold=cfg.performance_min_alpha,
                    passed=alpha is None or alpha >= cfg.performance_min_alpha,
                    note="정산된 과거 결정 없음" if alpha is None else f"최근 {cfg.performance_n}건 평균 알파",
                )
            apply(rule, verdicts)
        elif rule == "momentum_top_k":
            scores = {t: feats[t].score if feats[t].score is not None else momentum_scores({t: feats[t]})[t]
                      for t in alive}
            ranked = sorted(alive, key=lambda t: (scores[t] is None, -(scores[t] or 0)))
            verdicts = {}
            for i, t in enumerate(ranked):
                records[t].score = None if scores[t] is None else round(scores[t], 4)
                records[t].rank = i + 1
                verdicts[t] = FilterResult(
                    rule=rule, value=i + 1, threshold=cfg.top_k, passed=i < cfg.top_k and scores[t] is not None,
                    note=(f"12-1개월 {feats[t].mom_12_1:+.1%}, 6-1개월 {feats[t].mom_6_1:+.1%}"
                          if feats[t].mom_12_1 is not None and feats[t].mom_6_1 is not None else "모멘텀 산출 구간 부족"),
                )
            apply(rule, verdicts)
        else:
            raise ValueError(f"unknown rule: {rule}")

    for t in alive:
        records[t].selected = True
    for t, f in feats.items():
        if f.sector and records[t].name is None:
            records[t].name = None  # name is resolved by identity elsewhere; sector lives in the note

    return ScreenRun(
        run_id=run_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
        as_of=as_of, universe=universe_label, universe_size=len(universe),
        rules=list(cfg.rules), candidates=[records[t] for t in universe],
    )
