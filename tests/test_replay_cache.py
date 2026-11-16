"""The replay cache (perf/replay-cache): a replay of a market's book — positions_view, paper_progress, book_events,
book_series — is kept in memory under ``_Store.book_key`` and served as a copy: a hit is the bytes a fresh replay
gives, and every input moving (an order, an operation, a decision, a price file or the provisional list, the date,
a calendar file — another process's write included) replays again. And the 포지션 exit distribution counts the agent's
selling sell reviews apart from the rules' exits and the rating downgrade."""

import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import OperationRecord, OrderRecord, market_audit
from agent_trading_core.schemas import PortfolioRating, SellAction, SellReason
from tests.test_api import _cache
from tests.test_ticker_detail import _memo, _rec, _review

DAYS = ["2026-06-01", "2026-06-02", "2026-06-04", "2026-06-05", "2026-06-08"]
A, B, C, D = "000660.KS", "005380.KS", "005930.KS", "000270.KS"
REPLAYS = ("positions_view", "paper_progress", "book_events", "book_series")


@pytest.fixture(params=["sqlite", "jsonl"])
def ledger(request, tmp_path, monkeypatch):
    """KRX, four names bought on memos 06-01 (filled 06-02), then on 06-02:
    - A: an agent Exit review (thesis_broken) → sold in full 06-04 — the agent's sell
    - B: a memo rated Sell (no sell reason: a rating downgrade) → sold in full 06-04
    - D: a Trim review to 2% → a partial sell 06-04 (closes nothing)
    - C falls 30% on 06-04: the rule's stop-loss, sold 06-05.
    Returns (client, data dir, main, replay counts by function)."""
    from agent_trading_core.execution import Prices, execute_date
    from agent_trading_core.risk import RiskConfig

    monkeypatch.setenv("TRADING_STORE", request.param)
    data = tmp_path / "data"
    _cache(data, "^KS11", [(d, 100 + i, 100 + i) for i, d in enumerate(DAYS)])
    for t in (A, B, D):
        _cache(data, t, [(d, 10_000, 10_000) for d in DAYS])
    _cache(data, C, [("2026-06-01", 10_000, 10_000), ("2026-06-02", 10_000, 10_000), ("2026-06-04", 7_000, 7_000),
                     ("2026-06-05", 7_000, 7_000), ("2026-06-08", 7_000, 7_000)])
    krx = market_audit(data / "audit", "KRX")
    for t in (A, B, C, D):
        krx.record_decision(_rec(t, "2026-06-01", _memo()))
    prices, risk = Prices(data / "cache"), RiskConfig(regime_ma_days=3)
    execute_date(krx, "2026-06-01", prices, risk=risk)
    krx.record_decision(_rec(A, "2026-06-02", _review(SellAction.EXIT, (True, False), [SellReason.THESIS_BROKEN])))
    krx.record_decision(_rec(B, "2026-06-02", _memo(PortfolioRating.SELL)))
    krx.record_decision(_rec(D, "2026-06-02", _review(SellAction.TRIM, (False, False), [SellReason.OVERWEIGHT], 2)))
    for day in DAYS[1:]:
        execute_date(krx, day, prices, risk=risk)
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    from agent_trading_core.execution import Prices as P
    monkeypatch.setattr(P, "RECHECK_SECONDS", 0.0)          # a rewritten price file is seen at once
    from backend import main
    importlib.reload(main)
    counts = dict.fromkeys(REPLAYS, 0)
    for name in REPLAYS:
        real = getattr(main, name)

        def counted(*a, _real=real, _name=name, **k):
            counts[_name] += 1
            return _real(*a, **k)
        monkeypatch.setattr(main, name, counted)
    return TestClient(main.app), data, main, counts


GETS = ["/api/positions", "/api/markets", "/api/paper/progress", "/api/events", "/api/performance/series",
        f"/api/tickers/{D}"]


def _fresh(main) -> None:
    for s in main.STORES.values():
        s._cache.clear()


@pytest.mark.unit
def test_a_hit_is_the_bytes_of_a_fresh_replay(ledger):
    client, _, main, counts = ledger
    first = {p: client.get(p).content for p in GETS}
    replays = dict(counts)
    hits = {p: client.get(p).content for p in GETS}
    assert counts == replays                                  # nothing replayed again
    _fresh(main)
    again = {p: client.get(p).content for p in GETS}
    assert counts["positions_view"] > replays["positions_view"]     # the cache was empty: replayed
    for p in GETS:
        if p.endswith("series"):                                  # says whether it was a hit
            hits[p] = hits[p].replace(b'"cache":"hit"', b'"cache":"miss"')
        assert hits[p] == first[p] == again[p], p
    # one replay of the book serves the home cards, the 포지션 screen and the ticker page
    assert replays["positions_view"] == 1 + 1                  # KRX + US (the home cards)


@pytest.mark.unit
def test_a_caller_labelling_its_copy_does_not_change_the_kept_one(ledger):
    client, _, main, _ = ledger
    client.get("/api/positions")
    st = main.STORES["KRX"].refresh()
    book = main._book(st)
    book["positions"].clear()
    book["exits_by_reason"]["x"] = 1
    assert len(main._book(st)["positions"]) == 1 and "x" not in main._book(st)["exits_by_reason"]


def _replayed_again(client, counts, path="/api/positions") -> tuple[dict, dict]:
    before = dict(counts)
    body = client.get(path).json()
    return body, {k: counts[k] - before[k] for k in counts}


@pytest.mark.unit
def test_an_order_an_operation_or_a_decision_replays_again(ledger):
    client, data, _, counts = ledger
    base = client.get("/api/positions").json()
    progress = client.get("/api/paper/progress").json()
    krx = market_audit(data / "audit", "KRX")                 # another writer's handle
    # an order: a buy of B again, filled 06-08
    krx.record_order(OrderRecord(order_id="paper-2026-06-05-005380.KS", ticker=B, trade_date="2026-06-05", side="Buy",
                                 quantity=10, filled_quantity=10, filled_price=10_000.0, fill_date="2026-06-08",
                                 status="filled", market="KRX"))
    body, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1 and {r["ticker"] for r in body["positions"]} == {D, B}
    # an operation: a deposit (capital_set flow)
    krx.record_operation(OperationRecord(kind="capital_set", operator="관리자", role="admin", reason="입금", market="KRX",
                                         target="paper", before={}, effective_from="2026-06-08",
                                         after={"account": "paper", "amount": base["capital"] + 1_000_000.0,
                                                "change": "flow", "flow": 1_000_000.0}))
    body, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1 and body["capital"] == base["capital"] + 1_000_000.0
    client.get("/api/paper/progress"), client.get("/api/markets")       # read in: the decision alone moves next
    # a decision: a Hold review of D is its last review now
    krx.record_decision(_rec(D, "2026-06-08", _review(SellAction.HOLD)))
    body, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1
    assert {r["ticker"]: r["last_review"]["action"] for r in body["positions"] if r["last_review"]}[D] == "Hold"
    (home,) = [c for c in client.get("/api/markets").json() if c["market"] == "KRX"]
    assert home["flow"]["decisions"]["date"] == "2026-06-08"     # the home flow card, kept with the book
    _, n = _replayed_again(client, counts, "/api/paper/progress")
    assert n["paper_progress"] == 1
    assert client.get("/api/paper/progress").json() != progress
    _, n = _replayed_again(client, counts)
    assert n == dict.fromkeys(REPLAYS, 0)                       # nothing moved since: a hit


@pytest.mark.unit
def test_a_price_file_its_provisional_list_the_date_or_a_calendar_file_replays_again(ledger, monkeypatch):
    client, data, main, counts = ledger
    base = client.get("/api/positions").json()
    (row,) = [r for r in base["positions"] if r["ticker"] == D]
    # a refresh rewrites D's file: the last row (a provisional one) closes higher
    _cache(data, D, [(d, 10_000, 10_000) for d in DAYS[:-1]] + [(DAYS[-1], 10_000, 12_000)])
    body, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1
    assert [r["last_price"] for r in body["positions"] if r["ticker"] == D] == [12_000.0] != [row["last_price"]]
    # the provisional list alone
    (data / "cache" / "_provisional.json").write_text(json.dumps({DAYS[-1]: [D]}), encoding="utf-8")
    _, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1
    # the date (a default as_of falls back on today when the calendar is empty)
    monkeypatch.setattr(main, "_clock", lambda: datetime(2026, 6, 9, 1, 0, tzinfo=UTC))
    _, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1
    monkeypatch.setattr(main, "_clock", lambda: datetime(2026, 6, 10, 1, 0, tzinfo=UTC))
    _, n = _replayed_again(client, counts, "/api/events")
    assert n["book_events"] == 1
    client.get("/api/paper/progress"), client.get("/api/markets")      # read in (both books): the calendar alone moves next
    # a calendar file
    from agent_trading_core import market_calendar
    (market_calendar.calendar_dir() / "KRX-2026.json").write_text(
        json.dumps({"market": "KRX", "year": 2026, "holidays": []}), encoding="utf-8")
    _, n = _replayed_again(client, counts, "/api/paper/progress")
    assert n["paper_progress"] == 1
    _, n = _replayed_again(client, counts)
    assert n["positions_view"] == 1
    # a dated as_of is its own replay, and the default one is kept beside it
    old = client.get("/api/positions", params={"as_of": "2026-06-02"}).json()
    assert old["as_of"] == "2026-06-02" and {r["ticker"] for r in old["positions"]} == {A, B, C, D}
    _, n = _replayed_again(client, counts)
    assert n["positions_view"] == 0
    _, n = _replayed_again(client, counts, "/api/markets")              # the home cards' book: kept too
    assert n["positions_view"] == 0


@pytest.mark.unit
def test_another_process_writing_is_never_served_stale(ledger):
    client, data, main, _ = ledger
    base = client.get("/api/positions").json()
    # a write in another process (the scheduler): the next request reads it
    script = ("import sys; from agent_trading_core.audit import OperationRecord, market_audit;"
              "market_audit(sys.argv[1], 'KRX').record_operation(OperationRecord(kind='capital_set', operator='관리자',"
              " role='admin', reason='입금', market='KRX', target='paper', before={}, effective_from='2026-06-08',"
              " after={'account': 'paper', 'amount': float(sys.argv[2]), 'change': 'flow', 'flow': 500000.0}))")
    subprocess.run([sys.executable, "-c", script, str(data / "audit"), str(base["capital"] + 500_000.0)], check=True)
    assert client.get("/api/positions").json()["capital"] == base["capital"] + 500_000.0
    # a write while a replay runs: kept under the stamps read before it, so the next request replays again
    real, krx, wrote = main.positions_view, market_audit(data / "audit", "KRX"), []

    def writes_midway(*a, **k):
        if not wrote:
            krx.record_operation(OperationRecord(kind="capital_set", operator="관리자", role="admin", reason="입금",
                                                 market="KRX", target="paper", before={}, effective_from="2026-06-08",
                                                 after={"account": "paper", "amount": base["capital"] + 800_000.0,
                                                        "change": "flow", "flow": 300_000.0}))
            wrote.append(1)
        return real(*a, **k)
    main.positions_view = writes_midway
    try:
        main.STORES["KRX"]._cache.clear()
        assert client.get("/api/positions").json()["capital"] == base["capital"] + 500_000.0   # read before the write
        assert client.get("/api/positions").json()["capital"] == base["capital"] + 800_000.0
    finally:
        main.positions_view = real


@pytest.mark.unit
def test_a_price_file_rewritten_while_the_home_cards_replay_is_not_kept_stale(ledger):
    """Review 1R #1: the flow cards are counted from the book, so their key is read before it — a refresh landing
    right after the replay read its prices leaves the next request a new key, never the old book's counts."""
    client, data, main, _ = ledger
    real, rewrote = main.positions_view, []

    def refresh_after(*a, **k):
        out = real(*a, **k)
        if not rewrote and k["cfg"].market == "KRX":            # D closes 9,300: within 2% of its stop line
            _cache(data, D, [(d, 10_000, 10_000) for d in DAYS[:-1]] + [(DAYS[-1], 10_000, 9_300)])
            rewrote.append(1)
        return out
    main.positions_view = refresh_after
    try:
        (stale,) = [c for c in client.get("/api/markets").json() if c["market"] == "KRX"]
        assert stale["flow"]["holdings"]["near_stop"] == 0          # replayed before the rewrite
        (card,) = [c for c in client.get("/api/markets").json() if c["market"] == "KRX"]
    finally:
        main.positions_view = real
    assert card["flow"]["holdings"]["near_stop"] == 1 == client.get("/api/positions").json()["near_stop_count"]


@pytest.mark.unit
def test_the_exit_distribution_counts_the_agent_sells_apart(ledger):
    """By hand: A's Exit review → 에이전트 매도, B's memo rated Sell → 등급 하향 (the rating downgrade), C → 손절,
    D's Trim → a partial sell: the agent's too in its row, but it closes nothing. Before: {exit_signal: 2, stop_loss: 1}."""
    client, *_ = ledger
    body = client.get("/api/positions").json()
    assert body["exits_by_reason"] == {"agent_sell": 1, "exit_signal": 1, "stop_loss": 1}
    assert body["exits_by_reason_ko"] == {"에이전트 매도": 1, "등급 하향": 1, "손절": 1}
    reason = {(t["ticker"], t["full"]): t["reason"] for t in body["closed"]}
    assert reason == {(A, True): "agent_sell", (B, True): "exit_signal", (C, True): "stop_loss", (D, False): "agent_sell"}
    assert client.get("/api/labels").json()["exit_reason_ko"]["agent_sell"] == "에이전트 매도"
