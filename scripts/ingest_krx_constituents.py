"""KRX data downloaded by hand (data/external/SOURCES.md): KOSPI200 point-in-time members and the official KOSPI.

Inputs (data/external/, cp949, as saved from data.krx.co.kr; never modified):
- kospi200_YYYYMMDD.csv   [11006] 지수구성종목, one per snapshot date
- kospi_daily_*.csv       [11003] 개별지수 시세 추이, KOSPI (two files: the site serves at most two years per download)

Outputs:
- data/universe/kospi200_history.csv   same shape as sp500_history.csv (date, ticker, action, name). start = the
  snapshot dated on or before --start; changes = the difference between consecutive snapshots, dated on the later
  snapshot's date. The real change dates are not in the files: a change between two snapshots shows up on the
  next snapshot date (late, never early), and a name that joined and left between two snapshots is not seen.
- data/external/kospi_official_daily.csv   the two index files merged by date (overlap dropped, ascending), utf-8,
  columns Date, Open, High, Low, Close, Volume, TradingValue, MarketCap (KRX units: volume in thousands of shares,
  value in millions of KRW)
- data/external/ks11_check.csv   yfinance ^KS11 close (data/cache) vs the KRX close per date, diff_pct = yf/krx − 1

Run:  .venv/Scripts/python scripts/ingest_krx_constituents.py [--start 2024-01-02] [--end 2026-11-12]
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_trading_core.krx_names import load_listed_names  # after the path insert

EXT = ROOT / "data" / "external"
OUT = ROOT / "data" / "universe"
SNAPSHOT_GLOB = "kospi200_[0-9]*.csv"
INDEX_GLOB = "kospi_daily_*.csv"
SNAPSHOT_COLUMNS = ["종목코드", "종목명", "종가", "대비", "등락률", "상장시가총액"]
INDEX_COLUMNS = ["일자", "종가", "대비", "등락률", "시가", "고가", "저가", "거래량", "거래대금", "상장시가총액"]
INDEX_RENAME = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume",
                "거래대금": "TradingValue", "상장시가총액": "MarketCap"}


def read_krx_csv(path: Path, columns: list[str]) -> list[dict]:
    """Rows of a KRX download (cp949). A header other than ``columns`` stops the run: the menu or layout changed."""
    rows = list(csv.DictReader(path.read_bytes().decode("cp949").splitlines()))
    header = list(rows[0]) if rows else []
    if header != columns:
        raise SystemExit(f"{path.name}: 열이 다르다 {header} != {columns} — 멈춤")
    return rows


def read_snapshots(paths: list[Path]) -> list[tuple[date, dict[str, str]]]:
    """[(snapshot date, {"005930.KS": "삼성전자", ...})] in date order; the date is the file name's YYYYMMDD."""
    snaps = []
    for p in paths:
        d = datetime.strptime(p.stem.rsplit("_", 1)[1], "%Y%m%d").date()
        snaps.append((d, {f"{r['종목코드'].strip().zfill(6)}.KS": r["종목명"].strip()
                          for r in read_krx_csv(p, SNAPSHOT_COLUMNS)}))
    return sorted(snaps)


def build_history(snaps: list[tuple[date, dict[str, str]]], start: date, end: date) -> list[dict]:
    first = [s for s in snaps if s[0] <= start]
    if not first:
        raise SystemExit(f"{start} 이전 스냅샷이 없다: {[s[0].isoformat() for s in snaps]}")
    base_date, base = first[-1]
    rows = [{"date": start.isoformat(), "ticker": t, "action": "start", "name": n} for t, n in sorted(base.items())]
    prev = base
    for d, cur in (s for s in snaps if base_date < s[0] <= end):
        rows += [{"date": d.isoformat(), "ticker": t, "action": "remove", "name": prev[t]} for t in sorted(set(prev) - set(cur))]
        rows += [{"date": d.isoformat(), "ticker": t, "action": "add", "name": cur[t]} for t in sorted(set(cur) - set(prev))]
        prev = cur
    return rows


def merge_index(paths: list[Path]) -> pd.DataFrame:
    """The KOSPI downloads as one ascending frame. A date in two files must carry the same row; it is kept once."""
    frames = [pd.DataFrame(read_krx_csv(p, INDEX_COLUMNS)) for p in paths]
    df = pd.concat(frames, ignore_index=True)
    df["Date"] = pd.to_datetime(df["일자"], format="%Y/%m/%d")
    df = df.drop(columns=["일자", "대비", "등락률"]).rename(columns=INDEX_RENAME)
    for c in INDEX_RENAME.values():
        df[c] = df[c].astype(float)
    dup = df[df.duplicated("Date", keep=False)]
    clash = dup.groupby("Date").nunique().gt(1).any(axis=1)
    if clash.any():
        raise SystemExit(f"겹치는 날짜의 값이 파일마다 다르다: {[d.date().isoformat() for d in clash[clash].index]} — 멈춤")
    out = df.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)
    return out[["Date", *INDEX_RENAME.values()]]


def check_against(official: pd.DataFrame, yahoo_close: pd.Series) -> pd.DataFrame:
    """Per date in either series: yfinance close, KRX close, diff_pct (empty when one side has no row)."""
    j = pd.concat([yahoo_close.rename("yfinance"), official.set_index("Date")["Close"].rename("krx")], axis=1)
    j["diff_pct"] = (j["yfinance"] / j["krx"] - 1) * 100
    j.index.name = "date"
    return j.reset_index()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    snaps = read_snapshots(sorted(EXT.glob(SNAPSHOT_GLOB)))
    rows = build_history(snaps, start, end)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "kospi200_history.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "ticker", "action", "name"])
        w.writeheader()
        w.writerows(rows)
    unique = list(dict.fromkeys(r["ticker"] for r in rows))
    print(f"kospi200: snapshots {[s[0].isoformat() for s in snaps]}, rows {len(rows)}, "
          f"start {sum(r['action'] == 'start' for r in rows)}, add {sum(r['action'] == 'add' for r in rows)}, "
          f"remove {sum(r['action'] == 'remove' for r in rows)}, unique {len(unique)}")
    for d, cur in snaps:
        print(f"  {d}: {len(cur)} members")
    # market check: every KOSPI200 member should be listed on 유가증권 (.KS) in the KIND list
    kind = load_listed_names()
    by_code = {t[:6]: t[7:] for t in kind}
    kq = [t for t in unique if by_code.get(t[:6], "KS") != "KS"]
    missing = [t for t in unique if t[:6] not in by_code]
    names = {r["ticker"]: r["name"] for r in rows}
    print(f"  KIND: .KS {len(unique) - len(kq) - len(missing)}, other market {kq}, "
          f"not listed now {[(t, names[t]) for t in missing]}")
    if kq:
        raise SystemExit(f"KOSPI200 구성에 유가증권 아닌 종목: {kq} — 멈춤")

    official = merge_index(sorted(EXT.glob(INDEX_GLOB)))
    official.assign(Date=official["Date"].dt.strftime("%Y-%m-%d")).to_csv(EXT / "kospi_official_daily.csv", index=False,
                                                                          encoding="utf-8")
    print(f"kospi official: {len(official)} days {official['Date'].iloc[0].date()}..{official['Date'].iloc[-1].date()}")
    cache = ROOT / "data" / "cache" / "^KS11-YFin-data.csv"
    y = pd.read_csv(cache, usecols=["Date", "Close"])
    y["Date"] = pd.to_datetime(y["Date"].str[:10])
    y = y.set_index("Date")["Close"].loc[args.start:args.end]
    check = check_against(official, y)
    check.assign(date=check["date"].dt.strftime("%Y-%m-%d")).to_csv(EXT / "ks11_check.csv", index=False, encoding="utf-8")
    both = check.dropna()
    off = both[both["diff_pct"].abs() > 1]
    print(f"^KS11 check: both {len(both)}, only yfinance {check['krx'].isna().sum()}, only KRX {check['yfinance'].isna().sum()}, "
          f"|diff| > 1% {len(off)}, max |diff| {both['diff_pct'].abs().max():.6f}%")
    for d in off["date"]:
        print(f"  {d.date()}")
    row = check[check["date"] == pd.Timestamp("2026-07-31")]
    if not row.empty:
        r = row.iloc[0]
        ret = both.set_index("date")[["yfinance", "krx"]].pct_change().loc[pd.Timestamp("2026-07-31")]
        print(f"  2026-07-31: yfinance {r['yfinance']:.2f}, KRX {r['krx']:.2f}, diff {r['diff_pct']:+.6f}%; "
              f"day return yfinance {ret['yfinance']:+.4%}, KRX {ret['krx']:+.4%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
