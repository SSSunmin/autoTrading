"""One book for the screens and the gate (fix/replay-order): ``replay`` applies a day's fills of one side in the order
they were placed (``placed_at``), read from the records and not from the list, and fills placed in the same second
in the order they come in — record order for every caller that passes record rows. The API keeps one replay per
market for the screens and the gate alike."""

import pickle
import random
from itertools import groupby

import pytest

from agent_trading_core.audit import OrderRecord, latest_orders, market_audit
from agent_trading_core.execution import paper_config, replay, with_capital
from tests.test_replay_cache import ledger  # noqa: F401 (the fixture)

TK = "000270.KS"


def _sells(first_reason: str, second_reason: str, *, same_second: bool = False) -> list[OrderRecord]:
    """Two sells of the 200 left on one day, 100 each: the one applied second closes the holding (full)."""
    out = []
    for at, reason in (("2026-06-05T06:40:00Z", first_reason),
                       ("2026-06-05T06:40:00Z" if same_second else "2026-06-05T06:40:01Z", second_reason)):
        oid = f"paper-2026-06-05-{TK}" + ("-exit" if reason != "agent" else "")
        out.append(OrderRecord(order_id=oid, ticker=TK, trade_date="2026-06-05", placed_at=at, side="Sell", quantity=100,
                               filled_quantity=100, filled_price=9_000.0, fill_date="2026-06-08", status="filled",
                               market="KRX", rules_applied=[] if reason == "agent" else [reason]))
    return out


def _book(main, rows):
    st = main.STORES["KRX"].refresh()
    cfg = with_capital(paper_config("KRX"), st.operations)
    return replay(rows, main.PRICES, cfg, until=main.PRICES.calendar(cfg.index)[-1])


def _closes(book):
    return [(c.reason, c.full) for c in book.closed if c.ticker == TK and c.exit_date == "2026-06-08"]


def _reason(r: str) -> str:
    return "exit_signal" if r == "agent" else r


@pytest.mark.unit
def test_replay_is_the_same_book_however_orders_placed_apart_come_in(ledger):  # noqa: F811
    """Record rows, one row per order reversed, and shuffled — keeping only what was placed in one second together,
    in record order: the same book to the byte."""
    _, data, main, _ = ledger
    for o in _sells("agent", "stop_loss"):
        market_audit(data / "audit", "KRX").record_order(o)
    rows = main.STORES["KRX"].refresh().order_rows
    seconds = [list(g) for _, g in groupby(sorted(latest_orders(rows).values(), key=lambda o: o.placed_at),
                                             key=lambda o: o.placed_at)]   # stable: record order within a second
    filled = [o for g in seconds for o in g if o.status == "filled"]
    assert len({(o.fill_date, o.side) for o in filled}) < len(filled)      # a day has two fills of one side to order
    assert len(seconds) > 2
    rnd = random.Random(20261117)
    orders = [seconds[::-1], *(rnd.sample(seconds, len(seconds)) for _ in range(5))]
    books = [pickle.dumps(_book(main, r)) for r in (rows, *([o for g in s for o in g] for s in orders))]
    assert len(set(books)) == 1


@pytest.mark.unit
@pytest.mark.parametrize(("first", "second"), [("agent", "stop_loss"), ("stop_loss", "agent")])
def test_the_later_placed_of_two_same_day_sells_closes_the_holding_in_any_list_order(ledger, first, second):  # noqa: F811
    _, _, main, _ = ledger
    current = list(latest_orders(main.STORES["KRX"].refresh().order_rows).values())
    sells = _sells(first, second)
    for order in (current + sells, current + sells[::-1], (current + sells)[::-1]):
        assert _closes(_book(main, order)) == [(_reason(first), False), (_reason(second), True)]


@pytest.mark.unit
@pytest.mark.parametrize(("first", "second"), [("agent", "stop_loss"), ("stop_loss", "agent")])
def test_the_screens_and_the_gate_count_the_same_closes(ledger, first, second):  # noqa: F811
    """Written to the log in the opposite order to their placing: the screens and the gate still agree, and both
    follow the placing — the gate used to replay newest first and close on the other sale."""
    client, data, _, _ = ledger
    for o in _sells(first, second)[::-1]:
        market_audit(data / "audit", "KRX").record_order(o)
    pos = client.get("/api/positions").json()["exits_by_reason"]
    gate = client.get("/api/paper/progress").json()["paper"]["exits_by_reason"]
    assert pos.get("stop_loss") == gate.get("stop_loss") == (2 if second == "stop_loss" else 1)


@pytest.mark.unit
@pytest.mark.parametrize(("first", "second"), [("agent", "stop_loss"), ("stop_loss", "agent")])
def test_sells_placed_in_one_second_apply_in_record_order_on_the_screens_and_the_gate(ledger, first, second):  # noqa: F811
    """Same second (an executor run): the line written later closes the holding — on the positions and on the gate
    (review 1R #1: not by order_id, where the rule's ``…-exit`` would always win)."""
    client, data, main, _ = ledger
    for o in _sells(first, second, same_second=True):
        market_audit(data / "audit", "KRX").record_order(o)
    assert _closes(_book(main, main.STORES["KRX"].refresh().order_rows)) == [(_reason(first), False), (_reason(second), True)]
    pos = client.get("/api/positions").json()["exits_by_reason"]
    gate = client.get("/api/paper/progress").json()["paper"]["exits_by_reason"]
    assert pos.get("stop_loss") == gate.get("stop_loss") == (2 if second == "stop_loss" else 1)
