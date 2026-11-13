"""Point-in-time index membership for the 4th measurement (docs/09): start members + dated changes.

Inputs live in data/external/ (sources and dates in data/external/SOURCES.md); outputs in data/universe/:

- sp500_history.csv     start = the Wikipedia constituents table as of revision 1192789360 (2023-12-31, the
                        last edit before 2024-01-02), changes = the "Historical components of the S&P 500"
                        changes table, effective dates after that revision up to --end.
- kospi200_history.csv  start = the KRX [11006] constituent download dated on or before --start, changes = the
                        difference between consecutive downloads, dated on the later download's date (so a
                        change between two snapshots is applied late, never early). Skipped when no file.

Rows: date, ticker (Yahoo form), action (start | add | remove), name. Standard library HTML parsing only.

Run:  .venv/Scripts/python scripts/build_universe.py [--start 2024-01-02] [--end 2026-11-12]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "data" / "external"
OUT = ROOT / "data" / "universe"

SP500_START_HTML = EXT / "sp500_wikipedia_oldid1192789360_2023-12-31.html"
SP500_START_REV_DATE = date(2023, 12, 31)
SP500_CHANGES_HTML = EXT / "sp500_historical_components_2026-11-13.html"
SP500_NOW_HTML = EXT / "sp500_wikipedia_2026-11-13.html"
# KRX [11006] 지수구성종목 CSV saved by hand, one per snapshot date: krx_11006_kospi200_YYYYMMDD.csv
KRX_SNAPSHOT_GLOB = "krx_11006_kospi200_*.csv"

# Ticker renames inside the window that the changes table does not record (same company, new symbol): found by
# replaying start + changes and matching security names against the 2026-11-08 constituents table. Yahoo keeps a
# renamed company's history under the new symbol, so the history uses the new one throughout. WRK → SW is the
# WestRock / Smurfit Kappa combination (2024-07): Yahoo's SW rows before 2024-07-08 are another thinly traded
# line (0 ~ 15,700 shares a day), not WestRock, so WestRock's own prices for 2024-01-02 ~ 2024-07-05 are missing
# (docs/09 §2). The gap is reported, not patched.
US_RENAMES = {
    "BK": "BNY", "EQR": "VMRK", "FI": "FISV", "FLT": "CPAY", "MMC": "MRSH", "PARA": "SKYD", "PEAK": "DOC",
    "SATS": "ECHO", "WRK": "SW",
}


class _Tables(HTMLParser):
    """Every <table> as rows of cell text, with rowspan/colspan expanded. Tables keyed by id (or index)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[str]]] = {}
        self._stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._stack.append({"id": a.get("id") or f"#{len(self.tables) + len(self._stack)}", "rows": [],
                                "row": None, "cell": None, "span": {}})
        elif not self._stack:
            return
        elif tag == "tr":
            self._stack[-1]["row"] = []
        elif tag in ("td", "th") and self._stack[-1]["row"] is not None:
            self._stack[-1]["cell"] = {"text": [], "rowspan": int(a.get("rowspan", 1) or 1),
                                       "colspan": int(a.get("colspan", 1) or 1)}
        elif tag == "br" and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(" ")

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(data)

    def handle_endtag(self, tag):
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th") and t["cell"] is not None:
            c = t["cell"]
            text = re.sub(r"\s+", " ", "".join(c["text"])).strip()
            text = re.sub(r"\[\d+\]", "", text).strip()          # footnote markers
            for _ in range(c["colspan"]):
                t["row"].append((text, c["rowspan"]))
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            # place cells around the ones still spanning down from earlier rows
            out, cells, col = [], list(t["row"]), 0
            while cells or col in t["span"]:
                if col in t["span"]:
                    text, left = t["span"][col]
                    out.append(text)
                    if left > 1:
                        t["span"][col] = (text, left - 1)
                    else:
                        del t["span"][col]
                else:
                    text, rowspan = cells.pop(0)
                    out.append(text)
                    if rowspan > 1:
                        t["span"][col] = (text, rowspan - 1)
                col += 1
            t["rows"].append(out)
            t["row"] = None
        elif tag == "table":
            done = self._stack.pop()
            self.tables[done["id"]] = done["rows"]


def tables(path: Path) -> dict[str, list[list[str]]]:
    p = _Tables()
    p.feed(path.read_text(encoding="utf-8"))
    return p.tables


def yahoo_us(ticker: str) -> str:
    """Wikipedia/S&P share-class dots → Yahoo dashes (BRK.B → BRK-B, BF.B → BF-B)."""
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", ticker.strip().upper())


def constituents(path: Path, renames: dict[str, str] | None = None) -> dict[str, str]:
    rows = tables(path)["constituents"]
    renames = renames or {}
    return {renames.get(yahoo_us(r[0]), yahoo_us(r[0])): r[1] for r in rows[1:] if r and r[0]}


def sp500_changes(path: Path, renames: dict[str, str] | None = None) -> list[tuple[date, str, str, str]]:
    """(effective date, ticker, add|remove, name) from the changes table. Header rows are skipped."""
    out, renames = [], renames or {}
    for r in tables(path)["changes"]:
        if len(r) < 5:
            continue
        try:
            d = datetime.strptime(r[0], "%B %d, %Y").date()
        except ValueError:
            continue                                    # header rows
        if r[1]:
            out.append((d, renames.get(yahoo_us(r[1]), yahoo_us(r[1])), "add", r[2]))
        if r[3]:
            out.append((d, renames.get(yahoo_us(r[3]), yahoo_us(r[3])), "remove", r[4]))
    return out


def build_sp500(start: date, end: date) -> tuple[list[dict], dict]:
    members = constituents(SP500_START_HTML, US_RENAMES)
    rows = [{"date": start.isoformat(), "ticker": t, "action": "start", "name": n} for t, n in sorted(members.items())]
    changes = sorted((c for c in sp500_changes(SP500_CHANGES_HTML, US_RENAMES) if SP500_START_REV_DATE < c[0] <= end),
                     key=lambda c: (c[0], c[2] != "remove"))       # same day: removals first
    live, anomalies = set(members), []
    for d, t, action, name in changes:
        if action == "add" and t in live or action == "remove" and t not in live:
            anomalies.append(f"{d} {action} {t}: {'already a member' if action == 'add' else 'not a member'}")
        live = live | {t} if action == "add" else live - {t}
        rows.append({"date": max(d, start).isoformat(), "ticker": t, "action": action, "name": name})
    now = set(constituents(SP500_NOW_HTML))
    stats = {"start_members": len(members), "changes": len(changes), "unique_tickers": len({r["ticker"] for r in rows}),
             "replay_vs_now_missing": sorted(now - live), "replay_vs_now_extra": sorted(live - now),
             "anomalies": anomalies}
    return rows, stats


def _read_krx_csv(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.DictReader(text.splitlines()))


def build_kospi200(start: date, end: date) -> tuple[list[dict], dict] | None:
    files = sorted(EXT.glob(KRX_SNAPSHOT_GLOB))
    if not files:
        return None
    snaps = []
    for f in files:
        d = datetime.strptime(f.stem.rsplit("_", 1)[1], "%Y%m%d").date()
        recs = _read_krx_csv(f)
        snaps.append((d, {f"{r['종목코드'].strip().zfill(6)}.KS": r["종목명"].strip() for r in recs}))
    first = [s for s in snaps if s[0] <= start]
    if not first:
        raise SystemExit(f"KRX 스냅샷 중 {start} 이전 것이 없다: {[s[0] for s in snaps]}")
    base_date, base = first[-1]
    rows = [{"date": start.isoformat(), "ticker": t, "action": "start", "name": n} for t, n in sorted(base.items())]
    prev = base
    for d, cur in (s for s in snaps if base_date < s[0] <= end):
        rows += [{"date": d.isoformat(), "ticker": t, "action": "remove", "name": prev[t]} for t in sorted(set(prev) - set(cur))]
        rows += [{"date": d.isoformat(), "ticker": t, "action": "add", "name": cur[t]} for t in sorted(set(cur) - set(prev))]
        prev = cur
    stats = {"snapshots": [s[0].isoformat() for s in snaps], "start_snapshot": base_date.isoformat(),
             "start_members": len(base), "unique_tickers": len({r["ticker"] for r in rows})}
    return rows, stats


def write(rows: list[dict], path: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "ticker", "action", "name"])
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    rows, stats = build_sp500(start, end)
    write(rows, OUT / "sp500_history.csv")
    print(f"sp500: start {stats['start_members']}, changes {stats['changes']}, unique {stats['unique_tickers']}")
    print(f"  replay vs 2026-11-08 page: missing {stats['replay_vs_now_missing']} extra {stats['replay_vs_now_extra']}")
    for a in stats["anomalies"]:
        print(f"  anomaly: {a}")

    krx = build_kospi200(start, end)
    if krx is None:
        print(f"kospi200: 파일 없음 (data/external/{KRX_SNAPSHOT_GLOB}) — 건너뜀")
    else:
        rows, stats = krx
        write(rows, OUT / "kospi200_history.csv")
        print(f"kospi200: snapshots {len(stats['snapshots'])} (start {stats['start_snapshot']}), "
              f"start {stats['start_members']}, unique {stats['unique_tickers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
