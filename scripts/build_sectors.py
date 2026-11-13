"""Writes data/universe/sectors.csv (ticker, sector, source, as_of) once, so the sector quota needs no network.

Tickers: each market's fixed forty (scripts/paper_backtest.py) and every name in its point-in-time history
(data/universe/<index>_history.csv). Each is looked up with the Yahoo profile (identity.resolve_instrument_identity,
the lookup the quota used before); a name without a sector is kept with an empty one, so readers do not go back to
the network for it. The sectors are today's, not point-in-time (docs/09).

Run:  .venv/Scripts/python scripts/build_sectors.py [--missing-only]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from paper_backtest import HISTORIES, UNIVERSE, US_UNIVERSE

from agent_trading_core.files import replace_file
from agent_trading_core.identity import resolve_instrument_identity
from agent_trading_core.universe import SECTORS, load_history, tickers

SOURCE = "yahoo_profile"


def all_tickers() -> list[str]:
    out = [*UNIVERSE, *tickers(load_history(HISTORIES["KRX"])), *US_UNIVERSE, *tickers(load_history(HISTORIES["US"]))]
    return list(dict.fromkeys(out))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--missing-only", action="store_true", help="keep the rows already in the file")
    args = parser.parse_args()

    old = pd.read_csv(SECTORS, dtype=str, keep_default_na=False) if SECTORS.exists() else None
    keep = old if (args.missing_only and old is not None) else pd.DataFrame(columns=["ticker", "sector", "source", "as_of"])
    todo = [t for t in all_tickers() if t not in set(keep["ticker"])]
    today = date.today().isoformat()
    rows = []
    for i, t in enumerate(todo, 1):
        rows.append({"ticker": t, "sector": resolve_instrument_identity(t).get("sector") or "", "source": SOURCE,
                     "as_of": today})
        if i % 50 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} (no sector {sum(not r['sector'] for r in rows)})", flush=True)
    out = pd.concat([keep, pd.DataFrame(rows)], ignore_index=True)
    SECTORS.parent.mkdir(parents=True, exist_ok=True)
    replace_file(SECTORS, lambda tmp: out.to_csv(tmp, index=False, encoding="utf-8", lineterminator="\n"))
    missing = out[out["sector"] == ""]["ticker"].tolist()
    print(f"wrote {SECTORS.relative_to(ROOT)}: {len(out)} rows, no sector {len(missing)}"
          + (f" ({', '.join(missing[:30])}{' …' if len(missing) > 30 else ''})" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
