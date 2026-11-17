"""The LLM trial's figures per market (config.LLM trial, docs/01 §12-7), from the records only: no model call, nothing
written.

Run:  .venv/Scripts/python scripts/llm_trial_report.py --market all [--json] [--data-dir data]

Per market: the trial days (``daily_loop.llm_trial_days`` — the decide days the model decided) against
``LLM["trial_sessions"]``; the model's decisions of those days (ratings, REVIEW with its reason, the rule stand-ins
of the same days); tokens and cost (``agent.PRICE``); agreement with the rule baseline's call on the same ticker and
day (``daily_loop.rule_record`` over the decision's screen — computed, not recorded); the 5-day settlement of each
decision (``memory.settlement.fetch_returns``: entry at the first close after the decision day, raw and alpha against
the market's benchmark five sessions later) over the KIS cache; the orders dated on trial days by status (blocked =
a hard rule shrank it to zero), and what the blocked buys would have done had they gone through (``blocked_whatif``).
Exit codes: 0, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import daily_loop as dl

from agent_trading_core import agent
from agent_trading_core.audit import latest_orders, market_audit
from agent_trading_core.config import LLM, MARKETS, auto_execution
from agent_trading_core.execution import (
    BUY_RATINGS,
    OWN_CAPS,
    SMALL_ACCOUNT,
    Holding,
    Prices,
    _marks,
    fmt_money,
    next_session,
    paper_config,
    peak_base,
    position_state,
    replay,
    session_amounts,
    with_capital,
)
from agent_trading_core.krx_names import EXIT_REASON_KO
from agent_trading_core.memory import settlement
from agent_trading_core.rating import RATING_REVIEW
from agent_trading_core.risk import RiskConfig, drawdown_scale, size_targets
from agent_trading_core.simulate import sell_proceeds
from agent_trading_core.universe import sector_of

HOLDING_DAYS = 5
BUY_SIDE = ("Buy", "Overweight")
WHATIF_HORIZONS = (5, 10)          # sessions after the entry day
SMALL_SAMPLE = 10
# the rule ids of the sizing itself (size_targets · the per-order cap, execution._rule_ids): a buy they alone blocked
# would have gone out at the plan's own quantity without them
SIZE_CAPS = {"max_position_pct", "sector_cap", "max_gross_pct", "dd_halve", "auto_order_max", "cap_zero"}


@contextmanager
def kis_closes(cache: Path):
    """``settlement.fetch_returns`` reads Yahoo (``get_closes``, the network); the daily loop's prices are the KIS
    cache — the KIS file only: a name without one has no prices here (pending), never Yahoo's adjusted closes.
    ponytail: swaps the module's ``get_closes`` for the length of the report — drop it once fetch_returns takes its
    price source as an argument."""
    prices = Prices(cache, source="kis")

    def closes(symbol: str, start: str, end: str) -> pd.Series:
        if prices.info(symbol)["source"] != "kis":
            return pd.Series(dtype=float)
        c = prices.frame(symbol)["Close"].dropna()
        c = c[(c.index >= start) & (c.index < end)]
        return pd.Series(c.to_numpy(dtype=float), index=pd.DatetimeIndex(c.index))

    real = settlement.get_closes
    settlement.get_closes = closes
    try:
        yield
    finally:
        settlement.get_closes = real


def _reason(thesis: str) -> str:
    return thesis.split("**사유**: ")[1].split("\n")[0] if "**사유**: " in thesis else ""


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def trial_report(audit, market: str, cache: Path) -> dict:
    days = dl.llm_trial_days(audit, market)
    prefix = f"{dl.RUN_IDS[market]}_"
    decisions = audit.decisions()
    llm = [d for d in decisions if d.trade_date in days and d.settings.get("decider") == LLM["model"]
           and (d.screen_run_id or "").startswith(prefix)]
    stand_in = [d for d in decisions if d.trade_date in days and d.settings.get("fallback") == "rule"
                and (d.screen_run_id or "").startswith(prefix)]
    usage = Counter()
    for d in llm:
        usage.update(d.usage)
    screens = {r.run_id: r for r in audit.screens()}
    agree, compared, rule_ratings = 0, 0, Counter()
    for d in llm:
        run = screens.get(d.screen_run_id)
        if run is None:
            continue
        rule = dl.rule_record(market, run, d.ticker, d.trade_date).rating
        rule_ratings[rule] += 1
        compared += 1
        agree += rule == d.rating
    settled, pending, failed = [], 0, []
    bench = MARKETS[market]["benchmark"]
    with kis_closes(cache):
        for d in llm:
            try:
                raw, alpha, _, _ = settlement.fetch_returns(d.ticker, d.trade_date, HOLDING_DAYS, benchmark=bench)
            except Exception as e:  # one unreadable price file is reported, the rest still count
                failed.append(f"{d.ticker} {d.trade_date}: {type(e).__name__}")
                continue
            if raw is None:
                pending += 1
            else:
                settled.append((d.rating, raw, alpha))
    buy = [s for s in settled if s[0] in BUY_SIDE]
    orders = [o for o in latest_orders(audit.orders()).values() if o.trade_date in days]
    status = Counter(o.status for o in orders)
    return {
        "market": market, "trial_sessions": LLM["trial_sessions"], "runs": len(days),
        "first": days[0] if days else None, "last": days[-1] if days else None, "days": days,
        "decisions": len(llm), "ratings": dict(Counter(d.rating for d in llm)),
        "review": len([d for d in llm if d.rating == RATING_REVIEW]),
        "review_reasons": [f"{d.trade_date} {d.ticker}: {_reason(d.thesis)}" for d in llm if d.rating == RATING_REVIEW],
        "rule_stand_ins": len(stand_in), "reruns": len([d for d in llm if d.settings.get("rerun_of")]),
        "usage": {k: usage.get(k, 0) for k in agent.PRICE}, "cost_usd": round(agent.cost_usd(usage), 4),
        "rule_compared": compared, "rule_agree": agree, "rule_ratings": dict(rule_ratings),
        "settled": len(settled), "pending": pending, "settle_failed": failed,
        # the overall mean is the buy side's only: after a Hold · Sell · REVIEW a rise is no gain of the decision
        "buy_settled": len(buy), "mean_raw": _mean([r for _, r, _ in buy]), "mean_alpha": _mean([a for _, _, a in buy]),
        "alpha_by_rating": {k: {"n": len(v), "mean_alpha": _mean(v)} for k, v in
                            _group((rt, a) for rt, _, a in settled).items()},
        "orders": len(orders), "orders_by_status": dict(status), "blocked": status.get("blocked", 0),
        "filled": status.get("filled", 0) + status.get("partial", 0),
        "whatif": blocked_whatif(audit, market, cache, days),
    }


def blocked_whatif(audit, market: str, cache: Path, days: list[str]) -> dict:
    """The buys a hard rule blocked on trial days (``side=Buy``, ``status=blocked``), as if they had gone through, on the
    executor's own assumptions (``execution._place`` · ``_fill`` · ``_rule_exits``).

    Quantity: what ``_place`` would have placed with the day's locks lifted (regime, daily loss, drawdown stop,
    StoplossGuard, a ticker's Protections) — equal weight over the day's buy-rated names, ``size_targets`` (per-name and
    sector caps, the drawdown scale, the small-account one name), its unit price, then the per-order cap shrink
    (``auto_order_max``) and the session's buy cap (``auto_daily_order_cap``: past it the executor makes an exception,
    so the row is not priced). The day's buy list is the executor's: the day's agent decisions, the last one per name
    (``execute_date``). When that sizing gives no share: a buy only size caps blocked (``SIZE_CAPS``) takes the plan's
    own quantity before the rules (``plan.proposed``, the per-order cap shrink on it unless ``cap_zero`` — one share over
    it — blocked it), marked as such; any other block (regime …) stays unpriced — the executor would not have bought
    it even without that rule.
    In at the next session's open + slippage, commission on top; marked at the last close of the cache as if sold there
    (− slippage, commission, sell tax and fees: ``sell_proceeds``). Two paths: held to that close, and the position's
    own exits (``position_state``: stop-loss → trailing → expiry at a close, sold at the next open; a signal whose next
    open is not in the cache yet is marked at its close). The −20% drawdown stop, Protections and the fill's cash check
    are book-wide and not replayed — one blocked buy is not the book (on a regime day the book is cash anyway). The
    benchmark is the market's index from the entry day's open to the same close. The real book kept the cash: 0 change.
    One that cannot be priced stays a row with its reason."""
    prices = Prices(cache)
    operations = audit.operations()
    cfg = with_capital(paper_config(market), operations)
    risk = RiskConfig()
    calendar = prices.calendar(cfg.index)
    orders = audit.orders()
    current = latest_orders(orders)
    decisions = audit.decisions()
    caps = auto_execution(market)
    blocked = [o for o in current.values() if o.trade_date in days and o.side == "Buy" and o.status == "blocked"]
    rows = []
    for date in sorted({o.placed_on for o in blocked}):
        latest = {}
        for d in decisions:
            if d.trade_date == date and d.source == "agent":
                latest[d.ticker] = d                     # a re-recorded decision replaces the earlier one, in place
        buys = [t for t, d in latest.items() if d.rating in BUY_RATINGS]
        sizes = _executor_sizes(buys, date, list(current.values()), prices, cfg, risk, operations, caps)
        # the session's buy cap counts the real buys filling there too, then these in decision order
        _, day_amount, _ = session_amounts(current.values(), prices, calendar, date)
        for o in sorted((o for o in blocked if o.placed_on == date),
                        key=lambda o: buys.index(o.ticker) if o.ticker in buys else len(buys)):
            row, amount = _whatif(o, sizes.get(o.ticker), prices, cfg, risk, calendar, caps)
            if amount is not None and day_amount + amount > caps["auto_daily_order_cap"]:
                row = {k: row[k] for k in ("ticker", "trade_date", "rule", "qty", "sizing")} | {
                    "entry": None, "entry_price": None, "cost": None,
                    "why": f"일일 매수 상한 {fmt_money(caps['auto_daily_order_cap'], market)} 초과 — 실행기라면 예외(체결 안 됨)"}
            elif amount is not None:
                day_amount += amount
            rows.append(row)
    until = calendar[-1] if calendar else None
    by_rule: dict[str, list[dict]] = {}
    for r in rows:
        by_rule.setdefault(r["rule"], []).append(r)
    # a provisional row (the session was not over when the cache was written) marks the figures as such
    provisional = until is not None and any(prices.info(t)["provisional"] and prices.info(t)["as_of"] == until
                                            for t in {cfg.index, *(r["ticker"] for r in rows if r["cost"] is not None)})
    return {"until": until, "provisional": provisional, "benchmark": cfg.index, "rows": rows,
            "by_rule": {k: _whatif_sum(v) for k, v in sorted(by_rule.items())}, "total": _whatif_sum(rows)}


def _executor_sizes(buys: list[str], date: str, orders, prices: Prices, cfg, risk: RiskConfig, operations,
                    caps: dict) -> dict[str, tuple[int, list[str]]]:
    """``_place``'s buy quantity per name of ``date``'s buy list, the locks lifted: the same book, scale, caps and unit
    price (``execution._place``, the equal-weight and per-order-cap steps), with the cap texts that cut that name."""
    book = replay(orders, prices, cfg, until=date)
    equity, peak, _, _ = _marks(book, cfg, date, peak_base(operations, date))
    scale, _ = drawdown_scale(equity, peak, risk)
    if cfg.capital_on(date) < SMALL_ACCOUNT.get(cfg.market, 0.0):
        buys = buys[:1]
        risk = replace(risk, max_position_pct=1.0, max_sector_pct=1.0)
    weights, overrides = size_targets(buys, risk, scale=scale if scale > 0 else 1.0, sector_of=sector_of)
    out = {}
    for t in buys:
        ref = prices.close_asof(t, date)
        if ref is None:
            continue
        qty = int(weights.get(t, 0.0) * equity // (ref * (1 + cfg.slippage) * (1 + cfg.commission)))
        if qty * ref > caps["auto_order_max"]:
            qty = int(caps["auto_order_max"] // ref)
        out[t] = (qty, [x for x in overrides if f": {t} " in x or not x.startswith(OWN_CAPS)])
    return out


def _whatif(o, sized: tuple[int, list[str]] | None, prices: Prices, cfg, risk: RiskConfig, calendar: list[str],
            caps: dict) -> tuple[dict, float | None]:
    """One blocked buy priced as if filled, and its amount at the decision day's close (the daily cap's measure)."""
    row = {"ticker": o.ticker, "trade_date": o.trade_date, "rule": "+".join(o.rules_applied) or "(규칙 없음)",
           "qty": None, "sizing": None, "entry": None, "entry_price": None, "cost": None, "why": None}
    proposed = o.plan.proposed if o.plan else None
    ref = prices.close_asof(o.ticker, o.placed_on)
    if proposed is None:
        return row | {"why": "제안 수량 없음(원안 기록 없음)"}, None
    if o.retry_on:
        # ponytail: a retried order is sized on its first day's buy list capped at its first quantity (_place's
        # ``sizing`` · ``fixed``) — not replayed here; replay it from the order's earlier lines if one ever shows up
        return row | {"why": f"재시도 주문(원 결정 {o.trade_date}, 재시도 {o.retry_on}) — 재시도 크기는 재생하지 않음"}, None
    if ref is None:
        return row | {"why": f"결정일 {o.placed_on} 종가 없음 — 수량을 정할 수 없음"}, None
    if sized is None:
        return row | {"why": "그날 마지막 결정이 매수 등급이 아님(실행기 매수 목록에 없음)"}, None
    if sized[0] > 0:
        qty, sizing = sized[0], "실행기 크기"
    elif not (o.rules_applied and set(o.rules_applied) <= SIZE_CAPS):
        # the executor sizes it to nothing even without the rule that blocked it: no buy either way
        cut = " · ".join(sized[1]) or "목표 금액 < 1주"
        return row | {"qty": 0, "sizing": "실행기 크기",
                      "why": f"막은 규칙 없이도 실행기 크기 0주 — {cut}(주문 안 됨)"}, None
    elif proposed.qty <= 0:
        return row | {"why": "제안 수량 0"}, None
    elif "cap_zero" in o.rules_applied or proposed.qty * ref <= caps["auto_order_max"]:
        qty, sizing = proposed.qty, "원안(규칙 전)"
    elif (cut := int(caps["auto_order_max"] // ref)) > 0:
        qty, sizing = cut, "원안(규칙 전) → 주문당 상한"
    else:
        return row | {"why": f"원안 1주도 주문당 상한 {fmt_money(caps['auto_order_max'], cfg.market)} 초과(주문 안 됨)"}, None
    row |= {"qty": qty, "sizing": sizing}
    entry = next_session(calendar, o.placed_on)
    if entry is None:
        return row | {"why": f"진입일(결정일 {o.placed_on} 다음 거래일) 아직 없음"}, qty * ref
    px_open = prices.open_on(o.ticker, entry)
    if px_open is None:
        closes = prices.frame(o.ticker)["Close"].dropna().index
        last = closes[-1] if len(closes) else None
        prev = calendar[calendar.index(entry) - 1] if calendar.index(entry) > 0 else None
        # the executor's own split (_fill): the index has the day and this cache stops one session short — late
        late = last is not None and prev is not None and prev <= last < entry
        why = (f"진입일 {entry} 시세가 캐시에 아직 없음(마지막 {last}) — 데이터 지연" if late
               else f"진입일 {entry} 시가 없음(거래정지 또는 데이터 공백)")
        return row | {"entry": entry, "why": why}, qty * ref
    px = px_open * (1 + cfg.slippage)
    cost = qty * px * (1 + cfg.commission)
    i, until = calendar.index(entry), calendar[-1]

    def value_at(day: str) -> float:
        return sell_proceeds(qty, prices.close_asof(o.ticker, day) * (1 - cfg.slippage), cfg)

    hold = value_at(until)
    ruled, exit_note = hold, None
    h = Holding(o.ticker, qty, px, entry)
    for day in calendar[i:]:
        signal = position_state(h, prices, calendar, day, risk)["signal"]
        if signal is None:
            continue
        fill = next_session(calendar, day)
        px_exit = prices.open_on(o.ticker, fill) if fill else None
        label = EXIT_REASON_KO.get(signal.reason, signal.reason)
        if px_exit is None:
            ruled, exit_note = value_at(day), f"{label} 신호 {day}(체결일 시가 아직 없음 → 그 종가로)"
        else:
            ruled, exit_note = sell_proceeds(qty, px_exit * (1 - cfg.slippage), cfg), f"{label} {fill} 시가"
        break
    bench_open = prices.open_on(cfg.index, entry)
    horizon = {n: value_at(calendar[i + n]) / cost - 1 if i + n < len(calendar) else None for n in WHATIF_HORIZONS}
    marked = prices.frame(o.ticker)["Close"].dropna().index
    return row | {"entry": entry, "entry_price": px, "cost": cost, "hold_pnl": hold - cost,
                  "marked": str(marked[marked <= until][-1]),
                  "rule_pnl": ruled - cost, "exit": exit_note, "horizon": horizon,
                  "bench": prices.close_asof(cfg.index, until) / bench_open - 1 if bench_open else None}, qty * ref


def _whatif_sum(rows: list[dict]) -> dict:
    done = [r for r in rows if r["cost"] is not None]
    if not done:
        return {"n": len(rows), "priced": 0, "unpriced": len(rows), "cost": None, "hold_pnl": None, "rule_pnl": None,
                "hold_pct": None, "rule_pct": None, "bench_pct": None, "unbenched": 0}
    cost = sum(r["cost"] for r in done)
    benched = [r for r in done if r["bench"] is not None]
    bench_cost = sum(r["cost"] for r in benched)
    return {"n": len(rows), "priced": len(done), "unpriced": len(rows) - len(done), "cost": cost,
            "hold_pnl": sum(r["hold_pnl"] for r in done), "rule_pnl": sum(r["rule_pnl"] for r in done),
            "hold_pct": sum(r["hold_pnl"] for r in done) / cost, "rule_pct": sum(r["rule_pnl"] for r in done) / cost,
            # weighted by the entry amounts, as the sum of the positions is
            "bench_pct": sum(r["cost"] * r["bench"] for r in benched) / bench_cost if bench_cost else None,
            "unbenched": len(done) - len(benched)}


def _group(pairs) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for k, v in pairs:
        out.setdefault(k, []).append(v)
    return out


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:+.2%}"


def render(r: dict) -> str:
    n = r["trial_sessions"]
    lines = [f"## {r['market']} LLM 시험 — {r['runs']}/{'기한 없음' if n is None else n}회 "
             f"({r['first'] or '–'} ~ {r['last'] or '–'})"]
    if not r["runs"]:
        return "\n".join(lines + ["- 시험 기록 없음"])
    u = r["usage"]
    lines += [
        f"- 결정 {r['decisions']}건 · 등급 {dict(sorted(r['ratings'].items()))} · REVIEW {r['review']}건"
        f" · 재실행 답 {r['reruns']}건 · 같은 날 규칙 대체 {r['rule_stand_ins']}건",
        *[f"  - REVIEW {x}" for x in r["review_reasons"]],
        f"- 토큰 input {u['input_tokens']:,} · output {u['output_tokens']:,} · cache_read "
        f"{u['cache_read_input_tokens']:,} · cache_creation {u['cache_creation_input_tokens']:,} · 비용 ${r['cost_usd']:.4f}"
        f" (하루 ${r['cost_usd'] / r['runs']:.4f})",
        f"- 규칙 결정과 일치 {r['rule_agree']}/{r['rule_compared']} · 규칙 쪽 등급 {dict(sorted(r['rule_ratings'].items()))}",
        f"- 5일 정산 완료 {r['settled']} · 대기 {r['pending']}" + (f" · 가격 실패 {len(r['settle_failed'])}" if r["settle_failed"]
                                                               else "")
        + f" · 매수 쪽(Buy·Overweight) {r['buy_settled']}건 평균 수익 {_pct(r['mean_raw'])} · 평균 알파 {_pct(r['mean_alpha'])}",
        "  - 등급별 평균 알파(결정 다음 종가에 샀다고 가정 — Hold · 매도 쪽은 오르면 틀린 결정):",
        *[f"    - {k}: {v['n']}건 {_pct(v['mean_alpha'])}" for k, v in sorted(r["alpha_by_rating"].items())],
        f"- 주문 {r['orders']}건 · 체결 {r['filled']} · 하드 룰 차단(blocked) {r['blocked']} · 상태 {dict(sorted(r['orders_by_status'].items()))}",
    ]
    return "\n".join(lines + render_whatif(r["whatif"], r["market"]))


def _money(v: float | None, market: str) -> str:
    return "–" if v is None else ("+" if v > 0 else "−" if v < 0 else "") + fmt_money(abs(v), market)


def render_whatif(w: dict, market: str) -> list[str]:
    t = w["total"]
    lines = ["", f"### 막힌 매수를 했다면 — 가상 성과(평가일 {w['until'] or '–'} 종가"
             + (" — 잠정(장 마감 전) 값 포함" if w["provisional"] else "") + " · 시험 진행 중이면 중간값)",
             "수량 = 실행기가 그날 막는 규칙 없이 냈을 수량(균등 비중 → 종목 · 섹터 상한 · 드로다운 배율 → 주문당 상한 축소, 일일 상한 초과는 예외라 평가 불가),"
             " 그 크기 계산이 0주일 때: 크기 상한만 막은 매수는 원안(규칙 전) 수량(주문당 상한 축소 적용 — cap_zero(1주 > 주문당 상한)가"
             " 막은 것은 원안 그대로), 그 밖의 규칙이 막은 매수는"
             " 그 규칙이 없어도 주문이 안 나갔으므로 평가 불가."
             " 진입 = 결정 다음 거래일 시가 + 슬리피지 · 수수료(모의 체결과 같은 가정), 평가 = 그 종가에 판다고 보고 매도 비용을 뺀 값."
             " 청산 규칙 = 종목별 손절 · 트레일링 · 기간 만료(−20% 정지 · Protections는 장부 전체 규칙이라 넣지 않음)."
             f" 벤치마크 {w['benchmark']} = 진입일 시가 → 평가일 종가(진입 금액 가중). 실제 장부는 막혀서 현금 그대로(0 변동)."]
    if not t["n"]:
        return lines + ["- 시험 기간 막힌 매수 0건"]
    if t["priced"] < SMALL_SAMPLE:
        lines.append(f"- 평가된 표본 {t['priced']}건(전체 {t['n']}건) < {SMALL_SAMPLE}건 — 몇 건의 결과라 우연의 폭이 크다")
    lines += ["", "| 막은 규칙 | 건수 | 평가 | 평가 불가 | 진입 금액 | 보유만 손익 | 청산 규칙 적용 손익 | 벤치마크 | 실제(현금) |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, s in [*w["by_rule"].items(), ("합계", t)]:
        lines.append(f"| {name} | {s['n']} | {s['priced']} | {s['unpriced']} | {_money(s['cost'], market).lstrip('+')} | "
                     f"{_money(s['hold_pnl'], market)} ({_pct(s['hold_pct'])}) | {_money(s['rule_pnl'], market)} "
                     f"({_pct(s['rule_pct'])}) | {_pct(s['bench_pct'])}"
                     + (f" ({s['unbenched']}건 제외 — 지수 진입일 시가 없음)" if s["unbenched"] else "") + " | 0 |")
    lines += ["", "| 결정일 | 종목 | 막은 규칙 | 수량 | 진입 | 보유만 | 청산 규칙 적용 | 진입 후 5거래일 | 10거래일 | 벤치마크 |",
              "|---|---|---|---:|---|---:|---:|---:|---:|---:|"]
    for r in w["rows"]:
        if r["cost"] is None:
            qty = f"{r['qty']} ({r['sizing']})" if r["qty"] else "–"
            lines.append(f"| {r['trade_date']} | {r['ticker']} | {r['rule']} | {qty} | 평가 불가: {r['why']} "
                         "| – | – | – | – | – |")
            continue
        how = f" ({r['exit']})" if r["exit"] else ""
        stale = f" (종가 {r['marked']} — 평가일 시세 없음)" if r["marked"] != w["until"] else ""
        lines.append(f"| {r['trade_date']} | {r['ticker']} | {r['rule']} | {r['qty']} ({r['sizing']}) | {r['entry']} "
                     f"{fmt_money(r['entry_price'], market)} | {_pct(r['hold_pnl'] / r['cost'])}{stale} | "
                     f"{_pct(r['rule_pnl'] / r['cost'])}{how} | "
                     f"{_pct(r['horizon'][5])} | {_pct(r['horizon'][10])} | {_pct(r['bench'])} |")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", choices=[*sorted(dl.RUN_IDS), "all"], required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    args = parser.parse_args(argv)
    data = Path(args.data_dir)
    markets = sorted(dl.RUN_IDS) if args.market == "all" else [args.market]
    reports = [trial_report(market_audit(data / "audit", m), m, data / "cache") for m in markets]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        print("\n\n".join(render(r) for r in reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
