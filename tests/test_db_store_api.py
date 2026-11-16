"""The API on the store of record (docs/04 "기록 저장소"): range reads that equal a full read at their boundaries and
per market, under both stores; another writer's append seen on the next call; the price source (KIS first, the
Yahoo file for a ticker KIS has no file of); the executor's sector cap and the held sector exposure, by hand; the
daily loop's records and status from the scheduler's own functions; key presence only, never a value."""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agent_trading_core import execution, scheduler
from agent_trading_core.audit import (
    DecisionRecord,
    OperationRecord,
    OrderRecord,
    RunRecord,
    latest_orders,
    market_audit,
    pending_reruns,
)
from agent_trading_core.execution import Prices, execute_date
from agent_trading_core.risk import RiskConfig
from tests.test_exception_rerun import D1, D2, NoManager, _decide, _rerun, _waiting, reviewed  # noqa: F401
from tests.test_market_axis import _cache, _rising

DAYS = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    return request.param


def _client(data, monkeypatch):
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), main


def _kis(data, ticker, rows):
    """A KIS cache file (kis.cache_path): Date as YYYY-MM-DD."""
    (data / "cache").mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"] + [f"{d},{o},{o},{c},{c},1000" for d, o, c in rows]
    (data / "cache" / f"{ticker}-KIS-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def books(store, tmp_path):
    """Both markets with decisions on every day (agent and rule sources), orders that change state, runs and
    operations: what the range reads are compared against."""
    data = tmp_path / "data"
    out = {}
    for m, tickers in (("KRX", ["A.KS", "B.KS"]), ("US", ["AAPL", "MSFT"])):
        a = market_audit(data / "audit", m)
        for d in DAYS:
            for t in tickers:
                a.record_decision(DecisionRecord(ticker=t, trade_date=d, rating="Buy" if t in ("A.KS", "AAPL") else "REVIEW",
                                                 thesis=f"{m} {t} {d}", market=m))
            a.record_decision(DecisionRecord(ticker=tickers[0], trade_date=d, rating="Sell", thesis="exit", market=m,
                                             source="rule"))
            oid = f"paper-{d}-{tickers[0]}"
            a.record_order(OrderRecord(order_id=oid, ticker=tickers[0], trade_date=d, side="Buy", quantity=1,
                                       status="open", market=m))
            if d != DAYS[-1]:                                   # filled later: its current state is the second row
                a.record_order(OrderRecord(order_id=oid, ticker=tickers[0], trade_date=d, side="Buy", quantity=1,
                                           filled_quantity=1, filled_price=10.0, status="filled", fill_date=d, market=m))
            a.record_run(RunRecord(market=m, stage="decide", trade_date=d, started_at=f"{d}T06:35:00Z", status="ok"))
        a.record_operation(OperationRecord(kind="halt", operator="운영자A", role="operator", reason="점검", market=m))
        a.record_operation(OperationRecord(kind="resume", operator="관리자", role="admin", reason="재가동", market=m))
        out[m] = a
    return data, out


# --- range reads ---------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_a_range_read_is_the_full_read_filtered_at_inclusive_boundaries_and_per_market(books):
    _, out = books
    for m, a in out.items():
        full = a.decisions()
        assert all(d.market == m for d in full) and len(full) == 3 * len(DAYS)
        for lo, hi in ((DAYS[1], DAYS[2]), (DAYS[0], DAYS[0]), (None, DAYS[1]), (DAYS[2], None), ("2026-05-01", "2026-05-31")):
            want = [d for d in full if (lo is None or d.trade_date >= lo) and (hi is None or d.trade_date <= hi)]
            assert a.decisions_where(trade_date=(lo, hi)) == want
        assert a.decisions_where(trade_date=DAYS[1], source="rule") == [d for d in full if d.trade_date == DAYS[1] and d.source == "rule"]
        assert len(a.decisions_where(rating="REVIEW")) == len(DAYS)
        facts = a.decision_facts()
        assert (facts["dates"], facts["total"], facts["by_source"]) == (sorted(DAYS, reverse=True), 12, {"agent": 8, "rule": 4})
        latest = latest_orders(a.orders())
        assert a.latest_orders_where() == latest
        assert list(a.latest_orders_where(status="open")) == [f"paper-{DAYS[-1]}-{a.decisions()[0].ticker}"]
        assert a.latest_orders_where(trade_date=DAYS[0], status="open") == {}            # its latest row is filled
        assert a.order_facts() == {"total": len(DAYS), "last_date": DAYS[-1]}
        assert [r.trade_date for r in a.runs_where(last=2)] == DAYS[-2:]
        assert [o.kind for o in a.operations_where(last=1)] == ["resume"]
        assert [o.kind for o in a.operations_where(kind="halt")] == ["halt"]
    # the two books never mix: no US ticker in the KRX reads and back
    assert {d.ticker for d in out["KRX"].decisions_where(trade_date=(None, None))} == {"A.KS", "B.KS"}
    assert {o.ticker for o in out["US"].latest_orders_where().values()} == {"AAPL"}


@pytest.mark.unit
def test_the_api_serves_the_range_it_asks_for_and_sees_another_writers_append_on_the_next_call(books, monkeypatch):
    data, out = books
    _cache(data, "^KS11", [(d, 100, 100) for d in DAYS])
    _cache(data, "^GSPC", [(d, 100, 100) for d in DAYS])
    api, _ = _client(data, monkeypatch)
    day = api.get("/api/decisions", params={"date": DAYS[1], "market": "KRX"}).json()
    assert sorted((d["ticker"], d["source"]) for d in day) == [("A.KS", "agent"), ("A.KS", "rule"), ("B.KS", "agent")]
    assert api.get("/api/decisions/dates", params={"market": "US"}).json() == sorted(DAYS, reverse=True)
    assert [d["ticker"] for d in api.get("/api/decisions", params={"ticker": "msft", "market": "US"}).json()] == ["MSFT"] * 4
    assert api.get("/api/decisions", params={"review_only": True, "rating": "Buy", "market": "KRX"}).json() == []
    h = api.get("/api/health", params={"market": "KRX"}).json()
    assert (h["decisions"], h["decisions_by_source"], h["orders"]) == (12, {"agent": 8, "rule": 4, "human": 0}, 4)
    # the exception queue of the last day: its REVIEW decision only (the day's order is open, not an exception)
    assert [(x["ticker"], x["kind"]) for x in api.get("/api/exceptions", params={"market": "KRX"}).json()] == [("B.KS", "review")]
    # another writer (the executor, the loop) appends: the next call has it — no file is watched by the API
    out["KRX"].record_decision(DecisionRecord(ticker="C.KS", trade_date="2026-06-05", rating="Hold", thesis="new"))
    out["KRX"].record_order(OrderRecord(order_id="paper-2026-06-05-C.KS", ticker="C.KS", trade_date="2026-06-05",
                                        side="Buy", quantity=0, status="rejected", reason="x"))
    assert api.get("/api/decisions/dates", params={"market": "KRX"}).json()[0] == "2026-06-05"
    assert api.get("/api/health", params={"market": "KRX"}).json()["orders"] == 5
    assert [x["ref"] for x in api.get("/api/exceptions", params={"market": "KRX"}).json()] == ["paper-2026-06-05-C.KS"]
    assert [o["order_id"] for o in api.get("/api/orders", params={"status": "rejected", "market": "KRX"}).json()] == ["paper-2026-06-05-C.KS"]
    if out["KRX"].store.kind == "sqlite":                       # the store of record: no JSON-lines file appears
        assert list((data / "audit").rglob("*.jsonl")) == []


# --- runs and the loop status --------------------------------------------------------------------------------

@pytest.mark.unit
def test_runs_are_served_newest_first_per_market_with_their_labels(books, monkeypatch):
    data, _ = books
    api, _ = _client(data, monkeypatch)
    rows = api.get("/api/runs", params={"market": "US", "limit": 2}).json()
    assert [(r["market"], r["trade_date"], r["stage_ko"], r["status_ko"]) for r in rows] == [
        ("US", DAYS[3], "결정", "완료"), ("US", DAYS[2], "결정", "완료")]
    assert api.get("/api/runs", params={"market": "KRX", "stage": "fill"}).json() == []
    assert api.get("/api/runs", params={"limit": 0}).status_code == 422
    assert api.get("/api/runs", params={"market": "XX"}).status_code == 400


@pytest.mark.unit
def test_the_loop_status_is_the_schedulers_plan_and_its_retry_of_a_failed_stage(store, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _, main = _client(data, monkeypatch)
    now = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)                 # Monday 16:00 KST: that day's KRX decide is due
    dl = main._daily_loop()
    plan = scheduler.plan(dl, ["KRX"], now, 1)[0]
    s = main._loop_status("KRX", now)
    assert (s["status"], s["last"], s["retry_at_kst"]) == ("never", {"decide": None, "fill": None}, None)
    assert (s["next"]["at_kst"], s["next"]["stage"]) == (plan[0].isoformat(), plan[2]) == ("2026-11-17T09:05:00+09:00", "fill")
    a = market_audit(data / "audit", "KRX")
    a.record_run(RunRecord(market="KRX", stage="fill", trade_date="2026-11-16", started_at="x", status="ok"))
    a.record_run(RunRecord(market="KRX", stage="decide", trade_date="2026-11-16", started_at="x", status="failed",
                           error="VendorUnavailableError: KIS 503"))
    s = main._loop_status("KRX", now)
    assert (s["status"], s["last"]["fill"]["status"], [r["error"] for r in s["failed"]]) == (
        "failed", "ok", ["VendorUnavailableError: KIS 503"])
    # the scheduler wakes an hour later for it (scheduler.next_wake), before the planned fill
    assert s["retry_at_kst"] == "2026-11-16T17:00:00+09:00"
    assert datetime.fromisoformat(s["retry_at_kst"]) == scheduler.next_wake(dl, ["KRX"], now, data)
    a.record_run(RunRecord(market="KRX", stage="decide", trade_date="2026-11-16", started_at="y", status="ok"))
    s = main._loop_status("KRX", now)
    assert (s["status"], s["failed"], s["retry_at_kst"]) == ("ok", [], None)


# --- the price source ------------------------------------------------------------------------------------------

@pytest.mark.unit
def test_prices_read_the_kis_file_first_and_the_yahoo_file_for_a_ticker_kis_has_none_of(tmp_path):
    data = tmp_path / "data"
    _kis(data, "^KS11", [(d, 200, 200) for d in DAYS])
    _cache(data, "^KS11", [(d, 100, 100) for d in DAYS[:-1]])
    _cache(data, "B.KS", [(d, 50, 51) for d in DAYS])
    (data / "cache" / "_provisional.json").write_text(json.dumps({DAYS[-1]: ["^KS11"]}), encoding="utf-8")
    p = Prices(data / "cache")
    assert (p.close_asof("^KS11", DAYS[-1]), p.info("^KS11")) == (200.0, {"source": "kis", "as_of": DAYS[-1], "provisional": True})
    assert (p.close_asof("B.KS", DAYS[-1]), p.info("B.KS")) == (51.0, {"source": "yahoo", "as_of": DAYS[-1], "provisional": False})
    assert p.info("NONE.KS") == {"source": None, "as_of": None, "provisional": False} and p.close_asof("NONE.KS", DAYS[0]) is None
    y = Prices(data / "cache", source="yahoo")                      # Yahoo only: the KIS file is not read
    assert (y.close_asof("^KS11", DAYS[-1]), y.info("^KS11")["source"], y.calendar("^KS11")) == (100.0, "yahoo", DAYS[:-1])
    assert p.close_asof("^KS11", "2026-05-29") is None and p.close_asof("^KS11", "2026-06-02T") == 200.0


@pytest.mark.unit
def test_health_markets_and_positions_say_where_the_prices_come_from(store, tmp_path, monkeypatch):
    data = tmp_path / "data"
    _kis(data, "^KS11", [(d, 100, 100) for d in DAYS])
    _cache(data, "^GSPC", [(d, 100, 100) for d in DAYS])
    _cache(data, "B.KS", [(d, 10_000, 10_000) for d in DAYS])          # held, priced from the Yahoo file
    market_audit(data / "audit", "KRX").record_order(OrderRecord(
        order_id="paper-2026-06-01-B.KS", ticker="B.KS", trade_date="2026-06-01", side="Buy", quantity=100,
        filled_quantity=100, filled_price=10_000.0, status="filled", fill_date="2026-06-02"))
    api, _ = _client(data, monkeypatch)
    h = api.get("/api/health", params={"market": "KRX"}).json()
    assert (h["price_source"], h["price_as_of"], h["price_provisional"]) == ("kis", DAYS[-1], False)
    cards = {c["market"]: c for c in api.get("/api/markets").json()}
    assert (cards["KRX"]["price_source"], cards["US"]["price_source"]) == ("kis", "yahoo")
    pos = api.get("/api/positions", params={"market": "KRX"}).json()
    assert (pos["price_source"], pos["price_fallback"]) == ("kis", ["B.KS"])


# --- sectors ---------------------------------------------------------------------------------------------------

SECTOR = {"A.KS": "Technology", "B.KS": "Technology", "C.KS": "Technology", "D.KS": "Financial Services"}


@pytest.mark.unit
def test_the_executor_caps_a_sectors_buys_and_names_the_cap_on_that_order_only(store, tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "sector_of", SECTOR.get)
    a = market_audit(tmp_path / "audit", "KRX")
    days = [f"2026-06-{i:02d}" for i in range(1, 6)]
    for t in ("A.KS", "B.KS", "C.KS", "D.KS"):
        a.record_decision(DecisionRecord(ticker=t, trade_date=days[2], rating="Buy", thesis="b"))
    frames = {t: _rising(days) for t in ("^KS11", "A.KS", "B.KS", "C.KS", "D.KS")}
    execute_date(a, days[2], Prices(frames=frames), risk=RiskConfig(regime_ma_days=3))
    o = latest_orders(a.orders())
    # 4 buys → 25% each, cut to the 10% per name; Technology A 10% + B 10% = the 20% cap, C gets 0% → blocked
    # (the others fill at the next day's open, which is in the frames)
    assert {t: o[f"paper-{days[2]}-{t}"].status for t in SECTOR} == {"A.KS": "filled", "B.KS": "filled", "C.KS": "blocked",
                                                                     "D.KS": "filled"}
    c = o[f"paper-{days[2]}-C.KS"]
    assert "sector_cap" in c.rules_applied and "sector_cap: C.KS (Technology) 10.0% → 0.0%" in c.reason
    assert [i.rule for i in c.plan.interventions if i.rule == "sector_cap"] == ["sector_cap"]
    # the cap is C's alone: A's card names its own per-name cut, not C's sector cap
    assert [i.rule for i in o[f"paper-{days[2]}-A.KS"].plan.interventions] == ["max_position_pct"]


@pytest.mark.unit
def test_sector_exposure_is_the_three_largest_sectors_and_the_unclassified_sum_by_hand(monkeypatch):
    from backend import main
    sectors = {"A": "Technology", "B": "Technology", "C": "Financial Services", "D": "Energy", "E": "Utilities"}
    monkeypatch.setattr(main, "sector_of", sectors.get)
    rows = [{"ticker": t, "weight": w} for t, w in
            (("A", 0.10), ("B", 0.05), ("C", 0.12), ("D", 0.03), ("E", 0.02), ("F", 0.04), ("G", 0.01))]
    out = main.sector_exposure(rows)
    # Technology 0.10 + 0.05 = 0.15, Financial 0.12, Energy 0.03; Utilities 0.02 is fourth (left out); F + G unlisted
    assert [(r["sector"], round(r["pct"], 10), r["classified"]) for r in out] == [
        ("Technology", 0.15, True), ("Financial Services", 0.12, True), ("Energy", 0.03, True), ("미분류", 0.05, False)]
    assert main.sector_exposure([]) == []
    assert main.sector_exposure([{"ticker": "A", "weight": 0.1}]) == [{"sector": "Technology", "pct": 0.1, "classified": True}]


@pytest.mark.unit
def test_positions_serve_the_held_sectors_against_the_executors_cap(store, tmp_path, monkeypatch):
    data = tmp_path / "data"
    for t in ("^KS11", "^GSPC", "005930.KS", "NOSECTOR.KS"):
        _cache(data, t, [(d, 10_000, 10_000) for d in DAYS])
    a = market_audit(data / "audit", "KRX")
    for t, q in (("005930.KS", 1000), ("NOSECTOR.KS", 500)):
        a.record_order(OrderRecord(order_id=f"paper-2026-06-01-{t}", ticker=t, trade_date="2026-06-01", side="Buy",
                                   quantity=q, filled_quantity=q, filled_price=10_000.0, status="filled", fill_date="2026-06-02"))
    api, main = _client(data, monkeypatch)
    pos = api.get("/api/positions", params={"market": "KRX"}).json()
    w = {r["ticker"]: r["weight"] for r in pos["positions"]}
    assert main.sector_of("005930.KS") == "Technology" and main.sector_of("NOSECTOR.KS") is None
    assert pos["sector_exposure"] == [{"sector": "Technology", "pct": w["005930.KS"], "classified": True},
                                      {"sector": "미분류", "pct": w["NOSECTOR.KS"], "classified": False}]
    assert pos["limits"]["max_sector_pct"] == RiskConfig().max_sector_pct == 0.20


# --- keys: whether, never what ---------------------------------------------------------------------------------

@pytest.mark.unit
def test_the_data_status_says_whether_each_key_is_set_and_never_its_value(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _kis(data, "^KS11", [(d, 1, 1) for d in DAYS])
    _cache(data, "^GSPC", [(d, 1, 1) for d in DAYS])
    log = [{"at": "2026-11-13T15:40:00+09:00", "source": "kis", "market": "KRX", "symbols": 41, "failed": []},
           {"at": "2026-11-14T15:40:00+09:00", "source": "kis", "market": "KRX", "symbols": 41,
            "failed": [{"symbol": "005930.KS", "error": "VendorUnavailableError: timeout"}]}]
    (data / "cache" / "_refresh_log.jsonl").write_text("\n".join(map(json.dumps, log)) + "\n{cut", encoding="utf-8")
    env = tmp_path / "x.env"
    env.write_text("DART_API_KEY=dart-secret-value-31\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ENV_FILE", str(env))
    secrets = {"KIS_APP_KEY": "kis-key-secret-17", "KIS_APP_SECRET": "kis-secret-value-29"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    for k in ("KIS_ENV", "ANTHROPIC_API_KEY", "DART_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    api, _ = _client(data, monkeypatch)
    r = api.get("/api/data/status")
    body = r.json()
    v = {x["id"]: x for x in body["vendors"]}
    assert v["kis"]["keys"] == {"KIS_APP_KEY": True, "KIS_APP_SECRET": True, "KIS_ENV": False}
    assert (v["kis"]["state"], v["dart"]["keys"], v["dart"]["state"]) == ("no_key", {"DART_API_KEY": True}, "unknown")
    assert (v["yfinance"]["key_required"], v["yfinance"]["state"], v["anthropic"]["keys"]) == (False, "unknown", {"ANTHROPIC_API_KEY": False})
    assert body["cache"]["kis"]["files"] == 1 and body["cache"]["yahoo"]["files"] == 1
    assert body["refresh"]["KRX"] | {} == {"at": "2026-11-14T15:40:00+09:00", "source": "kis", "symbols": 41, "last_final_day": None,
                                         "failed_count": 1, "failed_symbols": ["005930.KS"],
                                         "failed_error": "VendorUnavailableError: timeout", "last_success_at": "2026-11-13T15:40:00+09:00"}
    monkeypatch.setenv("KIS_ENV", "paper")
    assert {x["id"]: x["state"] for x in api.get("/api/data/status").json()["vendors"]}["kis"] == "reachable"
    # no response carries a value: the status, health and both markets' cards scanned for every secret
    texts = [r.text, api.get("/api/data/status").text, api.get("/api/health", params={"market": "KRX"}).text,
             api.get("/api/markets").text, api.get("/api/settings/rules").text]
    found = [s for s in [*secrets.values(), "dart-secret-value-31"] for t in texts if s in t]
    assert found == []
    assert api.get("/api/health", params={"market": "KRX"}).json()["refresh"]["failed_count"] == 1


# --- a rerun's answer on the closed exception's card -----------------------------------------------------------

@pytest.mark.unit
def test_the_closed_review_card_links_to_the_decision_that_answered_its_rerun(reviewed):  # noqa: F811 (the fixture)
    client, audit, ticker, oid, api = reviewed
    _rerun(api, oid)
    # the queue's waiting items are the loop's reservations (audit.pending_reruns), read through the range queries
    assert {o.order_id for o in pending_reruns(audit.orders())} == _waiting(api) == {oid}
    def card():
        return next(d for d in api.get("/api/decisions", params={"date": D1, "market": "KRX"}).json()
                    if d["ticker"] == ticker and d["rating"] == "REVIEW")
    assert [o["rerun_answer"] for o in card()["orders"]] == [None]                    # reserved, not answered yet
    _decide(client, D2, audit.directory.parents[1], "rule", NoManager())
    assert pending_reruns(audit.orders()) == [] and _waiting(api) == set()
    (o,) = card()["orders"]
    assert (o["exception_resolved"], o["rerun_answer"]) == (True, {"ticker": ticker, "trade_date": D2, "rating": "Buy",
                                                                    "rating_ko": "매수", "attempt": 1, "same_day": True})
    reply = api.get("/api/decisions", params={"date": D2, "ticker": ticker, "market": "KRX"}).json()
    assert [d["rating"] for d in reply if d["source"] == "agent"] == ["Buy"]           # the chip's target exists


# --- container start (backend/entrypoint.py) -----------------------------------------------------------------------

@pytest.mark.unit
def test_the_container_loads_on_the_first_start_only_so_a_restart_after_a_days_writes_still_starts_the_api(
        tmp_path, monkeypatch):
    """--verify on every start would stop the API for good after the first day: the database takes the writes, the
    JSON-lines files stay as the load-time backup, the two replays differ (exit 5)."""
    import subprocess
    import sys

    from backend import entrypoint

    data = tmp_path / "data"
    monkeypatch.setenv("TRADING_STORE", "jsonl")
    market_audit(data / "audit", "KRX").record_decision(DecisionRecord(ticker="A.KS", trade_date=DAYS[0], rating="Buy",
                                                                       thesis="jsonl 시절", market="KRX"))
    monkeypatch.setenv("TRADING_STORE", "sqlite")
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    execs, real_run = [], subprocess.run
    monkeypatch.setattr(entrypoint.os, "execvp", lambda f, args: execs.append(args))
    ran = []
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or real_run(cmd, **kw))

    assert entrypoint.needs_load(data) and entrypoint.main() == 0       # first start: load + verify, then the API
    assert len(ran) == 1 and ran[0][-1] == "--verify" and execs == [entrypoint.UVICORN]
    assert not entrypoint.needs_load(data)

    market_audit(data / "audit", "KRX").record_run(RunRecord(market="KRX", stage="decide", trade_date=DAYS[0],
                                                             started_at=f"{DAYS[0]}T06:35:00Z", status="ok"))
    assert real_run([sys.executable, str(entrypoint.ROOT / "scripts" / "migrate_jsonl_to_db.py"), "--verify"],
                    capture_output=True, env={**entrypoint.os.environ}).returncode == 5   # what every-start would hit
    ran.clear()
    execs.clear()
    assert entrypoint.main() == 0                                       # restart: no load, the API starts
    assert ran == [] and execs == [entrypoint.UVICORN]


@pytest.mark.unit
def test_a_failed_first_load_does_not_start_the_api(tmp_path, monkeypatch):
    from backend import entrypoint

    monkeypatch.setenv("TRADING_STORE", "sqlite")
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path / "data"))
    execs = []
    monkeypatch.setattr(entrypoint.os, "execvp", lambda f, args: execs.append(args))
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 4})())
    assert entrypoint.main() == 4 and execs == []
