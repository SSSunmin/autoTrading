"""Container start for the API (Dockerfile CMD): load the JSON-lines records into the database on the first start only,
then exec uvicorn.

`migrate_jsonl_to_db.py --verify` compares the JSON-lines replay with the database replay. Once the database is the
store of record, the loop's and the API's writes go to the database only and the JSON-lines files stay as the load-time
backup, so the comparison differs after the first day of running. Running it on every start would keep the API down
(exit 5, restart loop, the scheduler waiting for a healthy API). trading.db exists only after a load finished (the load
renames trading.db.loading at the end), so its absence is the "not loaded yet" signal; with it present nothing is loaded
and the scheduler may be writing safely beside the API.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agent_trading_core.store import DB_NAME

ROOT = Path(__file__).resolve().parents[1]
UVICORN = ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]


def needs_load(data: Path) -> bool:
    return not (data / DB_NAME).exists()


def main() -> int:
    data = Path(os.environ.get("TRADING_DATA_DIR", ROOT / "data"))
    if os.environ.get("TRADING_STORE", "sqlite") == "sqlite" and needs_load(data):
        code = subprocess.run([sys.executable, str(ROOT / "scripts" / "migrate_jsonl_to_db.py"), "--verify"], check=False).returncode
        if code:
            return code
    os.execvp(UVICORN[0], UVICORN)
    return 0  # unreachable: execvp replaces the process


if __name__ == "__main__":
    sys.exit(main())
