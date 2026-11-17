"""Daily paper loop of one market: refresh → today's close → screen + rule-baseline decisions → paper orders.

Run:  .venv/Scripts/python scripts/daily_loop.py --market KRX --stage decide       # after the close (15:35 KST)
      .venv/Scripts/python scripts/daily_loop.py --market KRX --stage fill         # after the open (09:05 KST)
      .venv/Scripts/python scripts/daily_loop.py --market US --stage all --date 2026-11-12
      python -m agent_trading_core.scheduler --markets KRX,US                       # runs the stages on time

Stages, for one trading day of the market (default: today in the market's time zone):

- ``decide`` (session close + 5 min): KIS incremental refresh (``kis.update_cache``) of the benchmark, the book's
  universe (baseline_krx.SETUPS) and every ticker with an order → today's bar as a provisional row
  (``kis.fetch_today_close``) → the screen as of the day and the rule baseline's decisions (``audit/<market>/``) →
  the reruns a person asked for on REVIEW exceptions (``answer_reruns``) → ``execution.execute_date`` for the day:
  the decisions become ``open`` orders filling at the next session's open.
- ``fill`` (session open + 5 min): refresh the benchmark and the tickers of open orders, today's bar (its Open) as a
  provisional row → ``execute_date`` of the previous session again, which fills its open orders at today's open.
- ``all``: fill, then decide (a catch-up run after the close).

Each stage appends one ``RunRecord`` (``run_records``; ``audit/<market>/runs.jsonl`` with TRADING_STORE=jsonl). A stage that ended ``ok`` or ``skipped``
(holiday) is not run again for that day: a second call prints so and writes nothing; the screen, decisions and
orders are also keyed (screen run id, decision per screen and ticker, the executor's order id), so a rerun after a
failure writes only what is missing. A failure stops the stage (and ``all`` before decide) with ``failed``. The
executor's refusal (halted, not paper mode) is ``skipped`` with its reason: the decisions are still recorded.
A past day that is not in the benchmark's final bars is ``skipped`` (``holiday``); on the market's today a weekday
with no benchmark bar yet is ``failed`` (a holiday or a late feed — the next call, or the next day's catch-up,
settles it), and a fill whose open-order ticker has no bar yet is ``failed`` during the session (it may still come);
after the close it is final and goes to the executor as a ``data_delayed`` exception. Before the stage's time on the market's today the call does nothing.
A call without ``--date`` (a scheduler: ``agent_trading_core.scheduler``, cron, systemd) runs the day that is due
(``due_day``): the latest session (``market_calendar``: a weekday not on the exchange's holiday list) whose stage
time has come, while the market's next session has not opened — so a
restart, a late call or a retry in that window runs it once more if it is not done, and after midnight a day with
no bar is settled as a holiday on final bars. Weekdays before it that nobody ran get ``skipped`` (``missed``), the
listed holidays among them ``skipped`` (``holiday``) without a run — as does ``--date`` on one. A year with no
calendar file runs every weekday and notes "휴장 달력 없음 <year>".
``decide`` also compares the fills of the session before with that day's final Open (``check_fill_opens``): a fill
priced from a provisional row is reported in ``notes`` when they differ, never rewritten. Prices come from the KIS
cache (``<symbol>-KIS-data.csv``, ``execution.Prices(source="kis")``); a name with no KIS file falls back to its Yahoo
file, and the run's ``notes`` names it (``fallback_notes``) — never a silent switch to adjusted prices.

Exit codes: 0 ok / skipped(holiday, missed) / nothing to do, 1 failed, 2 bad arguments, 3 skipped (executor
refused). TRADING_DATA_DIR overrides data/.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from baseline_krx import SETUPS, decide_from_screen, memory_text, screen_day, screen_section, sector_of

from agent_trading_core import agent, kis
from agent_trading_core import market_calendar as cal
from agent_trading_core.audit import (
    BASELINE_DECIDER,
    RERUN_OPERATOR,
    AuditLog,
    DecisionRecord,
    OperationRecord,
    OrderRecord,
    RunRecord,
    ScreenRun,
    latest_orders,
    market_audit,
    pending_rerun_marker,
    pending_reruns,
)
from agent_trading_core.config import LLM, MARKETS
from agent_trading_core.execution import (
    Book,
    ExecutionRefused,
    PaperConfig,
    Prices,
    check_date,
    execute_date,
    fmt_money,
    paper_config,
    position_state,
    replay,
)
from agent_trading_core.files import locked
from agent_trading_core.rating import RATING_REVIEW
from agent_trading_core.risk import RiskConfig

STAGES = ("decide", "fill")
# What agent_trading_core.scheduler runs. No US fill: at 09:37 ET on 2026-11-13 KIS's today Open was off Yahoo's regular open by
# +1.84% (XOM) · +1.52% (CVX) · -0.21% (BRK-B) · -0.15% (JPM) — the opening prints are not settled yet. The US
# decide fills the day's open orders from the bar after the close instead. `--stage fill` still runs by hand.
SCHEDULED = {"KRX": STAGES, "US": ("decide",)}
RUN_IDS = {"KRX": "daily_krx", "US": "daily_us"}   # screen run id: <prefix>_<date>, apart from the measurement grids
AFTER = timedelta(minutes=5)                       # a stage runs this long after the open (fill) or close (decide)


def stage_time(market: str, stage: str, day: date) -> datetime:
    """When ``stage`` of ``day`` may run: the market's session open (fill) or close (decide) + 5 min, aware in the
    market's time zone (``MARKETS[market]["session"]`` · ``timezone``), so daylight saving follows the zone. The close
    is the day's own (``market_calendar.session_close``: a US half day closes at 13:00)."""
    tz = ZoneInfo(MARKETS[market]["timezone"])
    at = cal.session_open(market, day) if stage == "fill" else cal.session_close(market, day)
    return datetime.combine(day, at, tz) + AFTER


def next_open(market: str, day: date) -> datetime:
    """The open of the session after ``day`` (``market_calendar.next_session``): until then a stage of ``day`` is
    still caught up."""
    return stage_time(market, "fill", cal.next_session(market, day)) - AFTER


def due_day(market: str, stage: str, now: datetime) -> str | None:
    """The trading day whose ``stage`` is due at ``now``: the latest session (``market_calendar.is_session`` — a listed
    holiday never is) whose stage time has come, if the next session has not opened yet (the same session); None once
    it has (that day was missed)."""
    local = now.astimezone(ZoneInfo(MARKETS[market]["timezone"])).date()
    for back in range(14):              # the longest closure (a lunar new year beside a weekend) fits
        d = local - timedelta(days=back)
        if cal.is_session(market, d) and stage_time(market, stage, d) <= now:
            return d.isoformat() if now < next_open(market, d) else None
    return None


def holiday_record(market: str, stage: str, day: date) -> RunRecord:
    """A listed holiday's record, written without running the stage: ``skipped(holiday)`` like one found after the
    fact (no benchmark bar), with the list it came from."""
    name = cal.holiday(market, day) or "이름 없음"
    return RunRecord(market=market, stage=stage, trade_date=day.isoformat(), status="skipped", reason="holiday",
                     started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     notes=[f"휴장 달력: {name} — {cal.source(market, day.year)}"])


def fallback_notes(prices: Prices, tickers, day: str) -> list[str]:
    """The tickers ``prices`` serves from the Yahoo file (no KIS file of them: ``Prices(source="kis")`` falls back) —
    adjusted prices where a live run trades the real ones, so the run says so, one line each; none: no line."""
    out = []
    for t in dict.fromkeys(tickers):
        info = prices.info(t)
        if info["source"] == "yahoo":
            out.append(f"가격 Yahoo 폴백(KIS 파일 없음): {t} — {day} 실행, Yahoo 마지막 행 {info['as_of']}")
    return out


def _counts(orders, prefix: str) -> dict[str, int]:
    return {f"{prefix}_{k}": n for k, n in Counter(o.status for o in orders).items()}


def run_stage(market: str, stage: str, day: str, data: Path, client, now: datetime | None = None, *,
              decider: str | None = None, llm=None) -> RunRecord | None:
    """One stage of ``day``; the RunRecord it appended, or None when it did nothing (already done, or too early).
    One run of a market's stage at a time (a lock beside its audit files); the done check is made holding it.
    ``decider``: rule | llm, default ``default_decider()`` (the LLM trial, when on, counts this market's days);
    ``llm``: the Anthropic client (default: ``agent.make_client``)."""
    now = now or datetime.now(kis.KST)
    audit = market_audit(data / "audit", market)
    local_today = now.astimezone(ZoneInfo(MARKETS[market]["timezone"])).date().isoformat()
    if day > local_today:
        raise ValueError(f"{day}는 {market} 현지 오늘({local_today}) 뒤 날짜")
    if day == local_today and now < stage_time(market, stage, date.fromisoformat(day)):
        print(f"{market} {stage} {day}: {stage_time(market, stage, date.fromisoformat(day)):%H:%M} 전 — 실행 안 함")
        return None
    with locked(audit.directory / f"daily_loop_{stage}"):
        if any(r.final for r in audit.runs() if (r.stage, r.trade_date) == (stage, day)):
            print(f"{market} {stage} {day}: 이미 완료 — 새 기록 없음")
            return None
        d = date.fromisoformat(day)
        if cal.holiday(market, d) is not None:          # a listed holiday: nothing to fetch, nothing to fail
            record = holiday_record(market, stage, d)
            audit.record_run(record)
            return record
        notes = [] if cal.covered(market, d.year) else [f"휴장 달력 없음 {d.year}"]
        if decider is None:
            decider = default_decider(audit, market, day)
            if stage == "decide" and trial_on() and not LLM["enabled"]:
                notes.append(trial_note(audit, market, day, decider))
        return _run(audit, market, stage, day, data / "cache", client, now, day == local_today, decider, llm, notes)


def _run(audit: AuditLog, market: str, stage: str, day: str, cache: Path, client, now: datetime,
         today: bool, decider: str = "rule", llm=None, notes: list[str] | None = None) -> RunRecord:
    started, counts, notes = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), {}, list(notes or [])
    bench = MARKETS[market]["benchmark"]
    prices = None

    def done(status: str, reason: str | None = None, error: str | None = None) -> RunRecord:
        if prices is not None:          # every name the run priced: the refreshed ones, the book's, the day's calls
            try:
                notes.extend(fallback_notes(prices, [*symbols, *SETUPS[market]["universe"],
                                                     *sorted({o.ticker for o in orders}),
                                                     *sorted({d.ticker for d in audit.decisions_where(trade_date=day)})],
                                            day))
            except Exception as e:  # the file that failed the run may fail the check too: the record is still written
                notes.append(f"가격 폴백 확인 실패: {type(e).__name__}: {e}"[:200])
        record = RunRecord(market=market, stage=stage, trade_date=day, started_at=started, status=status,
                           reason=reason, error=error, counts=counts, notes=notes)
        audit.record_run(record)
        return record

    try:
        orders = latest_orders(audit.orders()).values()
        open_before = sorted({o.ticker for o in orders if o.status == "open" and o.trade_date < day})
        symbols = list(dict.fromkeys([bench, *(SETUPS[market]["universe"] if stage == "decide" else []),
                                      *(sorted({o.ticker for o in orders}) if stage == "decide" else open_before)]))
        failed = []
        for s in symbols:
            try:
                kis.update_cache(s, cache, client, now=now)
            except Exception as e:  # the rest still refresh; the stage fails below
                failed.append(f"{s} {type(e).__name__}: {e}"[:200])
        counts["refreshed"] = len(symbols) - len(failed)
        if failed:
            return done("failed", error="갱신 실패 " + "; ".join(failed))
        if today:                       # not final yet: today's bar as a provisional row
            got = kis.fetch_today_close(market, symbols, cache, client, now)
            counts["provisional"] = len(got["written"])
            if got["failed"]:
                return done("failed", error="당일 시세 실패 " + "; ".join(f"{f['symbol']} {f['error']}" for f in got["failed"]))
            if bench in got["missing"] and date.fromisoformat(day).weekday() < 5:
                # no index bar yet on a weekday: a holiday or a late feed — not final, the next call runs again;
                # a past day is judged on final bars (below) and a holiday then ends it
                return done("failed", error=f"당일 벤치마크({bench}) 행 없음 — 휴장 또는 지연")
            # in the session a missing bar may still come (the executor would make the order a data_delayed
            # exception for good); after the close it is final — a halted name — and goes to the executor as such
            in_session = now < stage_time(market, "decide", date.fromisoformat(day)) - AFTER
            late = [t for t in got["missing"] if t in open_before and stage == "fill" and in_session]
            if late:
                return done("failed", error="미체결 주문 종목 당일 행 없음(장중) " + ", ".join(late))
        prices = Prices(cache, source="kis")
        calendar = prices.calendar(bench)
        if day not in calendar:
            return done("skipped", reason="holiday")
        cfg = paper_config(market)
        prev = max((d for d in calendar if d < day), default=None)     # the session before: its bar is final
        if stage == "decide":
            run_id = f"{RUN_IDS[market]}_{day}"
            # a rerun decides from the screen already recorded, never from a new one over moved prices
            run = next((r for r in audit.screens() if r.run_id == run_id), None)
            if run is None:
                run = screen_day(market, day, cache_dir=str(cache), run_id=RUN_IDS[market],
                                 load=lambda t, d, cache_dir: kis.load_ohlcv(t, d, cache_dir, client=client))
                audit.record_screen(run)
            have = {d.ticker for d in audit.decisions() if d.screen_run_id == run.run_id}
            if decider == "llm":
                # what an earlier run already decided is not asked again (cost, and the same answer for a rerun)
                made, llm_counts = agent_decide(audit, market, run, day, cache, prices, cfg, llm, skip=have,
                                                on_record=audit.record_decision, notes=notes)
                counts |= {"screened": run.universe_size, "decisions": len(have) + len(made),
                           "decisions_new": len(made)} | llm_counts
                notes.append(f"결정자 {LLM['model']} (effort {LLM['effort']}, {agent.prompt_version()})")
            else:
                records = [r for c in run.candidates if (r := decide_from_screen(market, run, c.ticker, day)[1])]
                for r in records:
                    if r.ticker not in have:
                        audit.record_decision(r)
                counts |= {"screened": run.universe_size, "decisions": len(records),
                           "decisions_new": sum(r.ticker not in have for r in records)}
            # reruns a person asked for on REVIEW exceptions: after the day's decisions, before the executor
            reran, rerun_notes = answer_reruns(audit, market, day, decider, cache, prices, cfg, llm,
                                               used=counts.get("llm_calls", 0))
            for k, v in reran.items():
                counts[k] = counts.get(k, 0) + v
            notes += rerun_notes
            checked, moved = check_fill_opens(audit, prices, cfg, prev) if prev else (0, [])
            counts |= {"fill_open_checked": checked, "fill_open_mismatch": len(moved)}
            notes += [f"체결가 ≠ 확정 시가 기준: {m}" for m in moved]
            written = execute_date(audit, day, prices, cfg=cfg)
        else:
            written = execute_date(audit, prev, prices, cfg=cfg) if open_before and prev else []
        counts |= _counts(written, "orders")
        return done("ok")
    except ExecutionRefused as e:
        return done("skipped", reason=str(e))
    except Exception as e:
        return done("failed", error=f"{type(e).__name__}: {e}"[:500])


def check_fill_opens(audit: AuditLog, prices: Prices, cfg, day: str) -> tuple[int, list[str]]:
    """Fills on ``day`` (now a final bar) against that bar's Open: a fill stage priced them from a provisional row,
    and the recorded fill price is never rewritten — a difference is only reported. Returns (checked, differing)."""
    filled = [o for o in latest_orders(audit.orders()).values()
              if o.status in ("filled", "partial") and o.fill_date == day and o.filled_price is not None]
    moved = []
    for o in filled:
        px = prices.open_on(o.ticker, day)
        want = None if px is None else round(px * (1 + cfg.slippage if o.side == "Buy" else 1 - cfg.slippage), 4)
        if want is None or abs(o.filled_price / want - 1) > 1e-6:
            moved.append(f"{o.order_id} {o.filled_price} vs {want}")
    return len(filled), moved


def default_decider(audit: AuditLog | None = None, market: str | None = None, day: str | None = None) -> str:
    """The decide stage's decider when none is given: ``config.LLM["enabled"]`` switches the loop to the model. Else,
    while the LLM trial is on (``trial_on``), the model for a market whose trial days other than ``day``
    (``llm_trial_days`` — ``day`` itself stays as it began, so a rerun of it does not count twice) are fewer than
    ``LLM["trial_sessions"]``, then the rule. No book given, or the trial off: the rule."""
    if LLM["enabled"]:
        return "llm"
    if not trial_on() or audit is None:
        return "rule"
    n = LLM["trial_sessions"]
    return "llm" if n is None or len([d for d in llm_trial_days(audit, market) if d != day]) < n else "rule"


def trial_on() -> bool:
    """The LLM trial: ``LLM["trial"]`` or TRADING_LLM_TRIAL=1 (the docker .env). Off by default."""
    return bool(LLM["trial"]) or os.environ.get("TRADING_LLM_TRIAL") == "1"


def llm_trial_days(audit: AuditLog, market: str) -> list[str]:
    """The decide days the model decided in this market's book: dates of decisions the model wrote
    (``settings.decider`` = ``LLM["model"]``, so a rule stand-in does not count) from the daily loop's screens
    (``RUN_IDS`` — a ``baseline_krx.py --decider llm`` grid has its own run ids). Replayed from the records."""
    prefix = f"{RUN_IDS[market]}_"
    return sorted({d.trade_date for d in audit.decisions() if d.settings.get("decider") == LLM["model"]
                   and (d.screen_run_id or "").startswith(prefix)})


def trial_note(audit: AuditLog, market: str, day: str, decider: str) -> str:
    """The decide run's line about the trial: which session this is, or that it ended and the rule decides."""
    n, used = LLM["trial_sessions"], len([d for d in llm_trial_days(audit, market) if d != day])
    if decider == "llm":
        return f"LLM 시험 {used + 1}/{n}회째" if n is not None else "LLM 시험 (기한 없음)"
    return f"LLM 시험 종료 → 규칙 ({used}/{n}회 끝남)"


def rule_fallback(market: str, run: ScreenRun, ticker: str, day: str, reason: str) -> DecisionRecord:
    """The rule baseline's decision for a ticker the model was not asked about, marked ``fallback: rule``. A Hold is
    recorded too (the rule path leaves it out): every target of an llm run has a record saying who decided it."""
    record = rule_record(market, run, ticker, day)
    record.settings |= {"fallback": "rule", "fallback_reason": reason}
    return record


def rule_record(market: str, run: ScreenRun, ticker: str, day: str) -> DecisionRecord:
    """The rule baseline's decision for ``ticker`` from ``run``, a Hold included (a record either way)."""
    text = f"**Rating**: Hold\n\n**근거**: 규칙 결정 없음 — {ticker}는 이 날의 선별 대상 밖입니다."
    record = None
    if any(c.ticker == ticker for c in run.candidates):
        decision, record = decide_from_screen(market, run, ticker, day)
        text = decision.text
    record = record or DecisionRecord(market=market, ticker=ticker, trade_date=day, screen_run_id=run.run_id,
                                      rating="Hold", action="Hold", thesis=text, settings={"decider": BASELINE_DECIDER})
    return record


def book_section(book: Book, prices: Prices, day: str, cfg: PaperConfig) -> tuple[agent.Section, float]:
    values = {t: h.quantity * (prices.close_asof(t, day) or h.avg_price) for t, h in book.holdings.items()}
    equity = book.cash + sum(values.values())
    lines = [f"- 평가 금액 {fmt_money(equity, cfg.market)} · 현금 {book.cash / equity:.1%} · 보유 {len(values)}종목"]
    lines += [f"- {t} ({sector_of(t) or '섹터 -'}): 비중 {v / equity:.1%}" for t, v in sorted(values.items())]
    return agent.Section("portfolio", "포트폴리오", "\n".join(lines), lines[0][2:]), equity


def position_text(ticker: str, book: Book, prices: Prices, calendar: list[str], day: str, equity: float,
                  decisions: list[DecisionRecord]) -> str:
    h = book.holdings[ticker]
    st = position_state(h, prices, calendar, day, RiskConfig())
    last = st["last"] or h.avg_price
    memo = next((d for d in reversed(decisions) if d.ticker == ticker and d.trade_date <= h.entry_date
                 and d.rating in ("Buy", "Overweight")), None)
    lines = [f"- 보유 {h.quantity}주 · 평균 매입가 {h.avg_price:g} · 매입일 {h.entry_date} · 최근 종가 {last:g} "
             f"({last / h.avg_price - 1:+.1%}) · 비중 {h.quantity * last / equity:.1%} · 보유 후 최고 종가 {st['high']:g}",
             "", "### 매수 당시 결정"]
    if memo is None:
        lines.append("매수 결정 기록 없음.")
    elif memo.settings.get("decider") == BASELINE_DECIDER:
        lines.append(f"{memo.trade_date} 규칙 베이스라인 매수(LLM 메모 없음): {memo.thesis}")
    else:
        lines.append(f"{memo.trade_date} 투자 메모:\n\n{memo.thesis}")
    return "\n".join(lines)


def agent_decide(audit: AuditLog, market: str, run: ScreenRun, day: str, cache: Path, prices: Prices,
                 cfg: PaperConfig, llm=None, *, skip=frozenset(), on_record=None, only: list[str] | None = None,
                 used: int = 0, deferred: list | None = None, notes: list | None = None) -> tuple[list[DecisionRecord], dict]:
    """The model's decisions of ``day``: a sell review of every holding (at the day's close), then a memo for every
    screened survivor not held, in rank order; tickers in ``skip`` are left out. Past ``LLM["max_calls_per_run"]``
    calls, or without a client or a snapshot, a ticker gets the rule decision (``rule_fallback``). Each record goes
    to ``on_record`` as soon as it is made, so a crash keeps what was paid for. Returns (records, counts).
    ``only``: ask these tickers instead (a holding gets the sell review); ``used``: calls this run made already;
    ``deferred``: given a list, a ticker that cannot be asked goes there as (ticker, reason), not to the rule.
    ``notes``: given a list, a line per reason the DART or SEC EDGAR section had no figures (``LLM["fundamentals"]``
    on)."""
    book = replay(list(latest_orders(audit.orders()).values()), prices, cfg, until=day)
    targets = [(agent.HOLDING, t) for t in sorted(book.holdings)]
    targets += [(agent.CANDIDATE, c.ticker) for c in sorted(run.selected, key=lambda c: c.rank or 0)
                if c.ticker not in book.holdings]
    if only is not None:
        targets = [(agent.HOLDING if t in book.holdings else agent.CANDIDATE, t) for t in only]
    targets = [(k, t) for k, t in targets if t not in skip]
    decisions = audit.decisions()
    calendar = prices.calendar(cfg.index)
    provisional = set(kis.provisional(cache).get(day, []))
    portfolio, equity = book_section(book, prices, day, cfg)
    raw_dir = audit.directory / "agent_raw" / day
    client, no_client = llm, None
    if client is None and targets:
        try:
            client = agent.make_client()
        except Exception as e:      # no key: every target gets the rule decision, said so in each record
            no_client = f"client: {type(e).__name__}: {e}"[:200]
    records, counts, no_fundamentals = [], Counter(), {}
    fundamentals_client = agent.fundamentals_client(market, cache)     # one a run (DART or SEC EDGAR)
    for kind, t in targets:
        cap = LLM["max_calls_per_run"]
        reason = no_client or (f"max_calls_per_run({cap})" if used + counts["llm_calls"] >= cap else None)
        snap = None
        if reason is None:
            try:
                prices_now = agent.market_section(kis.read_cache(kis.cache_path(t, cache)), day, t in provisional)
                fundamentals, why = agent.fundamentals_section(market, t, day, prices_now.close, cache, fundamentals_client)
                if why:
                    no_fundamentals.setdefault(why, []).append(t)
                snap = agent.Snapshot(t, day, MARKETS[market]["currency"], [
                    prices_now, *[s for s in [screen_section(run, t), fundamentals] if s], portfolio])
            except Exception as e:  # no price file, no row before the day
                reason = f"snapshot: {type(e).__name__}: {e}"[:200]
        if reason is None:
            try:
                memory = memory_text(t, day, decisions, book.closed)
                position = (position_text(t, book, prices, calendar, day, equity, decisions)
                            if kind == agent.HOLDING else None)
            except Exception as e:  # one ticker's prompt cannot be built: it alone gets the rule decision
                reason = f"prep: {type(e).__name__}: {e}"[:200]
        if reason is not None and deferred is not None:
            deferred.append((t, reason))
            continue
        if reason is not None:
            record = rule_fallback(market, run, t, day, reason)
            counts["llm_fallback"] += 1
        else:
            if kind == agent.HOLDING:
                out = agent.review_holding(t, snap, position, memory, client=client, raw_dir=raw_dir)
            else:
                out = agent.decide_candidate(t, snap, memory, client=client, raw_dir=raw_dir)
            counts["llm_calls"] += 1
            try:
                record = agent.to_record(out, market=market, day=day, screen_run_id=run.run_id,
                                         evidence=snap.evidence(run.run_id))
            except Exception as e:  # a paid answer that cannot be recorded as given: REVIEW, the raw file kept
                out.output, out.review = None, f"record: {type(e).__name__}: {e}"[:300]
                record = DecisionRecord(market=market, ticker=t, trade_date=day, screen_run_id=run.run_id,
                                        rating="REVIEW", action="Sell" if kind == agent.HOLDING else None,
                                        thesis=f"**Rating**: REVIEW\n\n**사유**: {out.review}\n\n**원문**: {out.raw}",
                                        settings=agent.settings() | {"kind": kind, "raw": out.raw})
            counts["llm_review"] += out.output is None
            counts.update(out.usage)
        records.append(record)
        if on_record:
            on_record(record)
    if notes is not None:
        notes += [f"재무 데이터 없음({why}) {len(ts)}종목: {', '.join(ts)}" for why, ts in no_fundamentals.items()]
    return records, {k: int(v) for k, v in counts.items()}


REVIEW_REASON = "등급을 읽을 수 없어 주문하지 않았습니다"      # the executor's text for a REVIEW order (execution._place)


def answer_reruns(audit: AuditLog, market: str, day: str, decider: str, cache: Path, prices: Prices, cfg: PaperConfig,
                  llm=None, used: int = 0) -> tuple[dict, list[str]]:
    """The reruns a person asked for on REVIEW exceptions (``audit.pending_reruns``), answered by ``day``'s decide
    after the day's own decisions and before the executor runs. An exception of an earlier decision day is decided
    again as of ``day`` from the screen that surfaced it, by the day's ``decider``: a new ``DecisionRecord`` dated
    ``day`` (``settings.rerun_of`` = the order id, ``attempt``), so the executor places it like any decision of the
    day — the REVIEW order's id already has its exception line, and the executor never orders an id twice. When the
    day already decided that ticker, that decision is the answer (no second one). Then the stage's marker
    (``OperationRecord`` exception_action, operator ``RERUN_OPERATOR``) and a closing line of the REVIEW order: off
    the queue. A REVIEW again goes back on it as the day's REVIEW order, written here with the rerun count carried
    (the executor would start it at 0), so the API's cap of two keeps counting. Nothing is changed in place.

    An llm rerun past ``LLM["max_calls_per_run"]`` (``used`` = the calls the day's decisions made), without a client
    or a prompt is put off to the next decide, never decided by the rule instead: a person asked for the model again.
    Same with an exception of ``day`` itself (its id is the order the answer would get), and with a ticker the day's
    llm run gave the rule instead (``fallback``). Returns (counts, notes)."""
    pending = pending_reruns(audit.orders())
    if not pending:
        return {}, []
    counts, notes = Counter(reruns_pending=len(pending)), []
    decisions, operations = audit.decisions(), audit.operations()
    screens = {r.run_id: r for r in audit.screens()}
    current = latest_orders(audit.orders())
    for o in pending:
        n = o.reruns
        if (marker := pending_rerun_marker(operations, o)) is not None:      # answered, not closed: a crash between
            audit.record_order(o.model_copy(update={"exception_resolved": True,
                                                    "reason": f"{o.reason} · {marker.reason}"}))
            counts["reruns_done"] += 1
            continue
        why, answer = None, next((d for d in decisions if d.settings.get("rerun_of") == o.order_id
                                  and d.settings.get("attempt") == n), None)
        same_day = None if answer is not None or o.trade_date >= day else next(
            (d for d in reversed(decisions) if d.source == "agent" and (d.ticker, d.trade_date) == (o.ticker, day)), None)
        asked = next((d for d in reversed(decisions) if d.source == "agent" and d.rating == RATING_REVIEW
                      and (d.ticker, d.trade_date) == (o.ticker, o.trade_date)), None)
        run = screens.get(asked.screen_run_id) if asked is not None and asked.screen_run_id else None
        if answer is None and o.trade_date >= day:
            why = "결정일 당일 — 다음 결정일에"
        elif answer is None and same_day is not None and same_day.settings.get("fallback"):
            # the day's llm run gave it the rule instead (cap, no key, no prompt): not an answer, and asking now
            # would make a second decision of the ticker that day
            why = f"그날 결정이 규칙 대체({same_day.settings.get('fallback_reason', 'fallback')}) — 다음 결정일에"
        elif answer is None and same_day is not None:
            answer = same_day
        elif answer is None and run is None:
            why = f"원 선별 {asked.screen_run_id if asked else '(결정 없음)'} 없음"
        elif answer is None:
            tag = {"rerun_of": o.order_id, "attempt": n}

            def keep(r: DecisionRecord, tag: dict = tag) -> None:
                r.settings |= tag
                audit.record_decision(r)

            if decider == "llm":
                put_off: list = []
                made, c = agent_decide(audit, market, run, day, cache, prices, cfg, llm, on_record=keep,
                                       only=[o.ticker], used=used, deferred=put_off)
                used += c.get("llm_calls", 0)
                counts.update(c)
                answer = made[0] if made else None
                why = put_off[0][1] if put_off else None
            else:
                answer = rule_record(market, run, o.ticker, day)
                keep(answer)
            if answer is not None:
                decisions.append(answer)            # a second reservation of the ticker takes it as the day's
        if answer is None:
            counts["reruns_deferred"] += 1
            notes.append(f"재실행 미룸 {o.order_id} ({n}회째): {why}")
            continue
        oid = f"paper-{day}-{o.ticker}"
        if not answer.tradeable and oid not in current:
            current[oid] = OrderRecord(order_id=oid, ticker=o.ticker, trade_date=day, market=market, quantity=0,
                                       side="Sell" if answer.action == "Sell" else "Buy", status="exception",
                                       exception_kind="review", reruns=n, retries=o.retries,
                                       reason=f"{REVIEW_REASON} · 재실행 {n}회째 결과")
            audit.record_order(current[oid])
        text = f"재실행 {n}회째 → {day} {answer.rating}" + (" (그날 결정)" if answer is same_day else "")
        audit.record_operation(OperationRecord(
            kind="exception_action", operator=RERUN_OPERATOR, role="auto", reason=text, market=market,
            target=o.order_id, before={"exception_action": "rerun", "reruns": n},
            after={"action": "rerun", "attempt": n, "pending": False,
                   "decision": {"trade_date": day, "rating": answer.rating, "decider": answer.settings.get("decider"),
                                "same_day": answer is same_day}}))
        audit.record_order(o.model_copy(update={"exception_resolved": True, "reason": f"{o.reason} · {text}"}))
        counts["reruns_done"] += 1
        notes.append(f"{o.order_id}: {text}")
    return dict(counts), notes


def dry_run(market: str, day: str, data: Path, llm=None) -> int:
    """``--decider llm --dry-run``: the model's decisions of ``day`` from the screen already recorded, over the book
    as it is — printed, never recorded (the responses are kept under ``agent_raw/``). Every target is asked, also
    one decided before. Exit 1 when the day has no recorded screen."""
    audit = market_audit(data / "audit", market)
    run = next((r for r in audit.screens() if r.run_id == f"{RUN_IDS[market]}_{day}"), None)
    if run is None:
        print(f"{market} {day}: 기록된 선별({RUN_IDS[market]}_{day}) 없음 — decide 단계가 먼저", file=sys.stderr)
        return 1
    prices = Prices(data / "cache", source="kis")
    records, counts = agent_decide(audit, market, run, day, data / "cache", prices, paper_config(market), llm)
    # no run record to carry it: a fallback to Yahoo's adjusted prices goes to stderr, never unsaid
    try:
        lines = fallback_notes(prices, [MARKETS[market]["benchmark"], *(c.ticker for c in run.candidates),
                                        *sorted({o.ticker for o in audit.orders()})], day)
    except Exception as e:  # after the paid calls: an unreadable file of some old name must not lose the table
        lines = [f"가격 폴백 확인 실패: {type(e).__name__}: {e}"[:200]]
    for line in lines:
        print(line, file=sys.stderr)
    print(f"{market} {day} 드라이런 — {LLM['model']} effort {LLM['effort']} {agent.prompt_version()}")
    print("| 종목 | 종류 | 등급 | 매도 사유 | 확신도 | REVIEW·폴백 | 입력 | 출력 | 캐시 읽기 | 캐시 쓰기 | 초 | 비용 $ |")
    print("|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|")
    for r in records:
        u = r.usage
        flag = r.settings.get("fallback_reason") or (r.thesis.split("**사유**: ")[1].split("\n")[0]
                                                     if r.rating == "REVIEW" else "")
        print(f"| {r.ticker} | {r.settings.get('kind', 'rule')} | {r.rating} | {r.sell_reason or ''} | "
              f"{'' if r.confidence is None else f'{r.confidence:.2f}'} | {flag} | {u.get('input_tokens', 0)} | "
              f"{u.get('output_tokens', 0)} | {u.get('cache_read_input_tokens', 0)} | "
              f"{u.get('cache_creation_input_tokens', 0)} | {r.settings.get('seconds', '')} | {agent.cost_usd(u):.4f} |")
    print(" · ".join(f"{k} {v}" for k, v in counts.items()) + f" · 비용 ${agent.cost_usd(counts):.4f}")
    return 0


def missed_days(audit: AuditLog, stage: str, day: str) -> list[str]:
    """Weekdays after the stage's last recorded day and before ``day`` with no record at all: runs nobody made (the
    process was down past the next session's open) — and the listed holidays among them, which no run was due on.
    Both in date order; ``run_due`` tells them apart (``market_calendar.holiday``). None before the first record — the loop starts on its first
    day."""
    seen = {r.trade_date for r in audit.runs() if r.stage == stage}
    if not seen:
        return []
    d, out = date.fromisoformat(max(seen)) + timedelta(days=1), []
    while d.isoformat() < day:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def run_due(market: str, stage: str, data: Path, client, now: datetime | None = None, *,
            decider: str | None = None, llm=None) -> list[RunRecord]:
    """What every scheduler runs (the scheduler module, this CLI without ``--date`` from cron): the day due at ``now``
    (``due_day``), after a ``skipped(missed)`` record for each weekday before it that nobody ran. The records written,
    [] when nothing is due, the day is done, or the executor refused it (halted: after the resume, ``--date``)."""
    now = now or datetime.now(kis.KST)
    day = due_day(market, stage, now)
    if day is None:
        print(f"{market} {stage}: 할 단계 없음(다음 세션 개장 뒤 — 지난 날은 --date로)")
        return []
    audit = market_audit(data / "audit", market)
    out = []
    with locked(audit.directory / f"daily_loop_{stage}"):
        for d in missed_days(audit, stage, day):
            if cal.holiday(market, date.fromisoformat(d)) is not None:     # a listed holiday: no run was due
                out.append(holiday_record(market, stage, date.fromisoformat(d)))
            else:
                out.append(RunRecord(market=market, stage=stage, trade_date=d, status="skipped", reason="missed",
                                     started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
            audit.record_run(out[-1])
        last = [r for r in audit.runs() if (r.stage, r.trade_date) == (stage, day)][-1:]
    if last and last[0].status == "skipped" and not last[0].final:
        print(f"{market} {stage} {day}: 실행기 거부로 끝남({last[0].reason}) — 재가동 뒤 --date {day}로")
        return out
    r = run_stage(market, stage, day, data, client, now, decider=decider, llm=llm)
    return out + ([r] if r else [])


def summary(r: RunRecord) -> str:
    extra = r.reason or r.error or ""
    return (f"{r.market} {r.stage} {r.trade_date}: {r.status}" + (f"({extra})" if extra else "") + " · "
            + " · ".join(f"{k} {v}" for k, v in r.counts.items()) + "".join(f"\n  {n}" for n in r.notes))


def main(argv: list[str] | None = None, client=None, now: datetime | None = None, llm=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", choices=sorted(RUN_IDS), required=True)
    parser.add_argument("--stage", choices=[*STAGES, "all"], required=True)
    parser.add_argument("--date", help="trading day YYYY-MM-DD (default: the day due now — due_day)")
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    parser.add_argument("--decider", choices=("rule", "llm"),
                        help=f"decide stage: rule baseline or {LLM['model']} (default: config.LLM enabled → llm)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --decider llm --stage decide --date: ask the model, print, record nothing")
    args = parser.parse_args(argv)
    now = now or datetime.now(kis.KST)
    if args.dry_run:
        if args.decider != "llm" or args.stage != "decide" or not args.date:
            parser.error("--dry-run은 --decider llm --stage decide --date와 함께")
        check_date(args.date)
        return dry_run(args.market, args.date, Path(args.data_dir), llm)
    if args.date:
        try:
            check_date(args.date)
        except ValueError:
            parser.error(f"--date는 YYYY-MM-DD 형식이어야 합니다: {args.date!r}")
    if client is None:
        try:
            client = kis.KisClient()
        except Exception as e:     # a missing variable is named, never a value
            print(f"KIS 클라이언트 실패 — {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    data, code = Path(args.data_dir), 0
    for stage in (["fill", "decide"] if args.stage == "all" else [args.stage]):
        try:
            if args.date:
                records = [r for r in [run_stage(args.market, stage, args.date, data, client, now,
                                                  decider=args.decider, llm=llm)] if r]
            else:
                records = run_due(args.market, stage, data, client, now, decider=args.decider, llm=llm)
        except ValueError as e:
            parser.error(str(e))
        for r in records:
            print(summary(r))
            if r.status == "failed":
                return 1
            if r.status == "skipped" and r.reason not in ("holiday", "missed"):
                code = 3
    return code


if __name__ == "__main__":
    sys.exit(main())
