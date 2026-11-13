"""API for the dashboard (docs/02 v0.3): the audit log, the memory log and the backtest summary served as
recorded, plus the three write actions the plan allows — emergency halt, resume and mode change — each
appended to the operations log. Korean labels are attached server-side so the frontend never translates
rule ids, rating values or order statuses.

Markets (docs/08): KRX and US are separate books. Every read takes ``market`` (default KRX, anything else
outside config.MARKETS is 400) and serves that market's log only (data/audit/<market>/; KRX also reads the
files under data/audit/ from before the market axis). Halt and resume take a ``scope`` ("all" by default, or
one market); a mode change names its market.

Run:  .venv/Scripts/python -m uvicorn backend.main:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs   (OpenAPI, generated)

Environment:
  TRADING_DATA_DIR      where data/ lives (default: <repo>/data)
  TRADING_MEMORY_LOG    memory log to read (default: the log named in data/baseline_summary.json; KRX only)
  TRADING_CORS          comma-separated origins allowed (default: localhost dev ports)
  TRADING_ADMIN_TOKEN   when set, resume and mode change require header X-Admin-Token with this value.
                        Unset = development: anyone may, and the record says so (role "admin(dev, no token)").
"""

from __future__ import annotations

import json
import os
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from agent_trading_core import __version__ as core_version
from agent_trading_core.audit import (
    AuditLog,
    DecisionRecord,
    OperationRecord,
    OrderRecord,
    ScreenRun,
    baseline_summary_path,
    latest_orders,
    market_audit,
    stage_counts,
)
from agent_trading_core.config import AUTO_EXECUTION, MARKETS, PAPER_TRADING, auto_execution
from agent_trading_core.execution import (
    EXIT_SUFFIX,
    Prices,
    cancel_orders,
    check_date,
    dd_stop_standing,
    effective_state,
    execution_lock,
    open_orders,
    paper_config,
    paper_expected,
    paper_progress,
    positions_view,
)
from agent_trading_core.krx_names import (
    ACCOUNT_MODE_KO,
    AUTO_RULE_KO,
    DECISION_SOURCE_KO,
    EXCEPTION_KIND_KO,
    EXECUTION_MODE_KO,
    EXIT_REASON_KO,
    HALT_SCOPE_KO,
    HALT_TARGET_KO,
    HALTED_BY_KO,
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
    SIDE_KO,
    korean_name,
)
from agent_trading_core.memory import TradingMemoryLog
from agent_trading_core.rating import RATING_REVIEW, RATINGS_5_TIER
from agent_trading_core.risk import RiskConfig
from agent_trading_core.screening import ScreenConfig

VARIANT_KO = {"rules_off": "하드 룰 끔", "rules_on": "하드 룰 켬", "rules_on_regime": "하드 룰 + 국면 필터"}
KST = ZoneInfo("Asia/Seoul")
# the executor runs after the close: the decision is made on the close, the fill at the next open
RUN_AFTER_CLOSE = timedelta(minutes=5)
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
                          "쓰기는 긴급 정지·재가동·모드 변경만.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in (os.environ.get("TRADING_CORS") or
                   "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000").split(",") if o],
    allow_methods=["GET", "POST"], allow_headers=["*"],
)


# --- data access, one store per market, reloaded when the append-only files change -------

class _Store:
    def __init__(self, market: str) -> None:
        self.market = market
        self._stamp: tuple = ()
        self.screens: list[ScreenRun] = []
        self.decisions: list[DecisionRecord] = []
        self.orders: list[OrderRecord] = []
        self.operations: list[OperationRecord] = []
        self.memory: list[dict] = []
        self.summary: dict = {}

    @property
    def audit(self) -> AuditLog:
        return market_audit(DATA_DIR / "audit", self.market)

    def _paths(self) -> list[Path]:
        audit = self.audit
        dirs = ([audit.legacy] if audit.legacy else []) + [audit.directory]
        paths = [d / n for d in dirs for n in ("screens.jsonl", "decisions.jsonl", "orders.jsonl", "operations.jsonl")]
        paths.append(self.summary_path())
        log = self.memory_log_path()
        if log:
            paths.append(log)
        return paths

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
        stamp = tuple(p.stat().st_mtime_ns if p.exists() else None for p in self._paths())
        if stamp == self._stamp:
            return self
        audit = self.audit
        self.screens = sorted(audit.screens(), key=lambda s: s.as_of, reverse=True)
        self.decisions = sorted(audit.decisions(), key=lambda d: (d.trade_date, d.ticker), reverse=True)
        # an order that filled or was cancelled after it was placed has later lines; serve its current state
        self.orders = sorted(latest_orders(audit.orders()).values(), key=lambda o: (o.placed_at, o.order_id), reverse=True)
        self.operations = audit.operations()
        log = self.memory_log_path()
        self.memory = TradingMemoryLog({"memory_log_path": str(log)}).load_entries() if log else []
        summary = self.summary_path()
        self.summary = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {}
        self._stamp = stamp
        return self

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
        return float(text.rstrip("%")) / 100
    except ValueError:
        return None


def _outcome(entry: dict | None) -> dict | None:
    if entry is None:
        return None
    return {"pending": entry["pending"], "raw_return": _pct(entry.get("raw")), "alpha": _pct(entry.get("alpha")),
            "holding": entry.get("holding"), "resolved": entry.get("resolved"), "reflection": entry.get("reflection") or ""}


def _name(ticker: str, screens: list[ScreenRun]) -> str | None:
    ko = korean_name(ticker)
    if ko:
        return ko
    for s in screens:
        for c in s.candidates:
            if c.ticker == ticker and c.name:
                return c.name
    return None


def _order_out(o: OrderRecord, st: _Store) -> dict:
    out = o.model_dump()
    out.update(name=_name(o.ticker, st.screens), side_ko=SIDE_KO.get(o.side, o.side),
               status_ko=ORDER_STATUS_KO.get(o.status, o.status), amount=o.amount)
    return out


def _decision_out(d: DecisionRecord, st: _Store, memory_by_key: dict, orders_by_key: dict) -> dict:
    out = d.model_dump()
    out["name"] = _name(d.ticker, st.screens)
    out["rating_ko"] = RATING_KO.get(d.rating, d.rating)
    out["tradeable"] = d.tradeable
    out["source_ko"] = DECISION_SOURCE_KO.get(d.source, d.source)
    # the memory log scores the agent's call; a rule exit on the same ticker and day is not that call
    out["outcome"] = _outcome(memory_by_key.get((d.ticker, d.trade_date))) if d.source == "agent" else None
    # an agent decision and a rule exit can share (ticker, day): each card shows its own order
    rule = d.source == "rule"
    out["orders"] = [_order_out(o, st) for o in orders_by_key.get((d.ticker, d.trade_date), [])
                     if o.order_id.endswith(EXIT_SUFFIX) == rule]
    return out


def _screen_summary(s: ScreenRun) -> dict:
    counts = stage_counts(s)
    return {"run_id": s.run_id, "as_of": s.as_of, "recorded_at": s.recorded_at, "universe": s.universe,
            "universe_size": s.universe_size, "selected_count": counts[-1] if counts else 0,
            "rules": s.rules, "rule_count": len(s.rules), "rules_ko": [RULE_KO.get(r, r) for r in s.rules], "stage_counts": counts}


def _orders_by_key(st: _Store) -> dict:
    by: dict = {}
    for o in st.orders:
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
    label = f"{market_ko} 정지됨 · {st['halted_at'][:16].replace('T', ' ')} UTC · {who}" if not st["running"] else (
        f"{market_ko} 자동 매매 실행 중" if st["mode"] == "auto" else f"{market_ko} {mode_ko} 모드")
    # two axes on the stored mode (docs/10 D4): account paper | live, execution auto | manual approval
    account = "paper" if st["mode"] == "paper" else "live"
    execution = "manual" if st["mode"] == "manual" else "auto"
    return st | {"market": market, "market_ko": market_ko, "mode_ko": mode_ko, "label_ko": label, "halted_by_ko": by_ko,
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


def _exceptions(st: _Store, date: str | None) -> list[dict]:
    """REVIEW decisions and orders that were refused: what a human must look at (docs/02 화면 3, 예외 큐)."""
    items = []
    for d in st.decisions:
        if d.rating == RATING_REVIEW and (date is None or d.trade_date == date):
            items.append({"kind": "review", "kind_ko": EXCEPTION_KIND_KO["review"], "ticker": d.ticker,
                          "name": _name(d.ticker, st.screens), "trade_date": d.trade_date,
                          "summary": "등급을 읽을 수 없어 주문하지 않았습니다", "detail": d.thesis, "ref": d.screen_run_id})
    for o in st.orders:
        # a REVIEW decision is listed above already; its exception order is the same item
        if o.status in ("rejected", "exception") and o.exception_kind != "review" and (date is None or o.trade_date == date):
            kind = _exception_kind(o)
            items.append({"kind": kind, "kind_ko": EXCEPTION_KIND_KO[kind], "ticker": o.ticker,
                          "name": _name(o.ticker, st.screens), "trade_date": o.trade_date,
                          "summary": o.reason or ORDER_STATUS_KO[o.status], "detail": ", ".join(o.rules_applied), "ref": o.order_id})
    return sorted(items, key=lambda x: (x["trade_date"], x["ticker"]), reverse=True)


def _flow(st: _Store, book: dict) -> dict:
    """Home flow cards (screen 1): the latest screen, the latest decision day by rating, that day's exception queue
    by kind, the holdings. Counted here so the screen shows numbers it does not add up itself (docs/02 §0)."""
    latest = st.screens[0] if st.screens else None
    day = max((d.trade_date for d in st.decisions), default=None)
    # rule exits (stop-loss, trailing, …) are not agent judgments (docs/04 /api/decisions): counted apart
    by_rating = Counter(d.rating for d in st.decisions if d.trade_date == day and d.source == "agent")
    rule = sum(d.trade_date == day and d.source == "rule" for d in st.decisions)
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
    dates = [d.trade_date for d in st.decisions] + [o.trade_date for o in st.orders]
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
    """The regular session of ``day`` (the market's local date) as aware datetimes in KST. The zone database
    applies daylight saving (New York 09:30 is 22:30 KST in summer, 23:30 in winter), never a hand offset."""
    tz = ZoneInfo(MARKETS[market]["timezone"])
    start, end = (time.fromisoformat(t) for t in MARKETS[market]["session"].split("-"))
    return (datetime.combine(day, start, tz).astimezone(KST), datetime.combine(day, end, tz).astimezone(KST))


def next_run(market: str, now: datetime) -> datetime:
    """When the executor next runs for ``market``: ``RUN_AFTER_CLOSE`` after the next session close after ``now``,
    in KST. ponytail: weekdays only — exchange holidays ahead are not in the price cache, so a holiday shows the
    run that will not happen; upgrade with an exchange calendar."""
    tz = ZoneInfo(MARKETS[market]["timezone"])
    day = now.astimezone(tz).date()
    while True:
        if day.weekday() < 5:
            run = session_window(market, day)[1] + RUN_AFTER_CLOSE
            if run > now:
                return run
        day += timedelta(days=1)


def _market_labels(market: str, now: datetime) -> dict:
    m = MARKETS[market]
    open_kst, close_kst = session_window(market, now.astimezone(ZoneInfo(m["timezone"])).date())
    return {"market": market, "label_ko": MARKET_KO.get(market, market), "currency": m["currency"],
            "currency_symbol": "$" if m["currency"] == "USD" else "₩", "benchmark": m["benchmark"],
            "benchmark_name_ko": m["benchmark_name_ko"], "timezone": m["timezone"], "session": m["session"],
            "session_kst": f"{open_kst:%H:%M}-{close_kst:%H:%M}",
            "name_style": "name_ticker" if market == "US" else "name"}


# --- read endpoints ----------------------------------------------------------------------

@app.get("/api/health", tags=["meta"])
def health(market: str | None = MarketQuery) -> dict:
    st = _store(market)
    return {"ok": True, "core_version": core_version, "data_dir": str(DATA_DIR), "market": st.market,
            "market_ko": MARKET_KO.get(st.market, st.market), "markets": list(MARKETS), "screens": len(st.screens),
            "decisions": len(st.decisions),
            "decisions_by_source": {s: sum(d.source == s for d in st.decisions) for s in ("agent", "rule")},
            "orders": len(st.orders), "memory_entries": len(st.memory),
            "memory_log": str(st.memory_log_path() or ""), "server_time": datetime.now(UTC).isoformat()}


@app.get("/api/labels", tags=["meta"])
def labels() -> dict:
    """UI가 쓰는 한글 라벨. 데이터의 값(rating, rule id, status)은 영문으로 고정이고 표시만 여기로 바꾼다."""
    now = datetime.now(UTC)
    return {"ratings": list(RATINGS_5_TIER) + [RATING_REVIEW], "rating_ko": RATING_KO, "rule_ko": RULE_KO,
            "rule_desc_ko": RULE_DESC_KO, "sell_reason_ko": SELL_REASON_KO, "mode_ko": MODE_KO,
            "order_status_ko": ORDER_STATUS_KO, "exception_kind_ko": EXCEPTION_KIND_KO, "exit_reason_ko": EXIT_REASON_KO,
            "risk_rule_ko": RISK_RULE_KO, "decision_source_ko": DECISION_SOURCE_KO, "market_ko": MARKET_KO,
            "halt_scope_ko": HALT_SCOPE_KO, "halt_target_ko": HALT_TARGET_KO, "markets": {m: _market_labels(m, now) for m in MARKETS},
            # one convention for both markets (docs/08 §4 Q1): a person reading both books must not flip colours
            "color_convention": {"up": "red", "down": "blue",
                                 "note": "국내 관례로 통일(시장 무관): 빨강=상승·매수, 파랑=하락·매도"}}


@app.get("/api/markets", tags=["meta"])
def markets() -> list[dict]:
    """홈 시장 요약 카드: 시장마다 평가액(그 시장 통화) · 오늘 손익 · 드로다운 · 운용 상태 · 다음 실행 시각(KST). 합산 없음.
    한 시장 기록이 파손돼도 다른 시장 카드는 나온다(그 카드만 error, 숫자는 null)."""
    now = datetime.now(UTC)
    out = []
    for m in MARKETS:
        run = next_run(m, now)
        card = _market_labels(m, now) | {"next_run_kst": run.isoformat(), "next_run_label_ko": f"{run:%m-%d %H:%M} KST"}
        try:
            st = _store(m)
            book = positions_view(st.audit.orders(), PRICES, cfg=paper_config(m), operations=st.operations)
            card |= {"as_of": book["as_of"], "equity": book["equity"], "initial_cash": book["initial_cash"],
                     "day_return": book["day_return"], "drawdown": book["drawdown"], "positions": len(book["positions"]),
                     "state": _state_out(m), "flow": _flow(st, book), "error": None}
        except ValueError as exc:                    # a cut-short line, or another market's record, in that book
            card |= dict.fromkeys(("as_of", "equity", "initial_cash", "day_return", "drawdown", "positions", "state",
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
        c["name"] = c.get("name") or _name(c["ticker"], st.screens)
        c["failed_rules"] = [f["rule"] for f in c["filters"] if not f["passed"]]
        c["failed_rules_ko"] = [RULE_KO.get(r, r) for r in c["failed_rules"]]
    return out


@app.get("/api/decisions/dates", tags=["decisions"])
def decision_dates(market: str | None = MarketQuery) -> list[str]:
    """결정일 목록(그 시장의 거래일, 내림차순). 미국 결정일은 미국 날짜다."""
    return sorted({d.trade_date for d in _store(market).decisions}, reverse=True)


@app.get("/api/decisions", tags=["decisions"])
def list_decisions(date: str | None = None, rating: str | None = None, review_only: bool = False,
                   ticker: str | None = None, overrides_only: bool = False,
                   source: Literal["agent", "rule"] | None = None, market: str | None = MarketQuery) -> list[dict]:
    st = _store(market)
    rows = st.decisions
    if source:
        rows = [d for d in rows if d.source == source]
    if date:
        rows = [d for d in rows if d.trade_date == date]
    if rating:
        rows = [d for d in rows if d.rating == rating]
    if review_only:
        rows = [d for d in rows if d.rating == RATING_REVIEW]
    if overrides_only:
        rows = [d for d in rows if d.hard_rule_overrides]
    if ticker:
        rows = [d for d in rows if d.ticker == ticker.upper()]
    memory_by_key = {(e["ticker"], e["date"]): e for e in st.memory}
    orders_by_key = _orders_by_key(st)
    return [_decision_out(d, st, memory_by_key, orders_by_key) for d in rows]


@app.get("/api/orders", tags=["execution"])
def list_orders(date: str | None = None, status: str | None = None, ticker: str | None = None,
                market: str | None = MarketQuery) -> list[dict]:
    """주문·체결 기록(화면 1 '오늘 자동 실행', 화면 3 실행 상태 띠). 주문마다 현재 상태 한 줄(미체결 → 체결·취소는 마지막 기록)."""
    _date_param(date)
    if status is not None and status not in ORDER_STATUS_KO:
        raise HTTPException(400, f"status는 {', '.join(ORDER_STATUS_KO)} 중 하나여야 합니다: {status!r}")
    st = _store(market)
    rows = st.orders
    if date:
        rows = [o for o in rows if o.trade_date == date]
    if status:
        rows = [o for o in rows if o.status == status]
    if ticker:
        rows = [o for o in rows if o.ticker == ticker.upper()]
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
    tickers = sorted({c.ticker for s in st.screens for c in s.candidates} | {d.ticker for d in st.decisions})
    latest = st.screens[0] if st.screens else None
    out = []
    for t in tickers:
        cand = next((c for c in latest.candidates if c.ticker == t), None) if latest else None
        out.append({"ticker": t, "name": _name(t, st.screens),
                    "latest_selected": cand.selected if cand else None, "latest_rank": cand.rank if cand else None})
    return out


@app.get("/api/tickers/{ticker}", tags=["tickers"])
def get_ticker(ticker: str, market: str | None = MarketQuery) -> dict:
    st = _store(market)
    t = ticker.upper()
    memory_by_key = {(e["ticker"], e["date"]): e for e in st.memory}
    orders_by_key = _orders_by_key(st)
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
    decisions = [_decision_out(d, st, memory_by_key, orders_by_key) for d in st.decisions if d.ticker == t]
    outcomes = [{"trade_date": e["date"], "rating": e["rating"], "rating_ko": RATING_KO.get(e["rating"], e["rating"]),
                 **_outcome(e)} for e in sorted(st.memory, key=lambda e: e["date"], reverse=True) if e["ticker"] == t]
    if not screening and not decisions and not outcomes:
        raise HTTPException(404, f"종목 {t!r} 기록 없음 ({st.market})")
    return {"ticker": t, "name": _name(t, st.screens), "market": st.market, "screening": screening, "decisions": decisions,
            "outcomes": outcomes, "orders": [_order_out(o, st) for o in st.orders if o.ticker == t],
            "timeline": [{"date": o["trade_date"], "rating": o["rating"]} for o in reversed(outcomes)]}


@app.get("/api/performance", tags=["performance"])
def performance(market: str | None = MarketQuery) -> dict:
    st = _store(market)
    s = st.summary
    if not s:
        raise HTTPException(404, "백테스트 요약 없음: scripts/baseline_krx.py를 먼저 실행" if st.market == "KRX" else
                            f"{MARKET_KO.get(st.market, st.market)} 결정 성과 요약 없음: "
                            f"scripts/baseline_krx.py --market {st.market}를 먼저 실행")
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
    reflections = [{"date": e["date"], "ticker": e["ticker"], "name": _name(e["ticker"], st.screens),
                    "rating": e["rating"], "rating_ko": RATING_KO.get(e["rating"], e["rating"]),
                    "raw_return": _pct(e.get("raw")), "alpha": _pct(e.get("alpha")), "resolved": e.get("resolved"),
                    "reflection": e.get("reflection") or ""}
                   for e in sorted(st.memory, key=lambda e: e["date"], reverse=True) if not e["pending"] and e.get("reflection")]
    by_rating = {r: v | {"rating_ko": RATING_KO.get(r, r)} for r, v in s.get("by_rating", {}).items()}
    return {"run_id": s.get("run_id"), "cells_run": s.get("cells_run"), "resolved": s.get("resolved"),
            "pending": s.get("pending"), "unscored": s.get("unscored"), "holding": s.get("holding"),
            "dates": s.get("dates", []), "universe": s.get("universe", []), "by_rating": by_rating,
            "per_date": per_date, "reflections": reflections[:50],
            "caveat": "알파는 결정일 다음 첫 종가 진입 기준. 규칙 전용 run은 재현 가능하지만 에이전트 run은 모델 샘플링과 피드 변동으로 재현값이 아니다."}


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
    p = paper_progress(st.orders, st.decisions, st.operations, PRICES, paper_config(m))
    for c in p["criteria"]:
        c |= {"label_ko": PAPER_CRITERIA_KO.get(c["id"], c["id"]), "threshold_ko": _threshold_ko(c["id"], c["threshold"]),
              "status_ko": PAPER_STATUS_KO[c["status"]]}
    for v in p["violations"]:
        v |= {"kind_ko": PAPER_VIOLATION_KO[v["kind"]], "name": _name(v["ticker"], st.screens) if v["ticker"] else None}
    for n in p["notes"]:
        n |= {"kind_ko": PAPER_NOTE_KO[n["kind"]], "name": _name(n["ticker"], st.screens) if n["ticker"] else None}
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
    # the live-switch modal's "rules at this capital" (docs/10 D5: the live capital input is stage 5; until then the
    # paper capital and the configured caps)
    p["go_live"] = {"capital": p["initial_cash"], "capital_source_ko": "모의 자본(실거래 자본 입력은 단계 5)",
                    "auto_order_max": caps["auto_order_max"], "auto_daily_order_cap": caps["auto_daily_order_cap"],
                    "daily_loss_limit": caps["auto_daily_loss_limit"],
                    "daily_loss_amount": p["initial_cash"] * caps["auto_daily_loss_limit"],
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
    return {"screen_rules": screen_rules, "screen_params": {k: v for k, v in vars(cfg).items() if k != "rules"},
            "risk_rules": risk_rules, "auto_rules": auto_rules,
            "auto_by_market": {m: auto_execution(m) for m in MARKETS}, "paper_criteria": criteria,
            "market": MARKETS[st.market], "market_code": st.market, "markets": MARKETS,
            "mode": _state_out(st.market), "change_history": history, "history_errors": history_errors}


@app.get("/api/positions", tags=["positions"])
def positions(as_of: str | None = None, market: str | None = MarketQuery) -> dict:
    """모의투자 장부(화면 5): 그 시장의 orders.jsonl 체결 기록을 캐시 종가로 재생한 보유·평가·드로다운·가드 상태. 저장된 상태 없음."""
    _date_param(as_of, "as_of")
    st = _store(market)
    out = positions_view(st.audit.orders(), PRICES, as_of=as_of, cfg=paper_config(st.market), operations=st.operations)
    for r in out["positions"] + out["closed"]:
        r["name"] = _name(r["ticker"], st.screens)
    for g in out["protections"]:
        g["label_ko"] = RISK_RULE_KO.get(g["id"], g["id"])
    out["exits_by_reason_ko"] = {EXIT_REASON_KO.get(k, k): n for k, n in out["exits_by_reason"].items()}
    out["mode"] = _state_out(st.market)
    return out


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


class ResumeRequest(BaseModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    scope: str = Field(default="all", description="all(기본, 정지된 모든 시장) | KRX | US")
    reset_peak: bool = Field(default=True, description="−20% 드로다운 정지 뒤에만 의미: 드로다운 피크를 지금 평가액으로 재설정. "
                                                        "false면 피크를 그대로 둔다(드로다운이 여전히 −20%면 다음 실행에서 다시 정지)")


@app.post("/api/trading/resume", tags=["trading"])
def resume(body: ResumeRequest, x_admin_token: str | None = Header(default=None)) -> dict:
    """재가동. 관리자만(kill_switch_reset = admin). scope=all이면 정지된 모든 시장, 시장 이름이면 그 시장만 — 다른 시장은 그대로."""
    role = _require_admin(x_admin_token)
    markets = _scope_markets(body.scope)
    resumed, skipped, peak_resets = [], [], {}
    with _locks(markets):
        at = _now()
        for m in markets:
            st = STORES[m].refresh()
            state = st.state()
            if state["running"]:
                skipped.append({"market": m, "reason": "이미 실행 중"})
                continue
            # a halt that could not read the orders log left its open orders standing; they must not fill at an old open
            stale = [o for o in open_orders(st.audit, keep_sells=state["keep_exits"]) if o.placed_at <= state["halted_at"]]
            after = {"running": True} | ({"open_orders_cancelled": [o.order_id for o in stale]} if stale else {})
            # after a −20% stop: the book's equity at the last close becomes the peak the drawdown counts from
            if body.reset_peak and dd_stop_standing(st.operations):
                book = positions_view(st.audit.orders(), PRICES, cfg=paper_config(m), operations=st.operations)
                after |= {"peak_reset_to": book["equity"], "peak_reset_at": book["as_of"], "peak_before": book["peak"]}
                peak_resets[m] = {"to": book["equity"], "at": book["as_of"], "before": book["peak"]}
            st.audit.record_operation(OperationRecord(
                kind="resume", at=at, operator=body.operator, role=role, reason=body.reason, market=m, scope=body.scope,
                target="manual", before={"running": False}, after=after))
            cancel_orders(st.audit, stale, f"정지 전 미체결 — 재가동 시 취소 ({body.operator})")
            resumed.append(m)
        if not resumed:
            raise HTTPException(409, "이미 실행 중입니다" + ("" if len(markets) == 1 else " (모든 시장)"))
    return {"scope": body.scope, "scope_ko": HALT_SCOPE_KO.get(body.scope, body.scope), "resumed": resumed,
            "skipped": skipped, "peak_resets": peak_resets, "states": {m: _state_out(m) for m in markets}}


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


class ApproveRequest(BaseModel):
    approver: str
    note: str = ""


@app.post("/api/decisions/{ticker}/{trade_date}/approve", tags=["decisions"], status_code=501)
def approve(ticker: str, trade_date: str, body: ApproveRequest, market: str | None = MarketQuery) -> dict:
    """수동 승인 모드 전용(v0.3). 자동·모의 모드에서는 승인 단계가 없어 409. 실행 레이어 전까지 501."""
    st = _store(market)
    if st.state()["mode"] != "manual":
        raise HTTPException(409, "자동 모드에서는 승인 단계가 없습니다. 설정에서 수동 승인 모드로 바꾼 뒤 사용하세요.")
    exists = any(d.ticker == ticker.upper() and d.trade_date == trade_date for d in st.decisions)
    if not exists:
        raise HTTPException(404, f"결정 {ticker} {trade_date} 없음")
    return {"implemented": False, "message": "실행 레이어(주문·체결)가 아직 없습니다. 승인 요청은 접수 형식만 검증했습니다.",
            "ticker": ticker.upper(), "trade_date": trade_date, "approver": body.approver}
