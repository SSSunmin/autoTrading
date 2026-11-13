-- The audit records as tables (agent_trading_core/store/sqlite.py). One database, every market: the ``market`` column
-- replaces the per-market folders. Append-only: every table refuses UPDATE and DELETE (triggers below); a change of
-- state is a new row, as a new JSON line was. No state table: the trading state, the book and the paper verdict are
-- replayed from these rows (audit.trading_state, execution.replay), never stored.
--
-- Every record table: id (insert order = replay order), market, recorded_at (the record's own UTC time), payload
-- (the record's JSON exactly as written — what is read back and exported), source_hash (UNIQUE: the key of a line
-- loaded from the JSONL files, so loading twice adds nothing; NULL for a record appended since — two appends are two
-- rows even when their bytes match), then columns unpacked from the payload for queries, indexed.
--
-- Portable to Postgres except the triggers' syntax (a plpgsql function there) and ``INTEGER PRIMARY KEY``
-- (``BIGINT GENERATED ALWAYS AS IDENTITY``). Types: TEXT, INTEGER, REAL, BOOLEAN; times are ISO-8601 text.

CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screen_runs (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_hash TEXT UNIQUE,
    run_id TEXT NOT NULL,
    as_of TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS screen_runs_market_as_of ON screen_runs (market, as_of);
CREATE INDEX IF NOT EXISTS screen_runs_run_id ON screen_runs (run_id);

CREATE TABLE IF NOT EXISTS screen_candidates (
    id INTEGER PRIMARY KEY,
    screen_id INTEGER NOT NULL REFERENCES screen_runs (id),
    market TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    ticker TEXT NOT NULL,
    selected BOOLEAN NOT NULL,
    rank INTEGER,
    score REAL
);
CREATE INDEX IF NOT EXISTS screen_candidates_screen ON screen_candidates (screen_id);
CREATE INDEX IF NOT EXISTS screen_candidates_market_ticker ON screen_candidates (market, ticker);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_hash TEXT UNIQUE,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    rating TEXT NOT NULL,
    source TEXT NOT NULL,
    screen_run_id TEXT
);
CREATE INDEX IF NOT EXISTS decisions_market_date ON decisions (market, trade_date);
CREATE INDEX IF NOT EXISTS decisions_market_ticker ON decisions (market, ticker, trade_date);
CREATE INDEX IF NOT EXISTS decisions_screen ON decisions (screen_run_id);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_hash TEXT UNIQUE,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    fill_date TEXT
);
CREATE INDEX IF NOT EXISTS orders_market_date ON orders (market, trade_date);
CREATE INDEX IF NOT EXISTS orders_market_status ON orders (market, status);
CREATE INDEX IF NOT EXISTS orders_market_ticker ON orders (market, ticker, trade_date);
CREATE INDEX IF NOT EXISTS orders_order_id ON orders (order_id);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_hash TEXT UNIQUE,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    operator TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS operations_market_at ON operations (market, recorded_at);
CREATE INDEX IF NOT EXISTS operations_market_kind ON operations (market, kind);

CREATE TABLE IF NOT EXISTS run_records (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_hash TEXT UNIQUE,
    stage TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_records_market_stage ON run_records (market, stage, trade_date);

-- append-only, enforced by the database
CREATE TRIGGER IF NOT EXISTS schema_version_no_update BEFORE UPDATE ON schema_version BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS schema_version_no_delete BEFORE DELETE ON schema_version BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_runs_no_update BEFORE UPDATE ON screen_runs BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_runs_no_delete BEFORE DELETE ON screen_runs BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_candidates_no_update BEFORE UPDATE ON screen_candidates BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_candidates_no_delete BEFORE DELETE ON screen_candidates BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_update BEFORE UPDATE ON orders BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_delete BEFORE DELETE ON orders BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS operations_no_update BEFORE UPDATE ON operations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS operations_no_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_records_no_update BEFORE UPDATE ON run_records BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_records_no_delete BEFORE DELETE ON run_records BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- INSERT OR REPLACE deletes the row it collides with, and a connection without recursive_triggers (any client
-- but store/sqlite.py) fires no delete trigger for it: refuse an insert onto an existing id, and let an insert
-- onto an existing source_hash do nothing (as ON CONFLICT DO NOTHING) instead of replacing that row
CREATE TRIGGER IF NOT EXISTS schema_version_no_replace BEFORE INSERT ON schema_version WHEN EXISTS (SELECT 1 FROM schema_version WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_runs_no_replace BEFORE INSERT ON screen_runs WHEN EXISTS (SELECT 1 FROM screen_runs WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_candidates_no_replace BEFORE INSERT ON screen_candidates WHEN EXISTS (SELECT 1 FROM screen_candidates WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_replace BEFORE INSERT ON decisions WHEN EXISTS (SELECT 1 FROM decisions WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_replace BEFORE INSERT ON orders WHEN EXISTS (SELECT 1 FROM orders WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS operations_no_replace BEFORE INSERT ON operations WHEN EXISTS (SELECT 1 FROM operations WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_records_no_replace BEFORE INSERT ON run_records WHEN EXISTS (SELECT 1 FROM run_records WHERE id = NEW.id) BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS screen_runs_same_hash BEFORE INSERT ON screen_runs WHEN EXISTS (SELECT 1 FROM screen_runs WHERE source_hash = NEW.source_hash) BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS decisions_same_hash BEFORE INSERT ON decisions WHEN EXISTS (SELECT 1 FROM decisions WHERE source_hash = NEW.source_hash) BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS orders_same_hash BEFORE INSERT ON orders WHEN EXISTS (SELECT 1 FROM orders WHERE source_hash = NEW.source_hash) BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS operations_same_hash BEFORE INSERT ON operations WHEN EXISTS (SELECT 1 FROM operations WHERE source_hash = NEW.source_hash) BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS run_records_same_hash BEFORE INSERT ON run_records WHEN EXISTS (SELECT 1 FROM run_records WHERE source_hash = NEW.source_hash) BEGIN SELECT RAISE(IGNORE); END;
