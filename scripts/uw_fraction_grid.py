"""7th measurement: how much of a position an Underweight sells — is the whole of it a "panic sell" in volatile markets?
(docs/13)

Same book, window and pipeline as ``regime_release_grid.py``'s baseline and off cells (``paper_backtest.py --universe
constituents``, ``reset_peak``), in its two series: rules + regime filter (``rules_on_regime``) and rules only
(``rules_on``). Four cells a series, fixed before the run — the whole position (1.0, docs/09's numbers), half (0.5, the
executor's default), half then the rest if the next rebalance is Underweight again (``twice_full``), half on volatile
days only (``volatile_half``; ``simulate.volatile_days``, the index's own past only).

Measured of each cell: the whole window and its first 2/3 · last 1/3; the volatile days (the index's 20-day volatility
known at the day's open — the returns up to the day before — in the window's top third: the sum of the book's daily
returns on them); the 2026-06-01 ~ end stretch; and the regret of every Underweight sale — the name's close 5 and 20
sessions after the fill against the open it was sold at (after the fact: never read by a cell's rule).

Prices are read from the OHLCV cache as it is (rebound_grid.cached_frame), not downloaded again.

Run:  .venv/Scripts/python scripts/uw_fraction_grid.py --market KRX|US [--start 2024-01-02] [--end 2026-11-12]
      [--jobs 1..4]   (one market at a time)
Writes data/results/uw_fraction/<market>[_<end>].json and prints the tables.
"""

from __future__ import annotations

import argparse
import json
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

from agent_trading_core.risk import RiskConfig
from agent_trading_core.simulate import (
    VOLATILE_DAYS,
    VOLATILE_TOP,
    SimConfig,
    SimResult,
    run_simulation,
    sell_proceeds,
    window_metrics,
)
from agent_trading_core.universe import members_on

OUT = DATA / "results" / "uw_fraction"
DEFAULT_END = "2026-11-12"                     # docs/09 · 11 · 12
DROP_START = "2026-06-01"                      # the 2026-06 ~ 11 fall: from this day to the run's end
AFTER = (5, 20)                                # regret: sessions after the sale
# (name, underweight_sell_fraction, underweight_rule) — fixed before the run; each under both series
RULES = [("full", 1.0, None), ("half", 0.5, None), ("twice_full", 0.5, "twice_full"),
         ("volatile_half", 0.5, "volatile_half")]
SERIES = [("rules_on_regime", True), ("rules_on", False)]        # docs/09's variant names, regime filter
CELLS = [(f"{s}/{n}", regime, frac, rule) for s, regime in SERIES for n, frac, rule in RULES]


def run_cell(market: str, start: str, end: str, name: str, regime_filter: bool, fraction: float,
             rule) -> tuple[str, SimResult, float]:
    d = rg._DATA
    seen: dict = {}

    def universe_on(day):
        if day not in seen:
            seen[day] = members_on(d["history"], day)
        return seen[day]

    setup = SETUPS[market]
    cfg = SimConfig(start, end, label=name, initial_cash=setup["initial_cash"], commission=setup["commission"],
                    market=market, risk=RiskConfig(), regime_filter=regime_filter,
                    underweight_sell_fraction=fraction, underweight_rule=rule)
    t0 = time.perf_counter()
    r = run_simulation(d["frames"], d["index"], cfg, sector_of=sector_of, universe_on=universe_on)
    return name, r, time.perf_counter() - t0


# ---- what is measured of each run ----------------------------------------------------------------------------------

def index_closes(index: pd.DataFrame) -> pd.Series:
    s = index.set_index("Date")["Close"].sort_index()
    return s[~s.index.duplicated(keep="last")]


def eval_volatile(index: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
    """Per run day: the index's 20-day volatility known at its open (returns up to the day before, as
    ``simulate.volatile_days``) is in the top third of the window's values. After the fact — the window's whole
    distribution — so only for measuring, never for a cell's rule."""
    s = index_closes(index).dropna()
    vol = s.pct_change().rolling(VOLATILE_DAYS).std().shift(1).reindex(dates)
    return vol >= vol.quantile(1 - VOLATILE_TOP)


def day_sums(equity: pd.Series, flags: pd.Series) -> dict:
    """Sums of the daily returns on the flagged days and on the rest (the first day has no return)."""
    rets = equity.pct_change().iloc[1:]
    f = flags.reindex(rets.index).fillna(False).astype(bool)
    return {"volatile": float(rets[f].sum()), "calm": float(rets[~f].sum()), "volatile_days": int(f.sum()),
            "calm_days": int((~f).sum())}


def underweight_sales(r: SimResult) -> set[tuple[str, str]]:
    """(fill date, ticker) of every sale an Underweight rating ordered — whole or partial (the exit event's note)."""
    return {(e["date"], e["text"].split(" ", 1)[0]) for e in r.events
            if e["kind"] == "exit" and "등급 Underweight" in e["text"]}


def regret(r: SimResult, frames: dict, slippage: float, flags: pd.Series) -> list[dict]:
    """Every Underweight sale: the open it sold at (the fill before slippage), the name's close ``AFTER`` sessions on
    (None past the run's end or the name's last bar), and whether its decision day (the session before the fill) was
    volatile (``eval_volatile``)."""
    dates = r.equity.index
    pos = {d: k for k, d in enumerate(dates)}
    marks = underweight_sales(r)
    closes: dict[str, pd.Series] = {}
    out = []
    for t in r.trades:
        if (t.exit_date, t.ticker) not in marks:
            continue
        if t.ticker not in closes:
            f = frames[t.ticker].set_index("Date")["Close"]
            closes[t.ticker] = f[~f.index.duplicated(keep="last")].reindex(dates)
        k = pos[pd.Timestamp(t.exit_date)]
        sold = t.exit_price / (1 - slippage)
        row = {"ticker": t.ticker, "date": t.exit_date, "quantity": t.quantity, "full": t.full, "sold": sold,
               "volatile": bool(flags.iloc[k - 1])}
        for n in AFTER:
            later = float(closes[t.ticker].iloc[k + n]) if k + n < len(dates) else float("nan")
            row[f"after_{n}"] = None if pd.isna(later) else later
        out.append(row)
    return out


def regret_table(rows: list[dict]) -> dict:
    """Per group (all, volatile, calm) and horizon: sales, the share whose price rose after, the mean change, and the
    money the sold shares made or lost after (quantity × change, a gain = selling cost that much)."""
    out = {}
    for group, keep in (("all", lambda x: True), ("volatile", lambda x: x["volatile"]),
                        ("calm", lambda x: not x["volatile"])):
        g = [x for x in rows if keep(x)]
        out[group] = {"sales": len(g)}
        for n in AFTER:
            seen = [x for x in g if x[f"after_{n}"] is not None]
            ch = [x[f"after_{n}"] / x["sold"] - 1 for x in seen]
            out[group][str(n)] = {"measured": len(seen), "rose": sum(c > 0 for c in ch) / len(ch) if ch else None,
                                  "mean_change": sum(ch) / len(ch) if ch else None,
                                  "money": sum(x["quantity"] * (x[f"after_{n}"] - x["sold"]) for x in seen)}
    return out


def cause(r: SimResult, frames: dict, cfg_like: SimConfig) -> dict:
    """What the partial sale changes: Underweight sales and partial ones, positions an Underweight touched, the most
    sales of one position, sessions from its first Underweight sale to its close, the money the shares kept after a
    first partial sale made against selling them then (closed positions only) and the reason they finally went, the
    rebalances a StoplossGuard lock held new buys back on, the sell-side costs of every sale (slippage, commission,
    tax, fees) and the mean invested fraction."""
    dates = list(r.equity.index)
    marks = underweight_sales(r)
    by_pos: dict[tuple[str, str], list] = {}
    for t in r.trades:
        by_pos.setdefault((t.ticker, t.entry_date), []).append(t)
    touched, spans, kept, still_open, most, tail_exits = 0, [], 0.0, 0, 0, {}
    for trades in by_pos.values():
        uw = [k for k, t in enumerate(trades) if (t.exit_date, t.ticker) in marks]
        if not uw:
            continue
        touched += 1
        most = max(most, sum(1 for t in trades if (t.exit_date, t.ticker) in marks))
        first = trades[uw[0]]
        if not trades[-1].full:
            still_open += 1
            continue
        spans.append(dates.index(pd.Timestamp(trades[-1].exit_date)) - dates.index(pd.Timestamp(first.exit_date)))
        if not first.full:
            p0 = first.exit_price / (1 - cfg_like.slippage)
            kept += sum(t.quantity * (t.exit_price / (1 - cfg_like.slippage) - p0) for t in trades[uw[0] + 1:])
            why = "underweight" if (trades[-1].exit_date, trades[-1].ticker) in marks else trades[-1].reason
            tail_exits[why] = tail_exits.get(why, 0) + 1
    costs = 0.0
    for t in r.trades:
        raw = t.exit_price / (1 - cfg_like.slippage)
        costs += t.quantity * raw - sell_proceeds(t.quantity, t.exit_price, cfg_like)
    return {"underweight_sales": len(marks), "partial_sales": sum(1 for t in r.trades if not t.full),
            "positions": touched, "most_sales_of_one": most, "still_open_at_end": still_open,
            "mean_sessions_to_close": sum(spans) / len(spans) if spans else None,
            "kept_shares_money": kept, "tail_exits": tail_exits,
            "stoploss_guard_rebalances": len({e["date"] for e in r.events
                                              if e["kind"] == "lock" and e["text"].startswith("StoplossGuard")}),
            "sell_costs": costs, "mean_exposure": float(r.exposure.mean())}


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

    some = results[CELLS[0][0]][0]
    first, cut, last = rg.split_dates(some.equity.index)
    flags = eval_volatile(data["index"], some.equity.index)
    setup = SETUPS[args.market]
    cfg_like = SimConfig(args.start, args.end, commission=setup["commission"], market=args.market)
    drop = {"drop": (DROP_START, last.strftime("%Y-%m-%d"))}
    docs09 = json.loads((DATA / "results" / "paper_universe" / args.market.lower() / "summary.json")
                        .read_text(encoding="utf-8"))["variants"] if args.end == DEFAULT_END else None
    cells = {}
    for name, regime_filter, fraction, rule in CELLS:
        r, secs = results[name]
        rows = regret(r, data["frames"], cfg_like.slippage, flags)
        cell = {
            "regime_filter": regime_filter, "underweight_sell_fraction": fraction, "underweight_rule": rule,
            "full": rg.segment(r, first, last, first=True), "front": rg.segment(r, first, cut, first=True),
            "back": rg.segment(r, cut, last),
            "trades": len(r.trades), "days": day_sums(r.equity, flags),
            "drop": window_metrics(r.equity, drop)["drop"], "regret": regret_table(rows),
            "cause": cause(r, data["frames"], cfg_like), "exits_by_reason": r.exits_by_reason(),
            "sales": rows, "seconds": round(secs, 1),
        }
        series = name.split("/")[0]
        if docs09 is not None and fraction == 1.0:
            ref = docs09[series]["metrics"]
            cell["docs09_diff"] = {k: r.metrics[k] - ref[k] for k in ("total_return", "cagr", "max_drawdown", "sharpe",
                                                                       "volatility")}
            cell["docs09_trades"] = (len(r.trades), docs09[series]["trades"])
        cells[name] = cell
    out = {"market": args.market, "start": args.start, "end": args.end, "cut": cut.strftime("%Y-%m-%d"),
           "index": data["index_symbol"], "loaded": len(data["frames"]), "failed": data["failed"],
           "downloaded": data["downloaded"], "elapsed_s": round(elapsed, 1),
           "benchmark": {"days": day_sums(some.benchmark, flags), "drop": window_metrics(some.benchmark, drop)["drop"]},
           "cells": cells,
           "caveats": ["고른 칸 없음: 2계열 × 4칸 × 두 구간 전부 보고(param_grid 원칙).",
                       "구간 지표는 전체 실행의 평가액을 잘라 그 구간 시작부터 잰다(구간별 재실행이 아님).",
                       "변동장 날(평가용)은 기간 전체 분포의 상위 1/3 — 사후 정의. 칸 규칙(volatile_half)은 지수 자기 과거만 본다.",
                       "후회 매도는 사후 평가: 판 시가 대비 5·20거래일 뒤 종가. 칸 규칙에 쓰지 않는다.",
                       "가격은 docs/09가 잰 캐시 그대로(재다운로드 없음, downloaded가 0이 아니면 그만큼 새로 받음)."]}
    tag = "" if args.end == DEFAULT_END else f"_{args.end}"
    path = OUT / f"{args.market.lower()}{tag}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n{args.market} {args.start}..{args.end}, cut {out['cut']}, {elapsed:.0f}s")
    for series, _ in SERIES:
        sub = {n.split("/")[1]: c for n, c in cells.items() if n.startswith(series + "/")}
        sub = {"baseline" if n == "full" else n: c for n, c in sub.items()}          # rg.table's reference cell
        for part, label in (("full", "전체"), ("front", "앞 2/3"), ("back", "뒤 1/3")):
            print(f"\n### {series} · {label} (벤치마크 {rg.pct(sub['baseline'][part]['benchmark_return'])}) — baseline = full")
            print("\n".join(rg.table(sub, part)))
    b = out["benchmark"]
    print(f"\n### 변동장 날 {b['days']['volatile_days']}일 · 나머지 {b['days']['calm_days']}일 (일별 수익 합), "
          f"{DROP_START} ~ {last:%Y-%m-%d}")
    print("| 칸 | 거래 수 | 변동장 날 합 | 나머지 날 합 | 하락 구간 총수익 | 하락 구간 maxDD |\n|---|---:|---:|---:|---:|---:|")
    print(f"| 벤치마크 | | {rg.pct(b['days']['volatile'])} | {rg.pct(b['days']['calm'])} | {rg.pct(b['drop']['return'])} | "
          f"{rg.pct(b['drop']['max_drawdown'])} |")
    for name, c in cells.items():
        print(f"| {name} | {c['trades']} | {rg.pct(c['days']['volatile'])} | {rg.pct(c['days']['calm'])} | "
              f"{rg.pct(c['drop']['return'])} | {rg.pct(c['drop']['max_drawdown'])} |")
    print("\n### 후회 매도 (Underweight 매도마다 판 시가 대비 n거래일 뒤 종가)")
    print("| 칸 | 날 | 매도 | 5일 뒤 오름 · 평균 · 금액 | 20일 뒤 오름 · 평균 · 금액 |\n|---|---|---:|---:|---:|")
    for name, c in cells.items():
        for group, label in (("all", "전체"), ("volatile", "변동장"), ("calm", "나머지")):
            g = c["regret"][group]
            cols = []
            for n in AFTER:
                h = g[str(n)]
                cols.append("—" if h["rose"] is None else
                            f"{h['rose']:.0%} · {rg.pct(h['mean_change'])} · {h['money']:,.0f} ({h['measured']})")
            print(f"| {name} | {label} | {g['sales']} | " + " | ".join(cols) + " |")
    print("\n### 원인")
    print("| 칸 | UW 매도 · 부분 | 종목 | 한 종목 최다 | 첫 UW 매도→청산 평균 거래일 | 끝까지 보유 | 남긴 몫 손익 | 남긴 몫 청산 사유 | "
          "StoplossGuard 리밸런스 | 매도 비용 | 평균 투자 비중 |")
    print("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|")
    for name, c in cells.items():
        k = c["cause"]
        span = "—" if k["mean_sessions_to_close"] is None else f"{k['mean_sessions_to_close']:.1f}"
        print(f"| {name} | {k['underweight_sales']} · {k['partial_sales']} | {k['positions']} | {k['most_sales_of_one']} | "
              f"{span} | {k['still_open_at_end']} | {k['kept_shares_money']:,.0f} | "
              f"{' · '.join(f'{w} {n}' for w, n in sorted(k['tail_exits'].items())) or '—'} | "
              f"{k['stoploss_guard_rebalances']} | {k['sell_costs']:,.0f} | "
              f"{k['mean_exposure']:.2f} |")
    for name, c in cells.items():
        if "docs09_diff" in c:
            print(f"docs/09 {name}: diff {c['docs09_diff']} trades {c['docs09_trades']}")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
