"""Markdown tables for docs/05 · docs/07 straight from summary.json, so no number is copied by hand.

Run:  .venv/Scripts/python scripts/paper_tables.py [--us data/results/paper_us/summary.json]
                                                  [--krx data/results/paper/summary.json]
Prints the variant table, the window table and the worst trades for the US book, then the KRX-vs-US comparison.
With --universe <constituents summary.json> (docs/09) it prints the fixed-40 vs point-in-time table instead, the
fixed side read from --fixed (default: the --us file).
With --dd-old <summary.json> --dd-new <summary.json> it prints the −20%-stop recovery comparison (docs/05 §7-1:
half_size before, reset_peak after); with --grid-old / --grid-new the same for the 27-cell grid.
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


def dd_counts(events: list[dict]) -> tuple[int, int]:
    """(−20% stops, re-entries after one): a re-entry is a ``peak_reset`` event or the half_size rule's 재가동 line."""
    stops = sum(1 for e in events if e["kind"] == "guard" and e["text"].startswith("MaxDrawdown"))
    resets = sum(1 for e in events if e["kind"] == "peak_reset" or (e["kind"] == "guard" and e["text"].startswith("재가동")))
    return stops, resets


def dd_table(old: dict, new: dict) -> str:
    rows = ["| | 최대 드로다운 (전 → 후) | 수익 (전 → 후) | 거래 (전 → 후) | −20% 발동 (전 → 후) | 피크 재설정 (전 → 후) | "
            "피크 재설정 날짜 (후) |", "|---|---|---|---|---|---|---|"]
    for key, label in VARIANTS.items():
        o, n = old["variants"][key], new["variants"][key]
        (os_, or_), (ns, nr) = dd_counts(o["events"]), dd_counts(n["events"])
        dates = ", ".join(e["date"] for e in n["events"] if e["kind"] == "peak_reset") or "—"
        rows.append(f"| {label} | {pct(o['metrics']['max_drawdown'])} → {pct(n['metrics']['max_drawdown'])} | "
                    f"{pct(o['metrics']['total_return'])} → {pct(n['metrics']['total_return'])} | {o['trades']} → {n['trades']} | "
                    f"{os_} → {ns} | {or_} → {nr} | {dates} |")
    return "\n".join(rows)


def grid_dd_table(old: dict, new: dict) -> str:
    rows = ["| 손절 | 비중 | 트레일링 | 최대 드로다운 (전 → 후) | 수익 (전 → 후) | 거래 (전 → 후) | −20% 발동 (전 → 후) | "
            "피크 재설정 (후) | 같음 |", "|---|---|---|---|---|---|---|---|---|"]
    same = 0
    for o, n in zip(old["cells"], new["cells"], strict=True):
        assert (o["stop_loss_pct"], o["max_position_pct"], o["trailing_pct"]) == \
               (n["stop_loss_pct"], n["max_position_pct"], n["trailing_pct"])
        eq = (o["max_drawdown"], o["total_return"], o["trades"]) == (n["max_drawdown"], n["total_return"], n["trades"])
        same += eq
        rows.append(f"| {pct(o['stop_loss_pct'], 0)} | {o['max_position_pct']:.0%} | {o['trailing_pct']:.0%} | "
                    f"{pct(o['max_drawdown'])} → {pct(n['max_drawdown'])} | {pct(o['total_return'])} → {pct(n['total_return'])} | "
                    f"{o['trades']} → {n['trades']} | {o['stop_outs']} → {n['stop_outs']} | {n['peak_resets']} | "
                    f"{'같음' if eq else '다름'} |")
    stopped = sum(1 for o in old["cells"] if o["stop_outs"])
    rows.append(f"\n27칸 중 수치가 같은 칸 {same}, 옛 규칙에서 −20%가 발동한 칸 {stopped}.")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--us", default=str(ROOT / "data" / "results" / "paper_us" / "summary.json"))
    parser.add_argument("--krx", default=str(ROOT / "data" / "results" / "paper" / "summary.json"))
    parser.add_argument("--universe", help="constituents summary.json (docs/09)")
    parser.add_argument("--fixed", help="fixed-40 summary.json for --universe (default: --us)")
    parser.add_argument("--dd-old", help="summary.json under the half_size rule (docs/05 §7-1)")
    parser.add_argument("--dd-new", help="summary.json under the reset_peak rule")
    parser.add_argument("--grid-old", help="grid_half_size.json")
    parser.add_argument("--grid-new", help="grid.json")
    args = parser.parse_args()
    if args.dd_old and args.dd_new:
        old, new = (json.loads(Path(x).read_text(encoding="utf-8")) for x in (args.dd_old, args.dd_new))
        print(dd_table(old, new))
        return 0
    if args.grid_old and args.grid_new:
        old, new = (json.loads(Path(x).read_text(encoding="utf-8")) for x in (args.grid_old, args.grid_new))
        print(grid_dd_table(old, new))
        return 0
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
