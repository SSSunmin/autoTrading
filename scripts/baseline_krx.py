"""Rule-only baseline on one market's large caps: screen → momentum rank → rating, scored by realized alpha.

No LLM anywhere. This is the bar an agent has to beat (docs/01 §11-4 step 1). It
also writes the screening and decision audit records the dashboard reads.

``--market`` picks the book (config.MARKETS, separate portfolios): KRX = the 20 KOSPI200 names below, US = the
40 S&P 500 names of the 2nd-3rd measurements (scripts/paper_backtest.US_UNIVERSE) with the US screen preset.
Alpha is measured against the market's benchmark. Records go to data/audit/<market>/ (audit.market_audit — never
data/audit/ itself, which KRX only reads as the pre-market-axis book); the summary to
audit.baseline_summary_path (KRX keeps data/baseline_summary.json).

Run:  .venv/Scripts/python scripts/baseline_krx.py [--market KRX|US] [--dates 8] [--start 2026-06-05]
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

from agent_trading_core.audit import (
    BASELINE_DECIDER,
    DecisionRecord,
    EvidenceItem,
    ScreenRun,
    baseline_summary_path,
    market_audit,
    stage_counts,
)
from agent_trading_core.backtest import Decision, iter_grid, run_backtest, summarize
from agent_trading_core.config import MARKETS, default_config
from agent_trading_core.identity import resolve_instrument_identity
from agent_trading_core.krx_names import RULE_KO, korean_name
from agent_trading_core.screening import ScreenConfig, screen, sector_lookup, us_preset

sys.path.insert(0, str(ROOT / "scripts"))
from paper_backtest import US_UNIVERSE  # the US book of the 2nd-3rd measurements, one list

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

# Per-market book. Index and currency live in config.MARKETS; the universe, screen and run id here. The KRX
# values are the ones this script ran with before --market (same run id, screen and summary file).
SETUPS = {
    "KRX": {"universe": UNIVERSE, "label": "KOSPI200 sample (20)", "screen": CFG, "run_id": "baseline_krx_v2",
            "screen_note": "ScreenConfig 기본값, 거래대금 상위 15, 모멘텀 상위 5, 변동성 하한 0.10"},
    "US": {"universe": US_UNIVERSE, "label": "S&P 500 sample (40)", "screen": us_preset(CFG), "run_id": "baseline_us_v1",
           "screen_note": "ScreenConfig 기본값 + 미국 프리셋(us_preset), 거래대금 상위 15, 모멘텀 상위 5, 변동성 하한 0.10"},
}


class RuleReflector:
    """Settlement needs a reflection; the baseline has none to offer."""

    def reflect_on_final_decision(self, final_decision, raw_return, alpha_return, benchmark_name="SPY", holding_days=5):
        return (f"규칙 전용 베이스라인(LLM 없음). {holding_days}거래일 {benchmark_name} 대비 알파 {alpha_return:+.1%}, "
                f"실현수익 {raw_return:+.1%}.")


@cache
def sector_of(ticker: str) -> str | None:
    return sector_lookup(ticker)     # data/universe/sectors.csv first, the Yahoo profile only for an unlisted name


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


def screen_day(market: str, date: str, *, cache_dir: str, run_id: str, load=None) -> ScreenRun:
    """The market's screen as of ``date`` (``run_id_date``), candidates named. ``load``: the OHLCV loader
    (default ``screen``'s Yahoo one; the daily loop passes the KIS one)."""
    setup = SETUPS[market]
    run = screen(setup["universe"], date, setup["screen"], cache_dir=cache_dir, universe_label=setup["label"],
                 sector_of=sector_of, run_id=f"{run_id}_{date}", **({"load": load} if load else {}))
    for c in run.candidates:
        c.name = korean_name(c.ticker) or name_of(c.ticker)
    return run


def decide_from_screen(market: str, run: ScreenRun, ticker: str, date: str) -> tuple[Decision, DecisionRecord | None]:
    """The rule decision for ``ticker`` from the day's screen, and its audit record (None for Hold: not recorded)."""
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
    record = None
    if rating != "Hold":
        record = DecisionRecord(
            market=market, ticker=ticker, trade_date=date, screen_run_id=run.run_id, rating=rating,
            action="Buy" if rating in ("Buy", "Overweight") else "Sell",
            confidence={"Buy": 0.8, "Overweight": 0.5, "Underweight": 0.5, "Sell": 0.8}[rating],
            thesis=text,
            evidence=[EvidenceItem(source="market", summary=f"모멘텀 순위 {rank}/{len(ranked)}, {mom}", as_of=date,
                                   ref=run.run_id)],
            # ponytail: stored as source "agent" — the paper executor (execution.py) places and explains only
            # agent decisions; audit.decision_source reads the decider as "baseline". Write source="baseline"
            # once the executor accepts it (stage 4).
            settings={"decider": BASELINE_DECIDER,
                      "screen": SETUPS[market]["screen_note"]},
        )
    return Decision(text, rating), record


def rating_table(by_rating: dict) -> str:
    """docs/03 §2 · §6 table from the summary's by_rating, in tier order."""
    rows = ["| 등급 | n | 방향 적중률 | 평균 알파 |", "|---|---:|---:|---:|"]
    for r in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
        if r in by_rating:
            v = by_rating[r]
            hit = "방향 없음" if v["hit_rate"] is None else f"{v['hit_rate']:.0%}"
            rows.append(f"| {r} | {v['count']} | {hit} | {v['mean_alpha']:+.2%} |".replace("-", "−"))
    return "\n".join(rows)


def main(argv: list[str] | None = None, data: Path = DATA) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="KRX", choices=sorted(SETUPS))
    parser.add_argument("--start", default="2026-06-05")
    parser.add_argument("--dates", type=int, default=8)
    parser.add_argument("--every", type=int, default=7)
    parser.add_argument("--run-id", help="default: the market's run id (SETUPS)")
    args = parser.parse_args(argv)
    setup, market = SETUPS[args.market], MARKETS[args.market]
    universe = setup["universe"]
    run_id = args.run_id or setup["run_id"]

    cfg = default_config()
    cfg["data_cache_dir"] = str(data / "cache")
    cfg["results_dir"] = str(data / "results")
    cfg["memory_log_path"] = str(data / "live_unused.md")
    # alpha against the market's own index (KRX: the ^KS11 the .KS suffix map already gave; US: ^GSPC, not SPY)
    cfg["benchmark_ticker"] = market["benchmark"]
    audit = market_audit(data / "audit", args.market)

    dates = iter_grid(args.start, "2026-12-31", args.every)[: args.dates]
    print(f"{args.market} grid: {len(universe)} tickers x {len(dates)} dates ({dates[0]}..{dates[-1]}), "
          f"benchmark {market['benchmark']}, records {audit.directory}")

    screens: dict[str, object] = {}

    def screen_for(date: str):
        if date not in screens:
            run = screen_day(args.market, date, cache_dir=cfg["data_cache_dir"], run_id=run_id)
            audit.record_screen(run)
            screens[date] = run
            print(f"  screen {date}: {run.universe_size} -> {stage_counts(run)}")
        return screens[date]

    def decide(ticker: str, date: str) -> Decision:
        decision, record = decide_from_screen(args.market, screen_for(date), ticker, date)
        if record is not None:
            audit.record_decision(record)
        return decision

    result = run_backtest(universe, dates, decide, cfg, reflector=RuleReflector(), run_id=run_id,
                          progress=lambda i, n, t, d: None)
    print(f"cells run {result.cells_run}, skipped {result.skipped}, failures {len(result.failures)}, "
          f"settlement failures {len(result.settlement_failures)}")
    for f in result.failures[:5]:
        print("  fail:", f)
    for f in result.settlement_failures[:5]:
        print("  settle fail:", f)

    summary = summarize(result)
    print(f"{args.market}: {len(universe)} tickers, {len(dates)} dates, cells {result.cells_run} (+{result.skipped} already in the log), "
          f"decision records {sum(1 for d in audit.decisions() if d.screen_run_id.startswith(run_id))}, "
          f"REVIEW {summary.unscored}")
    print()
    print(summary.render())
    out = baseline_summary_path(data, args.market)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        # cells_run: the grid's cells in the log, this run's plus the ones an earlier run of the same run id
        # recorded (a rerun skips those) — on a first run the same number as before
        "run_id": result.run_id, "log_path": str(result.log_path), "cells_run": result.cells_run + result.skipped,
        "cells_run_this_time": result.cells_run, "skipped": result.skipped,
        "resolved": summary.resolved, "pending": summary.pending, "unscored": summary.unscored,
        "by_rating": {k: vars(v) for k, v in summary.by_rating.items()}, "holding": summary.holding,
        "dates": dates, "universe": universe, "market": args.market, "currency": market["currency"],
        "benchmark": market["benchmark"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print()
    print(rating_table({k: vars(v) for k, v in summary.by_rating.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
