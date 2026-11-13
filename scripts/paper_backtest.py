"""2nd measurement: does the hard-rule layer bound losses? (docs/03 §4 → docs/05)

Runs the same screen → quartile rating → target pipeline over a KRX large-cap book, 2024-01 → 2026-11,
three ways: rules off, rules on, rules on + 200-day regime filter. The first metric is the maximum
drawdown and how long it took to recover, inside the crash episodes especially; return comes second.
``--market US`` runs the same three variants over a separate US large-cap book (3rd measurement, docs/07):
own cash in USD, own benchmark, no FX, the same RiskConfig defaults measured once.

``--universe constituents`` runs the same book on point-in-time index members instead of the fixed forty
(4th measurement, docs/09): data/universe/<index>_history.csv from scripts/build_universe.py (US) or
scripts/ingest_krx_constituents.py (KRX).

Run:  .venv/Scripts/python scripts/paper_backtest.py [--market KRX|US] [--universe fixed40|constituents]
                                                    [--start 2024-01-02] [--end 2026-11-12]
Writes data/results/paper/ (KRX) or data/results/paper_us/ (US), or data/results/paper_universe/<market>/ for
constituents: summary.json and <variant>_equity.csv.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from dataclasses import asdict, replace
from functools import cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.config import MARKETS
from agent_trading_core.errors import NoMarketDataError
from agent_trading_core.identity import resolve_instrument_identity
from agent_trading_core.kis import KisClient, load_kis_ohlcv
from agent_trading_core.krx_names import display_name, korean_name
from agent_trading_core.risk import RiskConfig
from agent_trading_core.simulate import SimConfig, run_simulation, window_metrics
from agent_trading_core.universe import load_history, members_on, tickers
from agent_trading_core.yahoo import load_ohlcv

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("paper_backtest")

# Forty KOSPI large caps across sectors: the baseline twenty plus twenty more. Survivorship bias: these are
# *current* members, so names that fell out of the index since 2024 are missing. The membership-dated
# universe (docs/03 §4) replaces this list when the KRX constituent history is loaded.
UNIVERSE = [
    "005930.KS", "000660.KS", "373220.KS", "207940.KS", "005380.KS", "000270.KS", "068270.KS", "105560.KS",
    "055550.KS", "035420.KS", "012330.KS", "028260.KS", "006400.KS", "051910.KS", "003670.KS", "035720.KS",
    "032830.KS", "066570.KS", "015760.KS", "034730.KS",
    "005490.KS", "011200.KS", "009150.KS", "018260.KS", "017670.KS", "030200.KS", "086790.KS", "316140.KS",
    "000810.KS", "010950.KS", "096770.KS", "009540.KS", "329180.KS", "042660.KS", "012450.KS", "010140.KS",
    "000100.KS", "033780.KS", "259960.KS", "036570.KS",
]
INDEX = "^KS11"

# Episodes a Korean book had to live through in the window. Measured from each window's own start.
WINDOWS = {
    "2024-08 급락 (엔 캐리 청산)": ("2024-07-01", "2024-09-30"),
    "2025-04 관세 충격": ("2025-03-20", "2025-05-15"),
    "2026 급등 국면": ("2026-04-01", "2026-11-12"),
}

# Forty S&P 500 large caps covering all 11 GICS sectors, fixed by sector and size before any run:
# IT 6 · communication 4 · consumer discretionary 4 · staples 4 · health care 4 · financials 5 · industrials 4 ·
# energy 3 · utilities 2 · real estate 2 · materials 2. Same survivorship bias as the KRX list: current members.
US_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM",
    "GOOGL", "META", "NFLX", "DIS",
    "AMZN", "TSLA", "HD", "MCD",
    "WMT", "COST", "PG", "KO",
    "LLY", "JNJ", "UNH", "ABBV",
    "BRK-B", "JPM", "V", "MA", "BAC",
    "GE", "CAT", "RTX", "UNP",
    "XOM", "CVX", "COP",
    "NEE", "SO",
    "PLD", "AMT",
    "LIN", "SHW",
]
# Episodes read off the S&P 500 path alone (docs/07 §1): the KRX August window, the tariff shock from the
# index's 2025-02-19 high, and 2026 from its 01-27 high (March correction, April rebound, the 11-05 −4.4% day).
US_WINDOWS = {
    "2024-08 급락 (엔 캐리 청산)": ("2024-07-01", "2024-09-30"),
    "2025-04 관세 충격": ("2025-02-19", "2025-05-15"),
    "2026 조정·반등": ("2026-01-27", "2026-11-12"),
}

DATA = ROOT / "data"
OUT = DATA / "results" / "paper"

SETUPS = {
    "KRX": {"universe": UNIVERSE, "windows": WINDOWS, "out": OUT, "initial_cash": 100_000_000.0, "commission": 0.00015,
            "caveats": [
                "유니버스는 현재 KOSPI 대형주 40종목 고정: 생존 편향(2024년 이후 지수에서 빠진 종목 없음).",
                "결정은 종가, 체결은 다음날 시가. 가격제한폭·VI·호가단위는 반영하지 않음(갭은 시가로 그대로 반영).",
                "규칙 전용(LLM 없음). 에이전트가 들어오면 같은 격자에서 다시 측정.",
                "거래비용: 수수료 0.015%/편도, 매도 거래세 0.20%, 슬리피지 0.05%/편도.",
            ]},
    # USD 100,000: about the scale of the KRX book's 1억원 (no FX conversion), and a 10% name cap ($10,000) still buys whole shares of
    # the highest-priced names. Commission 0: the US online brokers' zero-commission norm.
    "US": {"universe": US_UNIVERSE, "windows": US_WINDOWS, "out": DATA / "results" / "paper_us",
           "initial_cash": 100_000.0, "commission": 0.0,
           "caveats": [
               "유니버스는 현재 S&P 500 대형주 40종목 고정(11개 GICS 섹터): 생존 편향(2024년 이후 지수에서 빠진 종목 없음).",
               "규모 기준도 현재 시점이라 기간 중 크게 오른 종목이 많이 들어갔다(선정 편향). A가 지수를 이긴 것을 전략 성과로 읽지 않는다.",
               "결정은 종가, 체결은 다음날 시가. LULD 정지·시장 서킷브레이커·프리/애프터마켓은 반영하지 않음(갭은 시가로 그대로 반영).",
               "규칙 전용(LLM 없음). 에이전트가 들어오면 같은 격자에서 다시 측정.",
               "거래비용: 수수료 0, 슬리피지 0.05%/편도, 매도 SEC fee $20.60/백만 달러 + FINRA TAF $0.000195/주(거래당 상한 $9.79). "
               "SEC fee는 기간 중 $27.80 → $0 → $20.60으로 바뀌었지만 2026-11-13 현재 요율을 전 기간에 적용. "
               "TAF는 2026 정규 요율(2026년 4분기 0 면제는 반영하지 않음).",
               "USD 단독 포트폴리오: 환율·국내장과의 합산 없음.",
           ]},
}


# Point-in-time membership (docs/09, scripts/build_universe.py). --universe constituents runs on these.
HISTORIES = {"KRX": DATA / "universe" / "kospi200_history.csv", "US": DATA / "universe" / "sp500_history.csv"}


def load_frame(ticker: str, end: str, cache_dir: str) -> tuple[pd.DataFrame, bool, str | None]:
    """(frame, not_cached_today, cut_at).

    A name whose prices stop before ``end`` (delisted, acquired) fails load_ohlcv's staleness check right after the
    download is cached; only then (a NoMarketDataError on a cache file written today) is it read up to its own last
    bar, returned as ``cut_at``. Any other failure (Yahoo down, rate limit, no rows, a stale cache from another day)
    raises, so the caller lists the name as failed instead of the simulator closing it as delisted.
    ``not_cached_today``: the cache was not already written today, so load_ohlcv downloaded it; only
    names that load are counted (a failure raises and goes to the caller's failed list) —
    yahoo._cache_is_fresh only trusts today's file, so on a new day every name is fetched again and overwritten."""
    path = os.path.join(cache_dir, f"{ticker}-YFin-data.csv")

    def written_today() -> bool:
        return os.path.exists(path) and datetime.date.fromtimestamp(os.path.getmtime(path)) == datetime.date.today()

    fresh = written_today()
    try:
        return load_ohlcv(ticker, end, cache_dir=cache_dir), not fresh, None
    except NoMarketDataError:
        if not written_today():
            raise
        last = pd.to_datetime(pd.read_csv(path, usecols=["Date"])["Date"].str[:10]).max()
        if pd.isna(last) or last >= pd.Timestamp(end):
            raise
        cut = last.strftime("%Y-%m-%d")
        return load_ohlcv(ticker, cut, cache_dir=cache_dir), not fresh, cut


@cache
def sector_of(ticker: str) -> str | None:
    try:
        return resolve_instrument_identity(ticker).get("sector")
    except Exception as exc:  # identity is fail-open; the quota rule then treats the name as unsectored
        logger.warning("%s: sector lookup failed: %s", ticker, exc)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="KRX", choices=sorted(SETUPS))
    parser.add_argument("--universe", default="fixed40", choices=["fixed40", "constituents"])
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    parser.add_argument("--source", choices=["yahoo", "kis"],
                        help="price source for the index and the stocks (default: MARKETS data_source_index / "
                             "data_source_equity). kis: KRX fixed40 only, cached as <symbol>-KIS-data.csv, written to "
                             "<dir>_kis/")
    parser.add_argument("--dd-recovery", default="reset_peak", choices=["reset_peak", "half_size"],
                        help="after a −20%% stop (RiskConfig.dd_stop_recovery); half_size = the rule before 2026-11-13, "
                             "written next to the default output as <dir>_half_size/")
    args = parser.parse_args()
    setup, market = SETUPS[args.market], MARKETS[args.market]
    out, windows, index_symbol = setup["out"], setup["windows"], market["benchmark"]
    risk = replace(RiskConfig(), dd_stop_recovery=args.dd_recovery)
    cache_dir = str(DATA / "cache")
    index_source = args.source or market["data_source_index"]
    equity_source = args.source or market["data_source_equity"]
    kis = None
    if "kis" in (index_source, equity_source):
        if args.market != "KRX" or args.universe != "fixed40":
            parser.error("KIS 소스는 KRX fixed40만 지원한다(미국은 거래소 코드, 시점 구성은 상장폐지 처리가 아직 없다)")
        kis = KisClient()
        out = out.with_name(f"{out.name}_kis")

    names, history, universe_on = setup["universe"], None, None
    if args.universe == "constituents":
        path = HISTORIES[args.market]
        if not path.exists():
            print(f"{args.market}: {path} 없음 — US는 scripts/build_universe.py, KRX는 scripts/ingest_krx_constituents.py로 먼저 만든다. 건너뜀")
            return 0
        history = load_history(path)
        names = tickers(history)
        out = DATA / "results" / "paper_universe" / args.market.lower()
        seen: dict[pd.Timestamp, set[str]] = {}

        def universe_on(d: pd.Timestamp) -> set[str]:
            if d not in seen:
                seen[d] = members_on(history, d)
            return seen[d]
        print(f"{args.market} constituents: {len(names)} unique tickers in {path.name}")
    if args.dd_recovery != "reset_peak":
        out = out.with_name(f"{out.name}_{args.dd_recovery}")
    out.mkdir(parents=True, exist_ok=True)

    frames, failed, downloaded, cut = {}, [], 0, {}
    for k, t in enumerate(names):
        try:
            if equity_source == "kis":
                calls = kis.stats["calls"]
                frames[t], cut_at = load_kis_ohlcv(t, args.end, cache_dir, kis), None
                fetched = kis.stats["calls"] > calls
            else:
                frames[t], fetched, cut_at = load_frame(t, args.end, cache_dir)
            downloaded += fetched
            if cut_at:
                cut[t] = cut_at
        except Exception as exc:
            print(f"  skip {t}: {exc}")
            failed.append(t)
        if k % 50 == 49:
            print(f"  prices {k + 1}/{len(names)} (not cached today {downloaded}, failed {len(failed)})")
    index = (load_kis_ohlcv(index_symbol, args.end, cache_dir, kis) if index_source == "kis"
             else load_ohlcv(index_symbol, args.end, cache_dir=cache_dir))
    if kis:
        print(f"KIS: token issued {kis.stats['token_issued']} · reused {kis.stats['token_reused']} · "
              f"calls {kis.stats['calls']} · throttled {kis.stats['throttled']}")
    print(f"universe {len(frames)} tickers ({len(failed)} failed, {downloaded} not cached today), index {index_symbol} "
          f"{index['Date'].iloc[0].date()}..{index['Date'].iloc[-1].date()}")
    sectors = {t: sector_of(t) for t in frames}
    print(f"sectors resolved {sum(1 for v in sectors.values() if v)}/{len(frames)}")

    common = {"initial_cash": setup["initial_cash"], "commission": setup["commission"], "market": args.market}
    variants = [
        SimConfig(args.start, args.end, label="rules_off", **common),
        SimConfig(args.start, args.end, label="rules_on", risk=risk, **common),
        SimConfig(args.start, args.end, label="rules_on_regime", risk=risk, regime_filter=True, **common),
    ]
    results = {}
    for cfg in variants:
        print(f"\n== {cfg.label} ==")
        r = run_simulation(frames, index, cfg, sector_of=sector_of,
                           progress=lambda i, n, d: print(f"  {d} ({i}/{n})", end="\r"), universe_on=universe_on)
        print()
        m = r.metrics
        print(f"  return {m['total_return']:+.1%} (CAGR {m['cagr']:+.1%})  maxDD {m['max_drawdown']:+.1%} "
              f"{m['dd_peak']}→{m['dd_trough']} 회복 {m['dd_recovery'] or '미회복'}  worst day {m['worst_day']:+.1%} "
              f"{m['worst_day_date']}  vol {m['volatility']:.1%}  trades {len(r.trades)} {r.exits_by_reason()}")
        win = window_metrics(r.equity, windows)
        bench_windows = window_metrics(r.benchmark, windows)
        for name, w in win.items():
            print(f"    {name}: {w['return']:+.1%} / maxDD {w['max_drawdown']:+.1%}  ({market['benchmark_name_ko']} "
                  f"{bench_windows[name]['return']:+.1%} / {bench_windows[name]['max_drawdown']:+.1%})")
        r.equity.to_frame().join(r.benchmark).join(r.exposure.rename("exposure")).to_csv(out / f"{cfg.label}_equity.csv")
        results[cfg.label] = {
            "label": cfg.label, "risk": asdict(cfg.risk) if cfg.risk else None, "regime_filter": cfg.regime_filter,
            "screen": asdict(cfg.screen), "rebalances": r.rebalances, "metrics": m, "windows": win,
            "exits_by_reason": r.exits_by_reason(), "trades": len(r.trades),
            "win_rate": sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) if r.trades else None,
            "mean_exposure": float(r.exposure.mean()),
            "events": r.events, "equity_csv": f"{cfg.label}_equity.csv",
            "trades_sample": [asdict(t) for t in sorted(r.trades, key=lambda t: t.pnl_pct)[:10]],
        }
        bench = {"metrics": r.benchmark_metrics, "windows": bench_windows}

    summary = {
        "start": args.start, "end": args.end, "universe": list(frames),
        "universe_ko": {t: korean_name(t) for t in frames}, "index": index_symbol, "windows": windows,
        "benchmark": bench, "variants": results,
        "caveats": setup["caveats"],
    }
    if args.market != "KRX" or history is not None:  # the fixed KRX file keeps its shape for GET /api/paper
        summary.update({"market": args.market, "currency": market["currency"], "initial_cash": setup["initial_cash"],
                        "commission": setup["commission"], "failed": failed, "sectors": sectors})
    if kis:
        summary.update({"source": "kis", "failed": failed, "kis_stats": kis.stats})
    if history is not None:
        end = pd.Timestamp(args.end)
        # a name whose first bar comes well after it joined the index: no usable history while a member (Yahoo keeps
        # one stray row for some acquired names), so it could never pass the screen — survivorship like `failed`
        joined = history.groupby("ticker")["date"].min().clip(lower=pd.Timestamp(args.start))
        no_history = {t: f["Date"].iloc[0].strftime("%Y-%m-%d") for t, f in frames.items()
                      if f["Date"].iloc[0] > joined[t] + pd.Timedelta(days=7)}
        summary["universe_ko"] = {t: display_name(t) for t in frames}
        summary.update({
            "universe_mode": "constituents", "history_file": str(HISTORIES[args.market].relative_to(ROOT)),
            "unique_tickers": len(names), "loaded": len(frames), "not_cached_today": downloaded,
            "read_to_last_bar": cut, "no_history": no_history,
            "start_members": len(members_on(history, args.start)), "end_members": len(members_on(history, end)),
            # prices that stop more than a week before the end: the names the delisted rule can fire on
            "ended_early": {t: f["Date"].iloc[-1].strftime("%Y-%m-%d") for t, f in frames.items()
                            if f["Date"].iloc[-1] < end - pd.Timedelta(days=7) and t not in no_history},
        })
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {out / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
