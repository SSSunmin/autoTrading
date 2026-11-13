"""Paper execution: a decision → the hard rules → a fill at the next trading day's open → ``orders.jsonl``.

The paper book is never stored. Cash, holdings and the equity curve are replayed from the orders log
over the cached prices (``replay``), the way the trading state is replayed from the operations log.
One run executes one trading day (docs/02 화면 3 실행 흐름):

1. refuse outside paper mode, and while halted unless the halt kept exits (``keep_exits``): then only exits run
2. fill the open orders whose next trading day now has a price (only sells while halted)
3. the day's loss past ``AUTO_EXECUTION.auto_daily_loss_limit`` halts the loop: a halt record by ``system``
   (keep_exits true, once per day, this market only), open buys cancelled, then the day runs exits only
4. rule exits at the day's close (``exit_signals``: stop-loss → trailing → expiry, then the −20% drawdown stop
   for the rest): a ``source="rule"`` decision record is appended first, then its sell ``paper-<date>-<ticker>-exit``.
   Every order has a decision behind it (``unexplained_orders``, ``PAPER_TRADING.max_unexplained_orders``).
5. each agent decision of the date becomes one order keyed ``paper-<date>-<ticker>``, so a second run writes
   nothing: Buy/Overweight buy names not held, sized by ``risk.size_targets`` under the guards (drawdown,
   daily loss, regime, Protections); Sell/Underweight sell the whole holding; Hold places nothing; REVIEW
   is an exception. A buy over an ``auto_execution(market)`` cap is an exception, not shrunk; sells reduce risk and are not capped.
6. the order fills at the next trading day's open (slippage, commission, sell tax), or stays open when that
   day has not happened yet. A halt cancels the open ones (``open_orders`` → ``cancel_orders``).

Markets are separate books (docs/08 §0): one ``AuditLog`` per market (``audit.market_audit``), its own index
calendar, currency, sell costs and caps (``paper_config``). Amounts in reasons are in the market's currency.

ponytail: no sector cap (sector lookup is a network call), and after a −20% stop-out the reference peak is not
reset as simulate.run_simulation does after its lock. The stop sells everything, an all-cash book does not move,
so new buys stay blocked (``dd_stop``) for good while the trading state still reads running. Upgrade: write a
system halt (target="drawdown_stop") on that day and measure the peak from the last resume.
"""

from __future__ import annotations

import bisect
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from agent_trading_core.audit import (
    AuditLog,
    DecisionRecord,
    EvidenceItem,
    OperationRecord,
    OrderRecord,
    latest_orders,
    trading_state,
)
from agent_trading_core.config import AUTO_EXECUTION, KRX_MARKET, MARKETS, auto_execution
from agent_trading_core.files import locked
from agent_trading_core.krx_names import EXIT_REASON_KO
from agent_trading_core.rating import RATING_REVIEW
from agent_trading_core.risk import (
    EPS,
    ExitSignal,
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
from agent_trading_core.simulate import sell_proceeds
from agent_trading_core.symbols import safe_ticker_component

BUY_RATINGS = ("Buy", "Overweight")
SELL_RATINGS = ("Sell", "Underweight")
EXIT_SUFFIX = "-exit"                        # order id of a rule exit: paper-<date>-<ticker>-exit


@dataclass(frozen=True)
class PaperConfig:
    initial_cash: float = 100_000_000.0      # simulate.SimConfig default
    commission: float = 0.00015              # per side
    slippage: float = 0.0005                 # per side
    sell_tax: float = KRX_MARKET["sell_tax_kospi"]
    regime_filter: bool = True               # docs/05 교훈 2: on by default
    index: str = "^KS11"                     # sets the trading calendar and feeds the regime filter
    market: str = "KRX"                      # config.MARKETS key: currency, regulatory sell fees, caps


# Starting cash and commission per book, as the 2nd (KRX) and 3rd (US) measurements ran them
# (scripts/paper_backtest.py SETUPS): 1억원 / $100,000, 0.015% / 0 per side. Not FX-related.
PAPER_BOOKS = {"KRX": {"initial_cash": 100_000_000.0, "commission": 0.00015},
               "US": {"initial_cash": 100_000.0, "commission": 0.0}}


def paper_config(market: str) -> PaperConfig:
    """The paper book of one market: its index calendar, sell tax and starting cash. KeyError for an unknown market."""
    m = MARKETS[market]
    return PaperConfig(market=market, index=m["benchmark"], sell_tax=m["sell_tax"], **PAPER_BOOKS[market])


def fmt_money(v: float, market: str) -> str:
    """An amount in ``market``'s currency for a reason text: 1,234원 / $1,234.56."""
    return f"${v:,.2f}" if MARKETS[market]["currency"] == "USD" else f"{v:,.0f}원"


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
    reason: str = "exit_signal"              # risk.ExitReason: a rule exit's rule, else the agent's sell (등급 하향)

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
        book.cash += sell_proceeds(q, px, cfg)
        reason = o.rules_applied[0] if o.order_id.endswith(EXIT_SUFFIX) and o.rules_applied else "exit_signal"
        book.closed.append(ClosedTrade(o.ticker, held.entry_date, o.fill_date, q, held.avg_price, px, reason))
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
        p.record(TradeOutcome(t.ticker, _day_index(calendar, t.exit_date), t.reason, t.pnl_pct))
    return p


def _marks(book: Book, cfg: PaperConfig, date: str) -> tuple[float, float, float | None, float | None]:
    """Equity at ``date``'s close, its peak, the previous close's equity and the day's return (None off a trading day)."""
    equity = book.equity[-1][1] if book.equity else book.cash
    peak = max([cfg.initial_cash] + [e for _, e in book.equity])
    if not book.equity or book.equity[-1][0] != date:
        return equity, peak, None, None
    prev = book.equity[-2][1] if len(book.equity) > 1 else cfg.initial_cash
    return equity, peak, prev, equity / prev - 1


def position_state(h: Holding, prices: Prices, calendar: list[str], as_of: str, risk: RiskConfig) -> dict:
    """A holding as the hard rules see it at ``as_of``'s close, with ``check_exit``'s verdict. The trailing high is the
    highest close before today's bar (from the fill day on, entry price included), as simulate.run_simulation keeps it.
    With no bar on ``as_of`` (a suspended ticker) only expiry can fire: simulate compares a NaN close, so does this."""
    closes = prices.frame(h.ticker)["Close"]
    since = closes[(closes.index >= h.entry_date) & (closes.index <= as_of)].dropna()
    last = float(since.iloc[-1]) if len(since) else None
    entry_i, day = _day_index(calendar, h.entry_date), _day_index(calendar, as_of)
    high = max([h.avg_price] + [float(c) for c in since[since.index < as_of]])
    today = last if len(since) and since.index[-1] == as_of else float("nan")
    signal = check_exit(Position(h.ticker, entry_i, h.avg_price, h.quantity, high_water=high), today, day, risk) \
        if last is not None else None
    return {"last": last, "last_date": since.index[-1] if len(since) else None, "high": high, "entry_i": entry_i,
            "day": day, "active": high / h.avg_price - 1 >= risk.trailing_activate_pct - EPS, "signal": signal}


def exit_signals(book: Book, prices: Prices, calendar: list[str], as_of: str, risk: RiskConfig,
                 equity: float, peak: float) -> dict[str, ExitSignal]:
    """Which holdings the rules close at ``as_of``'s close: each position's own exit first (so a stop is recorded as a
    stop), then the −20% drawdown stop for the rest — the order simulate.run_simulation applies them."""
    out = {t: s for t, h in book.holdings.items() if (s := position_state(h, prices, calendar, as_of, risk)["signal"])}
    scale, why = drawdown_scale(equity, peak, risk)
    if scale == 0.0:
        for t in book.holdings:
            out.setdefault(t, ExitSignal(t, "force", why))
    return out


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


def _unsettled(o: OrderRecord, date: str) -> bool:
    """An order the book as of ``date`` does not show yet: open, or filled at a session after ``date`` (a decision
    dated on a holiday shares its next session with the trading day before it)."""
    return o.status == "open" or (o.status in ("filled", "partial") and (o.fill_date or "") > date)


def _rule_exits(audit: AuditLog, date: str, book: Book, current: dict[str, OrderRecord], signals: dict[str, ExitSignal],
                existing: dict[str, DecisionRecord], cfg: PaperConfig) -> list[OrderRecord]:
    """A sell for each exit signal not already being sold. Its ``source="rule"`` decision is appended here, before the
    caller appends the order, so a crash in between leaves a decision without an order (redone next run), never the
    reverse. ``existing``: the day's rule decisions already recorded, by ticker."""
    pending_sells = {o.ticker for o in current.values() if o.side == "Sell" and _unsettled(o, date)}
    out = []
    for t, sig in signals.items():
        order_id = f"paper-{date}-{t}{EXIT_SUFFIX}"
        if order_id in current or t in pending_sells:
            continue
        label = EXIT_REASON_KO.get(sig.reason, sig.reason)
        held = book.holdings[t]
        if t not in existing:
            book_note = (f"{sig.note} · 보유 {held.quantity:,}주 · 평균 체결가 {fmt_money(held.avg_price, cfg.market)} · "
                         f"진입 {held.entry_date}")
            audit.record_decision(DecisionRecord(
                ticker=t, trade_date=date, rating="Sell", action="Sell", source="rule", thesis=sig.note, market=cfg.market,
                evidence=[EvidenceItem(source="portfolio", summary=book_note, as_of=date, ref=sig.reason)],
                settings={"decider": "rule_exit", "exit_reason": sig.reason}))
        out.append(OrderRecord(order_id=order_id, ticker=t, trade_date=date, side="Sell", quantity=held.quantity,
                               status="open", rules_applied=[sig.reason], market=cfg.market,
                               reason=f"규칙 청산({label}): {sig.note} · 보유 {held.quantity}주 전량 · 다음 거래일 시가"))
    return out


def _auto_halt(audit: AuditLog, date: str, equity: float, prev: float | None, day_ret: float | None,
               cfg: PaperConfig) -> bool:
    """The day's loss past ``auto_daily_loss_limit`` halts the loop (docs/02 화면 7): a halt by ``system`` that keeps
    exits running, then the open buys cancelled. Once per day: a day already halted for, then resumed, is not halted again.
    The halt is this market's only (``scope`` = the market): the other book's loss is its own."""
    limit = AUTO_EXECUTION["auto_daily_loss_limit"]
    if day_ret is None or day_ret > limit + EPS:
        return False
    ops = audit.operations()
    if any(op.kind == "halt" and op.target == "daily_loss" and op.after.get("date") == date for op in ops):
        return False
    buys = open_orders(audit, keep_sells=True)
    audit.record_operation(OperationRecord(
        kind="halt", operator="system", role="auto", reason=f"일일 손실 한도 초과: {day_ret:+.2%} (한도 {limit:+.0%})",
        target="daily_loss", market=cfg.market, scope=cfg.market, before={"running": True, "equity_prev": prev},
        after={"running": False, "keep_exits": True, "date": date, "day_return": day_ret, "equity": equity,
               "open_orders_cancelled": [o.order_id for o in buys]}))
    cancel_orders(audit, buys, f"일일 손실 자동 정지 ({date} {day_ret:+.2%})")
    return True


def _place(decisions: list[DecisionRecord], date: str, current: dict[str, OrderRecord], prices: Prices,
           risk: RiskConfig, cfg: PaperConfig, calendar: list[str]) -> list[OrderRecord]:
    """One order per decision not ordered yet, open or already refused, sized from the book as of ``date``'s close.
    ``decisions`` is the whole day, so a rerun after a crash sizes the rest against the same denominator."""
    book = replay(list(current.values()), prices, cfg, until=date)
    equity, peak, _, day_ret = _marks(book, cfg, date)

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
    pending_buys = {o.ticker for o in current.values() if o.side == "Buy" and _unsettled(o, date)}
    pending_sells = {o.ticker for o in current.values() if o.side == "Sell" and _unsettled(o, date)}
    # the daily cap counts every buy filling at the same session, whatever its decision date
    session = next_session(calendar, date)
    day_amount = 0.0
    for o in current.values():
        if o.side == "Buy" and o.status in ("filled", "partial", "open") and next_session(calendar, o.trade_date) == session:
            day_amount += o.amount if o.amount is not None else o.quantity * (prices.close_asof(o.ticker, o.trade_date) or 0.0)

    caps = auto_execution(cfg.market)

    def money(v: float) -> str:
        return fmt_money(v, cfg.market)

    out: list[OrderRecord] = []
    for d in sorted(decisions, key=lambda d: d.rating not in SELL_RATINGS):   # sells first, then decision order
        if _order_id(d) in current:
            continue
        base = {"order_id": _order_id(d), "ticker": d.ticker, "trade_date": date, "market": cfg.market}
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
                                       reason=f"목표 금액 {money(target)} < 1주 {money(unit)}" + (f" · {' · '.join(mine)}" if mine else "")))
                continue
            side, note = "Buy", f"목표 비중 {weights[d.ticker]:.1%} × 평가 {money(equity)}" + (f" · {' · '.join(mine)}" if mine else "")
        if side == "Sell":                     # risk-reducing: the auto-execution caps limit buys only
            out.append(OrderRecord(**base, side=side, quantity=qty, status="open", rules_applied=rules,
                                   reason=f"모의 주문: {note} · 다음 거래일 시가"))
            continue
        amount = qty * ref                     # in the market's currency, against that market's caps
        if amount > caps["auto_order_max"]:
            out.append(OrderRecord(**base, side=side, quantity=qty, status="exception", exception_kind="cap_exceeded",
                                   rules_applied=rules + ["auto_order_max"],
                                   reason=f"주문 금액 {money(amount)} > 주문당 상한 {money(caps['auto_order_max'])}"))
            continue
        if day_amount + amount > caps["auto_daily_order_cap"]:
            out.append(OrderRecord(**base, side=side, quantity=qty, status="exception", exception_kind="cap_exceeded",
                                   rules_applied=rules + ["auto_daily_order_cap"],
                                   reason=f"{session or '다음 거래일'} 매수 금액 {money(day_amount + amount)} > "
                                          f"일일 상한 {money(caps['auto_daily_order_cap'])}"))
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
                cash += sell_proceeds(q, px, cfg)
                new = _filled(o, q, px, day, px_open, cfg) if q else o.model_copy(update={
                    "status": "rejected", "exception_kind": "order_rejected", "reason": f"체결일 {day} 보유 없음 — 매도 거부"})
            else:
                px = px_open * (1 + cfg.slippage)
                q = min(o.quantity, int(cash // (px * (1 + cfg.commission))))
                cash -= q * px * (1 + cfg.commission)
                new = _filled(o, q, px, day, px_open, cfg) if q > 0 else o.model_copy(update={
                    "status": "rejected", "exception_kind": "order_rejected",
                    "reason": f"현금 부족 — 체결일 {day} 1주 {fmt_money(px, cfg.market)}, 현금 {fmt_money(cash, cfg.market)}"})
            current[o.order_id] = new
            out.append(new)
    return out


def _filled(o: OrderRecord, q: int, px: float, day: str, px_open: float, cfg: PaperConfig) -> OrderRecord:
    note = f"모의 체결 {day} 시가 {fmt_money(px_open, cfg.market)} (슬리피지 {cfg.slippage:.2%})"
    if q < o.quantity:
        note += f" · {o.quantity}주 중 {q}주, 잔량 취소(현금 부족)"
    return o.model_copy(update={"status": "filled" if q == o.quantity else "partial", "filled_quantity": q,
                                "filled_price": round(px, 4), "fill_date": day, "reason": f"{o.reason} → {note}"})


def _exits_only(audit: AuditLog) -> bool:
    """False when the loop runs; True when halted with exits kept (only exits run). Refuses otherwise."""
    state = trading_state(audit.operations(), default_mode=AUTO_EXECUTION["default_mode"])
    if state["mode"] != "paper":
        raise ExecutionRefused(f"운용 모드가 {state['mode']}입니다. 이 실행기는 모의투자(paper) 모드에서만 주문합니다"
                               " (실거래 주문 경로 없음, 수동 승인은 승인 API로)")
    if not state["running"] and not state["keep_exits"]:
        raise ExecutionRefused(f"정지 중(청산도 중단)이라 주문하지 않습니다 "
                               f"({state['halted_at']}, {state['halted_by']}: {state['halt_reason']})")
    return not state["running"]


def execute_date(audit: AuditLog, date: str, prices: Prices, *, risk: RiskConfig | None = None,
                 cfg: PaperConfig | None = None) -> list[OrderRecord]:
    """Run the paper loop for one trading day of ``audit``'s market. Returns the order lines appended; a second run
    of the same day appends only fills of orders that were still open. Rule-exit decisions and an automatic halt are
    appended to their own logs before any order line. ``cfg`` defaults to the audit log's market (``paper_config``);
    ``date`` is a day of that market's own index calendar."""
    check_date(date)
    risk, cfg = risk or RiskConfig(), cfg or paper_config(audit.market or "KRX")
    if audit.market is not None and audit.market != cfg.market:
        raise ValueError(f"{audit.market} 기록에 {cfg.market} 설정으로 실행할 수 없습니다")
    with execution_lock(audit.directory):
        exits_only = _exits_only(audit)
        current = latest_orders(audit.orders())          # a broken line raises here, before anything is appended
        decisions = [d for d in audit.decisions() if d.trade_date == date]
        later = sorted({o.trade_date for o in current.values() if o.trade_date > date})
        if later:
            raise ExecutionRefused(f"{date}보다 늦은 결정일({later[-1]})의 주문이 이미 있습니다. 결정일 순서대로 실행합니다")
        calendar = prices.calendar(cfg.index)
        if not calendar:
            raise ExecutionRefused(f"거래일 달력({cfg.index} 가격)이 없습니다")
        if calendar[-1] < date:
            # sizing, guards and the daily cap would read a book that stops short of the decision day
            raise ExecutionRefused(f"가격 데이터가 {calendar[-1]}까지라 {date} 결정을 실행하지 않습니다 (캐시 갱신 필요)")
        written = _fill([o for o in current.values() if o.status == "open" and (o.side == "Sell" or not exits_only)],
                        current, prices, cfg, calendar)
        for o in written:
            audit.record_order(o)

        book = replay(list(current.values()), prices, cfg, until=date)
        equity, peak, prev, day_ret = _marks(book, cfg, date)
        if not exits_only and _auto_halt(audit, date, equity, prev, day_ret, cfg):
            exits_only = True
            current = latest_orders(audit.orders())      # the open buys it cancelled

        rule_done = {d.ticker: d for d in decisions if d.source == "rule"}
        # exits are judged at a trading day's close; a holiday-dated run only places that day's agent decisions
        signals = exit_signals(book, prices, calendar, date, risk, equity, peak) if date in calendar else {}
        placed = _rule_exits(audit, date, book, current, signals, rule_done, cfg)
        for o in placed:
            current[o.order_id] = o
        if not exits_only:
            latest_decision: dict[str, DecisionRecord] = {}
            for d in decisions:
                if d.source == "agent":
                    latest_decision[d.ticker] = d            # a re-recorded decision replaces the earlier one
            agent = _place(list(latest_decision.values()), date, current, prices, risk, cfg, calendar)
            for o in agent:
                current[o.order_id] = o
            placed += agent
        filled = {o.order_id: o for o in _fill([o for o in placed if o.status == "open"], current, prices, cfg, calendar)}
        placed = [filled.get(o.order_id, o) for o in placed]
        for o in placed:
            audit.record_order(o)
        return written + placed


def unexplained_orders(orders: list[OrderRecord], decisions: list[DecisionRecord]) -> list[OrderRecord]:
    """Orders with no decision record behind them (``PAPER_TRADING.max_unexplained_orders``): a rule exit needs a
    ``source="rule"`` decision of its ticker and day, any other order an agent decision."""
    keys = {(d.source, d.ticker, d.trade_date) for d in decisions}
    return [o for o in latest_orders(orders).values()
            if ("rule" if o.order_id.endswith(EXIT_SUFFIX) else "agent", o.ticker, o.trade_date) not in keys]


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
    state, as of ``as_of`` (default: the last trading day in the data). ``exit_signal`` is the same verdict the
    executor turns into a rule-exit order at that day's close (``exit_signals``)."""
    risk, cfg = risk or RiskConfig(), cfg or PaperConfig()
    calendar = prices.calendar(cfg.index)
    as_of = check_date(as_of) if as_of else (calendar[-1] if calendar else datetime.now().strftime("%Y-%m-%d"))
    book = replay(orders, prices, cfg, until=as_of)
    day = _day_index(calendar, as_of)
    rows = []
    for h in sorted(book.holdings.values(), key=lambda h: h.entry_date):
        st = position_state(h, prices, calendar, as_of, risk)
        last, high, expiry_i = st["last"], st["high"], st["entry_i"] + risk.max_holding_days
        rows.append({
            "ticker": h.ticker, "quantity": h.quantity, "avg_price": h.avg_price, "entry_date": h.entry_date,
            "last_price": last, "last_date": st["last_date"],
            "market_value": h.quantity * last if last is not None else None,
            "unrealized": (last - h.avg_price) * h.quantity if last is not None else None,
            "unrealized_pct": last / h.avg_price - 1 if last is not None else None,
            "stop_price": h.avg_price * (1 + risk.stop_loss_pct),
            "trailing_active": st["active"], "trailing_high": high,
            "trailing_stop_price": high * (1 - risk.trailing_pct) if st["active"] else None,
            "holding_days": day - st["entry_i"],
            "expiry_date": calendar[expiry_i] if 0 <= expiry_i < len(calendar) else None,
        })
    value = sum(r["market_value"] or 0.0 for r in rows)
    equity = book.cash + value
    peak = max([cfg.initial_cash, equity] + [e for _, e in book.equity])
    signals = exit_signals(book, prices, calendar, as_of, risk, equity, peak)
    for r in rows:
        r["weight"] = (r["market_value"] or 0.0) / equity if equity > 0 else None
        sig = signals.get(r["ticker"])
        r["exit_signal"] = {"reason": sig.reason, "note": sig.note} if sig else None
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
        "market": cfg.market, "currency": MARKETS[cfg.market]["currency"],
        "as_of": as_of, "initial_cash": cfg.initial_cash, "cash": book.cash, "holdings_value": value, "equity": equity,
        "peak": peak, "drawdown": equity / peak - 1 if peak > 0 else 0.0,
        "day_return": _marks(book, cfg, as_of)[3],         # vs the previous close; None off a trading day or before a fill
        "gross_exposure": value / equity if equity > 0 else 0.0,
        "realized_pnl": sum(t.pnl for t in book.closed), "realized_today": sum(t.pnl for t in closed_today),
        "unrealized_pnl": sum(r["unrealized"] or 0.0 for r in rows),
        "regime_on": regime_on(index_closes[index_closes.index <= as_of], risk) if cfg.regime_filter else None,
        "positions": rows, "protections": guards,
        "exits_by_reason": dict(Counter(t.reason for t in book.closed)),
        "closed": [{"ticker": t.ticker, "entry_date": t.entry_date, "exit_date": t.exit_date, "quantity": t.quantity,
                    "entry_price": t.entry_price, "exit_price": t.exit_price, "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                    "reason": t.reason}
                   for t in reversed(book.closed)],
        "limits": {"stop_loss_pct": risk.stop_loss_pct, "dd_halve": risk.dd_halve, "dd_stop": risk.dd_stop,
                   "max_position_pct": risk.max_position_pct, "max_gross_pct": risk.max_gross_pct,
                   "daily_loss_limit": risk.daily_loss_limit,
                   "auto_daily_order_cap": auto_execution(cfg.market)["auto_daily_order_cap"]},
        "note": "모의투자 장부: orders.jsonl 체결 기록을 캐시 종가로 재생한 값. 실현 손익은 체결가 기준(수수료·세금 제외), "
                "평가 금액·드로다운은 수수료·세금 포함. 룰 판정(손절·트레일링·만료·DD 정지)은 실행기가 그날 종가로 "
                "규칙 청산 결정을 기록하고 다음 거래일 시가에 매도합니다.",
    }
