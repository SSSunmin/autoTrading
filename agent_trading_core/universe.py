"""Point-in-time index membership: who was in the index on a given day (docs/09).

A history is rows of (date, ticker, action) with action ``start`` (the members on the first day), ``add`` or
``remove``. A change dated D is in effect from D on (S&P effective dates, KRX snapshot dates), so a rebalance on
D sees it and one on D−1 does not. Rows on the same day apply in file order. ``scripts/build_universe.py`` writes
the histories from the sources in data/external/.

Sectors live in data/universe/sectors.csv (ticker, sector, source, as_of), written once by scripts/build_sectors.py
from the Yahoo profile. They are today's sectors, not point-in-time ones (docs/09). Reading it needs no network,
so the paper executor can apply the sector quota from the file alone.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pandas as pd

SECTORS = Path(__file__).resolve().parents[1] / "data" / "universe" / "sectors.csv"


def load_history(path) -> pd.DataFrame:
    h = pd.read_csv(path, dtype={"ticker": str})
    h["date"] = pd.to_datetime(h["date"])
    bad = set(h["action"]) - {"start", "add", "remove"}
    if bad:
        raise ValueError(f"알 수 없는 action: {sorted(bad)}")
    # a hand-made file may list dates out of order; same-day rows keep their file order
    return h.sort_values("date", kind="stable").reset_index(drop=True)


def members_on(history: pd.DataFrame, date) -> set[str]:
    """Members on ``date``: the start rows, then every add/remove dated on or before it, in order."""
    d = pd.Timestamp(date)
    members: set[str] = set()
    for row in history[history["date"] <= d].itertuples(index=False):
        if row.action == "remove":
            members.discard(row.ticker)
        else:
            members.add(row.ticker)
    return members


def tickers(history: pd.DataFrame) -> list[str]:
    """Every ticker that is a member at any point, in first-seen order."""
    return list(dict.fromkeys(history["ticker"]))


@cache
def load_sectors(path: Path = SECTORS) -> dict[str, str | None]:
    """Ticker → sector from the sector file; a listed ticker without one maps to None, a missing file to {}.
    Read once per process."""
    if not Path(path).exists():
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {t: (s or None) for t, s in zip(df["ticker"], df["sector"], strict=True)}


def sector_of(ticker: str, path: Path = SECTORS) -> str | None:
    """The ticker's sector from the file only (no network); None when it is not listed or has none."""
    return load_sectors(path).get(ticker)
