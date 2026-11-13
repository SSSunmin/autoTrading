"""The SQLite store of record (agent_trading_core/store/): append-only enforced by the database, one row per append,
the JSON-lines files loaded once in their read order and written back byte for byte, both stores replaying a book
to the same state, two writers at once losing nothing, and TRADING_STORE choosing the store."""

import json
import sqlite3
import threading

import pytest

from agent_trading_core.audit import (
    AuditLog,
    CandidateRecord,
    DecisionRecord,
    OperationRecord,
    OrderRecord,
    RunRecord,
    ScreenRun,
    market_audit,
    trading_state,
)
from agent_trading_core.execution import PaperConfig, execute_date
from agent_trading_core.store import JsonlStore, SqliteStore
from agent_trading_core.store.sqlite import TABLES, connect
from scripts import export_db_to_jsonl as export
from scripts import migrate_jsonl_to_db as migrate
from tests.test_execution import DAYS, RISK, _decide, _flat, _frame, _prices

ALL_TABLES = [*TABLES.values(), "screen_candidates", "schema_version"]


@pytest.fixture
def sqlite_store(monkeypatch):
    monkeypatch.setenv("TRADING_STORE", "sqlite")


def _order(oid="o1", status="open", market="KRX", **kw):
    return OrderRecord(order_id=oid, ticker="005930.KS", trade_date="2026-06-01", side="Buy", quantity=1,
                       status=status, market=market, **kw)


def _screen(run_id="r1"):
    return ScreenRun(run_id=run_id, as_of="2026-06-01", universe="t", universe_size=2, rules=["price_min"],
                     candidates=[CandidateRecord(ticker="A.KS", selected=True, rank=1, score=1.0),
                                 CandidateRecord(ticker="B.KS", selected=False)])


# --- the database refuses to change a record -------------------------------------------------------------------

@pytest.mark.unit
def test_every_table_refuses_update_delete_and_replace(sqlite_store, tmp_path):
    log = market_audit(tmp_path / "audit", "KRX")
    log.record_screen(_screen())
    log.record_decision(DecisionRecord(ticker="A.KS", trade_date="2026-06-01", rating="Buy", thesis="t"))
    log.record_order(_order())
    log.record_operation(OperationRecord(kind="halt", operator="x", role="admin", reason="r"))
    log.record_run(RunRecord(market="KRX", stage="decide", trade_date="2026-06-01", started_at="t", status="ok"))
    con = connect(tmp_path / "trading.db")
    try:
        for table in ALL_TABLES:
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= 1, table
            for sql in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    con.execute(sql)
            before = con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            try:                                     # a replace is a delete: refused, or (same hash) does nothing
                con.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
            except sqlite3.IntegrityError as exc:
                assert "append-only" in str(exc)
            assert con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == before, table
        assert con.execute("SELECT status FROM orders").fetchall() == [("open",)]   # nothing changed
    finally:
        con.close()
    # any other client (recursive_triggers off): a replace onto an id is refused, onto a loaded line's source_hash
    # does nothing
    SqliteStore(tmp_path / "trading.db").load([("orders", [(_order("loaded"), "KRX", _order("loaded").model_dump_json(),
                                                              "h1")])])
    plain = sqlite3.connect(tmp_path / "trading.db")
    try:
        cols = "market, recorded_at, payload, source_hash, order_id, ticker, trade_date, side, status"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            plain.execute(f"INSERT OR REPLACE INTO orders (id, {cols}) SELECT id, market, recorded_at, '{{}}', 'new', "
                          "order_id, ticker, trade_date, side, 'filled' FROM orders")
        plain.execute(f"INSERT OR REPLACE INTO orders ({cols}) SELECT market, recorded_at, '{{}}', source_hash, "
                      "order_id, ticker, trade_date, side, 'filled' FROM orders WHERE source_hash = 'h1'")
        plain.commit()
        assert plain.execute("SELECT id, order_id, status FROM orders").fetchall() == [(1, "o1", "open"),
                                                                                     (2, "loaded", "open")]
    finally:
        plain.close()


@pytest.mark.unit
def test_a_state_change_is_a_new_row_and_every_append_is_a_row(sqlite_store, tmp_path):
    log = market_audit(tmp_path / "audit", "KRX")
    o = _order()
    log.record_order(o)
    log.record_order(o.model_copy(update={"status": "filled", "filled_quantity": 1, "filled_price": 1.0}))
    log.record_order(o)                                              # the same bytes again: a second event, a row
    assert [x.status for x in log.orders()] == ["open", "filled", "open"]
    con = connect(tmp_path / "trading.db")
    try:
        rows = con.execute("SELECT order_id, status, source_hash FROM orders ORDER BY id").fetchall()
    finally:
        con.close()
    assert rows == [("o1", "open", None), ("o1", "filled", None), ("o1", "open", None)]   # only loaded lines have keys


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["jsonl", "sqlite"])
def test_halt_resume_halt_in_one_second_stays_halted_in_either_store(kind, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", kind)
    log = market_audit(tmp_path / "audit", "KRX")
    at = "2026-06-01T00:00:00Z"                                      # one second, one operator, one reason
    halt = OperationRecord(kind="halt", at=at, operator="x", role="admin", reason="r", target="manual",
                           before={"running": True}, after={"running": False, "open_orders_cancelled": []})
    log.record_operation(halt)
    log.record_operation(OperationRecord(kind="resume", at=at, operator="x", role="admin", reason="r"))
    log.record_operation(halt)                                       # byte for byte the first halt
    assert [op.kind for op in log.operations()] == ["halt", "resume", "halt"]
    assert trading_state(log.operations())["running"] is False


@pytest.mark.unit
def test_a_screen_and_its_candidates_are_one_transaction_with_the_candidates_unpacked(sqlite_store, tmp_path):
    log = market_audit(tmp_path / "audit", "US")
    screen = _screen()
    log.record_screen(screen)
    con = connect(tmp_path / "trading.db")
    try:
        (sid,) = con.execute("SELECT id FROM screen_runs").fetchone()
        rows = con.execute("SELECT screen_id, market, ticker, selected, rank, score FROM screen_candidates ORDER BY id")
        assert rows.fetchall() == [(sid, "US", "A.KS", 1, 1, 1.0), (sid, "US", "B.KS", 0, None, None)]
    finally:
        con.close()
    assert log.screens() == [screen]
    with pytest.raises(sqlite3.IntegrityError):                      # a candidate without its screen: refused (FK)
        con = connect(tmp_path / "trading.db")
        try:
            con.execute("INSERT INTO screen_candidates (screen_id, market, recorded_at, payload, ticker, selected) "
                        "VALUES (99, 'US', 't', '{}', 'X', 0)")
        finally:
            con.close()


@pytest.mark.unit
def test_one_database_keeps_the_books_apart_by_market(sqlite_store, tmp_path):
    krx, us = market_audit(tmp_path / "audit", "KRX"), market_audit(tmp_path / "audit", "US")
    assert krx.store.path == us.store.path == tmp_path / "trading.db"
    krx.record_order(_order("k"))
    us.record_order(_order("u", market="US"))
    with pytest.raises(ValueError, match="KRX 기록이 US 장부"):
        us.record_order(_order("x"))                                 # market left at its KRX default
    assert [o.order_id for o in krx.orders()] == ["k"] and [o.order_id for o in us.orders()] == ["u"]
    assert [o.order_id for o in AuditLog(tmp_path / "audit").orders()] == ["k", "u"]   # no market: every row


# --- TRADING_STORE ------------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_trading_store_picks_the_store_and_a_typo_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_STORE", raising=False)
    assert isinstance(market_audit(tmp_path / "a" / "audit", "KRX").store, SqliteStore)    # the default
    monkeypatch.setenv("TRADING_STORE", "jsonl")
    log = market_audit(tmp_path / "b" / "audit", "KRX")
    assert isinstance(log.store, JsonlStore)
    log.record_order(_order())
    assert (tmp_path / "b" / "audit" / "KRX" / "orders.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert not (tmp_path / "b" / "trading.db").exists()
    monkeypatch.setenv("TRADING_STORE", "postgres")
    with pytest.raises(ValueError, match="TRADING_STORE"):
        market_audit(tmp_path / "c" / "audit", "KRX")


@pytest.mark.unit
def test_a_book_with_unloaded_files_and_no_database_refuses_to_start_empty(sqlite_store, tmp_path):
    root = tmp_path / "audit"
    (root / "US").mkdir(parents=True)
    (root / "US" / "orders.jsonl").write_text(_order(market="US").model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="migrate_jsonl_to_db"):
        market_audit(root, "US")
    with pytest.raises(RuntimeError, match="migrate_jsonl_to_db"):
        market_audit(root, "KRX").orders()           # a book with no files of its own: reading it would make the DB
    assert not (tmp_path / "trading.db").exists()
    assert migrate.main(["--data-dir", str(tmp_path)]) == 0
    assert [o.order_id for o in market_audit(root, "US").orders()] == ["o1"]


@pytest.mark.unit
def test_a_sqlite_append_touches_the_books_file_for_the_backends_reload_and_leaves_it_empty(sqlite_store, tmp_path):
    log = market_audit(tmp_path / "audit", "KRX")
    log.record_order(_order())
    path = tmp_path / "audit" / "KRX" / "orders.jsonl"
    before = path.stat().st_mtime_ns
    log.record_order(_order(status="cancelled"))
    assert path.read_bytes() == b"" and path.stat().st_mtime_ns >= before


# --- load once, export back ---------------------------------------------------------------------------------------

def _legacy_book(root):
    """Files as the JSONL store left them: KRX's from before the market axis (no market field), KRX's and US's own,
    and one line written twice."""
    root.mkdir(parents=True)
    (root / "KRX").mkdir()
    (root / "US").mkdir()
    old = json.dumps({"order_id": "old", "ticker": "005930.KS", "trade_date": "2026-05-29", "side": "Buy",
                      "quantity": 3, "status": "open"})
    (root / "orders.jsonl").write_text(old + "\n", encoding="utf-8")
    twice = _order("k", status="filled", filled_quantity=1, filled_price=1.0).model_dump_json()
    (root / "KRX" / "orders.jsonl").write_text(f"{twice}\n\n{twice}\n", encoding="utf-8")    # a blank line too
    (root / "US" / "orders.jsonl").write_text(_order("u", market="US").model_dump_json() + "\n", encoding="utf-8")
    (root / "KRX" / "screens.jsonl").write_text(_screen().model_dump_json() + "\n", encoding="utf-8")


@pytest.mark.unit
def test_loading_keeps_the_read_order_and_repeated_lines_and_loading_again_adds_nothing(tmp_path, capsys):
    _legacy_book(tmp_path / "audit")
    before = {p: p.read_bytes() for p in (tmp_path / "audit").rglob("*.jsonl")}
    assert migrate.main(["--data-dir", str(tmp_path), "--verify"]) == 0
    out = capsys.readouterr().out
    assert "orders · KRX · 3 · 3 · 0" in out and "orders · US · 1 · 1 · 0" in out and "거부 0줄" in out
    assert "KRX ·" in out and "재생 동일: 모든 시장" in out
    assert migrate.main(["--data-dir", str(tmp_path)]) == 0
    assert "orders · KRX · 3 · 0 · 3" in capsys.readouterr().out                # nothing new
    con = connect(tmp_path / "trading.db")
    try:
        rows = con.execute("SELECT market, order_id FROM orders ORDER BY id").fetchall()
    finally:
        con.close()
    assert rows == [("KRX", "old"), ("KRX", "k"), ("KRX", "k"), ("US", "u")]
    assert {p: p.read_bytes() for p in (tmp_path / "audit").rglob("*.jsonl")} == before     # files untouched


@pytest.mark.unit
def test_a_first_load_appears_whole_or_not_at_all_and_verify_only_needs_a_database(sqlite_store, tmp_path, monkeypatch,
                                                                                      capsys):
    _legacy_book(tmp_path / "audit")
    assert migrate.main(["--data-dir", str(tmp_path), "--verify-only"]) == 2
    assert "먼저 적재" in capsys.readouterr().err
    real = migrate.SqliteStore.load

    def dies(self, groups):                                          # a load cut short after its first rows
        real(self, groups[:1])
        raise KeyboardInterrupt

    monkeypatch.setattr(migrate.SqliteStore, "load", dies)
    with pytest.raises(KeyboardInterrupt):
        migrate.main(["--data-dir", str(tmp_path)])
    assert not (tmp_path / "trading.db").exists()                   # the books' guard still holds
    with pytest.raises(RuntimeError, match="migrate_jsonl_to_db"):
        market_audit(tmp_path / "audit", "KRX")
    monkeypatch.setattr(migrate.SqliteStore, "load", real)
    assert migrate.main(["--data-dir", str(tmp_path)]) == 0         # the leftover .loading file is started over
    assert "orders · KRX · 3 · 3 · 0" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.glob("trading.db*")) == ["trading.db"]


@pytest.mark.unit
def test_a_rejected_line_loads_nothing(tmp_path, capsys):
    _legacy_book(tmp_path / "audit")
    with open(tmp_path / "audit" / "US" / "orders.jsonl", "a", encoding="utf-8") as f:
        f.write(_order("wrong").model_dump_json() + "\n")             # a KRX order in the US file
        f.write('{"order_id": "cut')                                  # a write cut short
    assert migrate.main(["--data-dir", str(tmp_path)]) == 4
    assert not (tmp_path / "trading.db").exists()
    err = capsys.readouterr().err
    assert "거부 2줄" in err and "orders.jsonl:2 — KRX 기록이 US 장부" in err and "orders.jsonl:3 — JSONDecodeError" in err


@pytest.mark.unit
def test_the_export_gives_the_loaded_lines_back_byte_for_byte_and_never_overwrites(tmp_path, capsys):
    _legacy_book(tmp_path / "audit")
    assert migrate.main(["--data-dir", str(tmp_path)]) == 0
    out = tmp_path / "backup"
    assert export.main(["--data-dir", str(tmp_path), "--out", str(out)]) == 0
    audit = tmp_path / "audit"
    # KRX: the files from before the market axis, then KRX's own, as one file (blank lines are not records)
    krx = [line for p in (audit / "orders.jsonl", audit / "KRX" / "orders.jsonl")
           for line in p.read_bytes().splitlines(keepends=True) if line.strip()]
    assert (out / "KRX" / "orders.jsonl").read_bytes() == b"".join(krx)
    assert (out / "US" / "orders.jsonl").read_bytes() == (audit / "US" / "orders.jsonl").read_bytes()
    assert (out / "KRX" / "screens.jsonl").read_bytes() == (audit / "KRX" / "screens.jsonl").read_bytes()
    assert export.main(["--data-dir", str(tmp_path), "--out", str(out)]) == 2               # exists: refused


# --- both stores replay a book to the same state ------------------------------------------------------------------

def _scenario(root):
    """A paper book through fills, a sell, a halt and a resume: what the backend replays."""
    log = market_audit(root, "KRX")
    prices = _prices(A_KS=_flat(), B_KS=_frame({d: (10_000, 10_000 + 100 * i) for i, d in enumerate(DAYS)}))
    _decide(log, "A.KS", "2026-06-01")
    _decide(log, "B.KS", "2026-06-01")
    execute_date(log, "2026-06-01", prices, risk=RISK, cfg=PaperConfig(initial_cash=5e7))
    execute_date(log, "2026-06-02", prices, risk=RISK, cfg=PaperConfig(initial_cash=5e7))
    _decide(log, "A.KS", "2026-06-04", rating="Sell")
    execute_date(log, "2026-06-04", prices, risk=RISK, cfg=PaperConfig(initial_cash=5e7))
    log.record_operation(OperationRecord(kind="halt", operator="x", role="admin", reason="점검", target="manual"))
    log.record_operation(OperationRecord(kind="resume", operator="x", role="admin", reason="재개"))
    return prices


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["jsonl", "sqlite"])
def test_a_book_written_through_either_store_replays_byte_identical_after_loading(kind, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", kind)
    prices = _scenario(tmp_path / "audit")
    if kind == "jsonl":
        assert migrate.main(["--data-dir", str(tmp_path)]) == 0
    else:                                                    # the other way: the export read as JSON lines
        assert export.main(["--data-dir", str(tmp_path), "--out", str(tmp_path / "export")]) == 0
    jsonl_root = tmp_path / ("audit" if kind == "jsonl" else "export")
    a = migrate.replay_dump(jsonl_root, "KRX", "jsonl", prices)
    b = migrate.replay_dump(tmp_path / "audit", "KRX", "sqlite", prices)
    assert a == b
    dump = json.loads(b)
    assert [o["status"] for o in dump["latest_orders"].values()].count("filled") == 3 and dump["trading_state"]["running"]
    assert isinstance(dump["positions_view"], dict) and isinstance(dump["paper_progress"], dict)   # replayed, not an error


# --- two writers at once ------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_two_writers_at_once_lose_and_duplicate_nothing(sqlite_store, tmp_path):
    root, n = tmp_path / "audit", 150
    errors = []

    def write(market):
        try:
            log = market_audit(root, market)                         # each thread its own book and connections
            for i in range(n):
                log.record_order(_order(f"{market}-{i}", market=market))
        except Exception as exc:                                     # surfaced below, not lost in the thread
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(m,)) for m in ("KRX", "US")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    for m in ("KRX", "US"):
        assert [o.order_id for o in market_audit(root, m).orders()] == [f"{m}-{i}" for i in range(n)]


@pytest.mark.unit
def test_back_from_jsonl_loads_from_scratch_and_reloading_onto_the_old_database_is_refused(tmp_path, monkeypatch,
                                                                                          capsys):
    monkeypatch.setenv("TRADING_STORE", "sqlite")
    market_audit(tmp_path / "a" / "audit", "KRX").record_order(_order("s"))        # written in sqlite
    back = tmp_path / "b"                                                           # the export as the audit root
    assert export.main(["--data-dir", str(tmp_path / "a"), "--out", str(back / "audit")]) == 0
    monkeypatch.setenv("TRADING_STORE", "jsonl")
    market_audit(back / "audit", "KRX").record_order(_order("j"))                   # then in jsonl
    monkeypatch.setenv("TRADING_STORE", "sqlite")
    (back / "trading.db").write_bytes((tmp_path / "a" / "trading.db").read_bytes())  # the old database, kept
    assert migrate.main(["--data-dir", str(back)]) == 4
    assert "이미 DB에 있는 기록" in capsys.readouterr().err
    assert [o.order_id for o in market_audit(back / "audit", "KRX").orders()] == ["s"]  # nothing went in
    (back / "trading.db").rename(tmp_path / "old.db")                               # moved aside: from scratch
    assert migrate.main(["--data-dir", str(back), "--verify"]) == 0
    assert "재생 동일: 모든 시장" in capsys.readouterr().out
    assert [o.order_id for o in market_audit(back / "audit", "KRX").orders()] == ["s", "j"]
