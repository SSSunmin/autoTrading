"""Audit records: why a ticker was screened in or out, and what each decision rested on.

A dashboard that shows "which stocks were picked and on what grounds" needs the
grounds recorded at the moment they were applied, not reconstructed later. These
models are the contract between the screening/decision program and the UI:

- ``FilterResult``   one rule applied to one ticker: the value seen, the threshold, pass/fail
- ``CandidateRecord`` one ticker through the whole screen, with every rule's result
- ``ScreenRun``      one screening pass: universe size, rules, survivors, written once
- ``EvidenceItem``   one piece of evidence a decision cites, with its source and as-of date
- ``DecisionRecord`` one agent decision: rating, evidence list, link to the memory log entry
- ``RunRecord``      one stage of the daily loop (scripts/daily_loop.py): market, stage, start/end, result, error

Records are appended, never changed, so a run can be replayed and nothing is
overwritten: rows of one SQLite database (agent_trading_core/store/, the default) or
JSON lines (``TRADING_STORE=jsonl``). The memory log stays the system of record for outcomes; a
DecisionRecord points at its entry by (ticker, trade_date).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agent_trading_core.rating import RATING_REVIEW, RATINGS_5_TIER
from agent_trading_core.schemas import SellReason
from agent_trading_core.store import DB_NAME, JsonlStore, SqliteStore, store_kind


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FilterResult(BaseModel):
    """One rule applied to one ticker."""

    rule: str = Field(description="Rule id, e.g. 'dollar_volume_top_n', 'listed_days_min'")
    value: float | str | None = Field(default=None, description="What the rule saw for this ticker")
    threshold: float | str | None = Field(default=None, description="What it was compared against")
    passed: bool
    note: str = ""


class CandidateRecord(BaseModel):
    """One ticker through one screening pass."""

    ticker: str
    name: str | None = None
    filters: list[FilterResult] = Field(default_factory=list)
    score: float | None = Field(default=None, description="Ranking score when the screen ranks")
    rank: int | None = None
    selected: bool

    @property
    def failed_rules(self) -> list[str]:
        return [f.rule for f in self.filters if not f.passed]


class ScreenRun(BaseModel):
    """One screening pass over a universe, as the dashboard lists it."""

    run_id: str
    as_of: str = Field(description="Data date the screen used, YYYY-MM-DD")
    recorded_at: str = Field(default_factory=_now)
    universe: str = Field(description="Universe label, e.g. 'KOSPI200', 'KRX all'")
    universe_size: int
    rules: list[str] = Field(description="Rule ids in the order they were applied")
    candidates: list[CandidateRecord]

    @property
    def selected(self) -> list[CandidateRecord]:
        return [c for c in self.candidates if c.selected]


class EvidenceItem(BaseModel):
    """One piece of evidence a decision cites."""

    source: Literal["market", "fundamentals", "news", "sentiment", "macro", "memory", "portfolio", "other"]
    summary: str = Field(description="One sentence, as shown in the UI")
    as_of: str | None = Field(default=None, description="Date the evidence was current, YYYY-MM-DD")
    ref: str | None = Field(default=None, description="Report section, URL or tool call id")


class DecisionRecord(BaseModel):
    """One agent decision and what it rested on. Keyed like the memory log entry."""

    ticker: str
    trade_date: str
    recorded_at: str = Field(default_factory=_now)
    screen_run_id: str | None = Field(default=None, description="The screen that surfaced this ticker")
    rating: str
    action: Literal["Buy", "Hold", "Sell"] | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    position_sizing: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    thesis: str = Field(description="The decision text shown in the UI")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    hard_rule_overrides: list[str] = Field(
        default_factory=list,
        description="Deterministic rules that changed or blocked the agent's call, e.g. 'max_position_pct'",
    )
    settings: dict = Field(default_factory=dict, description="Model/provider/debate depth that produced it")
    usage: dict[str, int] = Field(default_factory=dict,
                                  description="The model call's token counts (input_tokens, output_tokens, "
                                              "cache_read_input_tokens, cache_creation_input_tokens); empty for a "
                                              "decision no model made")
    sell_reason: SellReason | None = Field(default=None,
                                           description="A sell review's first reason (schemas.SellReason, docs/01 "
                                                       "§12-2), when its action is Trim or Exit")
    source: Literal["agent", "rule", "baseline"] = Field(
        default="agent",
        description="agent: the manager's call; rule: a hard-rule exit the executor recorded before its sell order "
                    "(stop-loss, trailing, expiry, drawdown stop); baseline: the rule-only momentum baseline. "
                    "Baseline records so far are stored as agent — read them through decision_source",
    )
    market: str = Field(default="KRX", description="config.MARKETS key of the book this belongs to; lines written "
                                                    "before the market axis have none and are KRX")

    @property
    def tradeable(self) -> bool:
        return self.rating in RATINGS_5_TIER and self.rating != RATING_REVIEW


BASELINE_DECIDER = "rule_momentum_baseline"     # settings["decider"] of scripts/baseline_krx.py


def decision_source(d: DecisionRecord) -> str:
    """agent · rule · baseline. A decision whose ``settings.decider`` is the rule baseline is baseline whatever was
    stored: the baseline's records (data/audit/, audit/KRX/, audit/US/) were written as agent and are not moved."""
    if d.settings.get("decider") == BASELINE_DECIDER:
        return "baseline"
    return d.source


class OrderRecord(BaseModel):
    """One order the execution layer placed, or refused to place, for a decision (docs/02 v0.3 §9).

    Keyed to its decision by (ticker, trade_date). ``status`` is the dashboard's execution strip:
    filled / partial / open are real orders; blocked means a hard rule shrank it to zero; rejected and
    exception were never sent and land in the exception queue; cancelled was open when the loop halted.
    The log stays append-only: an open order that later fills or is cancelled gets a new line with the
    same ``order_id``, and the last line for an id is its current state (``latest_orders``).
    """

    order_id: str
    ticker: str
    trade_date: str = Field(description="Decision date this order executes, YYYY-MM-DD")
    placed_at: str = Field(default_factory=_now)
    side: Literal["Buy", "Sell"]
    quantity: int = Field(ge=0)
    limit_price: float | None = None
    filled_quantity: int = Field(default=0, ge=0)
    filled_price: float | None = None
    status: Literal["filled", "partial", "open", "blocked", "rejected", "exception", "cancelled"]
    fill_date: str | None = Field(default=None, description="Trading day the fill happened (next open), YYYY-MM-DD")
    # luld_halt / pdt_blocked: value and label only — raising them needs a live data layer (docs/04)
    exception_kind: Literal["review", "cap_exceeded", "order_rejected", "vendor_unavailable",
                            "data_delayed", "luld_halt", "pdt_blocked"] | None = None
    reason: str = Field(default="", description="Fill note, or why it was blocked / rejected / exceptional")
    rules_applied: list[str] = Field(default_factory=list,
                                     description="Hard-rule and auto-execution rule ids that touched this order")
    market: str = Field(default="KRX", description="config.MARKETS key; amounts are in that market's currency")

    @property
    def amount(self) -> float | None:
        return self.filled_price * self.filled_quantity if self.filled_price is not None else None


def latest_orders(orders: list[OrderRecord]) -> dict[str, OrderRecord]:
    """The current state of each order: the last line written for its ``order_id``, in log order."""
    out: dict[str, OrderRecord] = {}
    for o in orders:
        out.pop(o.order_id, None)
        out[o.order_id] = o
    return out


class OperationRecord(BaseModel):
    """One operation on the trading loop: emergency halt, resume, mode or rule change.

    The current trading state is never stored; it is replayed from these records (``trading_state``),
    so the state and its audit trail cannot disagree.
    """

    kind: Literal["halt", "resume", "mode_change", "rule_change"]
    at: str = Field(default_factory=_now)
    operator: str
    role: str = Field(description="Authority used: operator / admin / admin(dev, no token) / auto (the executor's own halt)")
    reason: str
    target: str = Field(default="", description="What changed, e.g. 'mode', 'sector_quota.sector_max'. A halt: "
                                                "'manual' (a person; 'running' before halt-semantics), 'daily_loss' or "
                                                "'dd_stop' (the executor's own, by system)")
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)
    market: str = Field(default="KRX", description="The market whose loop this operated on")
    scope: str | None = Field(default=None, description="halt / resume: 'all' when one request wrote a line to every "
                                                         "market's log (same at, same reason), else the market")
    effective_from: str | None = Field(default=None, description="A system resume of a daily-loss halt: the trading day "
                                                                 "(YYYY-MM-DD, market time, from 00:00) the halt ended — "
                                                                 "``at`` is when the line was written, later")


class RunRecord(BaseModel):
    """One stage of the daily paper loop (scripts/daily_loop.py) for one market and trading day. ``ok`` and
    ``skipped`` for a holiday are final: the loop does not run that stage of that day again. ``failed`` is run again
    by a scheduler's next call while the day is due; ``skipped`` for any other reason (the executor refused: halted,
    not paper mode; ``missed``) only by a call with ``--date``."""

    market: str
    stage: Literal["decide", "fill"]
    trade_date: str
    started_at: str
    ended_at: str = Field(default_factory=_now)
    status: Literal["ok", "skipped", "failed"]
    reason: str | None = Field(default=None, description="skipped: 'holiday' or the executor's refusal")
    error: str | None = Field(default=None, description="failed: the exception type and message, never a credential")
    counts: dict[str, int] = Field(default_factory=dict, description="what the stage wrote or saw, e.g. decisions, "
                                                                      "orders_open, orders_filled, provisional")
    notes: list[str] = Field(default_factory=list, description="findings that are not failures, e.g. a fill price "
                                                               "that differs from the final open")

    @property
    def final(self) -> bool:
        return self.status == "ok" or (self.status == "skipped" and self.reason == "holiday")


# what a halt stops for: a person (records before halt-semantics wrote target="running"), or the executor itself
HALT_TARGETS = ("manual", "daily_loss", "dd_stop")


def trading_state(operations: list[OperationRecord], default_mode: str = "paper") -> dict:
    """Replay the operations log into the live trading state the status bar shows.

    ``mode``: auto | manual | paper. ``running`` is False after a halt until a resume. ``halt_target``: what the
    standing halt is (``HALT_TARGETS``), None while running.
    """
    state: dict = {"mode": default_mode, "running": True, "halted_at": None, "halted_by": None,
                   "halt_reason": None, "keep_exits": True, "mode_changed_at": None, "halt_target": None}
    for op in operations:
        if op.kind == "halt":
            state.update(running=False, halted_at=op.at, halted_by=op.operator, halt_reason=op.reason,
                         keep_exits=bool(op.after.get("keep_exits", True)),
                         halt_target=op.target if op.target in HALT_TARGETS else "manual")
        elif op.kind == "resume":
            state.update(running=True, halted_at=None, halted_by=None, halt_reason=None, halt_target=None)
        elif op.kind == "mode_change" and "mode" in op.after:
            state.update(mode=op.after["mode"], mode_changed_at=op.at)
    return state


RECORD_NAMES = ("screens", "decisions", "orders", "operations", "runs")


def _unloaded(root: Path) -> bool:
    """Whether the audit root holds JSON lines (its own files or a market folder's) that a missing database has not
    loaded: every book, not only the one opened — the first book read creates the database, and the next would
    then start empty."""
    dirs = [root, *(d for d in root.iterdir() if d.is_dir())] if root.is_dir() else []
    return any((p := d / f"{n}.jsonl").exists() and p.stat().st_size > 0 for d in dirs for n in RECORD_NAMES)


class AuditLog:
    """Append-only records, one kind per record type: in the SQLite database (``store.SqliteStore``, the default) or
    in JSON-lines files under ``directory`` (``TRADING_STORE=jsonl``, ``store.JsonlStore``).

    With ``market`` set it is one market's book: a record of another market is refused on write and on read
    (ValueError, like a broken line), so a default "KRX" never slips a line into the wrong book. ``legacy``: a
    directory read before ``directory`` and never written — the KRX files from before the market axis
    (``market_audit``). ``directory`` stays the book's folder either way: its locks (``execution.execution_lock``)
    and the model's raw answers live there.

    The database is ``trading.db`` beside the audit root (``data/audit`` -> ``data/trading.db``, the
    ``TRADING_DATA_DIR``), one for every market: the root is ``legacy`` when there is one, the parent of a market's
    ``directory`` (``market_audit``), else ``directory`` itself. A book with no ``market`` reads every market's rows,
    as it read every line of its files."""

    def __init__(self, directory: str | Path, market: str | None = None, legacy: str | Path | None = None):
        self.directory = Path(directory)
        self.market = market
        self.legacy = Path(legacy) if legacy else None
        self.directory.mkdir(parents=True, exist_ok=True)
        kind = store_kind()
        if kind == "jsonl":
            self.store = JsonlStore(self.directory, self.legacy)
            return
        root = self.legacy or (self.directory.parent if market else self.directory)
        self.store = SqliteStore(root.parent / DB_NAME)
        if not self.store.path.exists() and _unloaded(root):
            raise RuntimeError(f"{self.store.path}가 없고 {root} 아래에 JSONL 기록이 있습니다 — "
                               "scripts/migrate_jsonl_to_db.py로 먼저 적재하세요(빈 장부로 시작하면 주문이 겹칩니다)")

    def _check(self, record: BaseModel, where: str) -> None:
        got = getattr(record, "market", None)
        if self.market is not None and got is not None and got != self.market:
            raise ValueError(f"{where}: {got} 기록이 {self.market} 장부에 들어올 수 없습니다")

    def _append(self, name: str, record: BaseModel) -> None:
        self._check(record, f"{name}.jsonl 쓰기")
        self.store.append(name, record, getattr(record, "market", None) or self.market or "KRX")
        if self.store.kind == "sqlite":
            # ponytail: the backend reloads when a JSONL file's mtime changes (backend/main.py _Store._paths); touch
            # the book's file, contents untouched, until it watches the database (api feature/db-store-api)
            (self.directory / f"{name}.jsonl").touch()

    def _read(self, name: str, model):
        rows = [model.model_validate(json.loads(line)) for line in self.store.read(name, self.market)]
        for r in rows:
            self._check(r, f"{name}.jsonl 읽기")
        return rows

    def record_screen(self, run: ScreenRun) -> None:
        self._append("screens", run)

    def record_decision(self, decision: DecisionRecord) -> None:
        self._append("decisions", decision)

    def screens(self) -> list[ScreenRun]:
        return self._read("screens", ScreenRun)

    def decisions(self, ticker: str | None = None) -> list[DecisionRecord]:
        rows = self._read("decisions", DecisionRecord)
        return [d for d in rows if ticker is None or d.ticker == ticker]

    def record_order(self, order: OrderRecord) -> None:
        self._append("orders", order)

    def orders(self) -> list[OrderRecord]:
        return self._read("orders", OrderRecord)

    def record_operation(self, op: OperationRecord) -> None:
        self._append("operations", op)

    def operations(self) -> list[OperationRecord]:
        return self._read("operations", OperationRecord)

    def record_run(self, run: RunRecord) -> None:
        self._append("runs", run)

    def runs(self) -> list[RunRecord]:
        return self._read("runs", RunRecord)


def market_audit(root: str | Path, market: str) -> AuditLog:
    """One market's log under ``root`` (data/audit): ``root/<market>/``. KRX also reads the files written straight
    under ``root`` before the market axis, first, without moving them; new KRX lines go to ``root/KRX/``."""
    root = Path(root)
    return AuditLog(root / market, market=market, legacy=root if market == "KRX" else None)


def baseline_summary_path(data: str | Path, market: str) -> Path:
    """The rule-only decision baseline's summary (scripts/baseline_krx.py) under ``data``: KRX keeps the file from
    before the market axis, ``data/baseline_summary.json``; any other market writes ``data/audit/<market>/``."""
    data = Path(data)
    return data / "baseline_summary.json" if market == "KRX" else data / "audit" / market / "baseline_summary.json"


def stage_counts(run: ScreenRun) -> list[int]:
    """Survivors after each rule, in rule order, for the dashboard funnel.

    A ticker counts as surviving a rule when it has a passed result for it; a
    ticker never evaluated against a rule (it fell out earlier) does not.
    """
    counts = []
    for rule in run.rules:
        counts.append(sum(1 for c in run.candidates
                          if any(f.rule == rule and f.passed for f in c.filters)))
    return counts
