"""One replay per market, shared (perf/replay-share): the screen functions take the market's ``Book`` (``book=``) and
the API replays it once (``_Store.shared_book``) for the positions, the gate, the guard history and the series — the
same bytes as each replaying on its own. And a market with no index prices yet is dated by its own clock
(``market_today``), not the server's: 00:30 in Seoul is still the day before in New York."""

import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent_trading_core import execution
from agent_trading_core.audit import OperationRecord, OrderRecord, market_audit
from agent_trading_core.execution import (
    book_events,
    book_series,
    market_today,
    paper_config,
    paper_progress,
    positions_view,
    replay,
    with_capital,
)
from tests.test_replay_cache import GETS, ledger  # noqa: F401 (the fixture)

KST = timezone(timedelta(hours=9))
SCREENS = ["/api/positions", "/api/markets", "/api/paper/progress", "/api/events", "/api/performance/series"]


def _count_replays(monkeypatch, main) -> list[str]:
    """The market of every ``replay`` run from here on (the screen functions' and the API's)."""
    seen: list[str] = []

    def counted(orders, prices, cfg, until):
        seen.append(cfg.market)
        return replay(orders, prices, cfg, until)
    monkeypatch.setattr(execution, "replay", counted)
    monkeypatch.setattr(main, "replay", counted)
    return seen


def _fresh(main) -> None:
    for s in main.STORES.values():
        s._cache.clear()


@pytest.mark.unit
def test_shared_or_replayed_alone_the_screens_answer_the_same_bytes(ledger, monkeypatch):  # noqa: F811
    client, data, main, _ = ledger
    # a deposit while running: the shared replay carries the capital's cash flows as each function's own does
    krx = market_audit(data / "audit", "KRX")
    krx.record_operation(OperationRecord(
        kind="capital_set", operator="관리자", role="admin", reason="입금", market="KRX", target="paper", before={},
        effective_from="2026-06-04", after={"account": "paper", "amount": 11_000_000.0, "change": "flow", "flow": 1_000_000.0}))
    # same-day fills of one name, two reasons (review 1R #1): an agent sale of half the 200 left, then the rule's
    # stop-loss of the rest — the one applied last closes the holding (full), and the gate replays newest first
    for oid, rules in (("paper-2026-06-05-000270.KS", []), ("paper-2026-06-05-000270.KS-exit", ["stop_loss"])):
        krx.record_order(OrderRecord(order_id=oid, ticker="000270.KS", trade_date="2026-06-05", side="Sell", quantity=100,
                                     filled_quantity=100, filled_price=9_000.0, fill_date="2026-06-08", status="filled",
                                     market="KRX", rules_applied=rules))
    pos = client.get("/api/positions").json()
    assert pos["cash_flows"] == [{"date": "2026-06-04", "amount": 1_000_000.0}]
    # record order: the stop-loss came last and closed it; the gate's newest-first order closes it on the agent sale
    assert pos["exits_by_reason"]["stop_loss"] == 2
    assert client.get("/api/paper/progress").json()["paper"]["exits_by_reason"].get("stop_loss") == 1
    _fresh(main)
    shared = {p: client.get(p).content for p in GETS}
    _fresh(main)
    monkeypatch.setattr(main._Store, "shared_book", lambda self, **_: None)      # each function replays on its own
    alone = {p: client.get(p).content for p in GETS}
    for p in GETS:
        assert shared[p] == alone[p], p


@pytest.mark.unit
def test_a_request_replays_each_market_once(ledger, monkeypatch):  # noqa: F811
    client, _, main, _ = ledger
    seen = _count_replays(monkeypatch, main)
    _fresh(main)
    client.get("/api/positions")
    assert seen == ["KRX"]
    seen.clear(), _fresh(main)
    client.get("/api/markets")
    assert sorted(seen) == ["KRX", "US"]
    # a user opening the five screens one after another, nothing kept yet: one per market, the gate's apart
    seen.clear(), _fresh(main)
    for p in SCREENS:
        client.get(p)
    assert sorted(seen) == ["KRX", "KRX", "US"]        # the gate keeps its own (newest-first) replay
    # before the share each screen function replayed by itself
    seen.clear(), _fresh(main)
    monkeypatch.setattr(main._Store, "shared_book", lambda self, **_: None)
    for p in SCREENS:
        client.get(p)
    assert sorted(seen) == ["KRX", "KRX", "KRX", "KRX", "US"]


@pytest.mark.unit
def test_the_functions_without_a_book_replay_as_before(ledger):  # noqa: F811
    """The old call (no ``book``) is the same as handing them the market's replay to the last day."""
    _, _, main, _ = ledger
    st = main.STORES["KRX"].refresh()
    orders, ops, prices, cfg = st.order_rows, st.operations, main.PRICES, paper_config("KRX")
    book = replay(orders, prices, with_capital(cfg, ops), until=prices.calendar(cfg.index)[-1])
    assert positions_view(orders, prices, cfg=cfg, operations=ops) == positions_view(orders, prices, cfg=cfg, operations=ops, book=book)
    assert book_events(orders, ops, prices, cfg=cfg) == book_events(orders, ops, prices, cfg=cfg, book=book)
    assert book_series(orders, ops, prices, cfg) == book_series(orders, ops, prices, cfg, book=book)
    decisions = st.decisions()
    assert paper_progress(st.orders, decisions, ops, prices, cfg) == paper_progress(st.orders, decisions, ops, prices, cfg, book=book)


@pytest.mark.unit
def test_market_today_is_the_market_date_across_midnight():
    # winter (EST, UTC−5): New York's midnight is 14:00 in Seoul
    assert market_today("US", datetime(2026, 11, 17, 0, 30, tzinfo=KST)) == "2026-11-16"
    assert market_today("US", datetime(2026, 11, 17, 13, 59, tzinfo=KST)) == "2026-11-16"
    assert market_today("US", datetime(2026, 11, 17, 14, 0, tzinfo=KST)) == "2026-11-17"
    # summer (EDT, UTC−4): 13:00 in Seoul
    assert market_today("US", datetime(2026, 7, 1, 12, 59, tzinfo=KST)) == "2026-06-30"
    assert market_today("US", datetime(2026, 7, 1, 13, 0, tzinfo=KST)) == "2026-07-01"
    assert market_today("KRX", datetime(2026, 11, 17, 0, 30, tzinfo=KST)) == "2026-11-17"
    assert market_today("KRX", datetime(2026, 11, 16, 15, 30, tzinfo=UTC)) == "2026-11-17"


@pytest.mark.unit
def test_a_book_with_no_prices_is_dated_by_its_market_clock(tmp_path, monkeypatch):
    """No index file in either market (a fresh install): the positions' as_of is each market's own today, and the
    kept book moves to the next day when that market's midnight passes — not the server's."""
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path / "data"))
    from backend import main
    importlib.reload(main)
    client = TestClient(main.app)
    now = {"t": datetime(2026, 11, 17, 0, 30, tzinfo=KST)}            # Seoul 00:30 = New York 10:30 the day before
    monkeypatch.setattr(main, "_clock", lambda: now["t"])

    def as_of(m: str) -> str:
        return client.get("/api/positions", params={"market": m}).json()["as_of"]
    assert (as_of("KRX"), as_of("US")) == ("2026-11-17", "2026-11-16")
    now["t"] = datetime(2026, 11, 17, 14, 30, tzinfo=KST)              # New York 00:30
    assert (as_of("KRX"), as_of("US")) == ("2026-11-17", "2026-11-17")
    # the executor's own call (no clock passed) reads the market's clock too (either side of a midnight in between)
    before = market_today("US")
    dated = positions_view([], main.PRICES, cfg=paper_config("US"))["as_of"]
    assert dated in (before, market_today("US"))
