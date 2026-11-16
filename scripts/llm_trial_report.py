"""The LLM trial's figures per market (config.LLM trial, docs/01 §12-7), from the records only: no model call, nothing
written.

Run:  .venv/Scripts/python scripts/llm_trial_report.py --market all [--json] [--data-dir data]

Per market: the trial days (``daily_loop.llm_trial_days`` — the decide days the model decided) against
``LLM["trial_sessions"]``; the model's decisions of those days (ratings, REVIEW with its reason, the rule stand-ins
of the same days); tokens and cost (``agent.PRICE``); agreement with the rule baseline's call on the same ticker and
day (``daily_loop.rule_record`` over the decision's screen — computed, not recorded); the 5-day settlement of each
decision (``memory.settlement.fetch_returns``: entry at the first close after the decision day, raw and alpha against
the market's benchmark five sessions later) over the KIS cache; the orders dated on trial days by status (blocked =
a hard rule shrank it to zero). Exit codes: 0, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import daily_loop as dl

from agent_trading_core import agent
from agent_trading_core.audit import latest_orders, market_audit
from agent_trading_core.config import LLM, MARKETS
from agent_trading_core.memory import settlement
from agent_trading_core.rating import RATING_REVIEW

HOLDING_DAYS = 5
BUY_SIDE = ("Buy", "Overweight")


@contextmanager
def kis_closes(cache: Path):
    """``settlement.fetch_returns`` reads Yahoo (``get_closes``, the network); the daily loop's prices are the KIS
    cache. ponytail: swaps the module's ``get_closes`` for the length of the report — drop it once fetch_returns
    takes its price source as an argument."""
    prices = dl.KisPrices(cache)

    def closes(symbol: str, start: str, end: str) -> pd.Series:
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
    }


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
    return "\n".join(lines)


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
