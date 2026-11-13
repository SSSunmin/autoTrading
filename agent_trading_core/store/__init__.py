"""Where the audit records live (audit.AuditLog): one SQLite database, or the JSON-lines files before it.

``TRADING_STORE`` picks it: ``sqlite`` (default) or ``jsonl``. One store is the store of record at a time; the other
is not written (the JSONL files stay as the loaded-from backup, scripts/export_db_to_jsonl.py writes a new one).
"""

from __future__ import annotations

import os

from agent_trading_core.store.jsonl import JsonlStore
from agent_trading_core.store.sqlite import SqliteStore

STORES = ("sqlite", "jsonl")
DB_NAME = "trading.db"


def store_kind() -> str:
    kind = os.environ.get("TRADING_STORE", "sqlite").strip().lower() or "sqlite"
    if kind not in STORES:
        raise ValueError(f"TRADING_STORE={kind!r}: sqlite 또는 jsonl")
    return kind


__all__ = ["DB_NAME", "STORES", "JsonlStore", "SqliteStore", "store_kind"]
