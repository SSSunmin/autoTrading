"""The audit records as JSON-lines files, one per record type under a book's directory (the store before SQLite).

``TRADING_STORE=jsonl`` keeps it the store of record; otherwise the files are what ``scripts/migrate_jsonl_to_db.py``
loads once and ``scripts/export_db_to_jsonl.py`` writes back as a backup.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from agent_trading_core.files import locked


class JsonlStore:
    """``<directory>/<name>.jsonl``; ``legacy``: a directory read first and never written (the KRX files from before
    the market axis)."""

    kind = "jsonl"

    def __init__(self, directory: str | Path, legacy: str | Path | None = None):
        self.directory = Path(directory)
        self.legacy = Path(legacy) if legacy else None

    def append(self, name: str, record: BaseModel, market: str) -> None:
        path = self.directory / f"{name}.jsonl"
        with locked(path), open(path, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def read(self, name: str, market: str | None) -> list[str]:
        """The non-blank lines of the legacy file, then the book's own, in file order (``market``: the directory is
        the book)."""
        lines = []
        for d in ([self.legacy] if self.legacy else []) + [self.directory]:
            path = d / f"{name}.jsonl"
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    lines += [line for line in f if line.strip()]
        return lines
