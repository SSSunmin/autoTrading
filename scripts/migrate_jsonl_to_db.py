"""Load the JSON-lines audit files into the SQLite database once (agent_trading_core/store/), then compare the replays.

Run:  .venv/Scripts/python scripts/migrate_jsonl_to_db.py              # load data/audit/** into data/trading.db
      .venv/Scripts/python scripts/migrate_jsonl_to_db.py --verify     # load, then replay both stores and compare
      .venv/Scripts/python scripts/migrate_jsonl_to_db.py --verify-only
Reads, per record kind, the files in the order the JSONL store reads them: KRX's from before the market axis
(``data/audit/*.jsonl``, KRX), then ``data/audit/<market>/`` for each market of config.MARKETS, each file in line
order — that order is the rows' ``id`` order, so a market's replay reads them as it read the lines. A line is stored
as written (``payload``): the export gives the same bytes back. The files are left as they are (the backup).

Idempotent: a line's ``source_hash`` (records appended by the code have none) is the hash of its text, the k-th repeat of the same text in a kind's files
the hash of text and k — loading again inserts nothing, and two equal lines stay two rows. A line that is not JSON,
not a valid record, or another market's record in a market's file is rejected and then nothing is loaded (exit 4):
the store must not start from a book the files do not replay to.

``--verify``: per market, the records and their replay — ``latest_orders``, ``trading_state``, ``positions_view``,
``paper_progress`` (OHLCV cache data/cache) — dumped to JSON from each store and compared byte for byte (exit 5 on a
difference). TRADING_DATA_DIR overrides data/.

Stop the scheduler and the backend first: a book written while lines are loading gets ids before the older lines.

Exit codes: 0 done, 2 ``--verify-only`` with no database, 4 rejected lines (nothing loaded), 5 the replays differ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_trading_core.audit import (
    RECORD_NAMES,
    DecisionRecord,
    OperationRecord,
    OrderRecord,
    RunRecord,
    ScreenRun,
    latest_orders,
    market_audit,
    trading_state,
)
from agent_trading_core.config import MARKETS
from agent_trading_core.execution import Prices, paper_config, paper_progress, positions_view
from agent_trading_core.store import DB_NAME, SqliteStore
from agent_trading_core.store.sqlite import TABLES, connect

MODELS = {"screens": ScreenRun, "decisions": DecisionRecord, "orders": OrderRecord, "operations": OperationRecord,
          "runs": RunRecord}


def sources(root: Path) -> list[tuple[Path, str]]:
    """(directory, market) in the order the JSONL store reads a market: the KRX files from before the market axis
    first, then each market's own."""
    return [(root, "KRX")] + [(root / m, m) for m in MARKETS]


def lines_of(path: Path) -> list[tuple[int, str]]:
    """(line number, text without its newline) of the non-blank lines."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [(i, line.rstrip("\r\n")) for i, line in enumerate(f, 1) if line.strip()]


def plan(root: Path) -> tuple[dict, list[str]]:
    """Every row to insert per kind, in order, as ``(record, market, payload, source_hash, where)``; and the
    rejected lines."""
    rows: dict[str, list] = {n: [] for n in RECORD_NAMES}
    rejected = []
    for name in RECORD_NAMES:
        seen: Counter = Counter()
        for d, market in sources(root):
            path = d / f"{name}.jsonl"
            for i, text in lines_of(path):
                where = f"{path}:{i}"
                try:
                    record = MODELS[name].model_validate(json.loads(text))
                except ValueError as exc:                       # json / pydantic
                    rejected.append(f"{where} — {type(exc).__name__}: {str(exc).splitlines()[0]}")
                    continue
                got = getattr(record, "market", None)
                if got is not None and got != market:
                    rejected.append(f"{where} — {got} 기록이 {market} 장부 파일에 있음")
                    continue
                k = seen[text]
                seen[text] += 1
                key = hashlib.sha256((text if k == 0 else f"{text}#{k}").encode("utf-8")).hexdigest()
                rows[name].append((record, market, text, key, where))
    return rows, rejected


def load(root: Path, db: Path) -> tuple[dict, list[str]]:
    """Insert the planned rows in one transaction; per (kind, market): lines read and rows inserted. Nothing when a
    line is rejected. A first load fills ``<db>.loading`` and only then renames it to ``db``: a database that exists
    is a complete load, so the books' guard (audit._unloaded) holds until then and an interrupted load leaves none."""
    rows, rejected = plan(root)
    if not rejected and db.exists():
        rejected = appended_again(rows, db)
    if rejected:
        return {}, rejected
    groups, keys = [], []
    for name, items in rows.items():
        for market in dict.fromkeys(m for _, m, *_ in items):
            groups.append((name, [(r, m, p, h) for r, m, p, h, _ in items if m == market]))
            keys.append((name, market))
    first = not db.exists()
    target = db.with_name(db.name + ".loading") if first else db
    if first:
        for leftover in (target, *(target.with_name(target.name + s) for s in ("-wal", "-shm"))):
            leftover.unlink(missing_ok=True)                  # an earlier load that was interrupted
    inserted = SqliteStore(target).load(groups)
    if first:
        con = connect(target)
        try:
            con.execute("PRAGMA journal_mode = DELETE")         # the WAL folded into the file before it is renamed
        finally:
            con.close()
        os.replace(target, db)
    return {k: (len(g[1]), n) for k, g, n in zip(keys, groups, inserted, strict=True)}, rejected


def appended_again(rows: dict, db: Path) -> list[str]:
    """Lines that are a record the code appended to ``db`` (no ``source_hash``): an export read back as JSON lines.
    Loading them would add every such record a second time, for good — move the database aside and load from scratch
    instead."""
    con = connect(db)
    try:
        found = []
        for name, items in rows.items():
            appended = set(con.execute(f"SELECT market, payload FROM {TABLES[name]} WHERE source_hash IS NULL"))
            found += [f"{where} — 이미 DB에 있는 기록(코드가 쓴 행, export한 줄) — DB를 옮기고 처음부터 적재"
                      for _, m, p, _, where in items if (m, p) in appended]
        return found
    finally:
        con.close()


def table_counts(db: Path) -> list[tuple[str, str, int]]:
    con = connect(db)
    try:
        out = []
        for table in [*TABLES.values(), "screen_candidates"]:
            out += [(table, m, n) for m, n in
                    con.execute(f"SELECT market, count(*) FROM {table} GROUP BY market ORDER BY market")]
        return out
    finally:
        con.close()


@contextmanager
def store_env(kind: str):
    before = os.environ.get("TRADING_STORE")
    os.environ["TRADING_STORE"] = kind
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TRADING_STORE", None)
        else:
            os.environ["TRADING_STORE"] = before


def _try(fn):
    try:
        return fn()
    except Exception as exc:                    # the same failure from both stores is the same replay
        return f"{type(exc).__name__}: {exc}"


def replay_dump(root: Path, market: str, kind: str, prices: Prices) -> bytes:
    """A market's records and what is replayed from them, read through one store, as canonical JSON."""
    with store_env(kind):
        audit = market_audit(root, market)
    records = {n: [r.model_dump(mode="json") for r in getattr(audit, n)()] for n in RECORD_NAMES}
    orders = [OrderRecord.model_validate(o) for o in records["orders"]]
    decisions = [DecisionRecord.model_validate(d) for d in records["decisions"]]
    ops = [OperationRecord.model_validate(o) for o in records["operations"]]
    cfg = paper_config(market)
    out = {
        "records": records,
        "latest_orders": {k: o.model_dump(mode="json") for k, o in latest_orders(orders).items()},
        "trading_state": trading_state(ops),
        "positions_view": _try(lambda: positions_view(orders, prices, cfg=cfg, operations=ops)),
        "paper_progress": _try(lambda: paper_progress(orders, decisions, ops, prices, cfg)),
    }
    return json.dumps(out, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def verify(data: Path) -> bool:
    prices = Prices(data / "cache")
    same = True
    print("시장 · jsonl 바이트 · sqlite 바이트 · sha256(jsonl) · sha256(sqlite) · 동일")
    for market in MARKETS:
        a, b = (replay_dump(data / "audit", market, k, prices) for k in ("jsonl", "sqlite"))
        ha, hb = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
        print(f"{market} · {len(a)} · {len(b)} · {ha[:16]} · {hb[:16]} · {'예' if a == b else '아니오'}")
        same &= a == b
    return same


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR") or str(ROOT / "data"))
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--verify", action="store_true", help="load, then compare the two stores' replays")
    which.add_argument("--verify-only", action="store_true", help="compare without loading")
    args = parser.parse_args(argv)
    data = Path(args.data_dir)
    db = data / DB_NAME

    if args.verify_only and not db.exists():
        print(f"{db}가 없습니다 — 먼저 적재(--verify)", file=sys.stderr)
        return 2
    if not args.verify_only:
        counts, rejected = load(data / "audit", db)
        if rejected:
            print(f"거부 {len(rejected)}줄 — 적재하지 않음(파일을 고친 뒤 다시):", file=sys.stderr)
            for r in rejected:
                print(f"  {r}", file=sys.stderr)
            return 4
        print(f"적재: {db}")
        print("종류 · 시장 · 읽은 줄 · 새로 넣은 행 · 이미 있던 행")
        for (name, market), (read, new) in counts.items():
            print(f"{name} · {market} · {read} · {new} · {read - new}")
        print("거부 0줄")
        print("테이블 · 시장 · 행 수")
        for table, market, n in table_counts(db):
            print(f"{table} · {market} · {n}")
    if args.verify or args.verify_only:
        if not verify(data):
            print("재생 결과가 다릅니다", file=sys.stderr)
            return 5
        print("재생 동일: 모든 시장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
