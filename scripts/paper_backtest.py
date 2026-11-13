"""2nd measurement: does the hard-rule layer bound losses? (docs/03 §4 → docs/05)

Runs the same screen → quartile rating → target pipeline over a KRX large-cap book, 2024-01 → 2026-11,
three ways: rules off, rules on, rules on + 200-day regime filter. The first metric is the maximum
drawdown and how long it took to recover, inside the crash episodes especially; return comes second.

Run:  .venv/Scripts/python scripts/paper_backtest.py [--start 2024-01-02] [--end 2026-11-12]
Writes data/results/paper/summary.json and <variant>_equity.csv.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.identity import resolve_instrument_identity
from agent_trading_core.krx_names import korean_name
from agent_trading_core.risk import RiskConfig
from agent_trading_core.simulate import SimConfig, run_simulation, window_metrics
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

DATA = ROOT / "data"
OUT = DATA / "results" / "paper"


@cache
def sector_of(ticker: str) -> str | None:
    try:
        return resolve_instrument_identity(ticker).get("sector")
    except Exception as exc:  # identity is fail-open; the quota rule then treats the name as unsectored
        logger.warning("%s: sector lookup failed: %s", ticker, exc)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    cache_dir = str(DATA / "cache")

    frames = {}
    for t in UNIVERSE:
        try:
            frames[t] = load_ohlcv(t, args.end, cache_dir=cache_dir)
        except Exception as exc:
            print(f"  skip {t}: {exc}")
    index = load_ohlcv(INDEX, args.end, cache_dir=cache_dir)
    print(f"universe {len(frames)} tickers, index {INDEX} {index['Date'].iloc[0].date()}..{index['Date'].iloc[-1].date()}")
    for t in frames:
        sector_of(t)

    variants = [
        SimConfig(args.start, args.end, label="rules_off"),
        SimConfig(args.start, args.end, label="rules_on", risk=RiskConfig()),
        SimConfig(args.start, args.end, label="rules_on_regime", risk=RiskConfig(), regime_filter=True),
    ]
    results = {}
    for cfg in variants:
        print(f"\n== {cfg.label} ==")
        r = run_simulation(frames, index, cfg, sector_of=sector_of,
                           progress=lambda i, n, d: print(f"  {d} ({i}/{n})", end="\r"))
        print()
        m = r.metrics
        print(f"  return {m['total_return']:+.1%} (CAGR {m['cagr']:+.1%})  maxDD {m['max_drawdown']:+.1%} "
              f"{m['dd_peak']}→{m['dd_trough']} 회복 {m['dd_recovery'] or '미회복'}  worst day {m['worst_day']:+.1%} "
              f"{m['worst_day_date']}  vol {m['volatility']:.1%}  trades {len(r.trades)} {r.exits_by_reason()}")
        windows = window_metrics(r.equity, WINDOWS)
        bench_windows = window_metrics(r.benchmark, WINDOWS)
        for name, w in windows.items():
            print(f"    {name}: {w['return']:+.1%} / maxDD {w['max_drawdown']:+.1%}  (KOSPI {bench_windows[name]['return']:+.1%} / "
                  f"{bench_windows[name]['max_drawdown']:+.1%})")
        r.equity.to_frame().join(r.benchmark).join(r.exposure.rename("exposure")).to_csv(OUT / f"{cfg.label}_equity.csv")
        results[cfg.label] = {
            "label": cfg.label, "risk": asdict(cfg.risk) if cfg.risk else None, "regime_filter": cfg.regime_filter,
            "screen": asdict(cfg.screen), "rebalances": r.rebalances, "metrics": m, "windows": windows,
            "exits_by_reason": r.exits_by_reason(), "trades": len(r.trades),
            "win_rate": sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) if r.trades else None,
            "mean_exposure": float(r.exposure.mean()),
            "events": r.events, "equity_csv": f"{cfg.label}_equity.csv",
            "trades_sample": [asdict(t) for t in sorted(r.trades, key=lambda t: t.pnl_pct)[:10]],
        }
        bench = {"metrics": r.benchmark_metrics, "windows": bench_windows}

    summary = {
        "start": args.start, "end": args.end, "universe": list(frames),
        "universe_ko": {t: korean_name(t) for t in frames}, "index": INDEX, "windows": WINDOWS,
        "benchmark": bench, "variants": results,
        "caveats": [
            "유니버스는 현재 KOSPI 대형주 40종목 고정: 생존 편향(2024년 이후 지수에서 빠진 종목 없음).",
            "결정은 종가, 체결은 다음날 시가. 가격제한폭·VI·호가단위는 반영하지 않음(갭은 시가로 그대로 반영).",
            "규칙 전용(LLM 없음). 에이전트가 들어오면 같은 격자에서 다시 측정.",
            "거래비용: 수수료 0.015%/편도, 매도 거래세 0.20%, 슬리피지 0.05%/편도.",
        ],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {OUT / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
