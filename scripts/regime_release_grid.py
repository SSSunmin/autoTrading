"""6th measurement: releases of the regime filter — may some new buys open below the 200-day line? (docs/12)

Same book, window and pipeline as ``rebound_grid.py``'s baseline (``paper_backtest.py --universe constituents``
variant C: rules on + regime filter, ``reset_peak``): five cells a market, fixed before the run — the filter as is
(baseline), no filter (off), and three releases of it (``risk.regime_released``: half weight, index above its 50-day
average, index +10% over its 60-day low). Every cell is reported on the whole window and on its first 2/3 and last 1/3,
with rebound_grid's two down-market views. Plus an index-only side table over the whole index cache: holding the index
under each rule (cash when the rule blocks buys), which is not the stock book.

Prices are read from the OHLCV cache as it is (rebound_grid.cached_frame), not downloaded again.

Run:  .venv/Scripts/python scripts/regime_release_grid.py --market KRX|US [--start 2024-01-02] [--end 2026-11-12]
      [--jobs 1..4]   (one market at a time)
Writes data/results/regime_release/<market>[_<end>].json and prints the tables.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import rebound_grid as rg
from paper_backtest import DATA, SETUPS, sector_of

from agent_trading_core.risk import RELEASE_HALF_SCALE, RiskConfig, regime_on, regime_released
from agent_trading_core.simulate import SimConfig, SimResult, metrics, run_simulation
from agent_trading_core.universe import members_on

OUT = DATA / "results" / "regime_release"
DEFAULT_END = "2026-11-12"                     # docs/09 · docs/11
# (name, regime_filter, regime_release) — fixed before the run
CELLS = [("baseline", True, None), ("off", False, None), ("half", True, "half"), ("ma50", True, "ma50"),
         ("low_rebound", True, "low_rebound")]


def run_cell(market: str, start: str, end: str, name: str, regime_filter: bool, release) -> tuple[str, SimResult, float]:
    d = rg._DATA
    seen: dict = {}

    def universe_on(day):
        if day not in seen:
            seen[day] = members_on(d["history"], day)
        return seen[day]

    setup = SETUPS[market]
    cfg = SimConfig(start, end, label=name, initial_cash=setup["initial_cash"], commission=setup["commission"],
                    market=market, risk=RiskConfig(), regime_filter=regime_filter, regime_release=release)
    t0 = time.perf_counter()
    r = run_simulation(d["frames"], d["index"], cfg, sector_of=sector_of, universe_on=universe_on)
    return name, r, time.perf_counter() - t0


def released(r: SimResult, a: pd.Timestamp, b: pd.Timestamp, *, first: bool = False) -> dict:
    """Rebalances inside (a, b] (``a`` too when ``first``) where the release let buys through, and the buys they
    opened (the rebalance event's "신규 매수 n" on those days)."""
    def inside(day) -> bool:
        day = pd.Timestamp(day)
        return (a <= day if first else a < day) and day <= b

    days = {e["date"] for e in r.events if e["kind"] == "override" and e["text"].startswith("국면 해제") and inside(e["date"])}
    buys = sum(int(x.group(1)) for e in r.events if e["kind"] == "rebalance" and e["date"] in days
               for x in [re.search(r"신규 매수 (\d+)", e["text"])] if x)
    return {"rebalances": len(days), "buys": buys}


# ---- index only: hold the index when a rule lets buys open, cash otherwise (not the stock book) -------------------

def index_weights(closes: pd.Series, regime_filter: bool, release) -> pd.Series:
    """The weight held on each day, decided at the previous close from closes up to it: 1 with the regime on (or no
    filter), RELEASE_HALF_SCALE / 1 when a release lets buys open below the line, else 0. The first day has none."""
    risk = RiskConfig()
    w = {}
    for k in range(1, len(closes)):
        seen = closes.iloc[:k]
        if not regime_filter or regime_on(seen, risk):
            w[closes.index[k]] = 1.0
        elif regime_released(seen, release):
            w[closes.index[k]] = RELEASE_HALF_SCALE if release == "half" else 1.0
        else:
            w[closes.index[k]] = 0.0
    return pd.Series(w)


def index_only(index: pd.DataFrame) -> dict:
    """Each cell's rule applied to the index alone, from the first day the 200-day average exists to the cache's end
    (no costs, close to close). Whole span and its first 2/3 · last 1/3."""
    closes = index.set_index("Date")["Close"].sort_index().dropna()
    closes = closes[~closes.index.duplicated(keep="last")]
    rets = closes.pct_change()
    span = closes.index[RiskConfig().regime_ma_days - 1:]       # the first close with a 200-day average decides day 1
    first, cut, last = rg.split_dates(span)
    out = {"start": first.strftime("%Y-%m-%d"), "cut": cut.strftime("%Y-%m-%d"), "end": last.strftime("%Y-%m-%d"),
           "cells": {}}
    for name, regime_filter, release in CELLS:
        w = index_weights(closes, regime_filter, release).reindex(span)
        daily = (w * rets.reindex(span)).fillna(0.0)
        daily.iloc[0] = 0.0                                       # the base day
        equity = (1 + daily).cumprod()
        cell = {}
        for part, (a, b) in (("full", (first, last)), ("front", (first, cut)), ("back", (cut, last))):
            m = metrics(equity.loc[a:b])
            held = w.loc[a:b].iloc[1:]                            # the return days; the base day is not one
            cell[part] = {**{k: m[k] for k in ("total_return", "cagr", "max_drawdown", "sharpe")},
                          "days_held": int((held > 0).sum()), "days": len(held), "mean_weight": float(held.mean())}
        out["cells"][name] = cell
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True, choices=sorted(SETUPS))
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--jobs", type=int, default=rg.MAX_JOBS, help=f"worker processes, 1..{rg.MAX_JOBS}")
    args = parser.parse_args()
    if not 1 <= args.jobs <= rg.MAX_JOBS:
        parser.error(f"--jobs는 1~{rg.MAX_JOBS}")
    OUT.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    data = rg._load(args.market, args.end)
    print(f"{args.market}: {len(data['frames'])} names loaded, {len(data['failed'])} failed, "
          f"{data['downloaded']} downloaded ({time.perf_counter() - t0:.0f}s)", flush=True)
    results: dict[str, tuple[SimResult, float]] = {}
    with ProcessPoolExecutor(max_workers=args.jobs, initializer=rg._init, initargs=(args.market, args.end)) as pool:
        futures = [pool.submit(run_cell, args.market, args.start, args.end, *cell) for cell in CELLS]
        for f in futures:
            name, r, secs = f.result()
            results[name] = (r, secs)
            print(f"  {name}: return {rg.pct(r.metrics['total_return'])} maxDD {rg.pct(r.metrics['max_drawdown'])} "
                  f"trades {len(r.trades)} ({secs:.0f}s)", flush=True)
    elapsed = time.perf_counter() - t0

    some = results["baseline"][0]
    first, cut, last = rg.split_dates(some.equity.index)
    off = rg.regime_off_days(data["index"], some.equity.index)
    episodes = rg.drawdown_episodes(some.benchmark)
    cells = {}
    for name, regime_filter, release in CELLS:
        r, secs = results[name]
        cells[name] = {
            "regime_filter": regime_filter, "regime_release": release,
            "full": rg.segment(r, first, last, first=True), "front": rg.segment(r, first, cut, first=True),
            "back": rg.segment(r, cut, last),
            "released": {"full": released(r, first, last, first=True), "front": released(r, first, cut, first=True),
                         "back": released(r, cut, last)},
            "regime_off": {"days": len(off), "return": rg.compounded(r.equity, off),
                           "front": rg.compounded(r.equity, off[off <= cut]),
                           "back": rg.compounded(r.equity, off[off > cut])},
            "episodes": [{**ep, "return": rg.window_return(r.equity, ep["low"], ep["rebound_end"])} for ep in episodes],
            "exits_by_reason": r.exits_by_reason(), "seconds": round(secs, 1),
        }
    bench_off = {"return": rg.compounded(some.benchmark, off), "front": rg.compounded(some.benchmark, off[off <= cut]),
                 "back": rg.compounded(some.benchmark, off[off > cut])}
    bench_eps = [{**ep, "return": rg.window_return(some.benchmark, ep["low"], ep["rebound_end"])} for ep in episodes]
    side = index_only(data["index"])
    out = {"market": args.market, "start": args.start, "end": args.end, "cut": cut.strftime("%Y-%m-%d"),
           "index": data["index_symbol"], "loaded": len(data["frames"]), "failed": data["failed"],
           "downloaded": data["downloaded"], "elapsed_s": round(elapsed, 1),
           "benchmark": {"regime_off": {"days": len(off), **bench_off}, "episodes": bench_eps},
           "cells": cells, "index_only": side,
           "caveats": ["고른 칸 없음: 5칸 × 두 구간 전부 보고(param_grid 원칙).",
                       "구간 지표는 전체 실행의 평가액을 잘라 그 구간 시작부터 잰다(구간별 재실행이 아님).",
                       "index_only는 종목 장부가 아니다: 규칙이 매수를 막는 날 지수를 팔고 현금(비용 없음, 종가→종가).",
                       "가격은 docs/09가 잰 캐시 그대로(재다운로드 없음, downloaded가 0이 아니면 그만큼 새로 받음)."]}
    tag = "" if args.end == DEFAULT_END else f"_{args.end}"
    path = OUT / f"{args.market.lower()}{tag}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n{args.market} {args.start}..{args.end}, cut {out['cut']}, {elapsed:.0f}s")
    for part, label in (("full", "전체"), ("front", "앞 2/3"), ("back", "뒤 1/3")):
        print(f"\n### {label} ({cells['baseline'][part]['start']} ~ {cells['baseline'][part]['end']}, "
              f"벤치마크 {rg.pct(cells['baseline'][part]['benchmark_return'])})")
        print("\n".join(rg.table(cells, part)))
        print("| 칸 | 해제된 리밸런스 · 그날 신규 매수 |\n|---|---:|")
        for name, c in cells.items():
            print(f"| {name} | {c['released'][part]['rebalances']} · {c['released'][part]['buys']} |")
    print(f"\n### 국면 필터 꺼진 날 ({len(off)}일, 벤치마크 {rg.pct(bench_off['return'])} · 앞 {rg.pct(bench_off['front'])} · "
          f"뒤 {rg.pct(bench_off['back'])})")
    print("| 칸 | 전체 | 앞 2/3 | 뒤 1/3 |\n|---|---:|---:|---:|")
    for name, c in cells.items():
        o = c["regime_off"]
        print(f"| {name} | {rg.pct(o['return'])} | {rg.pct(o['front'])} | {rg.pct(o['back'])} |")
    print("\n### −10% 하락 뒤 저점부터 60거래일")
    print("| 칸 | " + " | ".join(f"{e['crossed']} · 저점 {e['low']} ({rg.pct(e['low_drawdown'])}) → {e['rebound_end']}"
                                for e in bench_eps) + " |")
    print("|---|" + "---:|" * len(bench_eps))
    print("| 벤치마크 | " + " | ".join(rg.pct(e["return"]) for e in bench_eps) + " |")
    for name, c in cells.items():
        print(f"| {name} | " + " | ".join(rg.pct(e["return"]) for e in c["episodes"]) + " |")
    print(f"\n### 지수만(종목 장부 아님) {side['start']} ~ {side['end']}, 나눔 {side['cut']}")
    print("| 칸 | 구간 | 총수익 | CAGR | maxDD | 샤프 | 보유일 / 거래일 | 평균 비중 |\n|---|---|---:|---:|---:|---:|---:|---:|")
    for name, c in side["cells"].items():
        for part, label in (("full", "전체"), ("front", "앞 2/3"), ("back", "뒤 1/3")):
            s = c[part]
            print(f"| {name} | {label} | {rg.pct(s['total_return'])} | {rg.pct(s['cagr'])} | {rg.pct(s['max_drawdown'])} | "
                  f"{s['sharpe']:.2f} | {s['days_held']} / {s['days']} | {s['mean_weight']:.2f} |")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
