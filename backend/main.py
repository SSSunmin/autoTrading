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

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent_trading_core import __version__ as core_version
from agent_trading_core.audit import (
    AuditLog,
    DecisionRecord,
    OperationRecord,
    OrderRecord,
    ScreenRun,
    latest_orders,
    market_audit,
    stage_counts,
    trading_state,
)
from agent_trading_core.config import AUTO_EXECUTION, MARKETS, PAPER_TRADING, auto_execution
from agent_trading_core.execution import (
    EXIT_SUFFIX,
    Prices,
    cancel_orders,
    check_date,
    execution_lock,
    open_orders,
    paper_config,
    positions_view,
)
from agent_trading_core.krx_names import (
    AUTO_RULE_KO,
    DECISION_SOURCE_KO,
    EXCEPTION_KIND_KO,
    EXIT_REASON_KO,
    HALT_SCOPE_KO,
    HALTED_BY_KO,
    MARKET_KO,
    MODE_KO,
    OPERATION_KO,
    ORDER_STATUS_KO,
    PAPER_CRITERIA_KO,
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
        if self.market == "KRX":
            paths.append(DATA_DIR / "baseline_summary.json")
            log = self.memory_log_path()
            if log:
                paths.append(log)
        return paths

    def memory_log_path(self) -> Path | None:
        """The memory log and the backtest summary are the KRX baseline's (scripts/baseline_krx.py); US has none yet."""
        if self.market != "KRX":
            return None
        env = os.environ.get("TRADING_MEMORY_LOG")
        if env:
            return Path(env)
        summary = DATA_DIR / "baseline_summary.json"
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
        summary = DATA_DIR / "baseline_summary.json"
        self.summary = json.loads(summary.read_text(encoding="utf-8")) if self.market == "KRX" and summary.exists() else {}
        self._stamp = stamp
        return self

    def state(self) -> dict:
        return trading_state(self.operations, default_mode=AUTO_EXECUTION["default_mode"])


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
            "rules": s.rules, "rules_ko": [RULE_KO.get(r, r) for r in s.rules], "stage_counts": counts}


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
    label = f"{market_ko} 정지됨 · {st['halted_at'][:16].replace('T', ' ')} UTC · {by_ko}" if not st["running"] else (
        f"{market_ko} 자동 매매 실행 중" if st["mode"] == "auto" else f"{market_ko} {mode_ko} 모드")
    return st | {"market": market, "market_ko": market_ko, "mode_ko": mode_ko, "label_ko": label, "halted_by_ko": by_ko,
                 "admin_token_required": ADMIN_TOKEN is not None}


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
            "halt_scope_ko": HALT_SCOPE_KO, "markets": {m: _market_labels(m, now) for m in MARKETS},
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
            book = positions_view(_store(m).audit.orders(), PRICES, cfg=paper_config(m))
            card |= {"as_of": book["as_of"], "equity": book["equity"], "initial_cash": book["initial_cash"],
                     "day_return": book["day_return"], "drawdown": book["drawdown"], "positions": len(book["positions"]),
                     "state": _state_out(m), "error": None}
        except ValueError as exc:                    # a cut-short line, or another market's record, in that book
            card |= dict.fromkeys(("as_of", "equity", "initial_cash", "day_return", "drawdown", "positions", "state")) | {
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
                            f"{MARKET_KO.get(st.market, st.market)} 결정 성과 요약 없음: 결정 베이스라인은 아직 국내(baseline_krx)만 있음")
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
    out = positions_view(st.audit.orders(), PRICES, as_of=as_of, cfg=paper_config(st.market))
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
                states[m] = trading_state(audits[m].operations(), default_mode=AUTO_EXECUTION["default_mode"])
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
                reason=body.reason + (f" ({error})" if error else ""), target="running",
                # an unreadable log: the state before is unknown, and an append-only line must not claim it
                before={"running": None, "unreadable": True} if before is None
                else {"running": True} if before["running"] else {"running": False, "keep_exits": True},
                after={"running": False, "keep_exits": body.keep_exits,
                       "open_orders_cancelled": None if pending is None else [o.order_id for o in pending]})
            audit.record_operation(op)
            cancelled = cancel_orders(audit, pending or [], f"긴급 정지 ({body.operator}: {body.reason})")
            try:
                st = trading_state(audit.operations(), default_mode=AUTO_EXECUTION["default_mode"])
            except ValueError:
                st = trading_state([op], default_mode=AUTO_EXECUTION["default_mode"])  # mode unknown: the log is unreadable
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


@app.post("/api/trading/resume", tags=["trading"])
def resume(body: ResumeRequest, x_admin_token: str | None = Header(default=None)) -> dict:
    """재가동. 관리자만(kill_switch_reset = admin). scope=all이면 정지된 모든 시장, 시장 이름이면 그 시장만 — 다른 시장은 그대로."""
    role = _require_admin(x_admin_token)
    markets = _scope_markets(body.scope)
    resumed, skipped = [], []
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
            st.audit.record_operation(OperationRecord(
                kind="resume", at=at, operator=body.operator, role=role, reason=body.reason, market=m, scope=body.scope,
                target="running", before={"running": False},
                after={"running": True} | ({"open_orders_cancelled": [o.order_id for o in stale]} if stale else {})))
            cancel_orders(st.audit, stale, f"정지 전 미체결 — 재가동 시 취소 ({body.operator})")
            resumed.append(m)
        if not resumed:
            raise HTTPException(409, "이미 실행 중입니다" + ("" if len(markets) == 1 else " (모든 시장)"))
    return {"scope": body.scope, "scope_ko": HALT_SCOPE_KO.get(body.scope, body.scope), "resumed": resumed,
            "skipped": skipped, "states": {m: _state_out(m) for m in markets}}


class ModeRequest(BaseModel):
    operator: str = Field(min_length=1)
    mode: Literal["auto", "manual", "paper"]
    reason: str = Field(min_length=1)
    market: str = Field(description="KRX | US — 모드는 시장마다 따로(기본값 없음: 다른 시장 모드를 실수로 바꾸지 않게)")


@app.post("/api/trading/mode", tags=["trading"])
def set_mode(body: ModeRequest, x_admin_token: str | None = Header(default=None)) -> dict:
    """그 시장의 운용 모드 전환(관리자). 자동 / 수동 승인 / 모의투자."""
    role = _require_admin(x_admin_token)
    m = _market(body.market)
    with _locks([m]):                                # the executor checks the mode under the same lock
        st = STORES[m].refresh()
        state = st.state()
        if state["mode"] == body.mode:
            raise HTTPException(409, f"{MARKET_KO[m]}은 이미 {MODE_KO[body.mode]} 모드입니다")
        # paper orders do not outlive paper mode: left open they would fill at an old open when paper comes back
        pending = open_orders(st.audit) if state["mode"] == "paper" else []
        st.audit.record_operation(OperationRecord(
            kind="mode_change", operator=body.operator, role=role, reason=body.reason, market=m, target="mode",
            before={"mode": state["mode"]},
            after={"mode": body.mode} | ({"open_orders_cancelled": [o.order_id for o in pending]} if pending else {})))
        cancel_orders(st.audit, pending, f"운용 모드 변경 paper → {body.mode} ({body.operator})")
    return {"state": _state_out(m)}


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
