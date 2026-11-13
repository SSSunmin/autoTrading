"""Rule-only baseline on KRX large caps: screen → momentum rank → rating, scored by realized alpha.

No LLM anywhere. This is the bar an agent has to beat (docs/01 §11-4 step 1). It
also writes the screening and decision audit records the dashboard mock-up reads.

Run:  .venv/Scripts/python scripts/baseline_krx.py [--dates 8] [--start 2026-06-05]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.audit import AuditLog, DecisionRecord, EvidenceItem, stage_counts
from agent_trading_core.backtest import Decision, iter_grid, run_backtest, summarize
from agent_trading_core.config import default_config
from agent_trading_core.identity import resolve_instrument_identity
from agent_trading_core.krx_names import RULE_KO, korean_name
from agent_trading_core.screening import ScreenConfig, screen

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# Twenty KOSPI200 large caps across sectors. A fixed list keeps the run repeatable;
# a real universe comes from the index constituents with their membership dates.
UNIVERSE = [
    "005930.KS", "000660.KS", "373220.KS", "207940.KS", "005380.KS",
    "000270.KS", "068270.KS", "105560.KS", "055550.KS", "035420.KS",
    "012330.KS", "028260.KS", "006400.KS", "051910.KS", "003670.KS",
    "035720.KS", "032830.KS", "066570.KS", "015760.KS", "034730.KS",
]

DATA = ROOT / "data"
# volatility_min lowered from the 0.15 draft: KOSPI large caps often run under 15%
# annualized over 20 days, and the first pass kept only 6-9 of 15 (docs/03).
CFG = ScreenConfig(dollar_volume_top_n=15, top_k=5, sector_max=3, volatility_min=0.10)


class RuleReflector:
    """Settlement needs a reflection; the baseline has none to offer."""

    def reflect_on_final_decision(self, final_decision, raw_return, alpha_return, benchmark_name="SPY", holding_days=5):
        return (f"규칙 전용 베이스라인(LLM 없음). {holding_days}거래일 {benchmark_name} 대비 알파 {alpha_return:+.1%}, "
                f"실현수익 {raw_return:+.1%}.")


@cache
def sector_of(ticker: str) -> str | None:
    return resolve_instrument_identity(ticker).get("sector")


@cache
def name_of(ticker: str) -> str | None:
    return resolve_instrument_identity(ticker).get("company_name")


def rating_for(rank: int | None, n_ranked: int) -> str:
    """Quartiles of the momentum ranking: top quarter Buy, next Overweight, bottom quarter Sell,
    next Underweight, the middle Hold. Unranked (failed an absolute filter): Hold, no view."""
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-06-05")
    parser.add_argument("--dates", type=int, default=8)
    parser.add_argument("--every", type=int, default=7)
    parser.add_argument("--run-id", default="baseline_krx_v2")
    args = parser.parse_args()

    cfg = default_config()
    cfg["data_cache_dir"] = str(DATA / "cache")
    cfg["results_dir"] = str(DATA / "results")
    cfg["memory_log_path"] = str(DATA / "live_unused.md")
    audit = AuditLog(DATA / "audit")

    dates = iter_grid(args.start, "2026-12-31", args.every)[: args.dates]
    print(f"grid: {len(UNIVERSE)} tickers x {len(dates)} dates ({dates[0]}..{dates[-1]})")

    screens: dict[str, object] = {}

    def screen_for(date: str):
        if date not in screens:
            run = screen(UNIVERSE, date, CFG, cache_dir=cfg["data_cache_dir"], universe_label="KOSPI200 sample (20)",
                         sector_of=sector_of, run_id=f"{args.run_id}_{date}")
            for c in run.candidates:
                c.name = korean_name(c.ticker) or name_of(c.ticker)
            audit.record_screen(run)
            screens[date] = run
            print(f"  screen {date}: {run.universe_size} -> {stage_counts(run)}")
        return screens[date]

    def decide(ticker: str, date: str) -> Decision:
        run = screen_for(date)
        cand = next(c for c in run.candidates if c.ticker == ticker)
        ranked = [c for c in run.candidates if c.rank is not None]
        # Rank every survivor of the absolute filters by momentum, not only the top_k.
        rank = cand.rank
        rating = rating_for(rank, len(ranked))
        failed = cand.failed_rules
        mom = next((f.note for f in cand.filters if f.rule == "momentum_top_k"), "")
        text = (f"**Rating**: {rating}\n\n**근거**: 규칙 전용 모멘텀 베이스라인. "
                + (f"모멘텀 순위 {rank}/{len(ranked)} ({mom})." if rank else
                   f"순위 없음: {', '.join(RULE_KO.get(r, r) for r in failed) or '규칙'} 탈락, 의견 없음."))
        if rating != "Hold":
            audit.record_decision(DecisionRecord(
                ticker=ticker, trade_date=date, screen_run_id=run.run_id, rating=rating,
                action="Buy" if rating in ("Buy", "Overweight") else "Sell",
                confidence={"Buy": 0.8, "Overweight": 0.5, "Underweight": 0.5, "Sell": 0.8}[rating],
                thesis=text,
                evidence=[EvidenceItem(source="market", summary=f"모멘텀 순위 {rank}/{len(ranked)}, {mom}", as_of=date,
                                       ref=run.run_id)],
                settings={"decider": "rule_momentum_baseline",
                          "screen": "ScreenConfig 기본값, 거래대금 상위 15, 모멘텀 상위 5, 변동성 하한 0.10"},
            ))
        return Decision(text, rating)

    result = run_backtest(UNIVERSE, dates, decide, cfg, reflector=RuleReflector(), run_id=args.run_id,
                          progress=lambda i, n, t, d: None)
    print(f"cells run {result.cells_run}, skipped {result.skipped}, failures {len(result.failures)}, "
          f"settlement failures {len(result.settlement_failures)}")
    for f in result.failures[:5]:
        print("  fail:", f)
    for f in result.settlement_failures[:5]:
        print("  settle fail:", f)

    summary = summarize(result)
    print()
    print(summary.render())
    (DATA / "baseline_summary.json").write_text(json.dumps({
        "run_id": result.run_id, "log_path": str(result.log_path), "cells_run": result.cells_run,
        "resolved": summary.resolved, "pending": summary.pending, "unscored": summary.unscored,
        "by_rating": {k: vars(v) for k, v in summary.by_rating.items()}, "holding": summary.holding,
        "dates": dates, "universe": UNIVERSE,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
