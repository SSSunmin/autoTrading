"""Hard-rule sensitivity grid: are the draft values (docs/01) luckily good or bad? (docs/05 §6)

Same book, window and pipeline as ``paper_backtest.py`` variant C (rules on + regime filter), with three
``RiskConfig`` fields swept and everything else at its default:

- stop-loss          −5% / −8% / −10%
- max position       5% / 10% / 15% of equity
- trailing width     3% / 5% / 7% below the high-water mark (default 5% ± 2%p; activation stays +10%)

Every cell is reported. No value is picked: choosing the best cell of a 27-cell grid on one 3-year path
is overfitting, which is the reason the grid exists.

Run:  .venv/Scripts/python scripts/param_grid.py [--start 2024-01-02] [--end 2026-11-12]
Writes data/results/paper/grid.json and grid.csv (summary.json is left alone).
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from paper_backtest import DATA, INDEX, OUT, UNIVERSE, sector_of

from agent_trading_core.config import PAPER_TRADING
from agent_trading_core.risk import EPS, RiskConfig
from agent_trading_core.simulate import SimConfig, SimResult, run_simulation
from agent_trading_core.yahoo import load_ohlcv

STOP_LOSS = (-0.05, -0.08, -0.10)
MAX_POSITION = (0.05, 0.10, 0.15)
TRAILING = (0.03, 0.05, 0.07)


def grid_configs(stops=STOP_LOSS, positions=MAX_POSITION, trails=TRAILING,
                 base: RiskConfig | None = None) -> list[RiskConfig]:
    """One ``RiskConfig`` per cell, stop-loss outermost; every other field stays at ``base``."""
    base = base or RiskConfig()
    return [replace(base, stop_loss_pct=s, max_position_pct=p, trailing_pct=t)
            for s, p, t in itertools.product(stops, positions, trails)]


def _guard_count(r: SimResult, prefix: str) -> int:
    return sum(1 for e in r.events if e["kind"] == "guard" and e["text"].startswith(prefix))


def peak_reset_count(events: list[dict]) -> int:
    """Re-entries after a −20% stop: a ``peak_reset`` event (reset_peak) or the half_size rule's 재가동 guard line."""
    return sum(1 for e in events if e["kind"] == "peak_reset" or (e["kind"] == "guard" and e["text"].startswith("재가동")))


def paper_checks(max_drawdown: float, daily_breaches: int, stop_outs: int, days: int) -> dict[str, bool | None]:
    """``config.PAPER_TRADING`` applied literally to the whole backtest. ``None``: not measurable in a rules-only
    backtest (no LLM decisions to be REVIEW, every order has a decision record by construction)."""
    c = PAPER_TRADING
    return {
        "min_weeks": days >= c["min_weeks"] * 5,
        "max_drawdown": max_drawdown >= c["max_drawdown"] - EPS,
        "max_daily_loss_breaches": daily_breaches <= c["max_daily_loss_breaches"],
        "max_stop_outs": stop_outs <= c["max_stop_outs"],
        "max_review_rate": None,
        "max_unexplained_orders": None,
    }


def cell_row(risk: RiskConfig, r: SimResult) -> dict:
    m = r.metrics
    worst = min(r.trades, key=lambda t: t.pnl_pct) if r.trades else None
    breaches = _guard_count(r, "일일 손실 한도")
    stop_outs = _guard_count(r, "MaxDrawdown")
    peak_resets = peak_reset_count(r.events)
    checks = paper_checks(m["max_drawdown"], breaches, stop_outs, m["days"])
    return {
        "stop_loss_pct": risk.stop_loss_pct, "max_position_pct": risk.max_position_pct, "trailing_pct": risk.trailing_pct,
        "max_drawdown": m["max_drawdown"], "total_return": m["total_return"], "volatility": m["volatility"],
        "trades": len(r.trades),
        "worst_trade_pct": worst.pnl_pct if worst else None,
        "worst_trade": f"{worst.ticker} {worst.exit_date} {worst.reason}" if worst else None,
        "stop_outs": stop_outs, "peak_resets": peak_resets, "stopped_out": stop_outs > 0, "daily_loss_breaches": breaches,
        "paper_checks": checks,
        "paper_passed": sum(1 for v in checks.values() if v),
        "paper_measurable": sum(1 for v in checks.values() if v is not None),
        "exits_by_reason": r.exits_by_reason(),
    }


def run_grid(frames, index, base: SimConfig, configs: list[RiskConfig], *, sector_of=None,
             progress=None) -> list[dict]:
    """``base`` carries window, costs and screen; each cell swaps in its ``RiskConfig`` with the regime filter on."""
    rows = []
    for k, risk in enumerate(configs):
        cfg = replace(base, label=f"grid_{k:02d}", risk=risk, regime_filter=True)
        rows.append(cell_row(risk, run_simulation(frames, index, cfg, sector_of=sector_of)))
        if progress:
            progress(k + 1, len(configs), rows[-1])
    return rows


CSV_COLUMNS = ["stop_loss_pct", "max_position_pct", "trailing_pct", "max_drawdown", "total_return", "volatility",
               "trades", "worst_trade_pct", "worst_trade", "stopped_out", "stop_outs", "peak_resets", "daily_loss_breaches",
               "paper_passed", "paper_measurable"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    parser.add_argument("--dd-recovery", default="reset_peak", choices=["reset_peak", "half_size"],
                        help="RiskConfig.dd_stop_recovery; half_size (the rule before 2026-11-13) writes grid_half_size.*")
    args = parser.parse_args()
    base_risk = replace(RiskConfig(), dd_stop_recovery=args.dd_recovery)
    stem = "grid" if args.dd_recovery == "reset_peak" else f"grid_{args.dd_recovery}"
    OUT.mkdir(parents=True, exist_ok=True)
    cache_dir = str(DATA / "cache")

    frames = {}
    for t in UNIVERSE:
        try:
            frames[t] = load_ohlcv(t, args.end, cache_dir=cache_dir)
        except Exception as exc:
            print(f"  skip {t}: {exc}")
    index = load_ohlcv(INDEX, args.end, cache_dir=cache_dir)
    sectors = {t: sector_of(t) for t in frames}
    # The OHLCV cache is only trusted on the day it was written (yahoo._cache_is_fresh); record what was read so a
    # rerun on another day that re-downloads can be told apart.
    inputs = {t: {"rows": len(f), "last": f["Date"].iloc[-1].strftime("%Y-%m-%d"),
                  "close_sum": round(float(f["Close"].sum()), 2)} for t, f in {**frames, INDEX: index}.items()}
    print(f"universe {len(frames)} tickers, sectors resolved {sum(1 for s in sectors.values() if s)}/{len(frames)}")

    configs = grid_configs(base=base_risk)
    t0 = time.perf_counter()
    rows = run_grid(frames, index, SimConfig(args.start, args.end), configs, sector_of=sector_of,
                    progress=lambda k, n, row: print(
                        f"  [{k:2d}/{n}] 손절 {row['stop_loss_pct']:+.0%} 비중 {row['max_position_pct']:.0%} "
                        f"트레일링 {row['trailing_pct']:.0%}: maxDD {row['max_drawdown']:+.1%} 수익 {row['total_return']:+.1%} "
                        f"거래 {row['trades']} 정지 {row['stop_outs']}  ({time.perf_counter() - t0:.0f}s)"))
    elapsed = time.perf_counter() - t0
    print(f"{len(rows)} cells in {elapsed:.0f}s")

    out = {
        "start": args.start, "end": args.end, "universe": list(frames), "sectors": sectors, "inputs": inputs, "index": INDEX,
        "variant": "rules_on_regime (docs/05 C) with three RiskConfig fields swept",
        "axes": {"stop_loss_pct": list(STOP_LOSS), "max_position_pct": list(MAX_POSITION), "trailing_pct": list(TRAILING)},
        "base_risk": asdict(base_risk), "paper_criteria": PAPER_TRADING, "elapsed_s": round(elapsed, 1),
        "cells": rows,
        "caveats": [
            "고른 값 없음: 격자 전체를 보고한다. 한 경로(2024-01~2026-11)에서 27칸 중 최선을 고르면 과최적화.",
            "모의투자 기준은 8주용인데 여기서는 3년 전체에 문자 그대로 적용했다. 일일 손실 위반 ≤2회는 사실상 전 칸 미충족.",
            "max_review_rate · max_unexplained_orders는 규칙 전용 백테스트에서 잴 수 없어 null(통과 개수에서 제외).",
            "섹터는 Yahoo 프로필(현재값) 조회. paper_backtest.py와 같은 경로.",
            "OHLCV 캐시는 쓴 날에만 유효(yahoo._cache_is_fresh). 다른 날 재실행하면 재다운로드되어 값이 달라질 수 있다 — inputs로 대조.",
        ],
    }
    (OUT / f"{stem}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    with (OUT / f"{stem}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT / f'{stem}.json'}, {OUT / f'{stem}.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
