"""KIS prices against what the backtests read (docs/09 §5, docs/01 §11-4). No KIS call: it reads the caches that
``paper_backtest.py --market KRX --source kis`` wrote (data/cache/<symbol>-KIS-data.csv).

1. ``^KS11``: adds KIS as the third column of data/external/ks11_check.csv (yfinance · KRX official from
   scripts/ingest_krx_constituents.py), with ``diff_kis_pct`` = KIS / KRX − 1 in percent, and prints 2026-07-20 ~
   2026-08-08 plus every date where either source is more than 1% from the KRX file.
2. Stocks: KIS close vs the Yahoo cache close per date for every symbol that has both files →
   data/results/kis/close_diff.csv (per symbol: days, median and max |diff| %, the date of the max), and
   data/results/kis/close_diff_monthly.csv (per month: the median over symbols of each symbol's median signed
   diff % = KIS / Yahoo − 1, and how many symbols KIS is above).

Run:  .venv/Scripts/python scripts/kis_check.py [--start 2024-01-02] [--end 2026-11-12]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
CHECK = ROOT / "data" / "external" / "ks11_check.csv"
OUT = ROOT / "data" / "results" / "kis"


def read_close(path: Path) -> pd.Series:
    df = pd.read_csv(path, usecols=["Date", "Close"])
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str[:10])      # Yahoo writes +09:00 offsets; KIS plain dates
    return df.set_index("Date")["Close"]


def add_kis(check: pd.DataFrame, kis: pd.Series) -> pd.DataFrame:
    """ks11_check rows with ``kis`` and ``diff_kis_pct`` (vs the KRX column); an earlier KIS column is replaced."""
    out = check.drop(columns=[c for c in ("kis", "diff_kis_pct") if c in check.columns])
    out["kis"] = pd.to_datetime(out["date"]).map(kis)
    out["diff_kis_pct"] = (out["kis"] / out["krx"] - 1) * 100
    return out


def close_diff(yahoo: pd.Series, kis: pd.Series, start: str, end: str) -> dict | None:
    j = pd.concat([yahoo.rename("y"), kis.rename("k")], axis=1, sort=True).loc[start:end].dropna()
    if j.empty:
        return None
    d = (j["k"] / j["y"] - 1).abs() * 100
    return {"days": len(j), "median_abs_pct": float(d.median()), "max_abs_pct": float(d.max()),
            "max_date": d.idxmax().strftime("%Y-%m-%d"), "over_1pct_days": int((d > 1).sum())}


def monthly_gap(pairs: dict[str, tuple[pd.Series, pd.Series]], start: str, end: str) -> pd.DataFrame:
    """Per month: the median over symbols of each symbol's monthly median of KIS / Yahoo − 1 (%), signed."""
    per = []
    for symbol, (yahoo, kis) in pairs.items():
        j = pd.concat([yahoo.rename("y"), kis.rename("k")], axis=1, sort=True).loc[start:end].dropna()
        if not j.empty:
            m = ((j["k"] / j["y"] - 1) * 100).groupby(j.index.to_period("M")).median()
            per.append(m.rename(symbol))
    if not per:
        return pd.DataFrame(columns=["month", "symbols", "median_pct", "kis_higher"])
    t = pd.concat(per, axis=1, sort=True)
    return pd.DataFrame({"month": t.index.astype(str), "symbols": t.notna().sum(axis=1).to_numpy(),
                         "median_pct": t.median(axis=1).to_numpy(), "kis_higher": (t > 0).sum(axis=1).to_numpy()})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default="2026-11-12")
    args = parser.parse_args()

    check = add_kis(pd.read_csv(CHECK), read_close(CACHE / "^KS11-KIS-data.csv"))
    check.to_csv(CHECK, index=False, encoding="utf-8")
    both = check.dropna(subset=["kis", "krx"])
    print(f"^KS11: rows {len(check)}, with KIS {len(both)}, without KIS {check['kis'].isna().sum()}, "
          f"|KIS−KRX| > 1% {int((both['diff_kis_pct'].abs() > 1).sum())}, max {both['diff_kis_pct'].abs().max():.6f}%, "
          f"|yfinance−KRX| > 1% {int((check['diff_pct'].abs() > 1).sum())}")
    window = check[(check["date"] >= "2026-07-20") & (check["date"] <= "2026-08-08")]
    print(window[["date", "yfinance", "krx", "kis", "diff_pct", "diff_kis_pct"]].to_string(index=False))
    for _, r in check[(check["diff_pct"].abs() > 1) | (check["diff_kis_pct"].abs() > 1)].iterrows():
        print(f"  off {r['date']}: yfinance {r['diff_pct']:+.3f}% · KIS {r['diff_kis_pct']:+.3f}%")

    rows, pairs = [], {}
    for k in sorted(CACHE.glob("*-KIS-data.csv")):
        symbol = k.name.removesuffix("-KIS-data.csv")
        y = CACHE / f"{symbol}-YFin-data.csv"
        if symbol.startswith("^") or not y.exists():
            continue
        pairs[symbol] = (read_close(y), read_close(k))
        d = close_diff(*pairs[symbol], args.start, args.end)
        if d:
            rows.append({"symbol": symbol, **d})
    table = pd.DataFrame(rows).sort_values("max_abs_pct", ascending=False)
    OUT.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT / "close_diff.csv", index=False, encoding="utf-8")
    print(f"\nstocks {len(table)}: per-symbol median |diff| — median {table['median_abs_pct'].median():.4f}% · "
          f"max {table['median_abs_pct'].max():.4f}%; per-symbol max |diff| — median {table['max_abs_pct'].median():.4f}% · "
          f"max {table['max_abs_pct'].max():.4f}% ({table.iloc[0]['symbol']} {table.iloc[0]['max_date']}); "
          f"symbols with any day > 1%: {int((table['over_1pct_days'] > 0).sum())}")
    print(table.head(10).to_string(index=False))
    monthly = monthly_gap(pairs, args.start, args.end)
    monthly.to_csv(OUT / "close_diff_monthly.csv", index=False, encoding="utf-8")
    print("\nmonthly median signed diff % (KIS / Yahoo − 1):")
    print(monthly.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
