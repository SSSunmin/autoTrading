"""Markdown tables for docs/05 · docs/07 straight from summary.json, so no number is copied by hand.

Run:  .venv/Scripts/python scripts/paper_tables.py [--us data/results/paper_us/summary.json]
                                                  [--krx data/results/paper/summary.json]
Prints the variant table, the window table and the worst trades for the US book, then the KRX-vs-US comparison.
With --universe <constituents summary.json> (docs/09) it prints the fixed-40 vs point-in-time table instead, the
fixed side read from --fixed (default: the --us file).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.krx_names import EXIT_REASON_KO, korean_name

VARIANTS = {"rules_off": "A 하드 룰 끔", "rules_on": "B 하드 룰 켬", "rules_on_regime": "C B + 국면 필터"}


def pct(x: float | None, digits: int = 1) -> str:
    return "" if x is None else f"{x:+.{digits}%}".replace("-", "−")


def dd_span(m: dict) -> str:
    return f"{m['dd_peak']} → {m['dd_trough']}, " + (f"회복 {m['dd_recovery']}" if m["dd_recovery"] else "미회복")


def variant_table(s: dict, bench_name: str) -> str:
    rows = ["| | 수익 | CAGR | 최대 드로다운 | 드로다운 구간 | 최악의 날 | 변동성 | 거래 | 청산 사유 | 평균 노출 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    b = s["benchmark"]["metrics"]
    rows.append(f"| {bench_name} 매수 보유 | {pct(b['total_return'])} | {pct(b['cagr'])} | **{pct(b['max_drawdown'])}** | "
                f"{dd_span(b)} | {pct(b['worst_day'])} ({b['worst_day_date']}) | {b['volatility']:.1%} | | | 100% |")
    for key, label in VARIANTS.items():
        v = s["variants"][key]
        m = v["metrics"]
        exits = " · ".join(f"{EXIT_REASON_KO.get(k, k)} {n}" for k, n in sorted(v["exits_by_reason"].items(), key=lambda kv: -kv[1]))
        win = f" (승률 {v['win_rate']:.0%})" if key != "rules_off" and v["win_rate"] is not None else ""
        rows.append(f"| {label} | {pct(m['total_return'])} | {pct(m['cagr'])} | **{pct(m['max_drawdown'])}** | {dd_span(m)} | "
                    f"{pct(m['worst_day'])} ({m['worst_day_date']}) | {m['volatility']:.1%} | {v['trades']}{win} | {exits} | "
                    f"{v['mean_exposure']:.0%} |")
    return "\n".join(rows)


def window_table(s: dict, bench_name: str) -> str:
    rows = [f"| 구간 | {bench_name} | A 끔 | B 켬 | C 국면 |", "|---|---|---|---|---|"]
    for name, (a, b) in s["windows"].items():
        cells = [s["benchmark"]["windows"][name]] + [s["variants"][k]["windows"][name] for k in VARIANTS]
        rows.append(f"| {name} ({a} ~ {b}) | " + " | ".join(f"{pct(c['return'])} / {pct(c['max_drawdown'])}" for c in cells) + " |")
    return "\n".join(rows)


def worst_trades(s: dict, key: str, n: int = 5) -> str:
    rows = ["| 종목 | 진입 | 청산 | 사유 | 손익 |", "|---|---|---|---|---|"]
    for t in s["variants"][key]["trades_sample"][:n]:
        name = korean_name(t["ticker"]) or t["ticker"]
        rows.append(f"| {name} ({t['ticker']}) | {t['entry_date']} | {t['exit_date']} | {EXIT_REASON_KO.get(t['reason'], t['reason'])} | "
                    f"{pct(t['pnl_pct'])} |")
    return "\n".join(rows)


def guard_counts(s: dict, key: str) -> str:
    ev = s["variants"][key]["events"]
    def count(kind, prefix):
        return sum(1 for e in ev if e["kind"] == kind and e["text"].startswith(prefix))
    return (f"일일 손실 한도 {count('guard', '일일 손실 한도')}회 · MaxDrawdown 정지 {count('guard', 'MaxDrawdown')}회 · "
            f"StoplossGuard 잠금 {count('lock', 'StoplossGuard')} · 국면 필터 잠금 {count('lock', '국면 필터')} 리밸런스일 · "
            f"일일 손실 한도 발동일 {', '.join(e['date'] for e in ev if e['kind'] == 'guard' and e['text'].startswith('일일 손실 한도'))}")


def compare_table(krx: dict, us: dict) -> str:
    rows = ["| | KRX 최대 드로다운 | US 최대 드로다운 | KRX 수익 | US 수익 | KRX 거래 | US 거래 |", "|---|---|---|---|---|---|---|"]
    kb, ub = krx["benchmark"]["metrics"], us["benchmark"]["metrics"]
    rows.append(f"| 지수 매수 보유 (코스피 / S&P 500) | {pct(kb['max_drawdown'])} | {pct(ub['max_drawdown'])} | "
                f"{pct(kb['total_return'])} | {pct(ub['total_return'])} | | |")
    for key, label in VARIANTS.items():
        k, u = krx["variants"][key], us["variants"][key]
        rows.append(f"| {label} | {pct(k['metrics']['max_drawdown'])} | {pct(u['metrics']['max_drawdown'])} | "
                    f"{pct(k['metrics']['total_return'])} | {pct(u['metrics']['total_return'])} | {k['trades']} | {u['trades']} |")
    return "\n".join(rows)


def universe_table(fixed: dict, pit: dict) -> str:
    rows = ["| | 고정 40 최대 드로다운 | 시점 구성 최대 드로다운 | 고정 40 수익 | 시점 구성 수익 | 고정 40 거래 | 시점 구성 거래 | "
            "시점 구성 상장폐지 청산 |", "|---|---|---|---|---|---|---|---|"]
    for key, label in VARIANTS.items():
        f, p = fixed["variants"][key], pit["variants"][key]
        rows.append(f"| {label} | {pct(f['metrics']['max_drawdown'])} | {pct(p['metrics']['max_drawdown'])} | "
                    f"{pct(f['metrics']['total_return'])} | {pct(p['metrics']['total_return'])} | {f['trades']} | {p['trades']} | "
                    f"{p['exits_by_reason'].get('delisted', 0)} |")
    return "\n".join(rows)


def universe_facts(pit: dict) -> str:
    return (f"등장 종목 {pit['unique_tickers']} · 시세 받음 {pit['loaded'] - len(pit['no_history'])} · "
            f"못 받음 {len(pit['failed'])} · 구성 기간 이력 없음 {len(pit['no_history'])} · "
            f"시작 구성 {pit['start_members']} · 끝 구성 {pit['end_members']} · 기간 중 시세가 끝난 종목 {len(pit['ended_early'])} · "
            f"마지막 시세까지로 읽음 {len(pit['read_to_last_bar'])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--us", default=str(ROOT / "data" / "results" / "paper_us" / "summary.json"))
    parser.add_argument("--krx", default=str(ROOT / "data" / "results" / "paper" / "summary.json"))
    parser.add_argument("--universe", help="constituents summary.json (docs/09)")
    parser.add_argument("--fixed", help="fixed-40 summary.json for --universe (default: --us)")
    args = parser.parse_args()
    if args.universe:
        pit = json.loads(Path(args.universe).read_text(encoding="utf-8"))
        fixed = json.loads(Path(args.fixed or args.us).read_text(encoding="utf-8"))
        print(universe_table(fixed, pit), "", universe_facts(pit), sep="\n")
        return 0
    us = json.loads(Path(args.us).read_text(encoding="utf-8"))
    krx = json.loads(Path(args.krx).read_text(encoding="utf-8"))
    print(variant_table(us, "S&P 500"), "", window_table(us, "S&P 500"), "", sep="\n")
    for key in ("rules_on", "rules_on_regime"):
        print(f"{VARIANTS[key]} 최악 거래:", worst_trades(us, key), f"{VARIANTS[key]}: {guard_counts(us, key)}", "", sep="\n")
    print(compare_table(krx, us))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
