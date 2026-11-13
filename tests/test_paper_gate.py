"""The paper-trading gate (screen 8, docs/05 §4, docs/10 §3 단계 2): the six PAPER_TRADING criteria judged by the
server from the market's own paper book, the live switch that only takes an eligible market, and the daily-loss halt
that ends when the next trading day opens (2026-11-13 user decision: judged on the close, lifted at the open)."""

import importlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent_trading_core.audit import DecisionRecord, OperationRecord, OrderRecord, market_audit
from agent_trading_core.execution import (
    ExecutionRefused,
    PaperConfig,
    Prices,
    effective_state,
    execute_date,
    paper_expected,
    paper_progress,
)

KST, NY = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")
CAL = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", "2026-08-07")]
# no costs, so every equity below is worked out by hand: 1,000,000 cash, 500 shares bought at 1,000 on 06-02
CFG = PaperConfig(initial_cash=1_000_000.0, commission=0.0, slippage=0.0, sell_tax=0.0, regime_filter=False,
                  index="IDX", market="KRX")


def _frame(closes: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({"Open": list(closes.values()), "Close": list(closes.values())}, index=pd.Index(list(closes), dtype=object))


def _prices(moves: dict[str, float] | None = None) -> Prices:
    """AAA closes at 1,000 every day except ``moves``; the index rises one point a day from 100."""
    return Prices(frames={"IDX": _frame({d: 100.0 + i for i, d in enumerate(CAL)}),
                          "AAA.KS": _frame({d: (moves or {}).get(d, 1000.0) for d in CAL})})


def _buy(ticker="AAA.KS", date="2026-06-01", q=500) -> OrderRecord:
    return OrderRecord(order_id=f"paper-{date}-{ticker}", ticker=ticker, trade_date=date, side="Buy", quantity=q,
                       filled_quantity=q, filled_price=1000.0, status="filled", fill_date="2026-06-02")


def _dec(ticker, date, rating="Buy", source="agent") -> DecisionRecord:
    return DecisionRecord(ticker=ticker, trade_date=date, rating=rating, thesis=f"{rating} {ticker}", source=source)


# 19 agent decisions with a rating (the buy + 18 holds) and one REVIEW: 1 / 20 = 5.0%, the limit itself
DECISIONS = [_dec("AAA.KS", "2026-06-01")] + [_dec(f"H{i:02d}.KS", "2026-06-10", "Hold") for i in range(18)] \
    + [_dec("R01.KS", "2026-06-12", "REVIEW")]


def _criteria(p: dict) -> dict:
    return {c["id"]: c for c in p["criteria"]}


# --- the six criteria, by hand ---------------------------------------------------------------------------

@pytest.mark.unit
def test_every_criterion_met_at_exactly_eight_weeks_and_the_limits_themselves_pass():
    # 06-03 closes at 960: 500,000 cash + 500 × 960 = 980,000, a −2.00% day (a breach: ≤ the limit, risk.EPS)
    p = paper_progress([_buy()], DECISIONS, [], _prices({"2026-06-03": 960.0}), CFG, as_of="2026-07-27")
    c = _criteria(p)
    assert (p["started"], p["start"], p["days"], p["weeks"], p["judge_date"]) == (True, "2026-06-01", 56, 8, "2026-07-27")
    assert {k: v["status"] for k, v in c.items()} == dict.fromkeys(c, "met")
    assert p["eligible"] is True and p["counts"] == {"met": 6, "unmet": 0, "in_progress": 0}
    assert c["max_drawdown"]["value"] == pytest.approx(980_000 / 1_000_000 - 1)
    assert c["max_drawdown"]["value_ko"] == "-2.0% (2026-06-03)" and c["max_drawdown"]["note_ko"] == "현재 +0.0%"
    assert c["max_daily_loss_breaches"]["value"] == 1 and c["max_daily_loss_breaches"]["note_ko"] == "1회 더 남음"
    assert (c["max_review_rate"]["numerator"], c["max_review_rate"]["denominator"]) == (1, 20)
    assert c["max_review_rate"]["value"] == pytest.approx(0.05) and c["max_review_rate"]["value_ko"] == "5.0% (1 / 20)"
    assert c["max_stop_outs"]["value"] == 0 and c["max_unexplained_orders"]["value"] == 0
    # a REVIEW and a −2% day break nothing yet but are listed, newest first
    assert [(v["date"], v["kind"]) for v in p["violations"]] == [("2026-06-12", "review"), ("2026-06-03", "daily_loss")]
    assert p["violations"][0]["ref"] == {"type": "decision", "date": "2026-06-12", "ticker": "R01.KS"}
    assert p["series"][0] == {"date": "2026-06-01", "equity": 1_000_000.0, "drawdown": 0.0}
    assert p["series"][2] == {"date": "2026-06-03", "equity": 980_000.0, "drawdown": pytest.approx(-0.02)}
    assert len(p["series"]) == CAL.index("2026-07-27") + 1
    # the figures the reference table compares
    assert p["paper"]["new_buys"] == 1 and p["paper"]["period_return"] == pytest.approx(0.0)
    assert p["paper"]["index_return"] == pytest.approx((100 + CAL.index("2026-07-27")) / 100 - 1)


@pytest.mark.unit
def test_one_day_short_of_eight_weeks_is_in_progress_and_not_eligible():
    p = paper_progress([_buy()], DECISIONS, [], _prices(), CFG, as_of="2026-07-26")            # 55 days
    c = _criteria(p)
    assert (p["days"], p["weeks"]) == (55, 7)
    assert c["min_weeks"]["status"] == "in_progress" and c["min_weeks"]["note_ko"] == "1일 남음 · 2026-07-27 장 마감 후 판정"
    assert p["eligible"] is False and p["counts"] == {"met": 5, "unmet": 0, "in_progress": 1}


@pytest.mark.unit
@pytest.mark.parametrize(("close", "status"), [(800.0, "unmet"), (801.0, "met")])
def test_a_drawdown_of_exactly_ten_percent_fails(close, status):
    # 500,000 + 500 × 800 = 900,000: −10.0% from 1,000,000 fails (≤ the limit); at 801 it is −9.95%
    p = paper_progress([_buy()], DECISIONS, [], _prices({"2026-06-03": close}), CFG, as_of="2026-07-27")
    dd = _criteria(p)["max_drawdown"]
    assert dd["value"] == pytest.approx((500_000 + 500 * close) / 1_000_000 - 1) and dd["status"] == status
    assert p["eligible"] is (status == "met")


@pytest.mark.unit
@pytest.mark.parametrize(("close", "breaches"), [(960.0000001, 1), (960.00001, 0)])
def test_a_day_at_the_loss_limit_within_float_noise_is_a_breach(close, breaches):
    # 500,000 + 500 × 960.0000001 = 980,000.00005: −1.999999995%, within risk.EPS of −2% (the executor halts on it);
    # 960.00001 is −1.9999995%, past EPS
    c = _criteria(paper_progress([_buy()], DECISIONS, [], _prices({"2026-06-03": close}), CFG, as_of="2026-07-27"))
    assert c["max_daily_loss_breaches"]["value"] == breaches


@pytest.mark.unit
def test_a_third_daily_loss_breach_fails():
    # three −2.00% days, each from a 1,000,000 close
    moves = {"2026-06-03": 960.0, "2026-06-05": 960.0, "2026-06-09": 960.0}
    c = _criteria(paper_progress([_buy()], DECISIONS, [], _prices(moves), CFG, as_of="2026-07-27"))
    assert c["max_daily_loss_breaches"]["value"] == 3 and c["max_daily_loss_breaches"]["status"] == "unmet"
    assert c["max_daily_loss_breaches"]["note_ko"] == "허용 횟수 초과"
    assert c["max_daily_loss_breaches"]["value_ko"] == "3회 (2026-06-09 -2.00%)"


@pytest.mark.unit
def test_a_dd_stop_inside_the_period_fails_and_one_outside_it_does_not_count():
    def stop(date):
        return OperationRecord(kind="halt", operator="system", role="auto", reason="드로다운 정지", target="dd_stop",
                               after={"running": False, "date": date, "equity": 790_000.0, "peak": 1_000_000.0})
    p = paper_progress([_buy()], DECISIONS, [stop("2026-05-29"), stop("2026-06-15")], _prices(), CFG, as_of="2026-07-27")
    assert _criteria(p)["max_stop_outs"]["value"] == 1 and _criteria(p)["max_stop_outs"]["status"] == "unmet"
    (v,) = [v for v in p["violations"] if v["kind"] == "dd_stop"]
    assert v["date"] == "2026-06-15" and v["value"] == pytest.approx(-0.21)


@pytest.mark.unit
def test_a_second_review_past_five_percent_fails_and_rule_exits_are_not_in_the_rate():
    decisions = DECISIONS + [_dec("R02.KS", "2026-06-15", "REVIEW"), _dec("AAA.KS", "2026-06-16", "Sell", source="rule")]
    c = _criteria(paper_progress([_buy()], decisions, [], _prices(), CFG, as_of="2026-07-27"))["max_review_rate"]
    assert (c["numerator"], c["denominator"]) == (2, 21) and c["value"] == pytest.approx(2 / 21)
    assert c["status"] == "unmet"


@pytest.mark.unit
def test_no_agent_decision_yet_leaves_the_review_rate_in_progress():
    c = _criteria(paper_progress([_buy()], [], [], _prices(), CFG, as_of="2026-07-27"))
    assert c["max_review_rate"]["status"] == "in_progress" and c["max_review_rate"]["value"] is None


@pytest.mark.unit
def test_an_order_without_a_decision_fails_the_audit_criterion():
    stray = OrderRecord(order_id="paper-2026-06-10-ZZZ.KS", ticker="ZZZ.KS", trade_date="2026-06-10", side="Buy",
                        quantity=0, status="blocked", reason="x")
    p = paper_progress([_buy(), stray], DECISIONS, [], _prices(), CFG, as_of="2026-07-27")
    c = _criteria(p)["max_unexplained_orders"]
    assert c["value"] == 1 and c["status"] == "unmet" and c["note_ko"] == "주문 2건 중 1건이 결정 기록과 연결"
    assert p["eligible"] is False
    (v,) = [v for v in p["violations"] if v["kind"] == "unexplained_order"]
    assert v["ref"] == {"type": "order", "order_id": "paper-2026-06-10-ZZZ.KS", "date": "2026-06-10"}


@pytest.mark.unit
def test_without_records_the_market_has_not_started():
    p = paper_progress([], [], [], _prices(), CFG)
    assert (p["started"], p["start"], p["eligible"], p["series"], p["violations"]) == (False, None, False, [], [])
    assert {c["status"] for c in p["criteria"]} == {"in_progress"} and p["counts"]["in_progress"] == 6


@pytest.mark.unit
def test_with_no_order_yet_the_switch_to_paper_starts_the_period_on_the_markets_own_date():
    op = OperationRecord(kind="mode_change", at="2026-06-01T23:30:00Z", operator="a", role="admin", reason="r",
                         target="mode", before={"mode": "auto"}, after={"mode": "paper"})
    assert paper_progress([], [], [op], _prices(), CFG)["start"] == "2026-06-02"                  # 08:30 KST
    assert paper_progress([], [], [op], _prices(), PaperConfig(index="IDX", market="US"))["start"] == "2026-06-01"


@pytest.mark.unit
def test_the_reference_table_against_a_reproduction_by_hand():
    equity = pd.DataFrame({"equity": [100.0, 110.0, 99.0, 104.5], "kospi": [100.0, 101.0, 102.0, 103.0],
                           "exposure": [0.0, 0.5, 0.7, 0.6]}, index=["2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03"])
    events = [{"date": "2026-05-29", "kind": "rebalance", "text": "선별 40→20순위, 목표 10종목, 신규 매수 9"},
              {"date": "2026-06-01", "kind": "rebalance", "text": "선별 40→21순위, 목표 10종목, 신규 매수 3 · 개입 1"},
              {"date": "2026-06-02", "kind": "exit", "text": "AAA.KS stop_loss -8.1%: 손절"},
              {"date": "2026-06-03", "kind": "exit", "text": "BBB.KS trailing +3.0%: 트레일링"},
              {"date": "2026-06-02", "kind": "guard", "text": "일일 손실 한도: -10.00% ≤ -2% → 다음 세션 신규 매수 중단"}]
    paper = {"mean_exposure": 0.5, "new_buys": 2, "new_buys_open": 1, "exits_by_reason": {"stop_loss": 2},
             "period_return": 0.01, "max_drawdown": -0.05, "daily_loss_dates": [], "index_return": 0.03}
    t = paper_expected(paper, "2026-06-01", "2026-06-03", equity, events, "코스피")
    rows = {r["id"]: r for r in t["rows"]}
    assert t["reference_only"] is True and t["error"] is None and t["covered_until"] is None
    assert rows["mean_exposure"]["reference"] == pytest.approx(0.6) and rows["mean_exposure"]["diff"] == pytest.approx(-0.1)
    assert (rows["new_buys"]["reference"], rows["new_buys"]["diff"], rows["new_buys"]["paper_open"]) == (3, -1, 1)
    assert rows["exits"]["reference_by_reason"] == {"stop_loss": 1, "trailing": 1} and rows["exits"]["diff"] == 0
    assert rows["period_return"]["reference"] == pytest.approx(0.045)                           # 104.5 / 100 (05-29)
    assert rows["max_drawdown"]["reference"] == pytest.approx(99 / 110 - 1)
    assert rows["daily_loss_guard"]["reference_dates"] == ["2026-06-02"] and rows["daily_loss_guard"]["diff"] == -1
    assert rows["index_return"]["reference"] == pytest.approx(0.03) and rows["index_return"]["diff"] is None
    assert rows["index_return"]["label_ko"] == "같은 기간 코스피"
    # a reproduction that ends before the paper period: nothing to compare, said so
    late = paper_expected(paper, "2026-07-01", "2026-07-03", equity, events, "코스피")
    assert late["rows"] == [] and "2026-06-03까지" in late["error"]


# --- the daily-loss halt ends when the next trading day opens -----------------------------------------------

def _halt(date, operator="system", target="daily_loss"):
    return OperationRecord(kind="halt", operator=operator, role="auto", reason="일일 손실", target=target,
                           after={"running": False, "keep_exits": True, "date": date})


@pytest.mark.unit
def test_a_daily_loss_halt_stands_through_its_day_and_ends_at_the_next_trading_days_midnight():
    cal = ["2026-06-01", "2026-06-02", "2026-06-04"]                        # 06-03 is a holiday
    ops = [_halt("2026-06-02")]
    st = effective_state(ops, cal, "KRX", datetime(2026, 6, 3, 23, 59, tzinfo=KST))
    assert st["running"] is False and st["auto_resume_at"] == "2026-06-04T00:00:00+09:00" and st["auto_resumed"] is False
    st = effective_state(ops, cal, "KRX", datetime(2026, 6, 4, 0, 0, tzinfo=KST))
    assert st["running"] is True and st["auto_resumed"] is True and st["halt_target"] is None


@pytest.mark.unit
def test_past_the_price_data_a_friday_halt_ends_on_monday_in_the_markets_own_time():
    cal = ["2026-06-04", "2026-06-05"]
    st = effective_state([_halt("2026-06-05")], cal, "US", datetime(2026, 6, 7, 12, 0, tzinfo=NY))
    assert st["running"] is False and st["auto_resume_at"] == "2026-06-08T00:00:00-04:00"
    assert effective_state([_halt("2026-06-05")], cal, "US", datetime(2026, 6, 8, 0, 0, tzinfo=NY))["running"] is True


@pytest.mark.unit
@pytest.mark.parametrize("ops", [[_halt("2026-06-02"), _halt("2026-06-02", operator="운영자", target="manual")],
                                 [_halt("2026-06-02", target="dd_stop")]])
def test_a_persons_halt_or_a_dd_stop_does_not_end_at_the_open(ops):
    st = effective_state(ops, ["2026-06-02", "2026-06-04"], "KRX", datetime(2026, 6, 10, tzinfo=KST))
    assert st["running"] is False and st["auto_resume_at"] is None


@pytest.mark.unit
def test_with_the_cache_ending_on_the_halt_day_a_weekday_holiday_is_not_known():
    """The documented limit (docs/04 auto_resume_at): while the halt stands the price cache usually ends on the halt
    day, so the next trading day is the next weekday; a weekday holiday after it reads as running from its 00:00."""
    st = effective_state([_halt("2026-06-02")], ["2026-06-01", "2026-06-02"], "KRX", datetime(2026, 6, 3, 9, 0, tzinfo=KST))
    assert st["auto_resume_at"] == "2026-06-03T00:00:00+09:00" and st["running"] is True      # 06-03 may be a holiday


# --- the API: progress per market, and the live switch behind the gate ---------------------------------------

def _cache(data, ticker, closes):
    cache = data / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Volume"] + [f"{d} 00:00:00+09:00,{c},{c},{c},{c},1000" for d, c in closes.items()]
    (cache / f"{ticker}-YFin-data.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _api(tmp_path, monkeypatch, until, token=None):
    """A KRX paper book from 06-01 with prices to ``until``: one agent buy, filled; the US book is empty."""
    data = tmp_path / "data"
    days = [d for d in CAL if d <= until]
    _cache(data, "^KS11", {d: 100.0 + i for i, d in enumerate(days)})
    _cache(data, "AAA.KS", dict.fromkeys(days, 10_000.0))
    audit = market_audit(data / "audit", "KRX")
    audit.record_decision(_dec("AAA.KS", "2026-06-01"))
    audit.record_order(OrderRecord(order_id="paper-2026-06-01-AAA.KS", ticker="AAA.KS", trade_date="2026-06-01", side="Buy",
                                   quantity=1000, filled_quantity=1000, filled_price=10_000.0, status="filled",
                                   fill_date="2026-06-02"))
    monkeypatch.setenv("TRADING_DATA_DIR", str(data))
    if token:
        monkeypatch.setenv("TRADING_ADMIN_TOKEN", token)
    else:
        monkeypatch.delenv("TRADING_ADMIN_TOKEN", raising=False)
    from backend import main
    importlib.reload(main)
    return TestClient(main.app), data


LIVE = {"operator": "관리자", "reason": "모의 8주 통과", "market": "KRX", "account_mode": "live", "confirmed": True}


@pytest.mark.unit
def test_progress_is_served_per_market_with_its_labels(tmp_path, monkeypatch):
    c, _ = _api(tmp_path, monkeypatch, until="2026-07-24")
    p = c.get("/api/paper/progress").json()
    assert (p["market"], p["started"], p["weeks"], p["eligible"], p["verdict_ko"]) == ("KRX", True, 7, False, "전환 불가")
    assert p["summary_ko"] == "충족 5 · 미충족 0 · 진행 중 1 (최소 모의 운용 기간(주))"
    assert p["weeks_badge_ko"] == "7/8주"
    first = p["criteria"][0]
    assert (first["label_ko"], first["threshold_ko"], first["status_ko"]) == ("최소 모의 운용 기간(주)", "≥ 8주", "진행 중")
    assert [x["threshold_ko"] for x in p["criteria"][1:]] == ["> −10%", "≤ 2회", "0회", "≤ 5%", "0건"]   # −10.0% fails
    assert p["limits"] == {"max_drawdown": -0.10, "dd_halve": -0.10, "dd_stop": -0.20}
    # no 2nd measurement in this data dir: the reference table says so, and the verdict never reads it
    assert p["expected"]["reference_only"] is True and p["expected"]["rows"] == [] and "2차 실측" in p["expected"]["error"]
    us = c.get("/api/paper/progress", params={"market": "US"}).json()
    assert (us["started"], us["eligible"], us["weeks_badge_ko"], us["expected"]) == (False, False, "0/8주", None)
    assert c.get("/api/paper/progress", params={"market": "JP"}).status_code == 400


@pytest.mark.unit
def test_the_live_switch_is_refused_until_every_criterion_holds_whichever_field_asks(tmp_path, monkeypatch):
    c, data = _api(tmp_path, monkeypatch, until="2026-07-24")
    r = c.post("/api/trading/mode", json=LIVE)
    assert r.status_code == 409
    assert r.json()["detail"] == ("국내 모의투자 통과 기준 미충족 — 실거래로 전환할 수 없습니다: "
                                  "최소 모의 운용 기간(주)(진행 중 · 7주 (2026-06-01 시작))")
    # the stored-mode field cannot go round the gate
    legacy = {k: v for k, v in LIVE.items() if k != "account_mode"} | {"mode": "manual"}
    assert c.post("/api/trading/mode", json=legacy).status_code == 409
    assert c.get("/api/trading/mode").json()["mode"] == "paper"
    assert market_audit(data / "audit", "KRX").operations() == []                       # nothing written
    # the empty US book has not started: refused as well
    assert c.post("/api/trading/mode", json=LIVE | {"market": "US"}).status_code == 409


@pytest.mark.unit
def test_the_live_switch_needs_the_confirmation_and_one_axis(tmp_path, monkeypatch):
    c, _ = _api(tmp_path, monkeypatch, until="2026-07-31")
    unconfirmed = {k: v for k, v in LIVE.items() if k != "confirmed"}
    r = c.post("/api/trading/mode", json=unconfirmed)
    assert r.status_code == 422 and "confirmed" in r.json()["detail"]
    assert c.post("/api/trading/mode", json=LIVE | {"mode": "auto"}).status_code == 422                # both axes
    assert c.post("/api/trading/mode", json={k: v for k, v in LIVE.items() if k != "account_mode"}).status_code == 422


@pytest.mark.unit
def test_an_eligible_market_goes_live_alone_with_the_gate_on_record_and_still_places_no_order(tmp_path, monkeypatch):
    c, data = _api(tmp_path, monkeypatch, until="2026-07-31")                           # 60 days: 8 weeks
    assert c.get("/api/paper/progress").json()["eligible"] is True
    r = c.post("/api/trading/mode", json=LIVE)
    assert r.status_code == 200
    st = r.json()["state"]
    assert (st["mode"], st["account_mode"], st["account_mode_ko"], st["execution_mode"]) == ("auto", "live", "실거래", "auto")
    assert "ExecutionRefused" in r.json()["note"]
    (op,) = market_audit(data / "audit", "KRX").operations()
    gate = op.before["paper_gate"]
    assert (op.before["mode"], op.after) == ("paper", {"mode": "auto", "account_mode": "live"})
    assert (gate["eligible"], gate["weeks"], gate["as_of"]) == (True, 8, "2026-07-31")
    assert {v["status"] for v in gate["criteria"].values()} == {"met"} and len(gate["criteria"]) == 6
    # the other market is untouched
    assert c.get("/api/trading/mode", params={"market": "US"}).json()["account_mode"] == "paper"
    assert c.get("/api/paper/progress", params={"market": "US"}).json()["started"] is False
    # there is no live order path: the executor refuses a live market
    with pytest.raises(ExecutionRefused, match="모의투자"):
        execute_date(market_audit(data / "audit", "KRX"), "2026-07-31", Prices(data / "cache"))
    # back to paper needs no gate
    back = c.post("/api/trading/mode", json=LIVE | {"account_mode": "paper"})
    assert back.status_code == 200 and back.json()["state"]["account_mode"] == "paper" and back.json()["note"] is None


@pytest.mark.unit
def test_the_live_switch_is_admin_only_when_a_token_is_set(tmp_path, monkeypatch):
    c, _ = _api(tmp_path, monkeypatch, until="2026-07-31", token="s3cret")
    assert c.post("/api/trading/mode", json=LIVE).status_code == 403
    assert c.post("/api/trading/mode", json=LIVE, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.post("/api/trading/mode", json=LIVE, headers={"X-Admin-Token": "s3cret"}).status_code == 200


@pytest.mark.unit
def test_the_reference_table_never_moves_the_verdict(tmp_path, monkeypatch):
    c, data = _api(tmp_path, monkeypatch, until="2026-07-24")
    before = c.get("/api/paper/progress").json()
    out = data / "results" / "paper"
    out.mkdir(parents=True)
    # a reproduction far from the paper run: huge exposure, a −30% drawdown, 9 daily-loss guards
    days = [d for d in CAL if d <= "2026-07-24"]
    pd.DataFrame({"Date": days, "equity": [100.0 - 30.0 * (d >= "2026-07-01") for d in days],
                  "kospi": [100.0] * len(days), "exposure": [0.99] * len(days)}).to_csv(out / "eq.csv", index=False)
    events = [{"date": d, "kind": "guard", "text": "일일 손실 한도: -5% ≤ -2%"} for d in days[:9]]
    (out / "summary.json").write_text(json.dumps({"variants": {"rules_on_regime": {"equity_csv": "eq.csv", "events": events}}}),
                                      encoding="utf-8")
    after = c.get("/api/paper/progress").json()
    rows = {r["id"]: r for r in after["expected"]["rows"]}
    assert rows["max_drawdown"]["reference"] == pytest.approx(-0.30) and rows["daily_loss_guard"]["reference"] == 9
    assert after["expected"]["measurement_ko"] == "2차 실측" and after["expected"]["variant_ko"] == "하드 룰 + 국면 필터"
    assert (after["eligible"], after["counts"], after["criteria"]) == (before["eligible"], before["counts"], before["criteria"])


@pytest.mark.unit
@pytest.mark.parametrize("csv", ["Date,equity,exposure\n2026-07-01,100,0.5\n",               # no index column
                                 "Date,equity,kospi,exposure\n2026-07-01,abc,100,0.5\n"])     # not a number
def test_a_broken_reference_is_the_tables_error_and_never_blocks_the_switch(tmp_path, monkeypatch, csv):
    c, data = _api(tmp_path, monkeypatch, until="2026-07-31")                           # eligible
    out = data / "results" / "paper"
    out.mkdir(parents=True)
    (out / "eq.csv").write_text(csv, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"variants": {"rules_on_regime": {"equity_csv": "eq.csv", "events": [{"kind": "guard"}]}}}),
                                      encoding="utf-8")
    p = c.get("/api/paper/progress")
    assert p.status_code == 200 and p.json()["eligible"] is True
    assert p.json()["expected"]["rows"] == [] and p.json()["expected"]["error"].startswith("2차 실측 결과를 읽지 못함(")
    assert c.post("/api/trading/mode", json=LIVE).status_code == 200


@pytest.mark.unit
def test_the_state_serves_when_a_daily_loss_halt_ends(tmp_path, monkeypatch):
    c, data = _api(tmp_path, monkeypatch, until="2026-06-05")
    market_audit(data / "audit", "KRX").record_operation(_halt("2026-06-04").model_copy(update={"market": "KRX"}))
    from backend import main
    monkeypatch.setattr(main, "_clock", lambda: datetime(2026, 6, 4, 22, 0, tzinfo=KST))
    st = c.get("/api/trading/mode").json()
    assert (st["running"], st["auto_resume_at"], st["halt_target"]) == (False, "2026-06-05T00:00:00+09:00", "daily_loss")
    monkeypatch.setattr(main, "_clock", lambda: datetime(2026, 6, 5, 0, 0, tzinfo=KST))
    st = c.get("/api/trading/mode").json()
    assert (st["running"], st["auto_resumed"], st["label_ko"]) == (True, True, "국내 모의투자 모드")
