"""5th measurement: does a rebound-speed measure make the screen's ranking better? (docs/11)

Same book, window and pipeline as ``paper_backtest.py --universe constituents`` variant C (rules on + regime filter):
the baseline screen against three measures (``screening.Rebound`` A · B · C) used two ways — added to the ranking
score (weight 0.25 · 0.5) or as a filter (bottom 30% out) — ten cells a market. Every cell is reported, on the whole
window and on its first 2/3 and last 1/3; no cell is picked (the param_grid rule). Plus two down-market views read off
the benchmark alone: the days the regime filter was off, and the 60 sessions after each −10% drawdown's low.

Prices are read from the OHLCV cache as it is (the files docs/09 measured on), not downloaded again: a name without a
cache file is downloaded and counted in ``downloaded``.

Run:  .venv/Scripts/python scripts/rebound_grid.py --market KRX|US [--start 2024-01-02] [--end 2026-11-12] [--jobs 1..4]
      (one market at a time)
Writes data/results/rebound/<market>.json and prints the tables.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from paper_backtest import DATA, HISTORIES, SETUPS, sector_of

from agent_trading_core import yahoo
from agent_trading_core.config import MARKETS
from agent_trading_core.errors import NoMarketDataError
from agent_trading_core.risk import RiskConfig, regime_on
from agent_trading_core.screening import Rebound
from agent_trading_core.simulate import SimConfig, SimResult, metrics, run_simulation
from agent_trading_core.universe import load_history, members_on, tickers

OUT = DATA / "results" / "rebound"
MODES = (("score25", "score", 0.25), ("score50", "score", 0.50), ("filter30", "filter", 0.25))
CELLS = [("baseline", None)] + [(f"{m}_{tag}", (m, mode, w)) for m in "ABC" for tag, mode, w in MODES]
REBOUND_DAYS = 60
MAX_JOBS = 4          # worker processes at most: about 0.2 GB each, the host is short of memory (2026-11-16)
DRAWDOWN = -0.10


def cell_screen(market: str, start: str, end: str, spec):
    """The variant-C screen of ``market`` (SimConfig's large-cap book, the US $5 floor), with the cell's measure."""
    base = SimConfig(start, end, market=market).screen
    if spec is None:
        return base
    metric, mode, weight = spec
    rules = base.rules
    if mode == "filter":
        k = rules.index("sector_quota")
        rules = (*rules[:k], "rebound_filter", *rules[k:])
    return replace(base, rules=rules, rebound=Rebound(metric=metric, benchmark=MARKETS[market]["benchmark"],
                                                      mode=mode, weight=weight))


def cached_frame(ticker: str, end: str, cache_dir: str) -> tuple[pd.DataFrame, bool]:
    """(frame, downloaded). The cache file is read whatever day it was written; a name whose prices stop before ``end``
    is read to its own last bar (paper_backtest.load_frame does the same for a file written today)."""
    path = os.path.join(cache_dir, f"{yahoo.safe_ticker_component(yahoo.normalize_symbol(ticker))}-YFin-data.csv")
    had = os.path.exists(path)
    try:
        return yahoo.load_ohlcv(ticker, end, cache_dir=cache_dir), not had
    except NoMarketDataError:
        last = pd.to_datetime(pd.read_csv(path, usecols=["Date"])["Date"].str[:10]).max()
        if pd.isna(last) or last >= pd.Timestamp(end):
            raise
        return yahoo.load_ohlcv(ticker, last.strftime("%Y-%m-%d"), cache_dir=cache_dir), not had


_DATA: dict = {}


def _load(market: str, end: str) -> dict:
    # ponytail: the cache is trusted whatever day it was written, so the grid reruns on the files docs/09 measured.
    # A fresh download is paper_backtest.py's job; rerun that first to measure on new prices.
    yahoo._cache_is_fresh = lambda *a, **k: True
    cache_dir = str(DATA / "cache")
    history = load_history(HISTORIES[market])
    frames, failed, downloaded = {}, [], 0
    for t in tickers(history):
        try:
            frames[t], got = cached_frame(t, end, cache_dir)
            downloaded += got
        except Exception as exc:
            failed.append(f"{t}: {type(exc).__name__}")
    index_symbol = MARKETS[market]["benchmark"]
    index = yahoo.load_ohlcv(index_symbol, end, cache_dir=cache_dir)
    return {"history": history, "frames": frames, "failed": failed, "downloaded": downloaded, "index": index,
            "index_symbol": index_symbol}


def _init(market: str, end: str) -> None:
    _DATA.update(_load(market, end))


def run_cell(market: str, start: str, end: str, name: str, spec) -> tuple[str, SimResult, float]:
    d = _DATA
    seen: dict = {}

    def universe_on(day):
        if day not in seen:
            seen[day] = members_on(d["history"], day)
        return seen[day]

    setup = SETUPS[market]
    cfg = SimConfig(start, end, label=name, initial_cash=setup["initial_cash"], commission=setup["commission"],
                    market=market, risk=RiskConfig(), regime_filter=True, screen=cell_screen(market, start, end, spec))
    # the benchmark rides in the frames for the screen's load (Rebound.benchmark); never a member, never a candidate
    frames = {**d["frames"], d["index_symbol"]: d["index"]}
    t0 = time.perf_counter()
    r = run_simulation(frames, d["index"], cfg, sector_of=sector_of, universe_on=universe_on)
    return name, r, time.perf_counter() - t0


# ---- what is measured of each run ----------------------------------------------------------------------------------

def split_dates(dates: pd.DatetimeIndex) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """(first, cut, last): the first 2/3 of the trading days run first..cut, the last 1/3 cut..last (cut in both,
    as the start of the second it is its base, not a return day)."""
    return dates[0], dates[int(len(dates) * 2 / 3)], dates[-1]


def segment(r: SimResult, a: pd.Timestamp, b: pd.Timestamp, *, first: bool = False) -> dict:
    """Metrics of the equity from ``a`` (its base) to ``b``. Buys and exits are counted on days after ``a`` up to
    ``b`` — the same days the returns are — and ``a`` itself too when it is the run's first day (``first``)."""
    e = r.equity.loc[a:b]
    m = metrics(e)
    years = max((b - a).days, 1) / 365.25
    def inside(day) -> bool:
        return (a <= pd.Timestamp(day) if first else a < pd.Timestamp(day)) and pd.Timestamp(day) <= b

    exits = [t for t in r.trades if inside(t.exit_date)]
    sold = sum(t.exit_price * t.quantity for t in exits)
    buys = sum(int(x.group(1)) for ev in r.events if ev["kind"] == "rebalance" and inside(ev["date"])
               for x in [re.search(r"신규 매수 (\d+)", ev["text"])] if x)
    bench = r.benchmark.loc[a:b]
    return {"start": a.strftime("%Y-%m-%d"), "end": b.strftime("%Y-%m-%d"),
            **{k: m[k] for k in ("total_return", "cagr", "max_drawdown", "sharpe", "volatility")},
            "turnover": sold / float(e.mean()) / years, "buys": buys, "exits": len(exits),
            "benchmark_return": float(bench.iloc[-1] / bench.iloc[0] - 1)}


def regime_off_days(index: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Days met with the regime filter off: the previous close was below its 200-day average (risk.regime_on)."""
    closes = index.set_index("Date")["Close"].sort_index()
    risk = RiskConfig()
    off = [d for prev, d in itertools.pairwise(dates) if not regime_on(closes.loc[:prev], risk)]
    return pd.DatetimeIndex(off)


def compounded(series: pd.Series, days: pd.DatetimeIndex) -> float:
    rets = series.pct_change().reindex(days).dropna()
    return float((1 + rets).prod() - 1)


def drawdown_episodes(bench: pd.Series, threshold: float = DRAWDOWN, after: int = REBOUND_DAYS) -> list[dict]:
    """Each fall of ``threshold`` or more from the running high (measured from the series' start): its low and the
    ``after`` sessions from it (cut at the end). An episode starts the first day the drawdown reaches the threshold
    and ends when the high is regained (or the series ends)."""
    dd = bench / bench.cummax() - 1
    out, i, n = [], 0, len(bench)
    while i < n:
        if dd.iloc[i] <= threshold:
            j = i
            while j < n and dd.iloc[j] < 0:
                j += 1
            seg = bench.iloc[i:j]
            low = seg.idxmin()
            k = bench.index.get_loc(low)
            out.append({"crossed": bench.index[i].strftime("%Y-%m-%d"), "low": low.strftime("%Y-%m-%d"),
                        "low_drawdown": float(dd.loc[low]),
                        "rebound_end": bench.index[min(k + after, n - 1)].strftime("%Y-%m-%d")})
            i = j
        else:
            i += 1
    return out


def window_return(series: pd.Series, a: str, b: str) -> float:
    s = series.loc[a:b]
    return float(s.iloc[-1] / s.iloc[0] - 1)


# ---- tables --------------------------------------------------------------------------------------------------------

def pct(x: float | None) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.1%}"


def table(cells: dict, part: str) -> list[str]:
    lines = ["| 칸 | 총수익 | CAGR | maxDD | 샤프 | 회전율 | 신규 매수 · 청산 | 기준 대비 총수익 | 기준 대비 샤프 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    base = cells["baseline"][part]
    for name, c in cells.items():
        s = c[part]
        diff = "" if name == "baseline" else pct(s["total_return"] - base["total_return"]).replace("%", "%p")
        dsh = "" if name == "baseline" else f"{s['sharpe'] - base['sharpe']:+.2f}"
        lines.append(f"| {name} | {pct(s['total_return'])} | {pct(s['cagr'])} | {pct(s['max_drawdown'])} | "
                     f"{s['sharpe']:.2f} | {s['turnover']:.2f} | {s['buys']} · {s['exits']} | {diff} | {dsh} |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True, choices=sorted(SETUPS))
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    parser.add_argument("--jobs", type=int, default=MAX_JOBS, help=f"worker processes, 1..{MAX_JOBS}")
    args = parser.parse_args()
    if not 1 <= args.jobs <= MAX_JOBS:
        parser.error(f"--jobs는 1~{MAX_JOBS}")
    OUT.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    data = _load(args.market, args.end)
    print(f"{args.market}: {len(data['frames'])} names loaded, {len(data['failed'])} failed, "
          f"{data['downloaded']} downloaded ({time.perf_counter() - t0:.0f}s)", flush=True)
    results: dict[str, tuple[SimResult, float]] = {}
    with ProcessPoolExecutor(max_workers=args.jobs, initializer=_init, initargs=(args.market, args.end)) as pool:
        futures = [pool.submit(run_cell, args.market, args.start, args.end, name, spec) for name, spec in CELLS]
        for f in futures:
            name, r, secs = f.result()
            results[name] = (r, secs)
            print(f"  {name}: return {pct(r.metrics['total_return'])} maxDD {pct(r.metrics['max_drawdown'])} "
                  f"trades {len(r.trades)} ({secs:.0f}s)", flush=True)
    elapsed = time.perf_counter() - t0

    some = results["baseline"][0]
    first, cut, last = split_dates(some.equity.index)
    off = regime_off_days(data["index"], some.equity.index)
    episodes = drawdown_episodes(some.benchmark)
    cells = {}
    for name, spec in CELLS:
        r, secs = results[name]
        cells[name] = {
            "spec": None if spec is None else dict(zip(("metric", "mode", "weight"), spec, strict=True)),
            "screen": asdict(cell_screen(args.market, args.start, args.end, spec)),
            "full": segment(r, first, last, first=True), "front": segment(r, first, cut, first=True),
            "back": segment(r, cut, last),
            "regime_off": {"days": len(off), "return": compounded(r.equity, off),
                           "front": compounded(r.equity, off[off <= cut]), "back": compounded(r.equity, off[off > cut])},
            "episodes": [{**ep, "return": window_return(r.equity, ep["low"], ep["rebound_end"])} for ep in episodes],
            "exits_by_reason": r.exits_by_reason(), "seconds": round(secs, 1),
        }
    bench_off = {"return": compounded(some.benchmark, off), "front": compounded(some.benchmark, off[off <= cut]),
                 "back": compounded(some.benchmark, off[off > cut])}
    bench_eps = [{**ep, "return": window_return(some.benchmark, ep["low"], ep["rebound_end"])} for ep in episodes]
    out = {"market": args.market, "start": args.start, "end": args.end, "cut": cut.strftime("%Y-%m-%d"),
           "index": data["index_symbol"], "loaded": len(data["frames"]), "failed": data["failed"],
           "downloaded": data["downloaded"], "elapsed_s": round(elapsed, 1),
           "benchmark": {"regime_off": {"days": len(off), **bench_off}, "episodes": bench_eps},
           "cells": cells,
           "caveats": ["고른 칸 없음: 10칸 × 두 구간 전부 보고(param_grid 원칙).",
                       "구간 지표는 전체 실행의 평가액을 잘라 그 구간 시작부터 잰다(구간별 재실행이 아님).",
                       "회전율 = 청산 매도 금액 합 ÷ 평균 평가액 ÷ 연수(끝에 남은 보유 제외).",
                       "가격은 docs/09가 잰 캐시 그대로(재다운로드 없음, downloaded가 0이 아니면 그만큼 새로 받음)."]}
    (OUT / f"{args.market.lower()}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                                     encoding="utf-8")

    print(f"\n{args.market} {args.start}..{args.end}, cut {out['cut']}, {elapsed:.0f}s")
    for part, label in (("full", "전체"), ("front", "앞 2/3"), ("back", "뒤 1/3")):
        print(f"\n### {label} ({cells['baseline'][part]['start']} ~ {cells['baseline'][part]['end']}, "
              f"벤치마크 {pct(cells['baseline'][part]['benchmark_return'])})")
        print("\n".join(table(cells, part)))
    print(f"\n### 국면 필터 꺼진 날 ({len(off)}일, 벤치마크 {pct(bench_off['return'])} · 앞 {pct(bench_off['front'])} · "
          f"뒤 {pct(bench_off['back'])})")
    print("| 칸 | 전체 | 앞 2/3 | 뒤 1/3 |\n|---|---:|---:|---:|")
    for name, c in cells.items():
        o = c["regime_off"]
        print(f"| {name} | {pct(o['return'])} | {pct(o['front'])} | {pct(o['back'])} |")
    print("\n### −10% 하락 뒤 저점부터 60거래일")
    print("| 칸 | " + " | ".join(f"{e['low']}~{e['rebound_end']} (저점 {pct(e['low_drawdown'])})" for e in bench_eps) + " |")
    print("|---|" + "---:|" * len(bench_eps))
    print("| 벤치마크 | " + " | ".join(pct(e["return"]) for e in bench_eps) + " |")
    for name, c in cells.items():
        print(f"| {name} | " + " | ".join(pct(e["return"]) for e in c["episodes"]) + " |")
    print(f"\nwrote {OUT / f'{args.market.lower()}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
