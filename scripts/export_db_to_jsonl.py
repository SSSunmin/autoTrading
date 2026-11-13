"""Write the SQLite audit records back out as JSON-lines files: the backup (database → files, one way).

Run:  .venv/Scripts/python scripts/export_db_to_jsonl.py                    # data/backup/jsonl-<UTC time>/
      .venv/Scripts/python scripts/export_db_to_jsonl.py --out D:/backup/audit
Writes ``<out>/<market>/<kind>.jsonl`` — the layout of data/audit/, so ``<out>`` can be read as an audit root with
``TRADING_STORE=jsonl`` — each row's payload as one line, in ``id`` order: the lines the records were loaded from or
appended as, byte for byte (a KRX book loaded from the files before the market axis comes out as one KRX file, the
old lines first). Never writes into an existing directory, never touches data/audit/. TRADING_DATA_DIR overrides
data/.

Exit codes: 0 done, 2 the output exists or the database is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.store import DB_NAME
from agent_trading_core.store.sqlite import TABLES, connect


def export(db: Path, out: Path) -> dict[tuple[str, str], int]:
    """Lines written per (kind, market)."""
    out.mkdir(parents=True)
    counts = {}
    con = connect(db)
    try:
        for name, table in TABLES.items():
            for (market,) in con.execute(f"SELECT DISTINCT market FROM {table} ORDER BY market").fetchall():
                (out / market).mkdir(exist_ok=True)
                rows = con.execute(f"SELECT payload FROM {table} WHERE market = ? ORDER BY id", (market,))
                n = 0
                with open(out / market / f"{name}.jsonl", "w", encoding="utf-8") as f:   # as the JSONL store writes
                    for (payload,) in rows:
                        f.write(payload + "\n")
                        n += 1
                counts[name, market] = n
    finally:
        con.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    parser.add_argument("--out", help="a directory that does not exist yet (default data/backup/jsonl-<UTC time>)")
    args = parser.parse_args(argv)
    data = Path(args.data_dir)
    db = data / DB_NAME
    out = Path(args.out) if args.out else data / "backup" / f"jsonl-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    if not db.exists():
        print(f"{db}가 없습니다", file=sys.stderr)
        return 2
    if out.exists():
        print(f"{out}가 이미 있습니다 — 덮어쓰지 않습니다", file=sys.stderr)
        return 2
    counts = export(db, out)
    print(f"내보냄: {db} → {out}")
    for (name, market), n in counts.items():
        print(f"{market}/{name}.jsonl · {n}줄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
