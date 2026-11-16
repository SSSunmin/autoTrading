"""A person's rerun of a REVIEW exception (POST /api/exceptions/{order_id}/action rerun) answered by the next decide
(scripts/daily_loop.py answer_reruns): the reservations the loop reads are the queue's "재실행 대기" items, the answer
is a decision of the day with its marker and the exception's closing line, the executor orders it, a REVIEW again
goes back on the queue with its count, an llm rerun past the call cap or without a key waits, a hold closes the
reservation, the books stay apart — under both stores. And the model's memory names a person's decision "(사람)".
No network: the fake KIS and model clients of tests/test_daily_loop.py."""

import pytest

from agent_trading_core.audit import (
    RERUN_OPERATOR,
    CandidateRecord,
    DecisionRecord,
    FilterResult,
    OperationRecord,
    OrderRecord,
    ScreenRun,
    latest_orders,
    market_audit,
    pending_rerun_marker,
    pending_reruns,
)
from tests.test_daily_loop import FakeManager, NoManager, _market, at, dl
from tests.test_risk_panels import _client

D1, D2, D3 = "2026-08-04", "2026-08-05", "2026-08-06"
H = {"X-Admin-Token": "t"}


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    return request.param


def _decide(client, day, data, decider, llm):
    client.today = day
    return dl.run_stage("KRX", "decide", day, data, client, at(day, "15:40"), decider=decider, llm=llm)


@pytest.fixture
def reviewed(store, tmp_path, monkeypatch):
    """D1 decided by the model, its first answer refused: a REVIEW exception; the book filled at D2's open."""
    client, llm = _market(), FakeManager(refuse_first=True)
    assert _decide(client, D1, tmp_path, "llm", llm).status == "ok"
    client.today = D2
    assert dl.run_stage("KRX", "fill", D2, tmp_path, client, at(D2, "09:10")).status == "ok"
    audit = market_audit(tmp_path / "audit", "KRX")
    ticker = llm.asked[0][0]
    api, _ = _client(tmp_path, monkeypatch, token="t")
    return client, audit, ticker, f"paper-{D1}-{ticker}", api


def _act(api, oid, action, reason="형식 오류"):
    return api.post(f"/api/exceptions/{oid}/action", params={"market": "KRX"},
                    json={"action": action, "operator": "운영자A", "reason": reason}, headers=H)


def _rerun(api, oid, reason="형식 오류"):
    r = _act(api, oid, "rerun", reason)
    assert r.status_code == 200, r.text
    return r.json()


def _queue(api):
    return api.get("/api/exceptions", params={"all_dates": True, "market": "KRX"}).json()


def _waiting(api):
    """The queue's "재실행 대기" items, as the screen shows them."""
    return {x["order_id"] for x in _queue(api) if x["pending_action"] == "rerun"}


def _skip(real, ticker):
    """agent_decide whose day run leaves ``ticker`` out: only its rerun (``only``) asks the model about it."""
    def run(*a, only=None, skip=frozenset(), **k):
        return real(*a, only=only, skip=skip if only is not None else {*skip, ticker}, **k)
    return run


@pytest.mark.unit
def test_the_loop_reads_the_reservations_the_queue_shows_waiting(reviewed):
    _, audit, _, oid, api = reviewed
    assert pending_reruns(audit.orders()) == [] and _waiting(api) == set()
    assert _rerun(api, oid)["attempt"] == 1
    assert {o.order_id for o in pending_reruns(audit.orders())} == _waiting(api) == {oid}
    assert pending_rerun_marker(audit.operations(), pending_reruns(audit.orders())[0]) is None


@pytest.mark.unit
def test_a_rerun_the_days_own_decision_answers_is_marked_closed_and_ordered_once(reviewed, monkeypatch):
    client, audit, ticker, oid, api = reviewed
    _rerun(api, oid)
    # the next decide, by rule: D2's screen ranks the ticker again, so its rule decision of D2 is the answer
    r = _decide(client, D2, data := audit.directory.parents[1], "rule", NoManager())
    assert r.status == "ok" and (r.counts["reruns_pending"], r.counts["reruns_done"]) == (1, 1)
    (same,) = [d for d in audit.decisions() if d.ticker == ticker and d.trade_date == D2]   # one, never two
    assert (same.rating, same.screen_run_id, same.settings.get("rerun_of")) == ("Buy", f"daily_krx_{D2}", None)
    assert r.notes[-1] == f"{oid}: 재실행 1회째 → {D2} Buy (그날 결정)"
    (mark,) = [op for op in audit.operations() if op.operator == RERUN_OPERATOR]
    assert (mark.kind, mark.target, mark.role, mark.after["attempt"], mark.after["pending"]) == (
        "exception_action", oid, "auto", 1, False)
    assert mark.after["decision"] == {"trade_date": D2, "rating": "Buy", "decider": "rule_momentum_baseline",
                                      "same_day": True}
    closed = latest_orders(audit.orders())[oid]
    assert (closed.exception_action, closed.exception_resolved, closed.reruns) == ("rerun", True, 1)
    assert pending_reruns(audit.orders()) == [] and _waiting(api) == set()
    assert oid not in {x["order_id"] for x in _queue(api)}
    assert _act(api, oid, "rerun", "또").status_code == 409                       # closed: a person cannot ask again
    # the executor ordered the answer like any buy of D2: open, filling at the next open
    assert latest_orders(audit.orders())[f"paper-{D2}-{ticker}"].status == "open"
    # the same day again: nothing more (the stage is done; without its records, nothing is pending)
    lines = (len(audit.decisions()), len(audit.operations()), len(audit.orders()))
    assert _decide(client, D2, data, "rule", NoManager()) is None
    monkeypatch.setattr(type(audit), "runs", lambda self: [])
    r2 = _decide(client, D2, data, "rule", NoManager())
    assert r2.status == "ok" and "reruns_pending" not in r2.counts
    assert (len(audit.decisions()), len(audit.operations()), len(audit.orders())) == lines


def _screen(day):
    """A screen of four ranked names, A.KS first: the rule's Buy."""
    cands = [CandidateRecord(ticker=t, rank=i + 1, selected=i < 2, score=1.0 - i / 10,
                             filters=[FilterResult(rule="momentum_top_k", passed=True, note=f"20일 +{9 - i}%")])
             for i, t in enumerate(("A.KS", "B.KS", "C.KS", "D.KS"))]
    return ScreenRun(run_id=f"daily_krx_{day}", as_of=day, universe="u", universe_size=4, rules=["momentum_top_k"],
                     candidates=cands)


def _reserved(audit, day=D1, reruns=1):
    oid = f"paper-{day}-A.KS"
    audit.record_order(OrderRecord(order_id=oid, ticker="A.KS", trade_date=day, side="Buy", quantity=0,
                                   status="exception", exception_kind="review", exception_action="rerun",
                                   reruns=reruns, reason=dl.REVIEW_REASON))
    return oid


@pytest.mark.unit
def test_a_rerun_the_day_did_not_decide_is_decided_by_rule_from_the_screen_that_surfaced_it(store, tmp_path):
    audit = market_audit(tmp_path / "audit", "KRX")
    audit.record_screen(_screen(D1))
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=D1, rating="REVIEW", thesis="x",
                                         screen_run_id=f"daily_krx_{D1}"))
    oid = _reserved(audit)
    counts, notes = dl.answer_reruns(audit, "KRX", D2, "rule", tmp_path, None, None)
    assert counts == {"reruns_pending": 1, "reruns_done": 1} and notes == [f"{oid}: 재실행 1회째 → {D2} Buy"]
    (d,) = [d for d in audit.decisions() if d.trade_date == D2]
    assert (d.ticker, d.rating, d.action, d.screen_run_id, d.source) == ("A.KS", "Buy", "Buy", f"daily_krx_{D1}", "agent")
    assert d.settings == {"decider": "rule_momentum_baseline", "screen": dl.SETUPS["KRX"]["screen_note"],
                          "rerun_of": oid, "attempt": 1}
    assert "모멘텀 순위 1/4 (20일 +9%)" in d.thesis
    assert latest_orders(audit.orders())[oid].exception_resolved is True
    assert f"paper-{D2}-A.KS" not in latest_orders(audit.orders())       # a Buy: the executor's to place, not this


@pytest.mark.unit
def test_a_review_again_goes_back_on_the_queue_with_its_count_and_the_cap_still_holds(reviewed, monkeypatch):
    client, audit, ticker, oid, api = reviewed
    _rerun(api, oid)
    llm = FakeManager()
    monkeypatch.setattr(llm, "create", _refuse_on(llm, ticker))
    monkeypatch.setattr(dl, "agent_decide", _skip(dl.agent_decide, ticker))
    r = _decide(client, D2, audit.directory.parents[1], "llm", llm)
    # the day's run asked the others; the rerun asked the ticker first and was refused again
    assert r.status == "ok" and r.counts["reruns_done"] == 1 and llm.asked[0] != (ticker, "candidate")
    assert llm.asked[-1] == (ticker, "candidate") and r.counts["llm_calls"] == len(llm.asked)
    (d,) = [d for d in audit.decisions() if d.settings.get("rerun_of") == oid]
    assert (d.rating, d.trade_date, d.settings["attempt"], d.settings["decider"]) == ("REVIEW", D2, 1, "claude-opus-5-5")
    new = latest_orders(audit.orders())[f"paper-{D2}-{ticker}"]
    assert (new.status, new.exception_kind, new.reruns, new.quantity) == ("exception", "review", 1, 0)
    assert new.reason == f"{dl.REVIEW_REASON} · 재실행 1회째 결과"
    # off the queue at D1, on it at D2 with one rerun used: the second is the last
    items = {x["order_id"]: x for x in _queue(api)}
    assert oid not in items and items[new.order_id]["reruns"] == 1 and items[new.order_id]["pending_action"] is None
    assert _rerun(api, new.order_id, reason="다시")["attempt"] == 2
    assert _act(api, new.order_id, "rerun", "세 번째").status_code == 409
    assert {o.order_id for o in pending_reruns(audit.orders())} == _waiting(api) == {new.order_id}


def _refuse_on(llm, ticker):
    """The fake model refuses ``ticker``'s next question (and only it)."""
    real = llm.create

    def create(**kw):
        llm.refuse_first = f"종목: {ticker}" in kw["messages"][0]["content"]
        return real(**kw)
    return create


@pytest.mark.unit
def test_an_llm_rerun_past_the_call_cap_waits_for_the_next_decide_never_the_rule(reviewed, monkeypatch):
    client, audit, ticker, oid, api = reviewed
    _rerun(api, oid)
    llm, data = FakeManager(), audit.directory.parents[1]
    monkeypatch.setattr(dl, "agent_decide", _skip(dl.agent_decide, ticker))
    monkeypatch.setitem(dl.LLM, "max_calls_per_run", 2)
    r = _decide(client, D2, data, "llm", llm)
    # the day's run spends both calls; the rerun waits — no rule decision, no marker, still on the queue
    assert r.status == "ok" and r.counts["llm_calls"] == 2 and ticker not in {t for t, _ in llm.asked}
    assert r.counts["reruns_deferred"] == 1 and "reruns_done" not in r.counts
    assert r.notes[-1] == f"재실행 미룸 {oid} (1회째): max_calls_per_run(2)"
    assert not [d for d in audit.decisions() if d.settings.get("rerun_of") or (d.ticker, d.trade_date) == (ticker, D2)]
    assert [op for op in audit.operations() if op.operator == RERUN_OPERATOR] == [] and _waiting(api) == {oid}
    # the next decide with calls left answers it, and the executor places the model's Buy
    monkeypatch.setitem(dl.LLM, "max_calls_per_run", 50)
    client.today = D3
    assert dl.run_stage("KRX", "fill", D3, data, client, at(D3, "09:10")).status == "ok"
    llm.asked.clear()
    r3 = _decide(client, D3, data, "llm", llm)
    assert r3.counts["reruns_done"] == 1 and _waiting(api) == set() and llm.asked[-1] == (ticker, "candidate")
    (d,) = [d for d in audit.decisions() if d.settings.get("rerun_of") == oid]
    assert (d.trade_date, d.rating, d.settings["decider"], d.settings["attempt"]) == (D3, "Buy", "claude-opus-5-5", 1)
    assert latest_orders(audit.orders())[f"paper-{D3}-{ticker}"].status == "open"


@pytest.mark.unit
def test_no_model_key_puts_the_rerun_off_too(reviewed, monkeypatch):
    client, audit, ticker, oid, api = reviewed
    _rerun(api, oid)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("TRADING_ENV_FILE", str(audit.directory / "none.env"))
    monkeypatch.setattr(dl, "agent_decide", _skip(dl.agent_decide, ticker))
    r = _decide(client, D2, audit.directory.parents[1], "llm", None)
    assert r.counts["reruns_deferred"] == 1 and r.notes[-1].startswith(f"재실행 미룸 {oid} (1회째): client: ")
    assert "ANTHROPIC_API_KEY" in r.notes[-1]
    assert not [d for d in audit.decisions() if d.settings.get("rerun_of")] and _waiting(api) == {oid}


@pytest.mark.unit
def test_a_hold_closes_the_reservation_and_the_loop_leaves_it(reviewed):
    client, audit, _, oid, api = reviewed
    _rerun(api, oid)
    assert _act(api, oid, "hold", "보유").status_code == 200
    assert pending_reruns(audit.orders()) == [] and _waiting(api) == set()
    r = _decide(client, D2, audit.directory.parents[1], "rule", NoManager())
    assert r.status == "ok" and "reruns_pending" not in r.counts
    assert not [d for d in audit.decisions() if d.settings.get("rerun_of")]
    assert [op for op in audit.operations() if op.operator == RERUN_OPERATOR] == []


@pytest.mark.unit
def test_a_rerun_of_the_day_itself_waits_and_one_answered_but_not_closed_is_closed_once(store, tmp_path):
    audit = market_audit(tmp_path / "audit", "KRX")
    audit.record_screen(_screen(D2))
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=D2, rating="REVIEW", thesis="x",
                                         screen_run_id=f"daily_krx_{D2}"))
    oid = _reserved(audit, D2)
    counts, notes = dl.answer_reruns(audit, "KRX", D2, "rule", tmp_path, None, None)
    assert counts == {"reruns_pending": 1, "reruns_deferred": 1}
    assert notes == [f"재실행 미룸 {oid} (1회째): 결정일 당일 — 다음 결정일에"]
    # its marker is there (a crash before the closing line): the next decide only closes it
    audit.record_operation(OperationRecord(kind="exception_action", operator=RERUN_OPERATOR, role="auto",
                                           reason=f"재실행 1회째 → {D3} Buy", market="KRX", target=oid,
                                           after={"action": "rerun", "attempt": 1, "pending": False}))
    n = (len(audit.decisions()), len(audit.operations()))
    assert dl.answer_reruns(audit, "KRX", D3, "rule", tmp_path, None, None)[0] == {"reruns_pending": 1, "reruns_done": 1}
    closed = latest_orders(audit.orders())[oid]
    assert closed.exception_resolved is True and closed.reason.endswith(f"· 재실행 1회째 → {D3} Buy")
    assert (len(audit.decisions()), len(audit.operations())) == n
    assert dl.answer_reruns(audit, "KRX", D3, "rule", tmp_path, None, None) == ({}, [])


@pytest.mark.unit
def test_an_answered_rerun_is_never_decided_twice(store, tmp_path):
    """A crash after the answer's decision: the next decide finds it by (rerun_of, attempt) and asks nobody."""
    audit = market_audit(tmp_path / "audit", "KRX")
    audit.record_screen(_screen(D1))
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=D1, rating="REVIEW", thesis="x",
                                         screen_run_id=f"daily_krx_{D1}"))
    oid = _reserved(audit)
    audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=D2, rating="REVIEW", thesis="y",
                                         settings={"decider": "claude-opus-5-5", "rerun_of": oid, "attempt": 1}))
    counts, _ = dl.answer_reruns(audit, "KRX", D2, "llm", tmp_path, None, None, llm=NoManager())
    assert counts == {"reruns_pending": 1, "reruns_done": 1} and len(audit.decisions()) == 2
    again = latest_orders(audit.orders())[f"paper-{D2}-A.KS"]                # its REVIEW, back on the queue
    assert (again.exception_kind, again.reruns) == ("review", 1)


@pytest.mark.unit
def test_each_book_answers_only_its_own_reruns(store, tmp_path):
    books = {m: market_audit(tmp_path / "audit", m) for m in ("KRX", "US")}
    for m, a in books.items():
        t = "A.KS" if m == "KRX" else "AAA"
        a.record_order(OrderRecord(order_id=f"paper-{D1}-{t}", ticker=t, trade_date=D1, side="Buy", quantity=0,
                                   market=m, status="exception", exception_kind="review", exception_action="rerun",
                                   reruns=1))
    assert [o.ticker for o in pending_reruns(books["KRX"].orders())] == ["A.KS"]
    assert [o.ticker for o in pending_reruns(books["US"].orders())] == ["AAA"]
    # KRX's decide finds no decision behind its REVIEW: it waits; the US book is untouched
    counts, notes = dl.answer_reruns(books["KRX"], "KRX", D2, "rule", tmp_path, None, None)
    assert counts == {"reruns_pending": 1, "reruns_deferred": 1} and notes[0].endswith("원 선별 (결정 없음) 없음")
    assert books["US"].operations() == [] and len(books["US"].orders()) == 1


@pytest.mark.unit
def test_no_reservation_leaves_the_decide_stage_as_it_was(store, tmp_path):
    r = _decide(_market(), D1, tmp_path, "rule", NoManager())
    assert r.status == "ok" and not any(k.startswith("reruns") for k in r.counts)
    assert not any("재실행" in n for n in r.notes)
    assert dl.answer_reruns(market_audit(tmp_path / "audit", "KRX"), "KRX", D2, "rule", tmp_path, None, None) == ({}, [])


@pytest.mark.unit
def test_the_memory_names_a_persons_decision_and_keeps_the_rest_as_they_were():
    def d(source, decider, rating, day):
        return DecisionRecord(ticker="A.KS", trade_date=day, rating=rating, thesis=f"{rating} 근거", source=source,
                              settings={"decider": decider} if decider else {})
    past = [d("agent", "claude-opus-5-5", "Buy", "2026-08-01"), d("agent", "rule_momentum_baseline", "Sell", "2026-08-02"),
            d("human", "human", "Hold", "2026-08-03"), d("rule", None, "Sell", "2026-08-03")]
    assert dl.memory_text("A.KS", D1, past).splitlines() == [
        "- 2026-08-01 Buy (매니저): Buy 근거", "- 2026-08-02 Sell (규칙): Sell 근거",
        "- 2026-08-03 Hold (사람): Hold 근거", "- 2026-08-03 Sell (규칙): Sell 근거"]


@pytest.mark.parametrize("cause", ["no_key", "cap"])
@pytest.mark.unit
def test_the_days_rule_stand_in_for_the_ticker_is_no_answer_the_rerun_waits(reviewed, monkeypatch, cause):
    """Review 1R #1: the day's llm run itself gave the ticker the rule (no key, past the cap) — not an answer."""
    client, audit, ticker, oid, api = reviewed
    _rerun(api, oid)
    if cause == "no_key":
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("TRADING_ENV_FILE", str(audit.directory / "none.env"))
        llm = None
    else:
        llm = FakeManager()
        monkeypatch.setitem(dl.LLM, "max_calls_per_run", 0)
    r = _decide(client, D2, audit.directory.parents[1], "llm", llm)
    (stand_in,) = [d for d in audit.decisions() if (d.ticker, d.trade_date) == (ticker, D2)]   # one, the day's
    assert stand_in.settings["fallback"] == "rule"
    assert r.counts["reruns_deferred"] == 1 and "reruns_done" not in r.counts
    assert r.notes[-1].startswith(f"재실행 미룸 {oid} (1회째): 그날 결정이 규칙 대체(")
    assert [op for op in audit.operations() if op.operator == RERUN_OPERATOR] == [] and _waiting(api) == {oid}


@pytest.mark.unit
def test_two_reservations_of_one_ticker_get_one_decision_of_the_day(store, tmp_path):
    """Review 1R #2: the second takes the answer the first made as the day's decision."""
    audit = market_audit(tmp_path / "audit", "KRX")
    for day in (D1, D2):
        audit.record_screen(_screen(day))
        audit.record_decision(DecisionRecord(ticker="A.KS", trade_date=day, rating="REVIEW", thesis="x",
                                             screen_run_id=f"daily_krx_{day}"))
    first, second = _reserved(audit, D1), _reserved(audit, D2)
    counts, notes = dl.answer_reruns(audit, "KRX", D3, "rule", tmp_path, None, None)
    assert counts == {"reruns_pending": 2, "reruns_done": 2}
    assert notes == [f"{first}: 재실행 1회째 → {D3} Buy", f"{second}: 재실행 1회째 → {D3} Buy (그날 결정)"]
    assert len([d for d in audit.decisions() if d.trade_date == D3]) == 1
    assert sorted(op.target for op in audit.operations()) == [first, second]
    assert pending_reruns(audit.orders()) == []


@pytest.mark.unit
def test_a_persons_reservation_under_the_loops_name_is_not_its_marker(store, tmp_path):
    """Review 1R #3: the API takes any operator string; a reservation says pending: true and role admin."""
    audit = market_audit(tmp_path / "audit", "KRX")
    oid = _reserved(audit)
    (o,) = pending_reruns(audit.orders())
    audit.record_operation(OperationRecord(kind="exception_action", operator=RERUN_OPERATOR, role="admin", reason="r",
                                           market="KRX", target=oid,
                                           after={"action": "rerun", "scored": False, "attempt": 1, "pending": True}))
    assert pending_rerun_marker(audit.operations(), o) is None
