"""The audit records in one SQLite database (schema.sql), the store of record unless ``TRADING_STORE=jsonl``.

Append-only: rows are inserted, never updated or deleted — the triggers refuse both, so no code path here or
elsewhere can change a record in place. Each append is one transaction (``BEGIN IMMEDIATE``: writers queue on the
database lock, ``busy_timeout`` long enough for the scheduler and the API writing at once). Reads return the
payloads in insert order, which is the order the JSON lines were written and loaded.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from pydantic import BaseModel

SCHEMA_VERSION = 2                 # 2: source_hash NULL for appended records (only loaded lines carry one)
BUSY_TIMEOUT_MS = 30_000
# ponytail: read beside this file; a wheel needs schema.sql as package data (pyproject [tool.setuptools.package-data])
SCHEMA = Path(__file__).with_name("schema.sql")

# record name (the JSONL file stem, audit.AuditLog) -> table
TABLES = {"screens": "screen_runs", "decisions": "decisions", "orders": "orders", "operations": "operations",
          "runs": "run_records"}


def _columns(name: str, r) -> dict:
    """The query columns unpacked from a record: its time as ``recorded_at``, then the indexed fields."""
    if name == "screens":
        return {"recorded_at": r.recorded_at, "run_id": r.run_id, "as_of": r.as_of}
    if name == "decisions":
        return {"recorded_at": r.recorded_at, "ticker": r.ticker, "trade_date": r.trade_date, "rating": r.rating,
                "source": r.source, "screen_run_id": r.screen_run_id}
    if name == "orders":
        return {"recorded_at": r.placed_at, "order_id": r.order_id, "ticker": r.ticker, "trade_date": r.trade_date,
                "side": r.side, "status": r.status, "fill_date": r.fill_date}
    if name == "operations":
        return {"recorded_at": r.at, "kind": r.kind, "target": r.target, "operator": r.operator}
    if name == "runs":
        return {"recorded_at": r.ended_at, "stage": r.stage, "trade_date": r.trade_date, "status": r.status}
    raise KeyError(name)


_ready: set[str] = set()
_ready_lock = threading.Lock()


def connect(path: str | Path) -> sqlite3.Connection:
    """A connection in autocommit mode (transactions are explicit), WAL, waiting ``BUSY_TIMEOUT_MS`` on a locked
    database. The schema is applied once per process and path (every statement is IF NOT EXISTS)."""
    path = Path(path)
    key = str(path.resolve())
    if not path.exists():
        _ready.discard(key)                            # a database removed and made again gets its schema again
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA recursive_triggers = ON")      # INSERT OR REPLACE deletes: the delete trigger refuses it too
    with _ready_lock:
        if key not in _ready:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA.read_text(encoding="utf-8"))
            con.execute("INSERT INTO schema_version (id, version, created_at) SELECT 1, ?, datetime('now') "
                        "WHERE NOT EXISTS (SELECT 1 FROM schema_version)", (SCHEMA_VERSION,))
            got = con.execute("SELECT version FROM schema_version").fetchone()[0]
            if got != SCHEMA_VERSION:
                con.close()
                raise RuntimeError(f"{path}: schema_version {got}, this code reads {SCHEMA_VERSION}")
            _ready.add(key)
    return con


class SqliteStore:
    """One database for every market; ``market`` is a column."""

    kind = "sqlite"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, name: str, record: BaseModel, market: str) -> None:
        """Insert one record (a screen with its candidates) in one transaction, its payload the record's JSON (what
        the JSONL store writes). Always a new row, as a new line was: two events can be the same bytes (times are to
        the second), and a rerun is kept from writing twice by the records' own keys, not here."""
        self.load([(name, [(record, market, record.model_dump_json(), None)])])

    def load(self, groups: list[tuple[str, list[tuple[BaseModel, str, str, str | None]]]]) -> list[int]:
        """Insert ``[(name, [(record, market, payload, source_hash), ...]), ...]`` in order in ONE transaction — every
        group or none. The payload is stored and read back as given. ``source_hash``: a loaded line's key
        (scripts/migrate_jsonl_to_db.py) — a row whose key is already there is not inserted again; None (a new
        record) is never a duplicate. Returns how many rows each group got."""
        con = connect(self.path)
        inserted = []
        try:
            con.execute("BEGIN IMMEDIATE")
            for name, items in groups:
                inserted.append(sum(_insert(con, name, *item) for item in items))
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return inserted

    def read(self, name: str, market: str | None) -> list[str]:
        """The payloads in insert order; ``market`` None: every market's."""
        con = connect(self.path)
        try:
            if market is None:
                rows = con.execute(f"SELECT payload FROM {TABLES[name]} ORDER BY id")
            else:
                rows = con.execute(f"SELECT payload FROM {TABLES[name]} WHERE market = ? ORDER BY id", (market,))
            return [p for (p,) in rows]
        finally:
            con.close()


def _insert(con: sqlite3.Connection, name: str, record: BaseModel, market: str, payload: str, hash_: str | None) -> int:
    """One row (and a screen's candidates) inside the caller's transaction; 0 when ``hash_`` is already there."""
    cols = {"market": market, **_columns(name, record), "payload": payload, "source_hash": hash_}
    cur = con.execute(f"INSERT INTO {TABLES[name]} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                      "ON CONFLICT (source_hash) DO NOTHING", tuple(cols.values()))
    if cur.rowcount != 1:
        return 0
    if name == "screens":
        con.executemany(
            "INSERT INTO screen_candidates (screen_id, market, recorded_at, payload, ticker, selected, rank, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(cur.lastrowid, market, cols["recorded_at"], c.model_dump_json(), c.ticker, c.selected, c.rank, c.score)
             for c in record.candidates])
    return 1
