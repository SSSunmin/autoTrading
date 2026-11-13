"""Paper execution: a decision → the hard rules → a fill at the next trading day's open → ``orders.jsonl``.

The paper book is never stored. Cash, holdings and the equity curve are replayed from the orders log
over the cached prices (``replay``), the way the trading state is replayed from the operations log.
One run executes one decision date (docs/02 화면 3 실행 흐름):

1. refuse unless the loop is running in paper mode; halted, auto or manual writes nothing
2. fill the open orders whose next trading day now has a price
3. each decision of the date becomes one order keyed ``paper-<date>-<ticker>``, so a second run writes
   nothing: Buy/Overweight buy names not held, sized by ``risk.size_targets`` under the guards (drawdown,
   daily loss, regime, Protections); Sell/Underweight sell the whole holding; Hold places nothing; REVIEW
   is an exception. A buy over an ``AUTO_EXECUTION`` cap is an exception, not shrunk; sells reduce risk and are not capped.
4. the order fills at the next trading day's open (slippage, commission, sell tax), or stays open when that
   day has not happened yet. A halt cancels the open ones (``cancel_open_orders``).

Rules that close a position on their own (stop-loss, trailing, expiry, the −20% liquidation) are shown on the
positions screen but place no order here: an order without a decision record is what the paper gate counts
as unexplained (``PAPER_TRADING.max_unexplained_orders``).
ponytail: no sector cap (sector lookup is a network call) and no −20% stop-out lock replay; both come with
the rule-exit orders, once those have a decision record of their own.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from agent_trading_core.audit import AuditLog, DecisionRecord, OrderRecord, latest_orders, trading_state
from agent_trading_core.config import AUTO_EXECUTION, KRX_MARKET
from agent_trading_core.files import locked
from agent_trading_core.rating import RATING_REVIEW
from agent_trading_core.risk import (
    EPS,
    Position,
    Protections,
    RiskConfig,
    TradeOutcome,
    check_exit,
    daily_loss_blocks,
    drawdown_scale,
    regime_on,
    size_targets,
)
from agent_trading_core.symbols import safe_ticker_component

BUY_RATINGS = ("Buy", "Overweight")
SELL_RATINGS = ("Sell", "Underweight")


@dataclass(frozen=True)
class PaperConfig:
    initial_cash: float = 100_000_000.0      # simulate.SimConfig default
    commission: float = 0.00015              # per side
    slippage: float = 0.0005                 # per side
    sell_tax: float = KRX_MARKET["sell_tax_kospi"]
    regime_filter: bool = True               # docs/05 교훈 2: on by default
    index: str = "^KS11"                     # sets the trading calendar and feeds the regime filter


class ExecutionRefused(RuntimeError):
    """The loop may not place orders now (halted, not paper mode, out of order). Nothing was written."""


def check_date(value: str) -> str:
    """'YYYY-MM-DD' or ValueError. Zero-padded only: dates are compared as strings."""
    if len(value) != 10 or datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
        raise ValueError(f"not YYYY-MM-DD: {value!r}")
    return value


def execution_lock(audit_dir: str | Path):
    """One writer of orders and trading state at a time: the executor, halt, resume and mode change all hold it,
    so a halt or a mode change cannot land between the state check and the orders it allowed."""
    return locked(Path(audit_dir) / "execution")


_EMPTY = pd.DataFrame({"Open": [], "Close": []}, index=pd.Index([], dtype=object), dtype=float)


class Prices:
    """Daily Open/Close per ticker from the OHLCV cache, indexed by 'YYYY-MM-DD'. Read only: never downloads.
    ``frames`` (same shape) stand in for the files in tests."""

    def __init__(self, cache_dir: str | Path | None = None, frames: dict[str, pd.DataFrame] | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._frames = dict(frames or {})
        self._files: dict[str, tuple[int, pd.DataFrame]] = {}

    def frame(self, ticker: str) -> pd.DataFrame:
        if ticker in self._frames:
            return self._frames[ticker]
        if self.cache_dir is None:
            return _EMPTY
        path = self.cache_dir / f"{safe_ticker_component(ticker)}-YFin-data.csv"
        if not path.exists():
            return _EMPTY
        stamp = path.stat().st_mtime_ns
        hit = self._files.get(ticker)
        if hit and hit[0] == stamp:
            return hit[1]
        raw = pd.read_csv(path, encoding="utf-8", on_bad_lines="skip")
        f = pd.DataFrame({"Open": pd.to_numeric(raw["Open"], errors="coerce").to_numpy(),
                          "Close": pd.to_numeric(raw["Close"], errors="coerce").to_numpy()},
                         index=raw["Date"].astype(str).str[:10])
        f = f[~f.index.duplicated(keep="last")].sort_index()
        self._files[ticker] = (stamp, f)
        return f

    def calendar(self, index: str) -> list[str]:
        return list(self.frame(index)["Close"].dropna().index)

    def close_asof(self, ticker: str, date: str) -> float | None:
        s = self.frame(ticker)["Close"]
        s = s[s.index <= date].dropna()
        return float(s.iloc[-1]) if len(s) else None

    def open_on(self, ticker: str, date: str) -> float | None:
        f = self.frame(ticker)
        if date not in f.index:
            return None
        v = f.at[date, "Open"]
        return float(v) if pd.notna(v) and v > 0 else None


def next_session(calendar: list[str], date: str) -> str | None:
    """The first trading day strictly after ``date`` (weekends and holidays skipped), None if not yet in the data."""
    i = bisect.bisect_right(calendar, date)
    return calendar[i] if i < len(calendar) else None


def _day_index(calendar: list[str], date: str) -> int:
    """Index of the last trading day on or before ``date`` (-1 before the calendar starts)."""
    return bisect.bisect_right(calendar, date) - 1


# --- the paper book, replayed from the orders log ------------------------------------------------

@dataclass
class Holding:
    ticker: str
    quantity: int
    avg_price: float                         # fill prices (slippage in), commission not
    entry_date: str


@dataclass(frozen=True)
class ClosedTrade:
    ticker: str
    entry_date: str
    exit_date: str
    quantity: int
    entry_price: float
    exit_price: float

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        return self.exit_price / self.entry_price - 1


@dataclass
class Book:
    cash: float
    holdings: dict[str, Holding]
    closed: list[ClosedTrade]
    equity: list[tuple[str, float]]          # close-of-day equity, each trading day from the first fill to ``until``


def replay(orders: list[OrderRecord], prices: Prices, cfg: PaperConfig, until: str) -> Book:
    """Cash, holdings and equity after every fill up to and including ``until``. Same-day sells settle before buys."""
    fills = sorted((o for o in latest_orders(orders).values()
                    if o.status in ("filled", "partial") and o.filled_quantity > 0 and o.filled_price
                    and o.fill_date and o.fill_date <= until),
                   key=lambda o: (o.fill_date, o.side != "Sell"))
    book = Book(cfg.initial_cash, {}, [], [])

    def apply(o: OrderRecord) -> None:
        q, px = o.filled_quantity, float(o.filled_price)
        held = book.holdings.get(o.ticker)
        if o.side == "Buy":
            book.cash -= q * px * (1 + cfg.commission)
            if held:
                held.avg_price = (held.avg_price * held.quantity + px * q) / (held.quantity + q)
                held.quantity += q
            else:
                book.holdings[o.ticker] = Holding(o.ticker, q, px, o.fill_date)
            return
        q = min(q, held.quantity if held else 0)
        if q == 0:
            return
        book.cash += q * px * (1 - cfg.commission - cfg.sell_tax)
        book.closed.append(ClosedTrade(o.ticker, held.entry_date, o.fill_date, q, held.avg_price, px))
        held.quantity -= q
        if held.quantity == 0:
            del book.holdings[o.ticker]

    i = 0
    if fills:
        for day in (d for d in prices.calendar(cfg.index) if fills[0].fill_date <= d <= until):
            while i < len(fills) and fills[i].fill_date <= day:
                apply(fills[i])
                i += 1
            value = sum(h.quantity * (prices.close_asof(h.ticker, day) or h.avg_price) for h in book.holdings.values())
            book.equity.append((day, book.cash + value))
    for o in fills[i:]:                      # fills on days the index calendar does not have
        apply(o)
    return book


def _protections(closed: list[ClosedTrade], calendar: list[str], risk: RiskConfig) -> Protections:
    p = Protections(risk)
    for t in sorted(closed, key=lambda t: t.exit_date):
        # every exit here follows a decision; rule exits (stop_loss …) would feed StoplossGuard with their reason
        p.record(TradeOutcome(t.ticker, _day_index(calendar, t.exit_date), "exit_signal", t.pnl_pct))
    return p


# --- placing and filling ----------------------------------------------------------------------------

def _order_id(d: DecisionRecord) -> str:
    return f"paper-{d.trade_date}-{d.ticker}"


def _rule_ids(texts: list[str]) -> list[str]:
    ids = []
    for t in texts:
        rid = "dd_halve" if t.startswith("가드 스케일") else t.split(":")[0]
        if rid not in ids:
            ids.append(rid)
    return ids


def _place(decisions: list[DecisionRecord], date: str, current: dict[str, OrderRecord], prices: Prices,
           risk: RiskConfig, cfg: PaperConfig, calendar: list[str]) -> list[OrderRecord]:
    """One order per decision not ordered yet, open or already refused, sized from the book as of ``date``'s close.
    ``decisions`` is the whole day, so a rerun after a crash sizes the rest against the same denominator."""
    book = replay(list(current.values()), prices, cfg, until=date)
    equity = book.equity[-1][1] if book.equity else book.cash
    peak = max([cfg.initial_cash] + [e for _, e in book.equity])
    day_ret = None
    if book.equity and book.equity[-1][0] == date:
        prev = book.equity[-2][1] if len(book.equity) > 1 else cfg.initial_cash
        day_ret = equity / prev - 1

    # guards on new buys, all decided from data up to ``date``
    locks: dict[str, str] = {}                                              # rule id → text
    scale, why = drawdown_scale(equity, peak, risk)
    if scale == 0.0:
        locks["dd_stop"] = why
    if day_ret is not None and (blocked := daily_loss_blocks(day_ret, risk)):
        locks["daily_loss_limit"] = blocked
    index_closes = prices.frame(cfg.index)["Close"]
    if cfg.regime_filter and not regime_on(index_closes[index_closes.index <= date], risk):
        locks["regime"] = f"국면 필터: {cfg.index}가 {risk.regime_ma_days}일선 아래, 신규 매수 없음"
    protections = _protections(book.closed, calendar, risk)
    day = _day_index(calendar, date)
    if g := protections.locked_global(day):
        locks["StoplossGuard"] = g

    # equal weight across every buy-rated name, held or not, as simulate.run_simulation sizes a rebalance
    buys = [d.ticker for d in decisions if d.rating in BUY_RATINGS]
    # at scale 0 every buy is already locked above; size at full scale so the text names only the caps
    weights, overrides = size_targets(buys, risk, scale=scale if scale > 0 else 1.0)
    shared = [o for o in overrides if not o.startswith("max_position_pct")]
    # orders the book as of ``date`` does not show yet: open, or filled at a session after ``date`` (a decision dated
    # on a holiday shares its next session with the trading day before it)
    def unsettled(o: OrderRecord) -> bool:
        return o.status == "open" or (o.status in ("filled", "partial") and (o.fill_date or "") > date)
    pending_buys = {o.ticker for o in current.values() if o.side == "Buy" and unsettled(o)}
    pending_sells = {o.ticker for o in current.values() if o.side == "Sell" and unsettled(o)}
    # the daily cap counts every buy filling at the same session, whatever its decision date
    session = next_session(calendar, date)
    day_amount = 0.0
    for o in current.values():
        if o.side == "Buy" and o.status in ("filled", "partial", "open") and next_session(calendar, o.trade_date) == session:
            day_amount += o.amount if o.amount is not None else o.quantity * (prices.close_asof(o.ticker, o.trade_date) or 0.0)

    out: list[OrderRecord] = []
    for d in sorted(decisions, key=lambda d: d.rating not in SELL_RATINGS):   # sells first, then decision order
        if _order_id(d) in current:
            continue
        base = {"order_id": _order_id(d), "ticker": d.ticker, "trade_date": date}
        if d.rating == RATING_REVIEW or not d.tradeable:
            out.append(OrderRecord(**base, side="Sell" if d.action == "Sell" else "Buy", quantity=0, status="exception",
                                   exception_kind="review", reason="등급을 읽을 수 없어 주문하지 않았습니다"))
            continue
        if d.rating not in BUY_RATINGS + SELL_RATINGS:
            continue                                                        # Hold: no order
        ref = prices.close_asof(d.ticker, date)
        if d.rating in SELL_RATINGS:
            held = book.holdings.get(d.ticker)
            if d.ticker in pending_sells:
                out.append(OrderRecord(**base, side="Sell", quantity=0, status="blocked", rules_applied=["already_selling"],
                                       reason="이미 매도 주문이 나가 있음 — 중복 매도 없음"))
                continue
            if held is None and d.ticker in pending_buys:
                out.append(OrderRecord(**base, side="Sell", quantity=0, status="blocked", rules_applied=["pending_buy"],
                                       reason="매수가 아직 장부에 없음(같은 세션 체결 대기) — 이번 결정으로는 매도하지 않음"))
                continue
            if held is None:
                out.append(OrderRecord(**base, side="Sell", quantity=0, status="blocked", rules_applied=["no_short"],
                                       reason="보유 없음 — 매도할 수량이 없습니다 (공매도하지 않음)"))
                continue
            side, qty, rules, note = "Sell", held.quantity, [], f"보유 {held.quantity}주 전량 매도"
        else:
            if d.ticker in book.holdings or d.ticker in pending_buys:
                out.append(OrderRecord(**base, side="Buy", quantity=0, status="blocked", rules_applied=["already_held"],
                                       reason="이미 보유 중 — 추가 매수 없음"))
                continue
            if locks:
                out.append(OrderRecord(**base, side="Buy", quantity=0, status="blocked", rules_applied=list(locks),
                                       reason=" · ".join(locks.values())))
                continue
            if tl := protections.locked_ticker(d.ticker, day):
                out.append(OrderRecord(**base, side="Buy", quantity=0, status="blocked",
                                       rules_applied=[tl.split(":")[0]], reason=tl))
                continue
            mine = [o for o in overrides if o.startswith(f"max_position_pct: {d.ticker} ")] + shared
            if ref is None:
                out.append(OrderRecord(**base, side="Buy", quantity=0, status="exception", exception_kind="vendor_unavailable",
                                       reason=f"결정일 {date} 종가 없음 — 수량을 정할 수 없어 주문하지 않았습니다"))
                continue
            target = weights.get(d.ticker, 0.0) * equity
            unit = ref * (1 + cfg.slippage) * (1 + cfg.commission)
            qty = int(target // unit)
            rules = _rule_ids(mine)
            if qty <= 0:
                out.append(OrderRecord(**base, side="Buy", quantity=0, status="blocked", rules_applied=rules,
                                       reason=f"목표 금액 {target:,.0f}원 < 1주 {unit:,.0f}원" + (f" · {' · '.join(mine)}" if mine else "")))
                continue
            side, note = "Buy", f"목표 비중 {weights[d.ticker]:.1%} × 평가 {equity:,.0f}원" + (f" · {' · '.join(mine)}" if mine else "")
        if side == "Sell":                     # risk-reducing: the auto-execution caps limit buys only
            out.append(OrderRecord(**base, side=side, quantity=qty, status="open", rules_applied=rules,
                                   reason=f"모의 주문: {note} · 다음 거래일 시가"))
            continue
        amount = qty * ref
        if amount > AUTO_EXECUTION["auto_order_max"]:
            out.append(OrderRecord(**base, side=side, quantity=qty, status="exception", exception_kind="cap_exceeded",
                                   rules_applied=rules + ["auto_order_max"],
                                   reason=f"주문 금액 {amount:,.0f}원 > 주문당 상한 {AUTO_EXECUTION['auto_order_max']:,.0f}원"))
            continue
        if day_amount + amount > AUTO_EXECUTION["auto_daily_order_cap"]:
            out.append(OrderRecord(**base, side=side, quantity=qty, status="exception", exception_kind="cap_exceeded",
                                   rules_applied=rules + ["auto_daily_order_cap"],
                                   reason=f"{session or '다음 거래일'} 매수 금액 {day_amount + amount:,.0f}원 > 일일 상한 {AUTO_EXECUTION['auto_daily_order_cap']:,.0f}원"))
            continue
        day_amount += amount
        out.append(OrderRecord(**base, side=side, quantity=qty, status="open", rules_applied=rules,
                               reason=f"모의 주문: {note} · 다음 거래일 시가"))
    return out


def _fill(pending: list[OrderRecord], current: dict[str, OrderRecord], prices: Prices, cfg: PaperConfig,
          calendar: list[str]) -> list[OrderRecord]:
    """Fill open orders at the open of the trading day after their decision date. Updates ``current``;
    returns the new versions. Orders whose fill day is not in the data yet stay open."""
    by_day: dict[str, list[OrderRecord]] = {}
    for o in pending:
        if (f := next_session(calendar, o.trade_date)) is not None:
            by_day.setdefault(f, []).append(o)
    out = []
    for day in sorted(by_day):
        book = replay(list(current.values()), prices, cfg, until=day)
        cash = book.cash
        for o in sorted(by_day[day], key=lambda o: o.side != "Sell"):
            px_open = prices.open_on(o.ticker, day)
            if px_open is None:
                new = o.model_copy(update={"status": "exception", "exception_kind": "vendor_unavailable",
                                           "reason": f"체결일 {day} 시가 없음 (거래정지 또는 데이터 공백) — 주문하지 않았습니다"})
            elif o.side == "Sell":
                held = book.holdings.get(o.ticker)
                q = min(o.quantity, held.quantity if held else 0)
                px = px_open * (1 - cfg.slippage)
                cash += q * px * (1 - cfg.commission - cfg.sell_tax)
                new = _filled(o, q, px, day, px_open, cfg) if q else o.model_copy(update={
                    "status": "rejected", "exception_kind": "order_rejected", "reason": f"체결일 {day} 보유 없음 — 매도 거부"})
            else:
                px = px_open * (1 + cfg.slippage)
                q = min(o.quantity, int(cash // (px * (1 + cfg.commission))))
                cash -= q * px * (1 + cfg.commission)
                new = _filled(o, q, px, day, px_open, cfg) if q > 0 else o.model_copy(update={
                    "status": "rejected", "exception_kind": "order_rejected",
                    "reason": f"현금 부족 — 체결일 {day} 1주 {px:,.0f}원, 현금 {cash:,.0f}원"})
            current[o.order_id] = new
            out.append(new)
    return out


def _filled(o: OrderRecord, q: int, px: float, day: str, px_open: float, cfg: PaperConfig) -> OrderRecord:
    note = f"모의 체결 {day} 시가 {px_open:,.0f}원 (슬리피지 {cfg.slippage:.2%})"
    if q < o.quantity:
        note += f" · {o.quantity}주 중 {q}주, 잔량 취소(현금 부족)"
    return o.model_copy(update={"status": "filled" if q == o.quantity else "partial", "filled_quantity": q,
                                "filled_price": round(px, 4), "fill_date": day, "reason": f"{o.reason} → {note}"})


def _require_paper_running(audit: AuditLog) -> None:
    state = trading_state(audit.operations(), default_mode=AUTO_EXECUTION["default_mode"])
    if not state["running"]:
        raise ExecutionRefused(f"정지 중이라 주문하지 않습니다 ({state['halted_at']}, {state['halted_by']}: {state['halt_reason']})")
    if state["mode"] != "paper":
        raise ExecutionRefused(f"운용 모드가 {state['mode']}입니다. 이 실행기는 모의투자(paper) 모드에서만 주문합니다"
                               " (실거래 주문 경로 없음, 수동 승인은 승인 API로)")


def execute_date(audit: AuditLog, date: str, prices: Prices, *, risk: RiskConfig | None = None,
                 cfg: PaperConfig | None = None) -> list[OrderRecord]:
    """Run the paper loop for one decision date. Returns the lines appended; a second run of the same date
    appends only fills of orders that were still open."""
    check_date(date)
    risk, cfg = risk or RiskConfig(), cfg or PaperConfig()
    with execution_lock(audit.directory):
        _require_paper_running(audit)
        current = latest_orders(audit.orders())          # a broken line raises here, before anything is appended
        later = sorted({o.trade_date for o in current.values() if o.trade_date > date})
        if later:
            raise ExecutionRefused(f"{date}보다 늦은 결정일({later[-1]})의 주문이 이미 있습니다. 결정일 순서대로 실행합니다")
        calendar = prices.calendar(cfg.index)
        if not calendar:
            raise ExecutionRefused(f"거래일 달력({cfg.index} 가격)이 없습니다")
        if calendar[-1] < date:
            # sizing, guards and the daily cap would read a book that stops short of the decision day
            raise ExecutionRefused(f"가격 데이터가 {calendar[-1]}까지라 {date} 결정을 실행하지 않습니다 (캐시 갱신 필요)")
        written = _fill([o for o in current.values() if o.status == "open"], current, prices, cfg, calendar)
        latest_decision: dict[str, DecisionRecord] = {}
        for d in audit.decisions():
            if d.trade_date == date:
                latest_decision[d.ticker] = d                # a re-recorded decision replaces the earlier one
        placed = _place(list(latest_decision.values()), date, current, prices, risk, cfg, calendar)
        for o in placed:
            current[o.order_id] = o
        filled = {o.order_id: o for o in _fill([o for o in placed if o.status == "open"], current, prices, cfg, calendar)}
        written += [filled.get(o.order_id, o) for o in placed]
        for o in written:
            audit.record_order(o)
        return written


def open_orders(audit: AuditLog, *, keep_sells: bool = False) -> list[OrderRecord]:
    """Orders still open. ``keep_sells``: leave exits standing (a halt with keep_exits). Raises on a broken log line."""
    return [o for o in latest_orders(audit.orders()).values()
            if o.status == "open" and not (keep_sells and o.side == "Sell")]


def cancel_orders(audit: AuditLog, orders: list[OrderRecord], reason: str) -> list[OrderRecord]:
    """Append a cancelled line for each order. The caller holds ``execution_lock`` (halt and mode change do)."""
    out = [o.model_copy(update={"status": "cancelled", "reason": f"{o.reason} → 취소: {reason}"}) for o in orders]
    for o in out:
        audit.record_order(o)
    return out


# --- the positions screen (docs/02 화면 5) -----------------------------------------------------------

def positions_view(orders: list[OrderRecord], prices: Prices, *, as_of: str | None = None,
                   risk: RiskConfig | None = None, cfg: PaperConfig | None = None) -> dict:
    """Holdings with their stop line, trailing high and expiry, the book's equity and drawdown, the guards'
    state, as of ``as_of`` (default: the last trading day in the data). Rule verdicts are shown, not acted on."""
    risk, cfg = risk or RiskConfig(), cfg or PaperConfig()
    calendar = prices.calendar(cfg.index)
    as_of = check_date(as_of) if as_of else (calendar[-1] if calendar else datetime.now().strftime("%Y-%m-%d"))
    book = replay(orders, prices, cfg, until=as_of)
    day = _day_index(calendar, as_of)
    rows = []
    for h in sorted(book.holdings.values(), key=lambda h: h.entry_date):
        closes = prices.frame(h.ticker)["Close"]
        since = closes[(closes.index >= h.entry_date) & (closes.index <= as_of)].dropna()
        last = float(since.iloc[-1]) if len(since) else None
        entry_i = _day_index(calendar, h.entry_date)
        # the trailing high is the highest close before today's bar, as check_exit measures it
        high = max([h.avg_price] + [float(c) for c in since.iloc[:-1]])
        pos = Position(h.ticker, entry_i, h.avg_price, h.quantity, high_water=high)
        active = high / h.avg_price - 1 >= risk.trailing_activate_pct - EPS
        signal = check_exit(pos, last, day, risk) if last is not None else None
        expiry_i = entry_i + risk.max_holding_days
        rows.append({
            "ticker": h.ticker, "quantity": h.quantity, "avg_price": h.avg_price, "entry_date": h.entry_date,
            "last_price": last, "last_date": since.index[-1] if len(since) else None,
            "market_value": h.quantity * last if last is not None else None,
            "unrealized": (last - h.avg_price) * h.quantity if last is not None else None,
            "unrealized_pct": last / h.avg_price - 1 if last is not None else None,
            "stop_price": h.avg_price * (1 + risk.stop_loss_pct),
            "trailing_active": active, "trailing_high": high,
            "trailing_stop_price": high * (1 - risk.trailing_pct) if active else None,
            "holding_days": day - entry_i,
            "expiry_date": calendar[expiry_i] if 0 <= expiry_i < len(calendar) else None,
            "exit_signal": {"reason": signal.reason, "note": signal.note} if signal else None,
        })
    value = sum(r["market_value"] or 0.0 for r in rows)
    equity = book.cash + value
    peak = max([cfg.initial_cash, equity] + [e for _, e in book.equity])
    for r in rows:
        r["weight"] = (r["market_value"] or 0.0) / equity if equity > 0 else None
    scale, why = drawdown_scale(equity, peak, risk)
    protections = _protections(book.closed, calendar, risk)

    def until(i: int) -> str | None:
        return calendar[i] if 0 <= i < len(calendar) else None

    locked_tickers = {t: protections.locked_ticker(t, day) for t in protections.ticker_lock_until}
    guards = [
        {"id": "StoplossGuard", "active": protections.locked_global(day) is not None, "detail": protections.locked_global(day),
         "until": until(protections.global_lock_until) if protections.locked_global(day) else None, "tickers": []},
        {"id": "MaxDrawdown", "active": scale < 1.0, "detail": why, "until": None, "tickers": []},
    ] + [
        {"id": kind, "active": bool(hits), "detail": "; ".join(r for _, r in hits) or None, "until": None,
         "tickers": [{"ticker": t, "until": until(protections.ticker_lock_until[t])} for t, _ in hits]}
        for kind in ("Cooldown", "LowProfit")
        for hits in [[(t, r) for t, r in locked_tickers.items() if r and r.startswith(kind)]]
    ]
    index_closes = prices.frame(cfg.index)["Close"]
    closed_today = [t for t in book.closed if t.exit_date == as_of]
    return {
        "as_of": as_of, "initial_cash": cfg.initial_cash, "cash": book.cash, "holdings_value": value, "equity": equity,
        "peak": peak, "drawdown": equity / peak - 1 if peak > 0 else 0.0,
        "gross_exposure": value / equity if equity > 0 else 0.0,
        "realized_pnl": sum(t.pnl for t in book.closed), "realized_today": sum(t.pnl for t in closed_today),
        "unrealized_pnl": sum(r["unrealized"] or 0.0 for r in rows),
        "regime_on": regime_on(index_closes[index_closes.index <= as_of], risk) if cfg.regime_filter else None,
        "positions": rows, "protections": guards,
        "exits_by_reason": {"exit_signal": len(book.closed)} if book.closed else {},
        "closed": [{"ticker": t.ticker, "entry_date": t.entry_date, "exit_date": t.exit_date, "quantity": t.quantity,
                    "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": t.pnl, "pnl_pct": t.pnl_pct}
                   for t in reversed(book.closed)],
        "limits": {"stop_loss_pct": risk.stop_loss_pct, "dd_halve": risk.dd_halve, "dd_stop": risk.dd_stop,
                   "max_position_pct": risk.max_position_pct, "max_gross_pct": risk.max_gross_pct,
                   "daily_loss_limit": risk.daily_loss_limit, "auto_daily_order_cap": AUTO_EXECUTION["auto_daily_order_cap"]},
        "note": "모의투자 장부: orders.jsonl 체결 기록을 캐시 종가로 재생한 값. 실현 손익은 체결가 기준(수수료·세금 제외), "
                "평가 금액·드로다운은 수수료·세금 포함. 손절·트레일링·만료는 판정만 표시하고 주문하지 않습니다.",
    }
