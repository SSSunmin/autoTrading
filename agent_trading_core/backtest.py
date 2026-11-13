"""Run any decision function over a grid of tickers and dates, and score what came back.

From TradingAgents' ``backtest.py`` with the graph dependency removed: the thing
under test is a ``decide(ticker, date) -> Decision`` callable the caller supplies.
It can be an LLM agent pipeline, a rule, or a mix; this module only records each
decision in a memory log of the run's own, settles it once its holding window
has traded, and aggregates realized alpha by rating.

Scope: this evaluates decision quality. It is not a portfolio simulator. Turning a
rating into a filled order needs a quantity, a fill price and a cash ledger, and
inventing them here would put an execution model behind an evaluation tool.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from agent_trading_core.date_window import get_current_date
from agent_trading_core.memory import TradingMemoryLog, settlement
from agent_trading_core.rating import RATING_REVIEW, parse_rating
from agent_trading_core.symbols import safe_ticker_component

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """What a decision function returns for one (ticker, date) cell.

    ``text`` is stored in the log and read back as past context; ``rating`` is
    one of the 5 tiers, or None to have it read from ``text`` (REVIEW when absent).
    """

    text: str
    rating: str | None = None


Decider = Callable[[str, str], Decision]


class Reflector(Protocol):
    def reflect_on_final_decision(self, final_decision: str, raw_return: float, alpha_return: float,
                                  benchmark_name: str, holding_days: int) -> str: ...


def iter_grid(start_date: str, end_date: str, every_n_days: int = 1) -> list[str]:
    """Analysis dates from ``start_date``, never past today."""
    start, end = _canonical(start_date), _canonical(end_date)
    if every_n_days < 1:
        raise ValueError("every_n_days must be at least 1")
    if end < start:
        raise ValueError(f"the grid ends before it starts: {end_date} is before {start_date}")
    last = min(end, datetime.strptime(get_current_date(), "%Y-%m-%d"))
    dates, cursor = [], start
    while cursor <= last:
        dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=every_n_days)
    return dates


def _canonical(date: str) -> datetime:
    try:
        parsed = datetime.strptime(str(date), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"grid dates must be in YYYY-MM-DD format, got {date!r}") from exc
    if parsed.strftime("%Y-%m-%d") != str(date):
        raise ValueError(f"grid dates must be in YYYY-MM-DD format, got {date!r}")
    return parsed


def _alpha(entry: dict) -> float | None:
    text = (entry.get("alpha") or "").strip().rstrip("%")
    try:
        return float(text) / 100
    except ValueError:
        return None


@dataclass
class BacktestResult:
    run_id: str
    log_path: Path
    cells_run: int = 0
    skipped: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    settlement_failures: list[tuple[str, str]] = field(default_factory=list)


# What each rating claims will happen. Hold claims no direction.
_DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


@dataclass
class RatingScore:
    count: int
    hit_rate: float | None
    mean_alpha: float


@dataclass
class BacktestSummary:
    resolved: int
    pending: int
    by_rating: dict[str, RatingScore]
    unscored: int = 0
    holding: str = ""

    def render(self) -> str:
        lines = [f"Resolved cells: {self.resolved} · pending: {self.pending}"
                 + (f" · unscored: {self.unscored}" if self.unscored else "")]
        for rating, score in self.by_rating.items():
            called = (f"called the direction {score.hit_rate:.0%}"
                      if score.hit_rate is not None else "no direction claimed")
            lines.append(f"- {rating}: n={score.count}, {called}, "
                         f"mean alpha {score.mean_alpha:+.2%} vs the benchmark")
        lines.append("")
        if self.pending:
            lines.append("Pending cells are not scored above; re-run to settle them.")
        lines.append(
            f"Alpha is measured over {self.holding} from the first close after each analysis date. "
            "One decision per cell; these figures are indicative rather than repeatable."
        )
        return "\n".join(lines)


def _new_run(runs: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    runs.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        run_id = stamp if n == 1 else f"{stamp}_{n}"
        try:
            (runs / run_id).mkdir()
            return run_id
        except FileExistsError:
            continue


def run_backtest(
    tickers: list[str],
    dates: list[str],
    decide: Decider,
    config: dict,
    reflector: Reflector | None = None,
    run_id: str | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
) -> BacktestResult:
    """Call ``decide`` on every ticker on every date, into a memory log of this run's own.

    Cells already in this run's log are skipped, so an interrupted sweep resumes
    by being run again with the same ``run_id``. Pending decisions are settled at
    the end; without a ``reflector`` the settlement step is skipped and cells stay
    pending until a later ``settle`` call.
    """
    runs = Path(config["results_dir"]) / "backtest"
    if run_id:
        run_id = safe_ticker_component(run_id)
        (runs / run_id).mkdir(parents=True, exist_ok=True)
    else:
        run_id = _new_run(runs)
    run_dir = runs / run_id
    run_config = {**config, "results_dir": str(run_dir),
                  "memory_log_path": str(run_dir / "trading_memory.md")}
    memory_log = TradingMemoryLog(run_config)
    result = BacktestResult(run_id=run_id, log_path=Path(run_config["memory_log_path"]))
    done = {(e["ticker"], e["date"]) for e in memory_log.load_entries()}

    tickers, dates = list(dict.fromkeys(tickers)), list(dict.fromkeys(dates))
    cells = [(ticker, date) for ticker in tickers for date in dates]
    todo = [cell for cell in cells if cell not in done]
    result.skipped = len(cells) - len(todo)
    for index, (ticker, date) in enumerate(todo, 1):
        if progress:
            progress(index, len(todo), ticker, date)
        try:
            decision = decide(ticker, date)
            memory_log.store_decision(ticker, date, decision.text,
                                      rating=decision.rating or parse_rating(decision.text))
            result.cells_run += 1
        except Exception as exc:  # one failed cell must not end the sweep
            logger.warning("Backtest cell %s %s failed: %s", ticker, date, exc)
            result.failures.append((ticker, date, str(exc)))

    if reflector is not None:
        for ticker in tickers:
            try:
                done_ = settlement.settle_pending(ticker, memory_log, reflector, run_config)
                result.settlement_failures.extend(
                    (t, f"{date}: {reason}") for t, date, reason in done_.failed)
            except Exception as exc:
                logger.warning("Settling %s failed: %s", ticker, exc)
                result.settlement_failures.append((ticker, str(exc)))
    return result


def summarize(source: BacktestResult | str | Path) -> BacktestSummary:
    """Score the settled decisions of a backtest, or of a memory log at a path, by rating."""
    if isinstance(source, BacktestResult):
        path = source.log_path
    elif Path(source).is_file():
        path = Path(source)
    else:
        raise FileNotFoundError(f"no memory log at {source}")
    entries = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()
    resolved = [(e, _alpha(e)) for e in entries if not e["pending"] and e["rating"] != RATING_REVIEW]
    resolved = [(e, a) for e, a in resolved if a is not None]
    by_rating: dict[str, RatingScore] = {}
    for rating in dict.fromkeys(e["rating"] for e, _ in resolved):
        alphas = [a for e, a in resolved if e["rating"] == rating]
        direction = _DIRECTION.get(rating, 0)
        by_rating[rating] = RatingScore(
            count=len(alphas),
            hit_rate=(sum(a * direction > 0 for a in alphas) / len(alphas)) if direction else None,
            mean_alpha=sum(alphas) / len(alphas),
        )
    unscored = sum(1 for e in entries if e["rating"] == RATING_REVIEW)
    windows = {f"{e['holding'][:-1]} trading days" for e, _ in resolved
               if (e.get("holding") or "").endswith("d")}
    return BacktestSummary(resolved=len(resolved), pending=len(entries) - len(resolved) - unscored,
                           by_rating=by_rating, unscored=unscored,
                           holding=", ".join(sorted(windows)) or "the configured window")
