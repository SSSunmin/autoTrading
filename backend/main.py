"""API for the dashboard (docs/02 v0.3): the audit log, the memory log and the backtest summary served as
recorded, plus the write actions the plan allows — emergency halt, resume, mode change and the exception queue's
actions (docs/10 D7) — each appended to the operations log. Korean labels are attached server-side so the frontend never translates
rule ids, rating values or order statuses.

Markets (docs/08): KRX and US are separate books. Every read takes ``market`` (default KRX, anything else
outside config.MARKETS is 400) and serves that market's records only — rows of the SQLite database
(``TRADING_DATA_DIR/trading.db``, docs/04 "기록 저장소") read through ``agent_trading_core.audit``, or its JSON-lines
files with ``TRADING_STORE=jsonl``. Halt and resume take a ``scope`` ("all" by default, or
one market); a mode change names its market.

Run:  .venv/Scripts/python -m uvicorn backend.main:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs   (OpenAPI, generated)

Environment:
  TRADING_DATA_DIR      where data/ lives (default: <repo>/data)
  TRADING_MEMORY_LOG    memory log to read (default: the log named in data/baseline_summary.json; KRX only)
  TRADING_CORS          comma-separated origins allowed (default: localhost dev ports)
  TRADING_ADMIN_TOKEN   when set, resume, mode change and exception actions require header X-Admin-Token with this value.
                        Unset = development: anyone may, and the record says so (role "admin(dev, no token)").
  TRADING_STORE         sqlite (default: TRADING_DATA_DIR/trading.db) | jsonl — the store agent_trading_core.audit reads
  TRADING_STATIC_DIR    the built dashboard (frontend/dist) to serve at / on the API's own origin (the Docker image)
  TRADING_ENV_FILE      the .env /api/data/status checks for key presence (default: <repo>/.env); values never served
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import threading
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_trading_core import __version__ as core_version
from agent_trading_core import kis, market_calendar, scheduler
from agent_trading_core.audit import (
    BASELINE_DECIDER,
    AuditLog,
    DecisionRecord,
    EvidenceItem,
    OperationRecord,
    OrderRecord,
    RunRecord,
    ScreenRun,
    baseline_summary_path,
    decision_source,
    latest_orders,
    market_audit,
    pending_rerun_marker,
    stage_counts,
)
from agent_trading_core.config import AUTO_EXECUTION, LLM, MARKETS, PAPER_TRADING, auto_execution
from agent_trading_core.execution import (
    CAPITAL_MAX,
    EXIT_SUFFIX,
    SMALL_ACCOUNT,
    Prices,
    book_events,
    book_series,
    cancel_orders,
    capital_state,
    check_date,
    dd_stop_standing,
    effective_state,
    execution_lock,
    fmt_money,
    next_trading_day,
    open_orders,
    paper_config,
    paper_expected,
    paper_progress,
    paper_start,
    positions_view,
    standing_halts,
)
from agent_trading_core.krx_names import (
    ACCOUNT_MODE_KO,
    AUTO_RULE_KO,
    BOOK_EVENT_KO,
    CAPITAL_ACCOUNT_KO,
    CAPITAL_CHANGE_KO,
    DECIDER_KO,
    DECISION_SOURCE_KO,
    EXCEPTION_ACTION_KO,
    EXCEPTION_KIND_KO,
    EXCEPTION_PENDING_KO,
    EXECUTION_MODE_KO,
    EXIT_REASON_KO,
    HALT_SCOPE_KO,
    HALT_TARGET_KO,
    HALTED_BY_KO,
    LOOP_PENDING_KO,
    LOOP_SOURCE_KO,
    MARKET_KO,
    MODE_KO,
    OPERATION_KO,
    ORDER_STATUS_KO,
    PAPER_CRITERIA_KO,
    PAPER_NOTE_KO,
    PAPER_STATUS_KO,
    PAPER_VIOLATION_KO,
    RATING_KO,
    RISK_RULE_KO,
    RULE_DESC_KO,
    RULE_KO,
    SELL_REASON_KO,
    SETTLEMENT_KO,
    SIDE_KO,
    korean_name,
)
from agent_trading_core.memory import TradingMemoryLog, settlement
from agent_trading_core.rating import RATING_REVIEW, RATINGS_5_TIER
from agent_trading_core.risk import EPS, RiskConfig, drawdown_scale
from agent_trading_core.screening import ScreenConfig, us_preset
from agent_trading_core.universe import SECTORS, load_history, members_on, sector_of

VARIANT_KO = {"rules_off": "하드 룰 끔", "rules_on": "하드 룰 켬", "rules_on_regime": "하드 룰 + 국면 필터"}
KST = ZoneInfo("Asia/Seoul")
WEEKDAY_KO = "월화수목금토일"
STAGE_KO = {"decide": "결정", "fill": "체결"}
RUN_STATUS_KO = {"ok": "완료", "skipped": "건너뜀", "failed": "실패"}
# the vendors the settings page lists (mockup v0.7.2 States ⑤): the environment names a key is read from — only
# whether one is set is ever served, never its value
VENDORS = [
    {"id": "kis", "label_ko": "KIS Open API", "use_ko": "시세 · 종가 · 지수(기본) · 실거래 주문", "cache": "kis",
     "keys": list(kis.REQUIRED)},
    {"id": "yfinance", "label_ko": "yfinance", "use_ko": "시세 대체(KIS 파일 없는 종목) · 백테스트", "cache": "yahoo",
     "keys": []},
    {"id": "dart", "label_ko": "DART OpenAPI", "use_ko": "국내 재무 · 공시 · 실적 발표", "cache": None,
     "keys": ["DART_API_KEY"]},
    {"id": "anthropic", "label_ko": "Anthropic API", "use_ko": "에이전트 결정(llm 결정자)", "cache": None,
     "keys": ["ANTHROPIC_API_KEY"]},
]
REFRESH_LOG = "_refresh_log.jsonl"          # scripts/refresh_cache.py LOG: one line per refresh run
# where scripts/paper_backtest.py --market M writes its summary
PAPER_SUMMARY = {"KRX": Path("results") / "paper" / "summary.json", "US": Path("results") / "paper_us" / "summary.json"}
# the measurement each summary is, and the variant screen 8 compares with the paper run (the executor's own rules:
# hard rules on, regime filter on — PaperConfig.regime_filter)
PAPER_MEASUREMENT_KO = {"KRX": "2차 실측", "US": "3차 실측"}
PAPER_REFERENCE_VARIANT = "rules_on_regime"

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR") or ROOT / "data")
ADMIN_TOKEN = os.environ.get("TRADING_ADMIN_TOKEN") or None

app = FastAPI(title="agent-trading-core API", version=core_version,
              description="자동매매 대시보드 API. 선별·결정·주문·정산 기록을 시장(국내·미국)별로 그대로 서빙하고, "
                          "쓰기는 긴급 정지·재가동·모드 변경·예외 처리만.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in (os.environ.get("TRADING_CORS") or
                   "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000").split(",") if o],
    allow_methods=["GET", "POST"], allow_headers=["*"],
)


# --- data access: one store per market over the audit's read API (agent_trading_core/audit.py) ------------
# No record file is read or watched here: the audit picks the store (SQLite by default, TRADING_STORE=jsonl). What
# is cached is kept per record kind and dropped when that kind's ``AuditLog.stamp()`` moves (a row appended by any
# writer — the scheduler, the executor, another worker); the baseline summary and the memory log are files and keep
# their mtime. Decisions are never cached whole: each call reads its range (``AuditLog.decisions_where``).

class _Store:
    def __init__(self, market: str) -> None:
        self.market = market
        self._audit: AuditLog | None = None
        self._stamp: dict = {}
        self._cache: dict[str, tuple] = {}

    @property
    def audit(self) -> AuditLog:
        if self._audit is None:
            self._audit = market_audit(DATA_DIR / "audit", self.market)
        return self._audit

    def summary_path(self) -> Path:
        """The market's rule-only decision baseline summary (scripts/baseline_krx.py --market): KRX at the data root
        as before the market axis, any other market under data/audit/<market>/."""
        return baseline_summary_path(DATA_DIR, self.market)

    def memory_log_path(self) -> Path | None:
        """The market's memory log, named by its baseline summary (``log_path``). TRADING_MEMORY_LOG stays the KRX
        override it was before the market axis."""
        env = os.environ.get("TRADING_MEMORY_LOG")
        if env and self.market == "KRX":
            return Path(env)
        summary = self.summary_path()
        if summary.exists():
            try:
                return Path(json.loads(summary.read_text(encoding="utf-8"))["log_path"])
            except (KeyError, ValueError):
                return None
        return None

    def refresh(self) -> _Store:
        files = {"summary": self.summary_path(), "memory": self.memory_log_path()}
        self._stamp = self.audit.stamp() | {k: p.stat().st_mtime_ns if p and p.exists() else None for k, p in files.items()}
        return self

    def _get(self, key: str, kinds: tuple[str, ...], load):
        """``load()`` once until one of ``kinds`` (record names, or summary / memory) changes."""
        stamp = tuple(self._stamp.get(k) for k in kinds)
        hit = self._cache.get(key)
        if hit is None or hit[0] != stamp:
            hit = self._cache[key] = (stamp, load())
        return hit[1]

    @property
    def screens(self) -> list[ScreenRun]:
        return self._get("screens", ("screens",), lambda: sorted(self.audit.screens(), key=lambda s: s.as_of, reverse=True))

    def name(self, ticker: str) -> str | None:
        """Korean name, else the name a screen recorded for the ticker."""
        names = self._get("names", ("screens",), lambda: {c.ticker: c.name for s in reversed(self.screens)
                                                           for c in s.candidates if c.name})
        return korean_name(ticker) or names.get(ticker)

    @property
    def orders(self) -> list[OrderRecord]:
        """Each order's current state (its latest row), newest first."""
        # an order that filled or was cancelled after it was placed has later rows; serve its current state
        return self._get("orders", ("orders",), lambda: sorted(self.audit.latest_orders_where().values(),
                                                                key=lambda o: (o.placed_at, o.order_id), reverse=True))

    def orders_where(self, **where) -> list[OrderRecord]:
        """``AuditLog.latest_orders_where(**where)``, newest first like ``orders`` — read for the call, not cached."""
        if not any(v is not None for v in where.values()):
            return self.orders
        return sorted(self.audit.latest_orders_where(**where).values(), key=lambda o: (o.placed_at, o.order_id), reverse=True)

    @property
    def order_facts(self) -> dict:
        return self._get("order_facts", ("orders",), self.audit.order_facts)

    @property
    def order_rows(self) -> list[OrderRecord]:
        """Every order row in record order: what the book replays (positions_view, book_events)."""
        return self._get("order_rows", ("orders",), self.audit.orders)

    @property
    def operations(self) -> list[OperationRecord]:
        return self._get("operations", ("operations",), self.audit.operations)

    @property
    def facts(self) -> dict:
        """Decision dates, tickers and counts by stored source, without reading the decisions."""
        return self._get("facts", ("decisions",), self.audit.decision_facts)

    def decisions(self, **where) -> list[DecisionRecord]:
        """``AuditLog.decisions_where(**where)``, newest day first, then ticker (as the screens list them)."""
        return sorted(self.audit.decisions_where(**where), key=lambda d: (d.trade_date, d.ticker), reverse=True)

    @property
    def memory(self) -> list[dict]:
        def load() -> list[dict]:
            log = self.memory_log_path()
            return TradingMemoryLog({"memory_log_path": str(log)}).load_entries() if log else []
        return self._get("memory", ("summary", "memory"), load)

    @property
    def summary(self) -> dict:
        path = self.summary_path()
        return self._get("summary", ("summary",), lambda: json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})

    def state(self) -> dict:
        return _effective(self.market, self.operations)


STORES = {m: _Store(m) for m in MARKETS}
PRICES = Prices(DATA_DIR / "cache")


def _market(value: str | None) -> str:
    """The ``market`` query or body value: a config.MARKETS key, or 400 (a typo must not read the KRX book)."""
    if value is None:
        return "KRX"
    if value not in MARKETS:
        raise HTTPException(400, f"market은 {', '.join(MARKETS)} 중 하나여야 합니다: {value!r}")
    return value


def _store(market: str | None) -> _Store:
    return STORES[_market(market)].refresh()


def _clock() -> datetime:
    """Now (UTC); tests set it."""
    return datetime.now(UTC)


def _effective(market: str, operations: list[OperationRecord]) -> dict:
    """The market's trading state now: a daily-loss halt has ended once the next trading day opened
    (execution.effective_state), whether or not the executor has written its resume line yet."""
    return effective_state(operations, PRICES.calendar(MARKETS[market]["benchmark"]), market, _clock())


MarketQuery = Query(None, description="KRX(기본) | US. 그 외 값은 400")


def _pct(text: str | None) -> float | None:
    """'+1.5%' in the memory log → 0.015."""
    if not text:
        return None
    try:
        return float(text.strip().rstrip("%")) / 100              # as backtest._alpha
    except ValueError:
        return None


def _outcome(entry: dict | None) -> dict | None:
    if entry is None:
        return None
    return {"pending": entry["pending"], "raw_return": _pct(entry.get("raw")), "alpha": _pct(entry.get("alpha")),
            "holding": entry.get("holding"), "resolved": entry.get("resolved"), "reflection": entry.get("reflection") or ""}


def _fill_at(o: OrderRecord) -> dict:
    """When the order filled (its fill day) or will (open: the trading day after its decision): that session's open in
    market time and in KST, as ISO — the zone database applies daylight saving, the screen only formats. None for an
    order that does not fill (blocked, rejected, cancelled, exception)."""
    if o.status in ("filled", "partial") and o.fill_date:
        day, planned = o.fill_date, False
    elif o.status == "open":
        day, planned = next_trading_day(PRICES.calendar(MARKETS[o.market]["benchmark"]), o.trade_date, o.market), True
    else:
        return {"fill_at_local": None, "fill_at_kst": None, "fill_planned": False}
    at = session_window(o.market, date.fromisoformat(day))[0]
    return {"fill_at_local": at.astimezone(ZoneInfo(MARKETS[o.market]["timezone"])).isoformat(),
            "fill_at_kst": at.isoformat(), "fill_planned": planned}


def _rerun_answer(o: OrderRecord, st: _Store) -> dict | None:
    """The decision that answered ``o``'s rerun (scripts/daily_loop.py ``answer_reruns``), for the closed exception's
    card to link to: the decide stage's marker names it (``pending_rerun_marker`` — also when the day's own decision
    was the answer, ``same_day``); without one, a decision whose ``settings.rerun_of`` is this order. None: not a
    rerun the loop closed."""
    if o.exception_action != "rerun" or not o.exception_resolved:
        return None
    if (m := pending_rerun_marker(st.operations, o)) is not None and (d := m.after.get("decision")):
        day, rating, same_day = d.get("trade_date"), d.get("rating"), bool(d.get("same_day"))
    else:
        answer = next((d for d in st.audit.decisions_where(ticker=o.ticker, trade_date=(o.trade_date, None))
                       if d.settings.get("rerun_of") == o.order_id), None)
        if answer is None:
            return None
        day, rating, same_day = answer.trade_date, answer.rating, False
    return {"ticker": o.ticker, "trade_date": day, "rating": rating, "rating_ko": RATING_KO.get(rating, rating),
            "attempt": o.reruns, "same_day": same_day}


def _order_out(o: OrderRecord, st: _Store) -> dict:
    out = o.model_dump()
    out.update(name=st.name(o.ticker), side_ko=SIDE_KO.get(o.side, o.side),
               status_ko=ORDER_STATUS_KO.get(o.status, o.status), amount=o.amount, rerun_answer=_rerun_answer(o, st),
               **_fill_at(o))
    return out


def _decision_out(d: DecisionRecord, st: _Store, memory_by_key: dict, orders_by_key: dict) -> dict:
    out = d.model_dump()
    out["name"] = st.name(d.ticker)
    out["rating_ko"] = RATING_KO.get(d.rating, d.rating)
    out["tradeable"] = d.tradeable
    out["source_ko"] = DECISION_SOURCE_KO.get(d.source, d.source)
    kind = decision_source(d)                    # the baseline's records are stored as agent
    out["source_kind"], out["source_kind_ko"] = kind, DECISION_SOURCE_KO.get(kind, kind)
    # the memory log scores the agent's call; a rule exit or a person's Hold on the same ticker and day is not that call
    out["outcome"] = _outcome(memory_by_key.get((d.ticker, d.trade_date))) if d.source == "agent" else None
    # an agent decision and a rule exit can share (ticker, day): each card shows its own order. A person's Hold
    # places none (the exception order it closed stays on the agent's card)
    rule = d.source == "rule"
    orders = [] if d.source == "human" else [o for o in orders_by_key.get((d.ticker, d.trade_date), [])
                                             if o.order_id.endswith(EXIT_SUFFIX) == rule]
    out["orders"] = [_order_out(o, st) for o in orders]
    # 하드룰 개입 tab: a rule cut or blocked one of its orders (OrderRecord.plan), or the agent listed one itself
    out["has_intervention"] = bool(d.hard_rule_overrides) or any(o.plan and o.plan.interventions for o in orders)
    return out


def _screen_summary(s: ScreenRun) -> dict:
    counts = stage_counts(s)
    return {"run_id": s.run_id, "as_of": s.as_of, "recorded_at": s.recorded_at, "universe": s.universe,
            "universe_size": s.universe_size, "selected_count": counts[-1] if counts else 0,
            "rules": s.rules, "rule_count": len(s.rules), "rules_ko": [RULE_KO.get(r, r) for r in s.rules], "stage_counts": counts}


def _orders_by_key(st: _Store, **where) -> dict:
    by: dict = {}
    for o in st.orders_where(**where):
        by.setdefault((o.ticker, o.trade_date), []).append(o)
    return by


def _state_out(market: str, st: dict | None = None) -> dict:
    st = st or STORES[market].refresh().state()
    mode_ko = MODE_KO.get(st["mode"], st["mode"])
    market_ko = MARKET_KO.get(market, market)
    by_ko = HALTED_BY_KO.get(st["halted_by"], st["halted_by"]) if st["halted_by"] else None
    target_ko = HALT_TARGET_KO.get(st.get("halt_target") or "", None)
    # a system halt names what stopped it (일일 손실 / 드로다운); a person's names the person
    who = target_ko if st["halted_by"] == "system" and target_ko else by_ko
    # two axes on the stored mode (docs/10 D4): account paper | live, execution auto | manual approval — the status
    # label names both ("국내 모의투자 · 자동 실행 중"), the stored mode stays the one value
    account = "paper" if st["mode"] == "paper" else "live"
    execution = "manual" if st["mode"] == "manual" else "auto"
    label = f"{market_ko} 정지됨 · {st['halted_at'][:16].replace('T', ' ')} UTC · {who}" if not st["running"] else (
        f"{market_ko} {ACCOUNT_MODE_KO[account]} · {EXECUTION_MODE_KO[execution]} 중")
    scope = st.get("halt_scope")
    # a daily-loss halt ends at 00:00 market time; trading starts at that day's session open, shown in KST
    opens = session_window(market, date.fromisoformat(st["auto_resume_at"][:10]))[0] if st.get("auto_resume_at") else None
    return st | {"halt_scope": scope, "halt_scope_ko": HALT_SCOPE_KO.get(scope, scope) if scope else None,
                 "auto_resume_open_kst": opens.isoformat() if opens else None,
                 "auto_resume_label_ko": f"{opens:%m-%d}({WEEKDAY_KO[opens.weekday()]}) {opens:%H:%M} KST" if opens else None,
                 "market": market, "market_ko": market_ko, "mode_ko": mode_ko, "label_ko": label, "halted_by_ko": by_ko,
                 "account_mode": account, "account_mode_ko": ACCOUNT_MODE_KO[account],
                 "execution_mode": execution, "execution_mode_ko": EXECUTION_MODE_KO[execution],
                 "halt_target_ko": target_ko, "admin_token_required": ADMIN_TOKEN is not None,
                 # a −20% stop stands (perhaps under a person's halt): the admin resume offers the peak reset
                 "dd_stop": not st["running"] and _dd_stop(market)}


def _dd_stop(market: str) -> bool:
    """Read from the operations log alone: a broken orders log (or an unreadable operations one) must not fail the
    state a halt returns."""
    try:
        return dd_stop_standing(STORES[market].audit.operations())
    except ValueError:
        return False


def _exception_kind(o: OrderRecord) -> str:
    if o.exception_kind:
        return o.exception_kind
    # records written before the execution layer tagged its own kind: inferred from the rule ids and the reason
    if any(r.startswith("auto_") for r in o.rules_applied):
        return "cap_exceeded"
    if "vendor" in o.reason.lower():
        return "vendor_unavailable"
    return "order_rejected"


REVIEW_ACTIONS = ("rerun", "hold", "dismiss")            # an unreadable rating (docs/10 D7)
ORDER_ACTIONS = ("retry_next", "dismiss_order")           # an order that did not go out: cap, vendor, LULD, rejected


def _allowed_actions(o: OrderRecord | None, kind: str) -> tuple[str, ...]:
    """A rule exit has no retry: the executor judges its signal again on the next close and places a new exit."""
    if kind == "review":
        return REVIEW_ACTIONS
    return ("dismiss_order",) if o is not None and o.order_id.endswith(EXIT_SUFFIX) else ORDER_ACTIONS
ACTION_LIMIT = 2                                          # reruns, and next-session retries, per exception


def _queue_state(o: OrderRecord | None, kind: str) -> dict:
    """What a person can do with one exception (POST /api/exceptions/{order_id}/action) and what is pending. No order
    (a REVIEW decision the executor has not run yet): nothing to act on."""
    if o is None:
        return {"order_id": None, "actions": [], "pending_action": None, "pending_action_ko": None, "reruns": 0, "retries": 0}
    pending = o.exception_action if o.exception_action in EXCEPTION_PENDING_KO and not o.exception_resolved else None
    used = {"rerun": o.reruns, "retry_next": o.retries}
    return {"order_id": o.order_id, "pending_action": pending, "pending_action_ko": EXCEPTION_PENDING_KO.get(pending or ""),
            "reruns": o.reruns, "retries": o.retries,
            "actions": [{"action": a, "label_ko": EXCEPTION_ACTION_KO[a], "used": used.get(a), "limit": ACTION_LIMIT if a in used else None,
                         "available": used.get(a, 0) < ACTION_LIMIT} for a in _allowed_actions(o, kind)]}


def _exceptions(st: _Store, date: str | None) -> list[dict]:
    """REVIEW decisions and orders that were refused: what a human must look at (docs/02 화면 3, 예외 큐). An exception
    a person closed (hold / dismiss — ``exception_resolved``) is off the queue; a pending rerun or retry stays on it."""
    items = []
    by_id = {o.order_id: o for o in st.orders_where(trade_date=date)}
    for d in st.decisions(source="agent", rating=RATING_REVIEW, trade_date=date):
        o = by_id.get(f"paper-{d.trade_date}-{d.ticker}")
        if o is not None and o.exception_resolved:
            continue
        items.append({"kind": "review", "kind_ko": EXCEPTION_KIND_KO["review"], "ticker": d.ticker,
                      "name": st.name(d.ticker), "trade_date": d.trade_date, "market": st.market,
                      "summary": "등급을 읽을 수 없어 주문하지 않았습니다", "detail": d.thesis, "ref": d.screen_run_id}
                     | _queue_state(o if o is not None and o.exception_kind == "review" else None, "review"))
    for o in st.orders_where(status="exception", trade_date=date) + st.orders_where(status="rejected", trade_date=date):
        # a REVIEW decision is listed above already; its exception order is the same item
        if (o.status in ("rejected", "exception") and o.exception_kind != "review" and not o.exception_resolved
                and (date is None or o.trade_date == date)):
            kind = _exception_kind(o)
            items.append({"kind": kind, "kind_ko": EXCEPTION_KIND_KO[kind], "ticker": o.ticker,
                          "name": st.name(o.ticker), "trade_date": o.trade_date, "market": st.market,
                          "summary": o.reason or ORDER_STATUS_KO[o.status], "detail": ", ".join(o.rules_applied), "ref": o.order_id}
                         | _queue_state(o, kind))
    return sorted(items, key=lambda x: (x["trade_date"], x["ticker"]), reverse=True)


def _flow(st: _Store, book: dict) -> dict:
    """Home flow cards (screen 1): the latest screen, the latest decision day by rating, that day's exception queue
    by kind, the holdings. Counted here so the screen shows numbers it does not add up itself (docs/02 §0)."""
    latest = st.screens[0] if st.screens else None
    day = st.facts["dates"][0] if st.facts["dates"] else None
    today = st.decisions(trade_date=day) if day else []
    # rule exits (stop-loss, trailing, …) are not agent judgments (docs/04 /api/decisions): counted apart
    by_rating = Counter(d.rating for d in today if d.source == "agent")
    rule = sum(d.source == "rule" for d in today)
    ratings = [r for r in [*RATINGS_5_TIER, RATING_REVIEW] if by_rating[r]]
    exc_day = _latest_date(st)
    by_kind = Counter(x["kind"] for x in _exceptions(st, exc_day))
    return {
        "screen": {"run_id": latest.run_id, "as_of": latest.as_of, "universe": latest.universe,
                   "universe_size": latest.universe_size, "selected_count": _screen_summary(latest)["selected_count"]}
        if latest else None,
        "decisions": {"date": day, "total": sum(by_rating.values()), "by_rating": {r: by_rating[r] for r in ratings},
                      "by_rating_ko": {RATING_KO.get(r, r): by_rating[r] for r in ratings}, "rule": rule},
        "exceptions": {"date": exc_day, "total": sum(by_kind.values()), "by_kind": dict(by_kind),
                       "by_kind_ko": {EXCEPTION_KIND_KO[k]: n for k, n in by_kind.items()}},
        "holdings": {"count": len(book["positions"]), "near_stop": book["near_stop_count"],
                     "protections_active": book["protections_active"]},
    }


def _latest_date(st: _Store) -> str | None:
    dates = [d for d in (st.facts["dates"][0] if st.facts["dates"] else None, st.order_facts["last_date"]) if d]
    return max(dates) if dates else None


def _date_param(value: str | None, name: str = "date") -> str | None:
    if value is None:
        return None
    try:
        return check_date(value)
    except ValueError:
        raise HTTPException(400, f"{name}는 YYYY-MM-DD 형식이어야 합니다: {value!r}") from None


def _require_admin(token: str | None) -> str:
    """Resume and mode change are admin-only (docs/02 화면 7). Returns the role to record."""
    if ADMIN_TOKEN is None:
        return "admin(dev, no token)"
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "관리자 토큰이 필요합니다 (X-Admin-Token)")
    return "admin"


# --- market hours: the session in the market's own time zone, shown in KST -------------------

def session_window(market: str, day: date) -> tuple[datetime, datetime]:
    """The session of ``day`` (the market's local date) as aware datetimes in KST: the calendar's hours that day
    (``market_calendar`` — a US half day closes 13:00, KRX's exam day runs 10:00-16:30), else the regular session.
    The zone database applies daylight saving (New York 09:30 is 22:30 KST in summer, 23:30 in winter)."""
    tz = ZoneInfo(MARKETS[market]["timezone"])
    start, end = market_calendar.session_open(market, day), market_calendar.session_close(market, day)
    return (datetime.combine(day, start, tz).astimezone(KST), datetime.combine(day, end, tz).astimezone(KST))


@cache
def _daily_loop():
    """scripts/daily_loop.py, loaded once: its schedule (``SCHEDULED`` · ``stage_time``) is what the scheduler runs."""
    return scheduler.daily_loop()


def next_run(market: str, now: datetime) -> tuple[datetime, str]:
    """The next stage the scheduler will start for ``market`` after ``now`` and its name (``scheduler.plan``: fill
    after the open, decide after the close, on the market's sessions — ``market_calendar``), in KST."""
    at, _, stage = scheduler.plan(_daily_loop(), [market], now, 1)[0]
    return at.astimezone(KST), stage


def _run_out(r: RunRecord) -> dict:
    return r.model_dump() | {"stage_ko": STAGE_KO.get(r.stage, r.stage), "status_ko": RUN_STATUS_KO.get(r.status, r.status)}


def _loop_status(market: str, now: datetime) -> dict:
    """The daily loop of ``market`` as the scheduler sees it: each scheduled stage's latest record, the next planned
    stage, and — a due stage whose last record failed — the retry the scheduler takes (``scheduler.next_wake``)."""
    dl = _daily_loop()
    runs = _store(market).audit.runs_where(last=200)
    last = {s: next((r for r in reversed(runs) if r.stage == s), None) for s in dl.SCHEDULED[market]}
    at, stage = next_run(market, now)
    wake = scheduler.next_wake(dl, [market], now, DATA_DIR).astimezone(KST)
    failed = [r for r in last.values() if r is not None and r.status == "failed"]
    status = "never" if not runs else "failed" if failed else "ok"
    return {"status": status, "status_ko": {"never": "실행 기록 없음", "failed": "실패", "ok": "정상"}[status],
            "last": {s: _run_out(r) if r else None for s, r in last.items()},
            "failed": [_run_out(r) for r in failed],
            "next": {"at_kst": at.isoformat(), "stage": stage, "stage_ko": STAGE_KO.get(stage, stage),
                     "label_ko": f"{at:%m-%d %H:%M} KST · {STAGE_KO.get(stage, stage)}"},
            "retry_at_kst": wake.isoformat() if wake < at else None}


def _refresh_lines() -> list[dict]:
    """scripts/refresh_cache.py's log (data/cache/_refresh_log.jsonl), oldest first; a cut-short line is skipped."""
    path = DATA_DIR / "cache" / REFRESH_LOG
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _refresh_out(lines: list[dict], market: str) -> dict | None:
    """The market's latest cache refresh: when, how many symbols, which failed, and the last refresh with none failed."""
    mine = [x for x in lines if x.get("market") == market]
    if not mine:
        return None
    last = mine[-1]
    failed = last.get("failed") or []
    ok = next((x for x in reversed(mine) if not x.get("failed")), None)
    return {"at": last.get("at"), "source": last.get("source"), "symbols": last.get("symbols"),
            "last_final_day": last.get("last_final_day"), "failed_count": len(failed),
            "failed_symbols": [f.get("symbol") for f in failed][:10], "failed_error": failed[0].get("error") if failed else None,
            "last_success_at": ok.get("at") if ok else None}


def _price_out(market: str) -> dict:
    """Where the market's prices come from: the benchmark index's file (``Prices.info``) — the calendar every replay
    and the executor follow."""
    info = PRICES.info(MARKETS[market]["benchmark"])
    return {"price_source": info["source"], "price_as_of": info["as_of"], "price_provisional": info["provisional"]}


def _key_set(name: str, env_file: dict) -> bool:
    return bool(os.environ.get(name) or env_file.get(name))


def sector_exposure(rows: list[dict], *, top: int = 3) -> list[dict]:
    """Held weight per sector (``universe.sector_of``, the sector file): the ``top`` largest, then one 미분류 row
    summing every holding the file gives no sector. Weights are of equity, as the position rows."""
    by: dict[str | None, float] = {}
    for r in rows:
        k = sector_of(r["ticker"])
        by[k] = by.get(k, 0.0) + (r.get("weight") or 0.0)
    known = sorted(((k, v) for k, v in by.items() if k is not None), key=lambda x: (-x[1], x[0]))[:top]
    out = [{"sector": k, "pct": v, "classified": True} for k, v in known]
    if by.get(None):
        out.append({"sector": "미분류", "pct": by[None], "classified": False})
    return out


def _market_labels(market: str, now: datetime | None) -> dict:
    """The market's fixed labels and today's session in KST (``now`` None: the session left out — the calendar failed)."""
    m = MARKETS[market]
    open_kst, close_kst = session_window(market, now.astimezone(ZoneInfo(m["timezone"])).date()) if now else (None, None)
    return {"market": market, "label_ko": MARKET_KO.get(market, market), "currency": m["currency"],
            "currency_symbol": "$" if m["currency"] == "USD" else "₩", "benchmark": m["benchmark"],
            "benchmark_name_ko": m["benchmark_name_ko"], "timezone": m["timezone"], "session": m["session"],
            "session_kst": f"{open_kst:%H:%M}-{close_kst:%H:%M}" if open_kst else None,
            "name_style": "name_ticker" if market == "US" else "name"}


# --- read endpoints ----------------------------------------------------------------------

@app.get("/api/health", tags=["meta"])
def health(market: str | None = MarketQuery) -> dict:
    """서버 · 장부 요약 + 일일 루프 상태(마지막 decide · fill, 다음 예정 — scheduler 계산 그대로) · 마지막 캐시 갱신 · 가격 출처."""
    st = _store(market)
    now = datetime.now(UTC)
    return {"ok": True, "core_version": core_version, "data_dir": str(DATA_DIR), "market": st.market,
            "market_ko": MARKET_KO.get(st.market, st.market), "markets": list(MARKETS), "screens": len(st.screens),
            "decisions": st.facts["total"],
            "decisions_by_source": {s: st.facts["by_source"].get(s, 0) for s in ("agent", "rule", "human")},
            "orders": st.order_facts["total"], "memory_entries": len(st.memory),
            "memory_log": str(st.memory_log_path() or ""), "server_time": now.isoformat(),
            "loop": _loop_status(st.market, now), "refresh": _refresh_out(_refresh_lines(), st.market)} | _price_out(st.market)


@app.get("/api/labels", tags=["meta"])
def labels() -> dict:
    """UI가 쓰는 한글 라벨. 데이터의 값(rating, rule id, status)은 영문으로 고정이고 표시만 여기로 바꾼다."""
    now = datetime.now(UTC)
    return {"ratings": list(RATINGS_5_TIER) + [RATING_REVIEW], "rating_ko": RATING_KO, "rule_ko": RULE_KO,
            "rule_desc_ko": RULE_DESC_KO, "sell_reason_ko": SELL_REASON_KO, "mode_ko": MODE_KO,
            "order_status_ko": ORDER_STATUS_KO, "exception_kind_ko": EXCEPTION_KIND_KO, "exit_reason_ko": EXIT_REASON_KO,
            "risk_rule_ko": RISK_RULE_KO, "decision_source_ko": DECISION_SOURCE_KO, "market_ko": MARKET_KO,
            "exception_action_ko": EXCEPTION_ACTION_KO, "auto_rule_ko": AUTO_RULE_KO,
            "halt_scope_ko": HALT_SCOPE_KO, "halt_target_ko": HALT_TARGET_KO, "book_event_ko": BOOK_EVENT_KO, "markets": {m: _market_labels(m, now) for m in MARKETS},
            # one convention for both markets (docs/08 §4 Q1): a person reading both books must not flip colours
            "color_convention": {"up": "red", "down": "blue",
                                 "note": "국내 관례로 통일(시장 무관): 빨강=상승·매수, 파랑=하락·매도"}}


CALENDAR_ERRORS = (ValueError, KeyError, TypeError)   # market_calendar on a broken data/calendar file


@app.get("/api/markets", tags=["meta"])
def markets() -> list[dict]:
    """홈 시장 요약 카드: 시장마다 평가액(그 시장 통화) · 오늘 손익 · 드로다운 · 운용 상태 · 다음 실행 시각(KST). 합산 없음.
    한 시장 기록이 파손돼도 다른 시장 카드는 나온다(그 카드만 error, 숫자는 null)."""
    now = datetime.now(UTC)
    out = []
    for m in MARKETS:
        try:
            run, stage = next_run(m, now)
            card = _market_labels(m, now) | {"next_run_kst": run.isoformat(), "next_run_stage": stage,
                                             "next_run_label_ko": f"{run:%m-%d %H:%M} KST · {STAGE_KO.get(stage, stage)}",
                                             "schedule_error": None}
        except CALENDAR_ERRORS as exc:              # a broken calendar file: this card says so, no weekday guess
            card = _market_labels(m, None) | {"next_run_kst": None, "next_run_stage": None, "next_run_label_ko": "일정 계산 오류",
                                              "schedule_error": f"{MARKET_KO[m]} 휴장 달력을 읽지 못함: {type(exc).__name__}: {exc}"[:300]}
        try:
            st = _store(m)
            book = positions_view(st.order_rows, PRICES, cfg=paper_config(m), operations=st.operations)
            card |= _price_out(m) | {"as_of": book["as_of"], "equity": book["equity"], "initial_cash": book["initial_cash"], "capital": book["capital"],
                     "day_return": book["day_return"], "drawdown": book["drawdown"], "positions": len(book["positions"]),
                     "state": _state_out(m), "flow": _flow(st, book), "error": None}
        except ValueError as exc:                    # a cut-short line, or another market's record, in that book
            card |= dict.fromkeys(("as_of", "equity", "initial_cash", "capital", "day_return", "drawdown", "positions", "state",
                                   "flow")) | {
                "error": f"{MARKET_KO[m]} 기록을 읽지 못함: {type(exc).__name__}"}
        out.append(card)
    return out


@app.get("/api/screens", tags=["screens"])
def list_screens(from_: str | None = Query(None, alias="from"), to: str | None = None,
                 universe: str | None = None, market: str | None = MarketQuery) -> list[dict]:
    st = _store(market)
    rows = st.screens
    if from_:
        rows = [s for s in rows if s.as_of >= from_]
    if to:
        rows = [s for s in rows if s.as_of <= to]
    if universe:
        rows = [s for s in rows if s.universe == universe]
    return [_screen_summary(s) for s in rows]


@app.get("/api/screens/{run_id}", tags=["screens"])
def get_screen(run_id: str, market: str | None = MarketQuery) -> dict:
    st = _store(market)
    s = next((s for s in st.screens if s.run_id == run_id), None)
    if s is None:
        raise HTTPException(404, f"선별 실행 {run_id!r} 없음 ({st.market})")
    out = s.model_dump()
    out.update(_screen_summary(s))
    for c in out["candidates"]:
        c["name"] = c.get("name") or st.name(c["ticker"])
        c["failed_rules"] = [f["rule"] for f in c["filters"] if not f["passed"]]
        c["failed_rules_ko"] = [RULE_KO.get(r, r) for r in c["failed_rules"]]
    return out


@app.get("/api/decisions/dates", tags=["decisions"])
def decision_dates(market: str | None = MarketQuery) -> list[str]:
    """결정일 목록(그 시장의 거래일, 내림차순). 미국 결정일은 미국 날짜다."""
    return _store(market).facts["dates"]


@app.get("/api/decisions", tags=["decisions"])
def list_decisions(date: str | None = None, rating: str | None = None, review_only: bool = False,
                   ticker: str | None = None, overrides_only: bool = False,
                   source: Literal["agent", "rule", "baseline", "human"] | None = None, market: str | None = MarketQuery) -> list[dict]:
    """overrides_only: 하드룰 개입 탭 — 룰이 주문을 줄이거나 막은 결정(주문의 plan.interventions)과 에이전트가 적은 개입만."""
    st = _store(market)
    if review_only:
        if rating not in (None, RATING_REVIEW):
            return []
        rating = RATING_REVIEW
    # the baseline's records are stored as agent (audit.decision_source): read the stored value, then tell them apart
    stored = {"baseline": "agent"}.get(source, source)
    t = ticker.upper() if ticker else None
    rows = st.decisions(trade_date=date, ticker=t, source=stored, rating=rating)
    if source:
        rows = [d for d in rows if decision_source(d) == source]
    memory_by_key = {(e["ticker"], e["date"]): e for e in st.memory}
    orders_by_key = _orders_by_key(st, trade_date=date, ticker=t)
    out = [_decision_out(d, st, memory_by_key, orders_by_key) for d in rows]
    return [d for d in out if d["has_intervention"]] if overrides_only else out


@app.get("/api/orders", tags=["execution"])
def list_orders(date: str | None = None, status: str | None = None, ticker: str | None = None,
                market: str | None = MarketQuery) -> list[dict]:
    """주문·체결 기록(화면 1 '오늘 자동 실행', 화면 3 실행 상태 띠). 주문마다 현재 상태 한 줄(미체결 → 체결·취소는 마지막 기록)."""
    _date_param(date)
    if status is not None and status not in ORDER_STATUS_KO:
        raise HTTPException(400, f"status는 {', '.join(ORDER_STATUS_KO)} 중 하나여야 합니다: {status!r}")
    st = _store(market)
    rows = st.orders_where(trade_date=date, status=status, ticker=ticker.upper() if ticker else None)
    return [_order_out(o, st) for o in rows]


@app.get("/api/exceptions", tags=["execution"])
def list_exceptions(date: str | None = None, all_dates: bool = False, market: str | None = MarketQuery) -> list[dict]:
    """예외 큐: REVIEW 결정 + 거부·예외 주문. date 없으면 최신 결정일(기록상 '오늘')만, all_dates=true면 전부."""
    _date_param(date)
    st = _store(market)
    return _exceptions(st, None if all_dates else (date or _latest_date(st)))


@app.get("/api/tickers", tags=["tickers"])
def list_tickers(market: str | None = MarketQuery) -> list[dict]:
    st = _store(market)
    tickers = sorted({c.ticker for s in st.screens for c in s.candidates} | set(st.facts["tickers"]))
    latest = st.screens[0] if st.screens else None
    out = []
    for t in tickers:
        cand = next((c for c in latest.candidates if c.ticker == t), None) if latest else None
        out.append({"ticker": t, "name": st.name(t),
                    "latest_selected": cand.selected if cand else None, "latest_rank": cand.rank if cand else None})
    return out


@app.get("/api/tickers/{ticker}", tags=["tickers"])
def get_ticker(ticker: str, market: str | None = MarketQuery) -> dict:
    st = _store(market)
    t = ticker.upper()
    memory_by_key = {(e["ticker"], e["date"]): e for e in st.memory}
    orders_by_key = _orders_by_key(st, ticker=t)
    screening = []
    for s in st.screens:
        c = next((c for c in s.candidates if c.ticker == t), None)
        if c is None:
            continue
        first_fail = next((f for f in c.filters if not f.passed), None)
        screening.append({"run_id": s.run_id, "as_of": s.as_of, "selected": c.selected, "rank": c.rank, "score": c.score,
                          "filters": [f.model_dump() | {"rule_ko": RULE_KO.get(f.rule, f.rule)} for f in c.filters],
                          "first_failed_rule": first_fail.rule if first_fail else None,
                          "first_failed_rule_ko": RULE_KO.get(first_fail.rule, first_fail.rule) if first_fail else None})
    decisions = [_decision_out(d, st, memory_by_key, orders_by_key) for d in st.decisions(ticker=t)]
    outcomes = [{"trade_date": e["date"], "rating": e["rating"], "rating_ko": RATING_KO.get(e["rating"], e["rating"]),
                 **_outcome(e)} for e in sorted(st.memory, key=lambda e: e["date"], reverse=True) if e["ticker"] == t]
    if not screening and not decisions and not outcomes:
        raise HTTPException(404, f"종목 {t!r} 기록 없음 ({st.market})")
    return {"ticker": t, "name": st.name(t), "market": st.market, "screening": screening, "decisions": decisions,
            "outcomes": outcomes, "orders": [_order_out(o, st) for o in st.orders_where(ticker=t)],
            "timeline": [{"date": o["trade_date"], "rating": o["rating"]} for o in reversed(outcomes)]}


@app.get("/api/performance", tags=["performance"])
def performance(market: str | None = MarketQuery, source: str = Query("all", pattern="^(agent|rule|all)$"),
                date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to")) -> dict:
    st = _store(market)
    lo, hi = _date_range(date_from, date_to)
    s = st.summary
    if not s:
        raise HTTPException(404, "백테스트 요약 없음: scripts/baseline_krx.py를 먼저 실행" if st.market == "KRX" else
                            f"{MARKET_KO.get(st.market, st.market)} 결정 성과 요약 없음: "
                            f"scripts/baseline_krx.py --market {st.market}를 먼저 실행")
    try:
        loop = _loop_scoring(st, source, lo, hi)
    except Exception as exc:  # the loop's own failure stays in its key; the baseline's scoring still answers
        loop = {"error": f"{type(exc).__name__}: {exc}"}
    direction = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
    resolved = [e for e in st.memory if not e["pending"] and _pct(e.get("alpha")) is not None]
    per_date = []
    for d in s.get("dates", []):
        rows = [e for e in resolved if e["date"] == d]
        longs = [_pct(e["alpha"]) for e in rows if direction.get(e["rating"], 0) > 0]
        shorts = [_pct(e["alpha"]) for e in rows if direction.get(e["rating"], 0) < 0]
        per_date.append({"date": d, "resolved": len(rows),
                         "long_mean_alpha": sum(longs) / len(longs) if longs else None,
                         "short_mean_alpha": sum(shorts) / len(shorts) if shorts else None})
    # a person's "보유로 처리" (source human, docs/10 D7) is not the agent's call: the memory log never has it, so it is
    # in no aggregate here; the scoring table shows it as its own count
    human = st.facts["by_source"].get("human", 0)
    reflections = [{"date": e["date"], "ticker": e["ticker"], "name": st.name(e["ticker"]),
                    "rating": e["rating"], "rating_ko": RATING_KO.get(e["rating"], e["rating"]),
                    "raw_return": _pct(e.get("raw")), "alpha": _pct(e.get("alpha")), "resolved": e.get("resolved"),
                    "reflection": e.get("reflection") or ""}
                   for e in sorted(st.memory, key=lambda e: e["date"], reverse=True) if not e["pending"] and e.get("reflection")]
    by_rating = {r: v | {"rating_ko": RATING_KO.get(r, r)} for r, v in s.get("by_rating", {}).items()}
    by_source = {k: v for k, v in st.facts["by_source"].items() if k != "human"}
    bars = _rating_bars(st.memory)
    return {"market": st.market, "loop": loop, "rating_bars": bars, "settlement": _settlement(st),
            "by_source": {"decisions": by_source, "human": human, "scored_source": "baseline",
                          "scored": sum(b["count"] for b in bars),
                          "note_ko": "채점(알파 · 적중률)은 규칙 베이스라인 실행의 메모리 로그에서만 나옵니다. 모의 루프의 "
                                     "agent · rule 결정은 기록 수만 셉니다 — 정산 경로가 아직 없습니다. 사람 처리는 채점 제외."},
            "run_id": s.get("run_id"), "cells_run": s.get("cells_run"), "resolved": s.get("resolved"),
            "pending": s.get("pending"), "unscored": s.get("unscored"), "holding": s.get("holding"),
            "dates": s.get("dates", []), "universe": s.get("universe", []), "by_rating": by_rating,
            "per_date": per_date, "reflections": reflections[:50], "human_handled": human,
            "caveat": "알파는 결정일 다음 첫 종가 진입 기준. 규칙 전용 run은 재현 가능하지만 에이전트 run은 모델 샘플링과 피드 변동으로 재현값이 아니다."}


RATING_ORDER = ("Buy", "Overweight", "Hold", "Underweight", "Sell")


def _rating_bars(memory: list[dict]) -> list[dict]:
    """등급별 막대: ``backtest.summarize``와 같은 대상(정산됨 · REVIEW 아님 · 알파 있음)과 같은 식 — 결정 수, 평균 알파,
    적중률(방향 있는 등급만: 알파 × 방향 > 0), 그리고 같은 보유 기간의 평균 실현 수익(raw)."""
    direction = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
    resolved = [e for e in memory if not e["pending"] and _pct(e.get("alpha")) is not None]   # REVIEW: not in RATING_ORDER
    out = []
    for r in RATING_ORDER:
        rows = [e for e in resolved if e["rating"] == r]
        alphas, raws = [_pct(e["alpha"]) for e in rows], [x for e in rows if (x := _pct(e.get("raw"))) is not None]
        d = direction[r]
        out.append({"rating": r, "rating_ko": RATING_KO.get(r, r), "count": len(rows),
                    "mean_alpha": sum(alphas) / len(alphas) if alphas else None,
                    "mean_raw": sum(raws) / len(raws) if raws else None,
                    "hit_rate": sum(a * d > 0 for a in alphas) / len(alphas) if d and alphas else None})
    return out


def _settlement(st: _Store) -> dict:
    """정산 실패: 정산 기간이 끝났는데 아직 대기인 결정을 사유별로 — 가격 파일 없음, 시세가 청산일 전에 끊김(상장 폐지 ·
    거래 정지 등). 정산 기간은 채점 경로(``memory.settlement.fetch_returns``)와 같다: 진입 = 결정일 다음 첫 거래일 종가,
    청산 = 그 뒤 ``holding`` 거래일째 종가(결정일 뒤 holding + 1번째 거래일, 벤치마크 달력). 청산일이 아직 안 온 것은
    가격 파일이 없어도 대기(실패 아님). REVIEW는 미채점으로 따로(``unscored``)."""
    calendar = PRICES.calendar(MARKETS[st.market]["benchmark"])
    reasons: dict[str, list[str]] = {"no_price": [], "price_ended": []}
    waiting = 0
    for e in st.memory:
        if not e["pending"] or e["rating"] == RATING_REVIEW:
            continue
        days = int(m.group(1)) if (m := re.match(r"(\d+)d$", e.get("holding") or "")) else 5
        i = bisect.bisect_right(calendar, e["date"]) + days      # entry calendar[i - days], exit holding sessions on
        due = calendar[i] if i < len(calendar) else None
        if due is None:                                           # the exit day has not come: still in its window
            waiting += 1
            continue
        info = PRICES.info(e["ticker"])
        if info["source"] is None:
            reasons["no_price"].append(e["ticker"])
        elif info["as_of"] is None or info["as_of"] < due:
            reasons["price_ended"].append(e["ticker"])
        else:
            waiting += 1                                      # priced through the window: the next settle run takes it
    rows = [{"id": k, "label_ko": SETTLEMENT_KO[k], "count": len(v), "tickers": sorted(set(v))[:20]}
            for k, v in reasons.items()]
    return {"failed": sum(r["count"] for r in rows), "by_reason": rows, "waiting": waiting}


# --- the performance series (6-A): the book replayed day by day, against the measured reproduction ---------------

def _reference_equity(m: str) -> tuple[pd.DataFrame | None, tuple | None, str | None]:
    """The reference variant's equity CSV (scripts/paper_backtest.py — paper-gate reads the same), its file stamp,
    and why it is missing."""
    path = DATA_DIR / PAPER_SUMMARY[m]
    try:
        variant = json.loads(path.read_text(encoding="utf-8"))["variants"][PAPER_REFERENCE_VARIANT]
        csv = path.parent / variant["equity_csv"]
        stamp = (path.stat().st_mtime_ns, csv.stat().st_mtime_ns)
        return pd.read_csv(csv, dtype={"Date": str}).set_index("Date").sort_index(), stamp, None
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return None, None, f"{PAPER_MEASUREMENT_KO[m]} 결과 없음({type(exc).__name__})"


def _series(st: _Store) -> tuple[dict, bool]:
    """The market's series, cached once per market until its orders or operations get a record, a price file it reads
    is rewritten or the reference CSV changes (memory only — no table, no file). Returns (series, cache hit)."""
    m, cfg = st.market, paper_config(st.market)
    rows = st.order_rows
    path = DATA_DIR / PAPER_SUMMARY[m]
    # the summary and the variants' equity CSVs beside it (a rerun of paper_backtest.py rewrites them)
    ref_key = tuple((f.name, f.stat().st_mtime_ns) for f in [path, *sorted(path.parent.glob("*equity*.csv"))] if f.exists())
    key = (st._stamp.get("orders"), st._stamp.get("operations"),
           PRICES.stamp([cfg.index] + [o.ticker for o in rows]), ref_key)
    hit = st._cache.get("series")
    if hit is not None and hit[0] == key:
        return hit[1], True
    book = book_series(rows, st.operations, PRICES, cfg)
    ref, _, ref_error = _reference_equity(m)
    base = None
    if ref is not None and book["start"] and len(ref):
        prior = ref[ref.index < book["start"]]
        base = float((prior.iloc[-1] if len(prior) else ref.iloc[0])["equity"])
    for r in book["rows"]:
        r["baseline_pct"] = (float(ref.at[r["date"], "equity"]) / base - 1
                             if base and ref is not None and r["date"] in ref.index else None)
    book["reference"] = {"variant": PAPER_REFERENCE_VARIANT, "variant_ko": VARIANT_KO[PAPER_REFERENCE_VARIANT],
                         "measurement_ko": PAPER_MEASUREMENT_KO[m], "covered_until": ref.index[-1] if ref is not None and len(ref) else None,
                         "error": ref_error}
    st._cache["series"] = (key, book)
    return book, False


# --- the daily loop's decisions, settled five sessions on (loop-scoring) --------------------------------------------
# The definition is scripts/llm_trial_report.py's — memory.settlement.fetch_returns over the KIS cache only: entry at
# the first close after the decision day, exit five of the stock's own sessions on (a holiday has no bar, so the
# calendar's closures are skipped), raw and alpha against the market's benchmark. A name without a KIS file has no
# prices here (pending), never Yahoo's. The scripts are not imported for it (tests check the two agree).

LOOP_HOLDING = 5                                       # llm_trial_report.HOLDING_DAYS
LOOP_DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
_settle_lock = threading.Lock()


def _kis_closes(symbol: str, start: str, end: str) -> pd.Series:
    """llm_trial_report.kis_closes: the KIS file's closes in [start, end), none for a name without a KIS file."""
    if PRICES.info(symbol)["source"] != "kis":
        return pd.Series(dtype=float)
    c = PRICES.frame(symbol)["Close"].dropna()
    c = c[(c.index >= start) & (c.index < end)]
    return pd.Series(c.to_numpy(dtype=float), index=pd.DatetimeIndex(c.index))


def _loop_settle(pairs: list[tuple[str, str]], benchmark: str) -> dict[tuple[str, str], tuple]:
    """(ticker, day) → (raw, alpha, None) settled, (None, None, reason) pending (``no_price`` · ``window``), or
    (None, None, "failed: <error>") when a price file cannot be read. ``fetch_returns`` reads its module's
    ``get_closes``; it is swapped for the KIS reader for the length of the call, one caller at a time.
    ponytail: the same swap as the report script — drop both once fetch_returns takes its price source as an argument
    (core)."""
    out: dict[tuple[str, str], tuple] = {}
    with _settle_lock:
        real = settlement.get_closes
        settlement.get_closes = _kis_closes
        try:
            for t, day in pairs:
                try:
                    raw, alpha, _, _ = settlement.fetch_returns(t, day, LOOP_HOLDING, benchmark=benchmark)
                except Exception as exc:  # one unreadable price file is reported, the rest still count (as the script)
                    out[(t, day)] = (None, None, f"failed: {type(exc).__name__}")
                    continue
                out[(t, day)] = (raw, alpha, None if raw is not None else _pending_reason(t, day, benchmark))
        finally:
            settlement.get_closes = real
    return out


def _pending_reason(ticker: str, day: str, benchmark: str) -> str:
    """Why fetch_returns gave nothing: no KIS file for the name (``no_price``) or the benchmark (``no_bench``); the
    benchmark has its five sessions but the name's bars stop before the benchmark's (``price_ended`` — delisted or
    halted); otherwise the five sessions have not passed yet (``window``)."""
    if PRICES.info(ticker)["source"] != "kis":
        return "no_price"
    if PRICES.info(benchmark)["source"] != "kis":
        return "no_bench"
    after = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    stock, bench = (_kis_closes(x, after, "9999-12-31") for x in (ticker, benchmark))
    if len(bench) > LOOP_HOLDING and (stock.empty or stock.index[-1] < bench.index[-1]):
        return "price_ended"
    return "window"


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _loop_column(src: str, decisions: list[DecisionRecord], settled: dict) -> dict:
    """One column of the comparison. ``settled`` · ``pending`` · ``buy_settled`` · ``mean_raw`` · ``mean_alpha`` ·
    ``alpha_by_rating`` are the report script's own figures (REVIEW and Hold settle too there; the overall mean is the
    buy side's). ``directional`` and ``hold`` are this screen's: a sell-side call scores the opposite sign, REVIEW is
    not scored, Hold is shown for reference."""
    rows = [(d, *settled[(d.ticker, d.trade_date)]) for d in decisions]
    done = [(d, raw, alpha) for d, raw, alpha, _ in rows if raw is not None]
    pending = Counter(r for _, raw, _, r in rows if raw is None)
    by_rating: dict[str, list[float]] = {}
    for d, _, a in done:
        by_rating.setdefault(d.rating, []).append(a)
    buy = [(raw, a) for d, raw, a in done if d.rating in ("Buy", "Overweight")]
    directed = [(LOOP_DIRECTION[d.rating], raw, a) for d, raw, a in done if LOOP_DIRECTION.get(d.rating)]
    hold = [a for d, _, a in done if d.rating == "Hold"]
    return {
        "source": src, "label_ko": LOOP_SOURCE_KO[src], "decisions": len(decisions),
        "ratings": dict(Counter(d.rating for d in decisions)),
        "review": sum(d.rating == RATING_REVIEW for d in decisions),
        "stand_ins": sum(bool(d.settings.get("fallback")) for d in decisions),
        "first": min((d.trade_date for d in decisions), default=None),
        "last": max((d.trade_date for d in decisions), default=None),
        "settled": len(done), "pending": sum(v for k, v in pending.items() if not k.startswith("failed")),
        "pending_by_reason": [{"id": k, "label_ko": LOOP_PENDING_KO[k], "count": pending.get(k, 0)} for k in LOOP_PENDING_KO],
        "settle_failed": [f"{d.ticker} {d.trade_date}: {r.split(': ', 1)[1]}" for d, raw, _, r in rows
                          if raw is None and r.startswith("failed")],
        "buy_settled": len(buy), "mean_raw": _mean([r for r, _ in buy]), "mean_alpha": _mean([a for _, a in buy]),
        "alpha_by_rating": {k: {"n": len(v), "mean_alpha": _mean(v)} for k, v in by_rating.items()},
        "directional": {"n": len(directed), "mean_return": _mean([k * r for k, r, _ in directed]),
                        "mean_alpha": _mean([k * a for k, _, a in directed]),
                        "hit_rate": _mean([float(k * a > 0) for k, _, a in directed])},
        "hold": {"n": len(hold), "mean_alpha": _mean(hold)},
    }


def _trial(st: _Store) -> dict | None:
    """The LLM trial as /api/model gives it (scripts/daily_loop ``trial_on`` · ``llm_trial_days``), None when off."""
    loop = _daily_loop()
    if not loop.trial_on():
        return None
    days, sessions = loop.llm_trial_days(st.audit, st.market), LLM.get("trial_sessions")
    return {"sessions": sessions, "days": days,
            "remaining": max(sessions - len(days), 0) if sessions is not None else None}


def _rule_calls(st: _Store, loop, decisions: list[DecisionRecord]) -> list[DecisionRecord | None]:
    """The rule baseline's call for each decision's ticker and day from that day's screen (``daily_loop.rule_record``,
    the report script's agreement), None without the screen. Kept per (run, ticker, day) until a screen is added."""
    screens = {r.run_id: r for r in st.screens}
    hit = st._cache.get("loop_rule")
    known = hit[1] if hit is not None and hit[0] == st._stamp.get("screens") else {}
    out = []
    for d in decisions:
        run = screens.get(d.screen_run_id)
        if run is not None and (k := (run.run_id, d.ticker, d.trade_date)) not in known:
            known[k] = loop.rule_record(st.market, run, d.ticker, d.trade_date)
        out.append(known[(run.run_id, d.ticker, d.trade_date)] if run is not None else None)
    st._cache["loop_rule"] = (st._stamp.get("screens"), known)
    return out


def _loop_scoring(st: _Store, source: str, lo: str | None, hi: str | None) -> dict:
    """The daily loop's decisions (``daily_<market>_`` screens) in [lo, hi]: the model's (``agent``: settings.decider =
    LLM["model"]) and, beside it, the rule baseline's call on the same ticker and day (``rule``: that day's screen
    through the rule, as the report script's agreement) — the two columns score the same names on the same days,
    never two periods. The rule's own records (before or after the trial, its stand-ins for the model) are counted
    apart, as are a person's decisions and the rule exits; none is scored. Settlements are cached per market until a
    decision or a price file changes."""
    loop = _daily_loop()
    prefix = f"{loop.RUN_IDS[st.market]}_"
    every = [d for d in st.audit.decisions_where(trade_date=(lo or "0000-00-00", hi or "9999-99-99"))
             if (d.screen_run_id or "").startswith(prefix) or d.source in ("human", "rule")]
    loop_ds = [d for d in every if (d.screen_run_id or "").startswith(prefix) and d.source != "human"]
    agent = [d for d in loop_ds if d.settings.get("decider") == LLM["model"]]
    paired = [(d, r) for d, r in zip(agent, _rule_calls(st, loop, agent)) if r is not None]
    groups = {"agent": agent, "rule": [r for _, r in paired]}
    recorded = [d for d in loop_ds if d.settings.get("decider") == BASELINE_DECIDER]
    bench = MARKETS[st.market]["benchmark"]
    pairs = sorted({(d.ticker, d.trade_date) for d in agent})
    # a settlement depends on the two price files and the day only: kept per (ticker, day) under the files' stamps
    # and today (fetch_returns leaves today's bar out), so a new decision settles alone (memory only — no table)
    today, files = datetime.now(UTC).date(), dict(PRICES.stamp([bench, *(t for t, _ in pairs)]))
    hit = st._cache.get("loop_settle")
    old = hit[1] if hit is not None and hit[0] == (today, files[bench]) else {}
    known = {x: v for x, (stamp, v) in old.items() if stamp == files.get(x[0])}
    missing = [x for x in pairs if x not in known]
    known |= _loop_settle(missing, bench) if missing else {}
    st._cache["loop_settle"] = ((today, files[bench]), {x: (files.get(x[0]), v) for x, v in known.items()})
    columns = {k: _loop_column(k, v, known) for k, v in groups.items() if source in (k, "all")}
    agree = sum(r.rating == d.rating for d, r in paired)
    stand_ins = [d for d in recorded if d.settings.get("fallback")]
    own = [d for d in recorded if not d.settings.get("fallback")]
    return {"market": st.market, "source": source, "from": lo, "to": hi, "holding_days": LOOP_HOLDING,
            "columns": columns, "trial": _trial(st),
            "agreement": {"compared": len(paired), "agree": agree, "rate": agree / len(paired) if paired else None,
                          "no_screen": len(agent) - len(paired)},
            "rule_recorded": {"decisions": len(own), "first": min((d.trade_date for d in own), default=None),
                              "last": max((d.trade_date for d in own), default=None), "stand_ins": len(stand_ins)},
            "human_excluded": sum(d.source == "human" for d in every),
            "rule_exits_excluded": sum(d.source == "rule" and not (d.screen_run_id or "").startswith(prefix) for d in every),
            "note_ko": "5일 정산 = 결정 다음 첫 종가에 진입해 종목 자신의 5거래일 뒤 종가(휴장일은 봉이 없어 건너뜀), "
                       "벤치마크 대비 알파, 가격은 KIS 캐시만(scripts/llm_trial_report.py와 같은 정의). 평균 수익 · "
                       "알파는 매수 쪽(매수 · 비중확대)만, 방향 수익 · 적중률은 매도 쪽 부호를 뒤집어 함께, 보유는 참고, "
                       "REVIEW는 미채점. 규칙 열은 모델과 같은 날 · 같은 종목을 그날 선별로 규칙이 판단한 결과(재계산)이고, "
                       "규칙이 직접 기록한 결정 · 모델 대신 규칙이 정한 결정 · 사람 처리 · 규칙 청산은 수만 셉니다. "
                       "베이스라인 채점과 섞지 않습니다."}


def _date_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    try:
        lo, hi = (check_date(d) if d else None for d in (date_from, date_to))
    except ValueError as exc:
        raise HTTPException(422, f"날짜 형식이 아닙니다(YYYY-MM-DD): {exc}") from exc
    if lo and hi and lo > hi:
        raise HTTPException(422, f"from {lo}이 to {hi}보다 늦습니다")
    return lo, hi


@app.get("/api/performance/loop", tags=["performance"])
def performance_loop(market: str | None = MarketQuery, source: str = Query("all", pattern="^(agent|rule|all)$"),
                     date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to")) -> dict:
    """모의 루프 채점(LLM vs 규칙) — `/api/performance`의 `loop`와 같다. 베이스라인 요약이 없어도 답한다."""
    st = _store(market)
    return _loop_scoring(st, source, *_date_range(date_from, date_to))


@app.get("/api/performance/series", tags=["performance"])
def performance_series(market: str | None = MarketQuery, date_from: str | None = Query(None, alias="from"),
                       date_to: str | None = Query(None, alias="to")) -> dict:
    """성과 6-A: 모의 장부의 일별 NAV(시간 가중 — 입출금은 성과가 아님) · 드로다운 · 노출 · 현금 비중(시작 자본 · 평가액
    대비), 같은 기간의 백테스트 비교선(2차 · 3차 실측의 규칙 + 국면 필터 변형, 모의 시작일 직전 평가액 기준 %).
    `from` · `to`는 보여 줄 범위만 자른다(% 기준은 모의 시작일 그대로). 장부 기록 · 가격 파일이 바뀌면 다시 계산."""
    st = _store(market)
    lo, hi = _date_range(date_from, date_to)
    book, cached = _series(st)
    rows = [r for r in book["rows"] if (lo is None or r["date"] >= lo) and (hi is None or r["date"] <= hi)]
    return {"market": st.market, "currency": MARKETS[st.market]["currency"], "start": book["start"],
            "as_of": book["as_of"], "from": lo, "to": hi, "initial_cash": book["initial_cash"], "rows": rows,
            "reference": book["reference"], "cache": "hit" if cached else "miss", **_price_out(st.market),
            "note_ko": "NAV는 입출금을 뺀 시간 가중 수익(시작 자본 대비), 드로다운은 그 NAV의 누적 최고 대비, 노출 = 보유 평가액 "
                       "/ 평가액. 비교선은 같은 규칙을 과거 데이터로 돌린 재현값(참고 — 판정에 안 씀)."}


@app.get("/api/paper", tags=["performance"])
def paper(market: str | None = MarketQuery) -> dict:
    """포트폴리오 실측 요약(하드 룰 끔/켬/국면 필터) + 모의투자 통과 기준. 국내 = 2차(results/paper), 미국 = 3차
    (results/paper_us). scripts/paper_backtest.py [--market US] 산출물."""
    m = _market(market)
    path = DATA_DIR / PAPER_SUMMARY[m]
    if not path.exists():
        hint = "" if m == "KRX" else " --market US"
        raise HTTPException(404, f"{MARKET_KO.get(m, m)} 포트폴리오 실측 결과 없음: scripts/paper_backtest.py{hint}를 먼저 실행")
    s = json.loads(path.read_text(encoding="utf-8"))
    for key, v in s["variants"].items():
        events = v.pop("events", [])
        v["label_ko"] = VARIANT_KO.get(key, key)
        v["event_counts"] = dict(Counter(e["kind"] for e in events))
        v["events_tail"] = events[-40:]
        v["exits_by_reason_ko"] = {EXIT_REASON_KO.get(k, k): n for k, n in v.get("exits_by_reason", {}).items()}
    s["market"] = s.get("market", m)
    s["currency"] = s.get("currency", MARKETS[m]["currency"])     # the KRX 2nd summary predates both keys
    s["criteria"] = [{"id": k, "label_ko": PAPER_CRITERIA_KO.get(k, k), "value": v} for k, v in PAPER_TRADING.items()]
    return s


def _threshold_ko(key: str, v: float) -> str:
    return {"min_weeks": f"≥ {v}주", "max_drawdown": f"> {v:+.0%}".replace("-", "−"), "max_daily_loss_breaches": f"≤ {v}회",
            "max_stop_outs": f"≤ {v}회" if v else "0회", "max_review_rate": f"≤ {v:.0%}",
            "max_unexplained_orders": f"≤ {v}건" if v else "0건"}[key]


def _progress(st: _Store, expected: bool = True) -> dict:
    """The market's paper-trading gate (execution.paper_progress) with its labels, and the reference table
    (``expected=False`` for the mode gate: the reference never reaches the verdict, not even by failing)."""
    m = st.market
    start = paper_start(st.orders, st.operations, m)
    p = paper_progress(st.orders, st.decisions(trade_date=(start, None)) if start else [], st.operations, PRICES,
                       paper_config(m))
    for c in p["criteria"]:
        c |= {"label_ko": PAPER_CRITERIA_KO.get(c["id"], c["id"]), "threshold_ko": _threshold_ko(c["id"], c["threshold"]),
              "status_ko": PAPER_STATUS_KO[c["status"]]}
    for v in p["violations"]:
        v |= {"kind_ko": PAPER_VIOLATION_KO[v["kind"]], "name": st.name(v["ticker"]) if v["ticker"] else None}
    for n in p["notes"]:
        n |= {"kind_ko": PAPER_NOTE_KO[n["kind"]], "name": st.name(n["ticker"]) if n["ticker"] else None}
    c = p["counts"]
    named = {s: ", ".join(PAPER_CRITERIA_KO[x["id"]] for x in p["criteria"] if x["status"] == s) for s in ("unmet", "in_progress")}
    p["summary_ko"] = " · ".join([f"충족 {c['met']}"] + [f"{PAPER_STATUS_KO[s]} {c[s]}" + (f" ({named[s]})" if c[s] else "")
                                                        for s in ("unmet", "in_progress")])
    p["verdict_ko"] = "전환 가능" if p["eligible"] else "전환 불가"
    p["weeks_badge_ko"] = f"{min(p['weeks'] or 0, p['min_weeks'])}/{p['min_weeks']}주"
    p |= {"market_ko": MARKET_KO.get(m, m), "currency": MARKETS[m]["currency"], "timezone": MARKETS[m]["timezone"],
          "benchmark_name_ko": MARKETS[m]["benchmark_name_ko"],
          "limits": {"max_drawdown": PAPER_TRADING["max_drawdown"], "dd_halve": RiskConfig().dd_halve,
                     "dd_stop": RiskConfig().dd_stop}}
    caps = auto_execution(m)
    # the live-switch modal's "rules at this capital": the live capital the admin set (docs/10 D5, POST /api/capital),
    # else the paper capital until one is set
    live = capital_state(st.operations, m)["live"]["amount"]
    capital = live if live is not None else p["capital"]
    p["go_live"] = {"capital": capital, "capital_source": "live" if live is not None else "paper",
                    "capital_source_ko": "실거래 자본(설정)" if live is not None else "모의 자본(실거래 자본 미설정 — 설정에서 입력)",
                    "auto_order_max": caps["auto_order_max"], "auto_daily_order_cap": caps["auto_daily_order_cap"],
                    "daily_loss_limit": caps["auto_daily_loss_limit"],
                    "daily_loss_amount": capital * caps["auto_daily_loss_limit"],
                    "other_markets_ko": [MARKET_KO[x] for x in MARKETS if x != m],
                    "note_ko": "실주문 경로는 아직 없습니다: 전환해도 실행기는 모의투자 모드에서만 주문하므로 이 시장은 주문을 내지 "
                               "않습니다(ExecutionRefused). 증권사 연동은 이후 단계입니다."}
    p["expected"] = _expected(m, p) if expected else None
    return p


def _expected(m: str, p: dict) -> dict | None:
    """Screen 8 기대 대비 (docs/10 D6): reference only — never read by ``eligible`` or the mode gate."""
    if not p["started"]:
        return None
    head = {"reference_only": True, "measurement_ko": PAPER_MEASUREMENT_KO[m],
            "variant_ko": VARIANT_KO[PAPER_REFERENCE_VARIANT]}
    path = DATA_DIR / PAPER_SUMMARY[m]
    try:
        variant = json.loads(path.read_text(encoding="utf-8"))["variants"][PAPER_REFERENCE_VARIANT]
        equity = pd.read_csv(path.parent / variant["equity_csv"], dtype={"Date": str}).set_index("Date")
        return head | paper_expected(p["paper"], p["start"], p["as_of"], equity, variant.get("events", []),
                                     MARKETS[m]["benchmark_name_ko"])
    except (OSError, KeyError, ValueError, TypeError, StopIteration) as exc:   # a malformed result is the table's error
        return head | {"start": p["start"], "end": p["as_of"], "rows": [], "covered_until": None,
                       "error": f"{PAPER_MEASUREMENT_KO[m]} 결과를 읽지 못함({type(exc).__name__}): "
                                f"scripts/paper_backtest.py{'' if m == 'KRX' else ' --market US'}"}


@app.get("/api/paper/progress", tags=["performance"])
def paper_gate(market: str | None = MarketQuery) -> dict:
    """화면 8 모의투자 검증: 그 시장 모의 운용의 시작일 · 경과 주 · 통과 기준 6개(PAPER_TRADING) 현재값과 판정 · 위반 목록 ·
    참고 목록 · 드로다운 시계열 · 기대 대비(참고, 판정에 안 씀). 판정은 서버만 한다(실거래 전환 API가 같은 함수로 검사).
    기록이 없으면 started=false(404 아님)."""
    return _progress(_store(market))


@app.get("/api/settings/rules", tags=["settings"])
def rules(market: str | None = MarketQuery) -> dict:
    """규칙 레지스트리(선별 + 하드 룰 + 자동 실행), 시장 상수(두 시장), 운용 모드, 모의투자 기준, 변경 이력(두 시장 운영 기록)."""
    st = _store(market)
    cfg = ScreenConfig()
    screen_rules = [{"id": r, "label_ko": RULE_KO.get(r, r), "description_ko": RULE_DESC_KO.get(r, ""), "layer": "screen",
                     "layer_ko": "선별"} for r in cfg.rules]
    risk_rules = [{"id": k, "label_ko": RISK_RULE_KO.get(k, k), "value": v, "layer": "hard_rule", "layer_ko": "하드룰"}
                  for k, v in asdict(RiskConfig()).items()]
    currency = MARKETS[st.market]["currency"]
    auto_rules = [{"id": k, "label_ko": AUTO_RULE_KO.get(k, k), "value": v, "layer": "auto_execution", "layer_ko": "자동 실행",
                   "currency": currency if k in AUTO_EXECUTION["by_market"][st.market] else None}
                  for k, v in auto_execution(st.market).items()]
    criteria = [{"id": k, "label_ko": PAPER_CRITERIA_KO.get(k, k), "value": v} for k, v in PAPER_TRADING.items()]
    # both markets' logs, newest first; within one second the log order decides (at has second resolution).
    # The other market's broken log leaves its rows out (history_errors) instead of failing this market's settings.
    ops, history_errors = [], []
    for m in MARKETS:
        try:
            ops += [(op.at, i, op) for i, op in enumerate(_store(m).operations)]
        except ValueError as exc:
            if m == st.market:
                raise
            history_errors.append(f"{MARKET_KO[m]} 운영 기록을 읽지 못함: {type(exc).__name__}")
    history = [op.model_dump() | {"kind_ko": OPERATION_KO.get(op.kind, op.kind), "market_ko": MARKET_KO.get(op.market, op.market)}
               for _, _, op in sorted(ops, key=lambda x: (x[0], x[1]), reverse=True)]
    by_market = {"KRX": cfg, "US": us_preset(cfg)}       # the US screen differs by its preset only (screening.us_preset)
    return {"screen_rules": screen_rules, "screen_params": {k: v for k, v in vars(cfg).items() if k != "rules"},
            "screen_params_by_market": {m: {k: v for k, v in vars(c).items() if k != "rules"} for m, c in by_market.items()},
            "regime_index": {m: {"benchmark": MARKETS[m]["benchmark"], "name_ko": MARKETS[m]["benchmark_name_ko"]}
                             for m in MARKETS},
            "risk_rules": risk_rules, "auto_rules": auto_rules,
            "auto_by_market": {m: auto_execution(m) for m in MARKETS}, "paper_criteria": criteria,
            "market": MARKETS[st.market], "market_code": st.market, "markets": MARKETS,
            "mode": _state_out(st.market), "change_history": history, "history_errors": history_errors}


@app.get("/api/positions", tags=["positions"])
def positions(as_of: str | None = None, market: str | None = MarketQuery) -> dict:
    """모의투자 장부(화면 5): 그 시장의 orders.jsonl 체결 기록을 캐시 종가로 재생한 보유·평가·드로다운·가드 상태. 저장된 상태 없음."""
    _date_param(as_of, "as_of")
    st = _store(market)
    out = positions_view(st.order_rows, PRICES, as_of=as_of, cfg=paper_config(st.market), operations=st.operations)
    for r in out["positions"] + out["closed"]:
        r["name"] = st.name(r["ticker"])
    for g in out["protections"]:
        g["label_ko"] = RISK_RULE_KO.get(g["id"], g["id"])
    out["exits_by_reason_ko"] = {EXIT_REASON_KO.get(k, k): n for k, n in out["exits_by_reason"].items()}
    out["mode"] = _state_out(st.market)
    out["sector_exposure"] = sector_exposure(out["positions"])
    out["limits"]["max_sector_pct"] = RiskConfig().max_sector_pct          # the executor's sizing cap (size_targets)
    out |= _price_out(st.market)
    # a holding priced from another file than the market's own (KIS missing for it: the Yahoo file)
    out["price_fallback"] = sorted(r["ticker"] for r in out["positions"] if PRICES.info(r["ticker"])["source"] != out["price_source"])
    return out


@app.get("/api/runs", tags=["meta"])
def list_runs(limit: int = Query(50, ge=1, le=500), stage: Literal["decide", "fill"] | None = None,
              market: str | None = MarketQuery) -> list[dict]:
    """일일 루프 단계 기록(run_records), 최신순. 단계마다 상태 · 사유 · 오류(자격 증명 없음) · 카운트."""
    return [_run_out(r) for r in reversed(_store(market).audit.runs_where(stage=stage, last=limit))]


FALLBACK_PREFIX = ("가격 Yahoo 폴백", "가격 폴백 확인 실패")   # scripts/daily_loop.fallback_notes · done()


def _fallback(m: str) -> dict | None:
    """The market's last daily-loop run and its price fallback lines (``RunRecord.notes``); None when that run had none."""
    try:
        last = STORES[m].refresh().audit.runs_where(last=1)
    except (OSError, ValueError):
        return None
    if not last:
        return None
    r = last[-1]
    notes = [n for n in r.notes if n.startswith(FALLBACK_PREFIX)]
    return {"stage": r.stage, "trade_date": r.trade_date, "ended_at": r.ended_at, "notes": notes} if notes else None


@app.get("/api/data/status", tags=["meta"])
def data_status() -> dict:
    """설정 '데이터 · 캐시 상태': 캐시 파일 수 · 크기(출처별), 시장별 마지막 갱신, 벤더별 키 유무(값은 절대 내보내지 않음).
    벤더 도달은 네트워크를 부르지 않고 마지막 갱신 기록으로 판단한다(갱신 기록이 없는 벤더는 '기록 없음')."""
    from dotenv import dotenv_values

    cache_dir = DATA_DIR / "cache"
    files = {"kis": list(cache_dir.glob("*-KIS-data.csv")), "yahoo": list(cache_dir.glob("*-YFin-data.csv"))}
    cache = {k: {"files": len(v), "kb": round(sum(f.stat().st_size for f in v) / 1024)} for k, v in files.items()}
    lines = _refresh_lines()
    env_path = Path(os.environ.get("TRADING_ENV_FILE") or ROOT / ".env")
    env_file = dotenv_values(env_path) if env_path.exists() else {}
    vendors = []
    for v in VENDORS:
        keys = {k: _key_set(k, env_file) for k in v["keys"]}
        runs = [x for x in lines if x.get("source") == v["cache"]] if v["cache"] else []
        if v["keys"] and not all(keys.values()):
            state = "no_key"
        elif not runs:
            state = "unknown"
        else:
            last = runs[-1]
            state = "reachable" if (last.get("symbols") or 0) > len(last.get("failed") or []) else "unreachable"
        vendors.append({"id": v["id"], "label_ko": v["label_ko"], "use_ko": v["use_ko"], "key_required": bool(v["keys"]),
                        "keys": keys, "state": state, "state_ko": {"reachable": "도달 가능", "unreachable": "도달 실패",
                                                                  "no_key": "키 없음", "unknown": "기록 없음"}[state],
                        "last_refresh_at": runs[-1].get("at") if runs else None})
    return {"cache": cache, "refresh": {m: _refresh_out(lines, m) for m in MARKETS}, "vendors": vendors,
            "price_source": {m: _price_out(m) for m in MARKETS}, "price_fallback": {m: _fallback(m) for m in MARKETS},
            "note": "키는 있는지만 보여 줍니다. 값은 서버 환경 변수(.env)에만 있고 화면 · 응답에 나오지 않습니다."}


@app.get("/api/events", tags=["positions"])
def events(market: str | None = MarketQuery, limit: int = Query(50, ge=1, le=500)) -> dict:
    """가드·잠금 이력(화면 5): 그 시장의 주문·운영 기록에서 모은 가드 · 잠금 · Protections · 개입 · 피크 재설정, 최신순.
    저장된 이력은 없다(execution.book_events)."""
    st = _store(market)
    rows = book_events(st.order_rows, st.operations, PRICES, cfg=paper_config(st.market))
    for e in rows:
        e["kind_ko"] = BOOK_EVENT_KO[e["kind"]]
        e["name"] = st.name(e["ticker"]) if e["ticker"] else None
    return {"market": st.market, "total": len(rows), "counts": dict(Counter(e["kind"] for e in rows)), "events": rows[:limit]}


# --- write endpoints: the three the plan allows, each appended to the operations log ------

@app.get("/api/trading/mode", tags=["trading"])
def get_mode(market: str | None = MarketQuery) -> dict:
    """그 시장의 운용 모드와 실행 상태. 운영 기록(operations.jsonl)을 재생해서 만들고 따로 저장하지 않는다."""
    return _state_out(_market(market))


def _scope_markets(scope: str) -> list[str]:
    if scope == "all":
        return list(MARKETS)
    if scope not in MARKETS:
        raise HTTPException(400, f"scope는 all, {', '.join(MARKETS)} 중 하나여야 합니다: {scope!r}")
    return [scope]


def _locks(markets: list[str]) -> ExitStack:
    """Every named market's execution lock, always in config.MARKETS order so two scoped requests cannot deadlock.
    An executor holds its own market's lock only."""
    stack = ExitStack()
    for m in MARKETS:
        if m in markets:
            stack.enter_context(execution_lock(STORES[m].audit.directory))
    return stack


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HaltRequest(BaseModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="감사 로그에 남는다")
    keep_exits: bool = Field(default=True, description="손절·트레일링 청산은 계속 작동")
    scope: str = Field(default="all", description="all(기본, 모든 시장) | KRX | US")


@app.post("/api/trading/halt", tags=["trading"])
def halt(body: HaltRequest) -> dict:
    """긴급 정지(운영자 이상). 신규 주문 중단, 미체결 주문 취소, 보유 포지션 유지. 다시 켜기는 /resume(관리자).
    scope=all이면 범위 안 모든 시장 운영 기록에 같은 시각·같은 사유로 정지 한 줄씩. 이미 정지된 시장은 건너뛰되,
    청산이 계속 도는 정지(keep_exits=true)에 keep_exits=false가 오면 한 줄 더 써서 청산까지 멈춘다."""
    # A human must always be able to stop the loop: one market's broken log never blocks another market's halt,
    # an unreadable operations log still gets its halt line (the executor cannot read it either and stops, exit 4),
    # and a broken orders log only skips the cancellations — the halt is recorded either way.
    markets = _scope_markets(body.scope)
    results, skipped = [], []
    with _locks(markets):                            # no order lands between this check and the halt record
        audits = {m: STORES[m].audit for m in markets}
        states: dict[str, dict | None] = {}
        for m in markets:
            try:
                states[m] = _effective(m, audits[m].operations())
            except ValueError:                       # a cut-short line, or another market's record, in operations.jsonl
                states[m] = None
        todo = []
        for m, s in states.items():
            if s is None or s["running"] or (s["keep_exits"] and not body.keep_exits):
                todo.append(m)
            else:
                skipped.append({"market": m, "keep_exits": s["keep_exits"],
                                "reason": f"이미 정지된 상태 ({s['halted_at']}, {s['halted_by']})"})
        if not todo:
            raise HTTPException(409, "이미 정지된 상태입니다 — " + "; ".join(f"{MARKET_KO[x['market']]}: {x['reason']}" for x in skipped))
        at = _now()
        for m in todo:
            audit, before = audits[m], states[m]
            errors = [] if before is not None else ["operations.jsonl을 읽지 못함(정지는 기록)"]
            try:
                pending = open_orders(audit, keep_sells=body.keep_exits)
            except ValueError as exc:                # a cut-short line or an invalid record in orders.jsonl
                pending = None
                errors.append(f"orders.jsonl을 읽지 못해 미체결 취소를 건너뜀: {type(exc).__name__}")
            error = "; ".join(errors) or None
            # halt first: while halted the executor fills nothing, so the cancellations after it are safe
            op = OperationRecord(
                kind="halt", at=at, operator=body.operator, role="operator", market=m, scope=body.scope,
                reason=body.reason + (f" ({error})" if error else ""), target="manual",
                # an unreadable log: the state before is unknown, and an append-only line must not claim it
                before={"running": None, "unreadable": True} if before is None
                else {"running": True} if before["running"] else {"running": False, "keep_exits": True},
                after={"running": False, "keep_exits": body.keep_exits,
                       "open_orders_cancelled": None if pending is None else [o.order_id for o in pending]})
            audit.record_operation(op)
            cancelled = cancel_orders(audit, pending or [], f"긴급 정지 ({body.operator}: {body.reason})")
            try:
                st = _effective(m, audit.operations())
            except ValueError:
                st = _effective(m, [op])             # mode unknown: the log is unreadable
            kept = "" if not body.keep_exits else " 청산(매도) 미체결은 유지합니다."
            results.append({"market": m, "state": _state_out(m, st), "error": error,
                            "effects": {"new_orders": "stopped", "open_orders_cancelled": None if pending is None else len(cancelled),
                                        "positions": "kept", "exits_active": body.keep_exits,
                                        "note": error or f"{MARKET_KO[m]} 모의투자: 미체결 주문 {len(cancelled)}건을 취소 기록했습니다"
                                                         f"(orders.jsonl).{kept} 실제 증권사 주문은 없습니다"}})
    return {"scope": body.scope, "scope_ko": HALT_SCOPE_KO.get(body.scope, body.scope), "results": results, "skipped": skipped}


def _peak_reset(st: _Store) -> dict:
    """What a resume after a −20% stop resets the peak to: the book's equity at the last close. The preview and the
    resume both call this, so the preview is the resume's own number."""
    book = positions_view(st.order_rows, PRICES, cfg=paper_config(st.market), operations=st.operations)
    return {"to": book["equity"], "at": book["as_of"], "before": book["peak"]}


RESUME_VARIANT_KO = {"running": "실행 중", "manual": "사람 정지", "daily_loss": "일일 손실 정지(자동 해제)", "dd_stop": "−20% 정지"}


@app.get("/api/trading/resume-preview", tags=["trading"])
def resume_preview(market: str | None = MarketQuery,
                   reset_peak: bool = Query(True, description="−20% 정지 뒤 피크를 지금 평가액으로 재설정할 때의 기준")) -> dict:
    """재가동 모달(읽기 전용): 그 시장의 정지 변형과, −20% 정지면 재가동 뒤 드로다운 기준 3줄(피크 · 반감 · 정지 금액,
    지금 평가액 ÷ 그 금액 − 1). 재설정 금액은 재가동이 쓰는 값과 같은 계산(`_peak_reset`). 일일 손실 정지는
    `can_resume` false — 다음 거래일 자동 해제를 기다린다."""
    st = _store(market)
    state = st.state()
    dd = not state["running"] and dd_stop_standing(st.operations)
    variant = ("running" if state["running"] else "daily_loss" if state["auto_resume_at"] else
               "dd_stop" if dd else "manual")
    out = {"market": st.market, "variant": variant, "variant_ko": RESUME_VARIANT_KO[variant],
           "can_resume": variant in ("manual", "dd_stop"), "reset_peak": reset_peak and dd, "equity": None,
           "as_of": None, "peak_before": None, "base": None, "restops": False, "lines": []}
    if not dd:
        return out
    r, risk = _peak_reset(st), RiskConfig()
    base = r["to"] if reset_peak else r["before"]
    lines = [("peak", 0.0, "피크 · DD 0%"), ("dd_halve", risk.dd_halve, f"반감 {risk.dd_halve:+.0%}"),
             ("dd_stop", risk.dd_stop, f"정지 {risk.dd_stop:+.0%}")]
    return out | {"equity": r["to"], "as_of": r["at"], "peak_before": r["before"], "base": base,
                  # without the reset the old peak still reads −20%: the next run halts again
                  "restops": drawdown_scale(r["to"], base, risk)[0] == 0.0,
                  "lines": [{"id": k, "label_ko": label.replace("-", "−"), "level": lv, "amount": base * (1 + lv),
                             "vs_equity": r["to"] / (base * (1 + lv)) - 1 if base > 0 else None}
                            for k, lv, label in lines]}


class ResumeRequest(BaseModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    scope: str = Field(default="all", description="all(기본, 정지된 모든 시장) | KRX | US")
    reset_peak: bool = Field(default=True, description="−20% 드로다운 정지 뒤에만 의미: 드로다운 피크를 지금 평가액으로 재설정. "
                                                        "false면 피크를 그대로 둔다(드로다운이 여전히 −20%면 다음 실행에서 다시 정지)")


@app.post("/api/trading/resume", tags=["trading"])
def resume(body: ResumeRequest, x_admin_token: str | None = Header(default=None)) -> dict:
    """재가동. 관리자만(kill_switch_reset = admin). scope=all이면 정지된 모든 시장, 시장 이름이면 그 시장만 — 다른 시장은 그대로.
    일일 손실 자동 정지(target daily_loss)는 재가동하지 않는다: 다음 거래일 개장에 스스로 풀린다(docs/10 D10). 그 시장은
    skipped에 auto_resume_at과 함께 남고, 재가동된 시장이 하나도 없으면 409(detail에 auto_resume_at)."""
    role = _require_admin(x_admin_token)
    markets = _scope_markets(body.scope)
    resumed, skipped, peak_resets, waiting = [], [], {}, []
    with _locks(markets):
        at = _now()
        for m in markets:
            st = STORES[m].refresh()
            state = st.state()
            if state["running"]:
                skipped.append({"market": m, "reason": "이미 실행 중"})
                continue
            # a daily-loss halt stands until the next session, even under a person's halt over it (D2: no early restart)
            dl = [h for h in standing_halts(st.operations) if h.operator == "system" and h.target == "daily_loss"]
            early = _effective(m, dl[-1:]) if dl else None
            if early is not None and not early["running"] and (early["auto_resume_at"] or state.get("halt_target") == "daily_loss"):
                label = _state_out(m, early)["auto_resume_label_ko"]
                waiting.append({"market": m, "auto_resume_at": early["auto_resume_at"], "auto_resume_label_ko": label,
                                "reason": f"일일 손실 자동 정지 — {label}에 자동 해제됩니다(조기 재가동 없음)"})
                skipped.append(waiting[-1])
                continue
            # a halt that could not read the orders log left its open orders standing; they must not fill at an old open
            stale = [o for o in open_orders(st.audit, keep_sells=state["keep_exits"]) if o.placed_at <= state["halted_at"]]
            after = {"running": True} | ({"open_orders_cancelled": [o.order_id for o in stale]} if stale else {})
            # after a −20% stop: the book's equity at the last close becomes the peak the drawdown counts from
            if body.reset_peak and dd_stop_standing(st.operations):
                peak_resets[m] = r = _peak_reset(st)
                after |= {"peak_reset_to": r["to"], "peak_reset_at": r["at"], "peak_before": r["before"]}
            st.audit.record_operation(OperationRecord(
                kind="resume", at=at, operator=body.operator, role=role, reason=body.reason, market=m, scope=body.scope,
                target="manual", before={"running": False}, after=after))
            cancel_orders(st.audit, stale, f"정지 전 미체결 — 재가동 시 취소 ({body.operator})")
            resumed.append(m)
        if not resumed and waiting:
            raise HTTPException(409, {"message": "; ".join(f"{MARKET_KO[w['market']]}: {w['reason']}" for w in waiting),
                                      "auto_resume_at": waiting[0]["auto_resume_at"], "waiting": waiting})
        if not resumed:
            raise HTTPException(409, "이미 실행 중입니다" + ("" if len(markets) == 1 else " (모든 시장)"))
    return {"scope": body.scope, "scope_ko": HALT_SCOPE_KO.get(body.scope, body.scope), "resumed": resumed,
            "skipped": skipped, "peak_resets": peak_resets, "states": {m: _state_out(m) for m in markets}}


class ExceptionActionRequest(BaseModel):
    action: Literal["rerun", "hold", "dismiss", "retry_next", "dismiss_order"] = Field(
        description="REVIEW: rerun | hold | dismiss. 주문 예외(한도 · 벤더 · LULD · 거부): retry_next | dismiss_order")
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="필수 · 감사 로그에 남는다")


ACTION_TOPIC_KO = {"rerun": "재실행은", "retry_next": "다음 세션 재시도는"}
ACTION_RESULT_KO = {
    "rerun": "재실행을 예약했습니다 ({n}/{limit}). 다음 일일 루프 decide에서 그날 결정자로 다시 판단하고 이 예외를 닫습니다 — "
             "답이 정상 등급이면 그날 결정으로 주문되고, 다시 읽지 못하면 그날 예외로 큐에 돌아옵니다",
    "retry_next": "다음 세션 재시도를 예약했습니다 ({n}/{limit}). 다음 실행이 그날 한도 · 하드 룰을 다시 거쳐 처음 수량 이하로 "
                  "주문하고, 그다음 세션 시가에 체결합니다",
    "hold": "보유(Hold)로 기록했습니다(source=human) · 주문 없음 · 채점에서 제외 — 채점 표에는 '사람 처리'로 따로 셉니다",
    "dismiss": "결정 없이 예외를 닫았습니다 · 주문 없음 · 결정 기록이 없어 채점 대상이 아닙니다",
    "dismiss_order": "주문 없이 닫았습니다 · 결정은 미체결로 남고 5일 정산은 결정 기준 그대로입니다",
}


def _action_out(o: OrderRecord, st: _Store, action: str, replayed: bool) -> dict:
    n = o.reruns if action == "rerun" else o.retries if action == "retry_next" else None
    return {"order": _order_out(o, st), "action": action, "action_ko": EXCEPTION_ACTION_KO[action], "replayed": replayed,
            "attempt": n, "limit": ACTION_LIMIT if n is not None else None,
            "result_ko": ACTION_RESULT_KO[action].format(n=n, limit=ACTION_LIMIT)}


@app.post("/api/exceptions/{order_id}/action", tags=["execution"])
def exception_action(order_id: str, body: ExceptionActionRequest, market: str | None = MarketQuery,
                     x_admin_token: str | None = Header(default=None)) -> dict:
    """예외 큐 처리(docs/10 D7). REVIEW는 재실행 · 보유로 처리 · 무시, 주문 예외는 다음 세션 재시도 · 무시. 사유 필수,
    운영 기록(kind exception_action, target = order_id)을 먼저 쓰고 주문 기록에 새 줄(exception_action · 횟수 ·
    exception_resolved). 보유로 처리는 그보다 먼저 `source="human"` Hold 결정을 쓴다(주문 없음, 채점 제외).
    재실행 · 재시도는 각 2회까지(넘으면 409). 마지막 처리와 같은 처리 · 운영자 · 사유를 다시 보내면 아무것도 쓰지 않고
    그 결과를 돌려준다(replayed). 재실행은 예약만 한다: 결정 루프(daily_loop)의 재실행 경로는 후속.
    ponytail: 운영자 역할이 아직 없어 관리자 토큰(X-Admin-Token)으로 받는다 — 역할 분리는 후속."""
    role = _require_admin(x_admin_token)
    m = _market(market)
    with _locks([m]):                                # the executor reads and writes these orders under the same lock
        st = STORES[m].refresh()
        audit = st.audit
        o = latest_orders(audit.orders()).get(order_id)
        if o is None:
            raise HTTPException(404, f"{MARKET_KO[m]} 주문 {order_id!r} 없음 (REVIEW 결정은 실행기가 돈 뒤에 처리할 수 있습니다)")
        last = next((op for op in reversed(audit.operations()) if op.kind == "exception_action" and op.target == order_id), None)
        if (last is not None and o.exception_action == body.action and last.after.get("action") == body.action
                and (last.operator, last.reason) == (body.operator, body.reason)):
            return _action_out(o, st, body.action, replayed=True)
        if o.status not in ("exception", "rejected"):
            raise HTTPException(409, f"예외 큐에 있는 주문이 아닙니다 (지금 {ORDER_STATUS_KO[o.status]})")
        if o.exception_resolved:
            raise HTTPException(409, f"이미 처리된 예외입니다 ({EXCEPTION_ACTION_KO.get(o.exception_action or '', '처리됨')})")
        kind = _exception_kind(o)
        allowed = _allowed_actions(o, kind)
        if body.action not in allowed:
            raise HTTPException(422, ("규칙 청산 주문은 실행기가 다음 종가에 신호를 다시 판정합니다 — " if o.order_id.endswith(
                EXIT_SUFFIX) else "") + f"{EXCEPTION_KIND_KO[kind]} 예외에는 {' · '.join(EXCEPTION_ACTION_KO[a] for a in allowed)}만 "
                                     f"쓸 수 있습니다: {body.action}")
        update: dict = {"exception_action": body.action}
        after: dict = {"action": body.action, "scored": False}
        if body.action in ("rerun", "retry_next"):
            field = "reruns" if body.action == "rerun" else "retries"
            used = getattr(o, field)
            if used >= ACTION_LIMIT:
                raise HTTPException(409, f"{ACTION_TOPIC_KO[body.action]} 예외마다 {ACTION_LIMIT}회까지입니다 "
                                         f"(이미 {used}회) — 보유로 처리하거나 무시하세요")
            update[field] = used + 1
            after |= {"attempt": used + 1, "limit": ACTION_LIMIT, "pending": True}
        else:
            update["exception_resolved"] = True
        if body.action == "hold":
            # the decision first: an order line that says "held" never stands without the decision it records
            if not audit.decisions_where(source="human", ticker=o.ticker, trade_date=o.trade_date):
                audit.record_decision(DecisionRecord(
                    ticker=o.ticker, trade_date=o.trade_date, rating="Hold", action=None, source="human", market=m,
                    thesis="사람 처리: 보유", settings={"decider": "human", "operator": body.operator, "order_id": order_id},
                    evidence=[EvidenceItem(source="other", summary=f"{body.operator}: {body.reason}", ref=order_id)]))
            after["decision"] = {"rating": "Hold", "source": "human"}
        audit.record_operation(OperationRecord(
            kind="exception_action", operator=body.operator, role=role, reason=body.reason, market=m, target=order_id,
            before={"status": o.status, "exception_kind": kind, "exception_action": o.exception_action,
                    "reruns": o.reruns, "retries": o.retries},
            after=after))
        new = o.model_copy(update=update)
        audit.record_order(new)
    return _action_out(new, STORES[m].refresh(), body.action, replayed=False)


class ModeRequest(BaseModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="감사 로그에 남는다")
    market: str = Field(description="KRX | US — 모드는 시장마다 따로(기본값 없음: 다른 시장 모드를 실수로 바꾸지 않게)")
    mode: Literal["auto", "manual", "paper"] | None = Field(default=None, description="저장 형식 그대로의 모드(호환)")
    account_mode: Literal["paper", "live"] | None = Field(
        default=None, description="계좌 축만 바꾼다: live = 실거래(실행 축 자동/수동 유지), paper = 모의투자")
    confirmed: bool = Field(default=False, description="모의 → 실거래 전환에는 true 필수(실계좌 주문 확인)")

    @model_validator(mode="after")
    def _one_axis(self) -> ModeRequest:
        if (self.mode is None) == (self.account_mode is None):
            raise ValueError("mode와 account_mode 중 하나만 보냅니다")
        return self


def _target_mode(current: str, body: ModeRequest) -> str:
    """The stored mode a request asks for. ``account_mode`` changes the account axis only: live keeps a live mode's
    execution side, and paper runs automatic, so paper → live is auto."""
    if body.mode is not None:
        return body.mode
    if body.account_mode == "paper":
        return "paper"
    return current if current != "paper" else "auto"


def _gate_snapshot(p: dict) -> dict:
    return {"as_of": p["as_of"], "start": p["start"], "weeks": p["weeks"], "eligible": p["eligible"],
            "summary_ko": p["summary_ko"],
            "criteria": {c["id"]: {"value": c["value"], "status": c["status"]} for c in p["criteria"]}}


@app.post("/api/trading/mode", tags=["trading"])
def set_mode(body: ModeRequest, x_admin_token: str | None = Header(default=None)) -> dict:
    """그 시장의 운용 모드 전환(관리자). 계좌 축(`account_mode`: 모의투자 ↔ 실거래) 또는 저장 형식 그대로의 `mode`
    (자동 / 수동 승인 / 모의투자). **모의 → 실거래**는 어느 쪽으로 요청하든 `confirmed: true`(없으면 422)와 서버의 통과 기준
    판정(`/api/paper/progress`의 eligible, 아니면 409 · 미충족 기준 목록)이 있어야 받는다. 운영 기록 before에 기준 스냅샷.
    실주문 경로는 아직 없다: 실거래 모드에서 실행기는 주문하지 않는다(ExecutionRefused)."""
    role = _require_admin(x_admin_token)
    m = _market(body.market)
    with _locks([m]):                                # the executor checks the mode under the same lock
        st = STORES[m].refresh()
        state = st.state()
        target = _target_mode(state["mode"], body)
        if state["mode"] == target:
            raise HTTPException(409, f"{MARKET_KO[m]} 시장은 이미 {MODE_KO[target]} 모드입니다")
        before: dict = {"mode": state["mode"]}
        live = state["mode"] == "paper" and target != "paper"
        if live:
            if not body.confirmed:
                raise HTTPException(422, "실거래 전환은 confirmed: true(실계좌로 주문이 나간다는 확인)가 필요합니다")
            gate = _progress(st, expected=False)
            if not gate["eligible"]:
                open_ = [f"{c['label_ko']}({c['status_ko']} · {c['value_ko']})" for c in gate["criteria"] if c["status"] != "met"]
                raise HTTPException(409, f"{MARKET_KO[m]} 모의투자 통과 기준 미충족 — 실거래로 전환할 수 없습니다: " + "; ".join(open_))
            before |= {"account_mode": "paper", "paper_gate": _gate_snapshot(gate)}
        # paper orders do not outlive paper mode: left open they would fill at an old open when paper comes back
        pending = open_orders(st.audit) if state["mode"] == "paper" else []
        after = {"mode": target} | ({"account_mode": "live"} if live else {})
        st.audit.record_operation(OperationRecord(
            kind="mode_change", operator=body.operator, role=role, reason=body.reason, market=m, target="mode",
            before=before, after=after | ({"open_orders_cancelled": [o.order_id for o in pending]} if pending else {})))
        cancel_orders(st.audit, pending, f"운용 모드 변경 paper → {target} ({body.operator})")
    note = (f"{MARKET_KO[m]} 실거래 모드로 바뀌었지만 실주문 경로는 아직 없습니다: 실행기는 모의투자 모드에서만 주문하므로 "
            "이 시장은 주문을 내지 않습니다(ExecutionRefused)") if target != "paper" else None
    return {"state": _state_out(m), "note": note}


# --- capital (docs/10 D5, 단계 5): replayed from capital_set operations, never stored as state ----------------------

CAPITAL_UNIT = {"KRX": 1.0, "US": 0.01}                   # whole won, cents
TICKER_RE = re.compile(r"^[0-9A-Z^][0-9A-Z.\-^]{0,14}$")
UNIVERSE_KO = {"loop": "지금 루프 유니버스", "index": "지수 구성(오늘)", "custom": "직접 지정"}
INDEX_HISTORY = {"KRX": "kospi200_history.csv", "US": "sp500_history.csv"}


def _capital_amount(m: str, amount: float) -> float:
    """``amount`` in the market's unit, or 422: positive, whole won / cents, under the typo guard ``CAPITAL_MAX``."""
    if not math.isfinite(amount) or amount <= 0:
        raise HTTPException(422, f"자본은 0보다 커야 합니다: {amount}")
    unit = CAPITAL_UNIT[m]
    units = round(amount / unit)
    if abs(units * unit - amount) > unit * 1e-6:
        raise HTTPException(422, f"{MARKET_KO[m]} 자본 단위는 {'1원' if m == 'KRX' else '$0.01'}입니다: {amount}")
    if amount > CAPITAL_MAX[m]:
        raise HTTPException(422, f"{MARKET_KO[m]} 자본 상한 {fmt_money(CAPITAL_MAX[m], m)}을 넘습니다(입력 오타 방지): "
                                 f"{fmt_money(amount, m)}")
    return float(units) if m == "KRX" else round(units * unit, 2)


def _set_by(op: OperationRecord | None) -> dict | None:
    return {"at": op.at, "operator": op.operator, "role": op.role, "reason": op.reason} if op is not None else None


def _capital_out(st: _Store) -> dict:
    m = st.market
    c = capital_state(st.operations, m)
    paper, live, small = c["paper"], c["live"], SMALL_ACCOUNT[m]
    running = st.order_facts["total"] > 0
    calendar = PRICES.calendar(MARKETS[m]["benchmark"])
    last = calendar[-1] if calendar else None
    return {
        "market": m, "market_ko": MARKET_KO[m], "currency": MARKETS[m]["currency"],
        "paper": {"account": "paper", "label_ko": CAPITAL_ACCOUNT_KO["paper"], "amount": paper["amount"],
                  "start": paper["start"], "source": "record" if paper["record"] else "default",
                  "set": _set_by(paper["record"]), "small_account": paper["amount"] < small},
        "live": {"account": "live", "label_ko": CAPITAL_ACCOUNT_KO["live"], "amount": live["amount"],
                 "source": "record" if live["record"] else "unset", "set": _set_by(live["record"]),
                 "small_account": live["amount"] < small if live["amount"] is not None else None},
        # pending: dated after the last close in the price data — in the book from that day's close on
        "cash_flows": [{"date": d, "amount": a, "pending": last is None or d > last} | (_set_by(op) or {})
                       for d, a, op in c["flows"]],
        "next_paper_change": "flow" if running else "start",
        "next_paper_change_ko": CAPITAL_CHANGE_KO["flow" if running else "start"],
        "small_threshold": small, "max_amount": CAPITAL_MAX[m], "unit": CAPITAL_UNIT[m],
        "account_mode": "paper" if st.state()["mode"] == "paper" else "live",
        "admin_token_required": ADMIN_TOKEN is not None,
        "note_ko": "모의 자본은 장부에 주문이 없으면 시작 자본을 바꾸고, 주문이 있으면 그날 종가에 입출금으로 들어갑니다(수익률 · "
                   "드로다운 · 모의 판정에서 빠짐). 실거래 자본은 저장 · 표시 · 미리보기만 합니다 — 실주문 경로는 아직 없습니다.",
    }


@app.get("/api/capital", tags=["settings"])
def get_capital(market: str | None = MarketQuery) -> dict:
    """그 시장의 모의투자 · 실거래 자본(운영 기록 capital_set 재생값, 없으면 기본값), 입출금 목록, 소액 1종목 모드 여부."""
    return _capital_out(_store(market))


class CapitalRequest(BaseModel):
    account: Literal["paper", "live"]
    amount: float = Field(description="그 시장 통화. 국내 1원 · 미국 $0.01 단위, 0보다 크고 상한 이하")
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="필수 · 감사 로그에 남는다")


@app.post("/api/capital", tags=["settings"])
def set_capital(body: CapitalRequest, market: str | None = MarketQuery,
                x_admin_token: str | None = Header(default=None)) -> dict:
    """자본 설정(관리자). 운영 기록 `capital_set`(target = paper | live, before · after, effective_from = 그 시장의 오늘)을
    append한다. 모의 자본: 장부에 주문이 없으면 시작 자본 교체, 있으면 그날 종가 기준 입출금(새 금액 − 지금 금액) — 출금이
    현금보다 크면 409. 실거래 자본: 저장만(실주문 없음). 마지막 설정과 금액 · 운영자 · 사유가 같으면 아무것도 쓰지 않는다
    (replayed). 금액만 같으면 409."""
    role = _require_admin(x_admin_token)
    m = _market(market)
    amount = _capital_amount(m, body.amount)
    with _locks([m]):                                # the executor reads the capital under the same lock
        st = STORES[m].refresh()
        state = capital_state(st.operations, m)
        cur = state[body.account]
        last = cur["record"]
        if (last is not None and float(last.after["amount"]) == amount
                and (last.operator, last.reason) == (body.operator, body.reason)):
            return {"replayed": True, "change": last.after.get("change"), "flow": last.after.get("flow"),
                    "effective_from": last.effective_from, "capital": _capital_out(st),
                    "result_ko": "같은 설정이 이미 기록돼 있습니다 — 새로 쓰지 않았습니다"}
        if cur["amount"] == amount:
            raise HTTPException(409, f"{MARKET_KO[m]} {CAPITAL_ACCOUNT_KO[body.account]}이 이미 {fmt_money(amount, m)}입니다")
        today = _clock().astimezone(ZoneInfo(MARKETS[m]["timezone"])).date().isoformat()
        after: dict = {"account": body.account, "amount": amount}
        if body.account == "live":
            after["change"] = "live"
        elif st.order_facts["total"] > 0:
            flow = amount - cur["amount"]
            if flow < 0:
                cfg = paper_config(m)
                book = positions_view(st.order_rows, PRICES, cfg=cfg, operations=st.operations)
                # the cash at the last close, and the flows dated after it (they go in at a close not in the data yet),
                # less the buys not in the book yet (open, or filling after it — they fill before the flow's close),
                # at the decision-day close plus costs as the daily cap counts them
                pending = sum(a for d, a, _ in state["flows"] if d > book["as_of"])
                reserved = sum((o.amount if o.amount is not None
                                else o.quantity * (PRICES.close_asof(o.ticker, o.placed_on) or 0.0))
                               * (1 + cfg.commission + cfg.slippage)
                               for o in latest_orders(st.order_rows).values()
                               if o.side == "Buy" and (o.status == "open" or (
                                   o.status in ("filled", "partial") and (o.fill_date or "") > book["as_of"])))
                cash = book["cash"] + pending - reserved
                if cash + flow < -EPS:
                    raise HTTPException(409, f"출금 {fmt_money(-flow, m)}이 장부 현금 {fmt_money(cash, m)}"
                                             + (f"(체결 대기 매수 {fmt_money(reserved, m)} 제외)" if reserved else "")
                                             + "보다 큽니다 — 보유를 줄인 뒤 다시 하세요")
                if book["equity"] + pending + flow <= EPS:
                    raise HTTPException(409, f"출금 {fmt_money(-flow, m)} 뒤 평가액이 0 이하입니다 — 장부 전액은 출금할 수 "
                                             "없습니다(수익률 · 드로다운을 이어 셀 수 없음)")
            after |= {"change": "flow", "flow": flow}
        else:
            after["change"] = "start"
        st.audit.record_operation(OperationRecord(
            kind="capital_set", operator=body.operator, role=role, reason=body.reason, market=m, target=body.account,
            before={"amount": cur["amount"]}, after=after, effective_from=today))
    change = after["change"]
    result = {"start": f"모의 시작 자본을 {fmt_money(amount, m)}으로 바꿨습니다 — 장부에 주문이 없어 처음부터 이 금액입니다",
              "flow": f"{today} 종가에 {'입금' if after.get('flow', 0) > 0 else '출금'} "
                      f"{fmt_money(abs(after.get('flow', 0.0)), m)} — 모의 자본 {fmt_money(amount, m)}. 수익률 · 드로다운 · "
                      "모의 판정은 이 입출금을 빼고 셉니다(시간 가중)",
              "live": f"실거래 자본을 {fmt_money(amount, m)}으로 저장했습니다 — 실주문 경로는 아직 없어 미리보기 · 전환 모달에만 "
                      "쓰입니다"}[change]
    return {"replayed": False, "change": change, "change_ko": CAPITAL_CHANGE_KO[change], "flow": after.get("flow"),
            "effective_from": today, "capital": _capital_out(STORES[m].refresh()), "result_ko": result}


def _universe(m: str, kind: str, tickers: str | None, as_of: str) -> tuple[list[str], str]:
    if kind == "loop":
        setup = _daily_loop().SETUPS[m]
        return list(setup["universe"]), setup["label"]
    if kind == "index":
        history = load_history(SECTORS.parent / INDEX_HISTORY[m])
        return sorted(members_on(history, as_of)), f"{MARKETS[m]['benchmark_name_ko'] if m == 'US' else 'KOSPI200'} 구성 {as_of}"
    names = [t for t in re.split(r"[\s,]+", (tickers or "").upper()) if t]
    if not names:
        raise HTTPException(422, "직접 지정은 tickers(쉼표 · 공백 구분)가 필요합니다")
    if len(names) > 600:
        raise HTTPException(422, f"직접 지정은 600종목까지입니다: {len(names)}")
    if bad := [t for t in names if not TICKER_RE.match(t)]:
        raise HTTPException(422, f"종목 코드 형식이 아닙니다: {', '.join(bad[:5])}")
    return list(dict.fromkeys(names)), f"직접 지정 {len(set(names))}종목"


@app.get("/api/capital/preview", tags=["settings"])
def capital_preview(market: str | None = MarketQuery, account: Literal["paper", "live"] = "paper",
                    amount: float | None = None, universe: Literal["loop", "index", "custom"] = "loop",
                    tickers: str | None = None) -> dict:
    """이 자본에서 걸리는 규칙과 금액, 유니버스에서 살 수 있는 종목 수(1주 종가 ≤ 종목당 · 주문당 상한) · 1주가 주문당 상한을
    넘는 종목. 서버 계산(가격 캐시 · 섹터 파일 · 유니버스 파일만 읽음, 네트워크 없음). amount 없으면 그 계좌의 지금 자본."""
    st = _store(market)
    m = st.market
    if amount is None:
        amount = capital_state(st.operations, m)[account]["amount"]
        if amount is None:
            raise HTTPException(422, "실거래 자본이 아직 없습니다 — amount를 보내세요")
    amount = _capital_amount(m, amount)
    calendar = PRICES.calendar(MARKETS[m]["benchmark"])
    if not calendar:
        raise HTTPException(503, f"{MARKETS[m]['benchmark']} 가격 캐시가 없어 미리보기를 계산할 수 없습니다")
    as_of = calendar[-1]
    names, label = _universe(m, universe, tickers, as_of)
    risk, caps, small_at = RiskConfig(), auto_execution(m), SMALL_ACCOUNT[m]
    small = amount < small_at
    per_name = amount if small else amount * risk.max_position_pct
    order_cap, day_cap = min(caps["auto_order_max"], amount), min(caps["auto_daily_order_cap"], amount)
    buy_limit = min(per_name, order_cap)
    priced = [(t, px) for t in names if (px := PRICES.close_asof(t, as_of)) is not None]
    missing = [t for t in names if t not in dict(priced)]
    can = [(t, px) for t, px in priced if px <= buy_limit + EPS]
    over = sorted(((t, px) for t, px in priced if px > caps["auto_order_max"] + EPS), key=lambda x: -x[1])
    max_hold = min(1, len(can)) if small else min(round(1 / risk.max_position_pct), len(can))
    money = (lambda v: fmt_money(v, m))

    def row(rid: str, label_ko: str, setting_ko: str, value: float | str | None, note_ko: str, warn: bool = False) -> dict:
        return {"id": rid, "label_ko": label_ko, "setting_ko": setting_ko, "amount": value if not isinstance(value, str) else None,
                "value_ko": value if isinstance(value, str) else (money(value) if value is not None else "–"),
                "note_ko": note_ko, "warn": warn}

    rules = [
        row("max_position_pct", "종목당 비중 상한",
            f"소액 1종목 모드 ({money(small_at)} 미만)" if small else f"{risk.max_position_pct:.0%} · max_position_pct",
            per_name, "1종목에 자본 전체" if small else "한 종목에 이 금액까지"),
        row("max_sector_pct", "섹터당 비중 상한", f"{risk.max_sector_pct:.0%} · max_sector_pct",
            None if small else amount * risk.max_sector_pct, "1종목이라 해당 없음" if small else "같은 섹터 합계"),
        row("buyable", "살 수 있는 종목", "1주 종가 ≤ 종목당 · 주문당 상한", f"{len(can)} / {len(priced)}",
            (f"1주 {money(buy_limit)} 이하" if can else "1주도 살 수 없음") + (f" · 가격 없는 {len(missing)}종목 제외" if missing else ""),
            warn=not can),
        row("max_holdings", "최대 보유 종목 수", "종목당 상한에서 나옴", f"{max_hold}종목",
            "소액 1종목 모드" if small else f"{risk.max_position_pct:.0%} × {round(1 / risk.max_position_pct)}종목이 최대",
            warn=max_hold == 0),
        row("auto_order_max", "주문당 금액 상한", f"{money(caps['auto_order_max'])} · auto_order_max", order_cap,
            "설정값과 자본 중 작은 쪽" + (" — 자본이 더 작음" if amount < caps["auto_order_max"] else "")),
        row("auto_daily_order_cap", "일일 주문 금액 상한", f"{money(caps['auto_daily_order_cap'])} · auto_daily_order_cap",
            day_cap, "설정값과 자본 중 작은 쪽" + (" — 자본이 더 작음" if amount < caps["auto_daily_order_cap"] else "")),
        row("auto_daily_loss_limit", "일일 손실 한도", f"{caps['auto_daily_loss_limit']:+.0%} · auto_daily_loss_limit",
            amount * -caps["auto_daily_loss_limit"], "하루에 이만큼 잃으면 자동 정지(그날 세션까지)"),
        row("dd_halve", "드로다운 → 신규 비중 절반", f"{risk.dd_halve:+.0%} · dd_halve", amount * (1 + risk.dd_halve),
            "평가액이 이 아래면(피크 = 이 자본일 때)"),
        row("dd_stop", "드로다운 → 전량 청산 · 정지", f"{risk.dd_stop:+.0%} · dd_stop", amount * (1 + risk.dd_stop),
            "평가액이 이 아래면 관리자 재가동까지 정지"),
        row("stop_loss_pct", "손절 (진입가 대비)", f"{risk.stop_loss_pct:+.0%} · stop_loss_pct", per_name * -risk.stop_loss_pct,
            "종목당 상한까지 샀을 때 손절 1건의 손실"),
    ]

    def named(t: str, px: float) -> dict:
        return {"ticker": t, "name": st.name(t), "price": px}

    cheapest = min(priced, key=lambda x: x[1], default=None)
    return {"market": m, "currency": MARKETS[m]["currency"], "account": account, "account_ko": CAPITAL_ACCOUNT_KO[account],
            "amount": amount, "small_account": small, "small_threshold": small_at, "as_of": as_of,
            "universe": {"id": universe, "label_ko": UNIVERSE_KO[universe], "label": label, "size": len(names),
                         "priced": len(priced), "missing": missing[:20], "missing_count": len(missing),
                         "sector_ready": sum(sector_of(t) is not None for t in names),
                         "name_ready": sum(korean_name(t) is not None for t in names)},
            "buyable": len(can), "none_buyable": not can, "cheapest": named(*cheapest) if cheapest else None,
            "over_order_cap": [named(t, px) for t, px in over[:20]], "over_order_cap_count": len(over),
            "rules": rules,
            "note_ko": f"가격은 {as_of} 종가(가격 캐시 — KIS 우선, 없으면 Yahoo). 주문당 · 일일 상한은 설정값과 자본 중 작은 쪽이 "
                       "실제로 걸립니다. 1종목만 살 수 있는 금액에서는 분산이 안 되므로 수익률보다 주문 · 체결이 맞는지를 먼저 보세요."
                       + (" 유니버스 선택은 미리보기만 — 선별 유니버스는 아직 스크립트 고정 목록입니다." if universe != "loop" else "")}


# --- model settings (config.LLM, read only) ------------------------------------------------------------------------

@app.get("/api/model", tags=["settings"])
def model_settings(market: str | None = MarketQuery) -> dict:
    """그 시장 일일 루프의 결정자와 모델 설정(config.LLM 읽기만 — 바꾸는 곳은 코드 설정). 결정자 · 시험 모드는
    scripts/daily_loop의 `default_decider` · `trial_on`을 그대로 부른다(LLM 시험: TRADING_LLM_TRIAL=1 또는 LLM.trial,
    시장별 모델 decide `trial_sessions`회 뒤 규칙 — 기록 재생). 키는 있는지만."""
    from dotenv import dotenv_values

    st = _store(market)
    loop = _daily_loop()
    env_path = Path(os.environ.get("TRADING_ENV_FILE") or ROOT / ".env")
    env_file = dotenv_values(env_path) if env_path.exists() else {}
    decider = loop.default_decider(st.audit, st.market)
    trial = _trial(st)
    sessions = LLM.get("trial_sessions")
    return {"market": st.market, "decider": decider, "decider_ko": DECIDER_KO[decider],
            "decider_id": LLM["model"] if decider == "llm" else BASELINE_DECIDER,
            "model": LLM.get("model"), "effort": LLM.get("effort"), "max_tokens": LLM.get("max_tokens"),
            "timeout": LLM.get("timeout"), "max_calls_per_run": LLM.get("max_calls_per_run"),
            "enabled": bool(LLM.get("enabled")), "trial_mode": trial is not None, "trial": trial,
            "config": dict(LLM), "keys": {"ANTHROPIC_API_KEY": _key_set("ANTHROPIC_API_KEY", env_file)},
            "output_language": "ko",
            "note_ko": "config.LLM의 값을 읽어 보여 줍니다(바꾸려면 코드 설정 — 컨트롤 타워). 시험 모드는 시장마다 모델 결정 "
                       f"{sessions}거래일 뒤 규칙 베이스라인으로 돌아갑니다. 결정자가 llm이어도 키가 없으면 그날 결정은 "
                       "규칙 베이스라인으로 남습니다."}


class ApproveRequest(BaseModel):
    approver: str
    note: str = ""


@app.post("/api/decisions/{ticker}/{trade_date}/approve", tags=["decisions"], status_code=501)
def approve(ticker: str, trade_date: str, body: ApproveRequest, market: str | None = MarketQuery) -> dict:
    """수동 승인 모드 전용(v0.3). 자동·모의 모드에서는 승인 단계가 없어 409. 실행 레이어 전까지 501."""
    st = _store(market)
    if st.state()["mode"] != "manual":
        raise HTTPException(409, "자동 모드에서는 승인 단계가 없습니다. 설정에서 수동 승인 모드로 바꾼 뒤 사용하세요.")
    exists = bool(st.decisions(ticker=ticker.upper(), trade_date=trade_date))
    if not exists:
        raise HTTPException(404, f"결정 {ticker} {trade_date} 없음")
    return {"implemented": False, "message": "실행 레이어(주문·체결)가 아직 없습니다. 승인 요청은 접수 형식만 검증했습니다.",
            "ticker": ticker.upper(), "trade_date": trade_date, "approver": body.approver}


# --- the built dashboard (frontend/dist), one origin with the API ------------------------------------------------
# Served only when TRADING_STATIC_DIR names it (the Docker image, compose.yaml): one process and one port, the API on
# the page's own origin — no CORS, no second server. A client route (/decisions, /tickers/…) falls back to
# index.html; /api/… never does (a missing endpoint stays 404). Development keeps Vite (npm run dev, its proxy).

class _Dashboard(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api"):
                raise
            return await super().get_response("index.html", scope)


if STATIC_DIR := os.environ.get("TRADING_STATIC_DIR"):
    app.mount("/", _Dashboard(directory=STATIC_DIR, html=True), name="dashboard")
