"""결정 카드 v0.8 (feature/decision-card-v08): the decision text read back field by field for the card — the API only
(``backend.main.thesis_card``); the record and its text are not changed. A memo and a sell review as the renderers
write them, a memo with fields missing or unknown, a text no label fits (kept whole), REVIEW's reason and raw text; and
on the API, both stores, the card beside the untouched ``thesis`` with the price slot's facts from the book."""
from __future__ import annotations

import pytest

from agent_trading_core.audit import DecisionRecord, EvidenceItem, OrderRecord, market_audit
from agent_trading_core.schemas import (
    Catalyst,
    InvalidationCheck,
    InvestmentMemo,
    PortfolioRating,
    SellAction,
    SellReason,
    SellReview,
    render_investment_memo,
    render_sell_review,
)
from backend.main import thesis_card
from tests.test_db_store_api import _client, _kis, store  # noqa: F401 (the fixture)

MEMO = InvestmentMemo(
    business_summary="결제망 운영. 수수료로 번다.", thesis="결제 건수 성장이 이어진다.\n해외 결제 비중이 는다.",
    bull_case="해외 결제가 빨라진다", bear_case="수수료 규제 입법", valuation_basis="선행 PER 27배",
    current_price=400.0, price_target=480.0, downside_price=360.0,
    catalysts=[Catalyst(event="1분기 실적", expected_by="2027-01-28"), Catalyst(event="연말 결제 데이터")],
    invalidation=["결제 성장 한 자릿수", "법안 통과"], rating=PortfolioRating.BUY, conviction=0.62, sizing_pct=8.0,
    exit_plan="목표가에서 절반 축소", time_horizon="6-12 months")
REVIEW = SellReview(
    checks=[InvalidationCheck(condition="영업이익률 12% 미만", met=True, evidence="3분기 11.2%"),
            InvalidationCheck(condition="점유율 하락", met=False, evidence="변화 없음")],
    reasons=[SellReason.THESIS_BROKEN, SellReason.OVERWEIGHT], reasoning="조건 하나가 충족됐다.",
    action=SellAction.TRIM, trim_to_pct=5.0, revised_target=450.0)


@pytest.mark.unit
def test_a_memo_as_rendered_reads_back_every_field():
    c = thesis_card("**Rating**: Buy\n\n" + render_investment_memo(MEMO).split("\n", 2)[2])
    assert c["kind"] == "memo" and c["complete"] and c["missing"] == [] and c["extra"] == []
    assert (c["business"], c["bull"], c["bear"], c["valuation"]) == ("결제망 운영. 수수료로 번다.", "해외 결제가 빨라진다", "수수료 규제 입법", "선행 PER 27배")
    assert c["thesis"] == "결제 건수 성장이 이어진다.\n해외 결제 비중이 는다."        # a value runs on over its lines
    assert (c["price"], c["target"], c["downside"], c["conviction"], c["sizing_pct"]) == (400.0, 480.0, 360.0, 0.62, 8.0)
    assert c["catalysts"] == [{"event": "1분기 실적", "date": "2027-01-28"}, {"event": "연말 결제 데이터", "date": None}]
    assert c["invalidation"] == ["결제 성장 한 자릿수", "법안 통과"]
    assert (c["horizon"], c["exit_plan"]) == ("6-12 months", "목표가에서 절반 축소")


@pytest.mark.unit
def test_a_memo_without_sizing_is_complete_with_no_weight():
    c = thesis_card(render_investment_memo(MEMO.model_copy(update={"sizing_pct": None})))
    assert c["complete"] and c["sizing_pct"] is None


@pytest.mark.unit
def test_a_memo_with_fields_missing_or_unknown_says_so_and_keeps_every_line():
    text = render_investment_memo(MEMO)
    text = text.replace("**Bull case**: 해외 결제가 빨라진다\n", "").replace("**Valuation**: 선행 PER 27배", "**Valuation**:")
    text = text.replace("**Price / Target / Downside**: 400", "**Price / Target / Downside**: n/a 400")
    text += "\n\n**Note**: 추가 메모\n떨어진 줄은 남는다"
    c = thesis_card(text)
    assert c["kind"] == "memo" and not c["complete"]
    assert sorted(c["missing"]) == ["Bull case", "Price / Target / Downside", "Valuation"]
    assert (c["bull"], c["valuation"], c["price"], c["target"]) == (None, None, None, None)
    assert c["extra"] == [{"label": "Note", "text": "추가 메모\n떨어진 줄은 남는다"}]
    assert c["thesis"] == "결제 건수 성장이 이어진다.\n해외 결제 비중이 는다."


@pytest.mark.unit
def test_a_text_no_label_fits_is_kept_whole():
    text = "등급: 매수인지 보유인지 모르겠다\n\n두 번째 문단"
    c = thesis_card(text)
    assert c["kind"] == "text" and c["label"] is None and c["text"] == "등급: 매수인지 보유인지 모르겠다\n두 번째 문단"
    assert c["extra"] == [] and c["complete"]
    # a line after a blank one under a label is not dropped either: it is extra, and the card shows the whole text
    c = thesis_card("**Rating**: Buy\n\n**근거**: 모멘텀 1위\n\n덧붙인 줄")
    assert (c["kind"], c["label"], c["text"], c["extra"], c["complete"]) == ("text", "근거", "모멘텀 1위", [{"label": None, "text": "덧붙인 줄"}], False)


@pytest.mark.unit
def test_a_sell_review_as_rendered_and_its_trim_note():
    text = ("**Rating**: Underweight\n\n" + render_sell_review(REVIEW)
            + "\n\n실행기는 부분 매도를 하지 않아 Trim을 보유 전량 매도(Underweight)로 집행한다.")
    c = thesis_card(text)
    assert (c["kind"], c["action"], c["trim_to_pct"], c["revised_target"]) == ("review", "Trim", 5.0, 450.0)
    assert c["reasons"] == ["thesis_broken", "overweight"] and c["reasoning"] == "조건 하나가 충족됐다."
    assert c["checks"] == [{"condition": "영업이익률 12% 미만", "met": True, "evidence": "3분기 11.2%"},
                           {"condition": "점유율 하락", "met": False, "evidence": "변화 없음"}]
    assert c["extra"] == [{"label": None, "text": "실행기는 부분 매도를 하지 않아 Trim을 보유 전량 매도(Underweight)로 집행한다."}]
    hold = thesis_card("**Rating**: Hold\n\n" + render_sell_review(REVIEW.model_copy(update={
        "action": SellAction.HOLD, "reasons": [], "trim_to_pct": None, "revised_target": None,
        "checks": [InvalidationCheck(condition="점유율 하락", met=False, evidence="변화 없음")]})))
    assert (hold["action"], hold["reasons"], hold["complete"]) == ("Hold", [], True)


@pytest.mark.unit
def test_review_keeps_its_reason_and_the_model_text():
    c = thesis_card("**Rating**: REVIEW\n\n**사유**: 등급 줄이 5종에 없음\n\n**원문**: Rating: Overweight-to-Hold")
    assert (c["kind"], c["reason"], c["raw"], c["complete"]) == ("raw", "등급 줄이 5종에 없음", "Rating: Overweight-to-Hold", True)


@pytest.mark.unit
def test_the_api_serves_the_card_beside_the_untouched_text_and_the_price_facts(tmp_path, monkeypatch, store):  # noqa: F811
    data = tmp_path / "data"
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]
    for t in ("^KS11", "A.KS"):
        _kis(data, t, [(d, 100, 100) for d in days])
    a = market_audit(data / "audit", "KRX")
    text = render_investment_memo(MEMO)
    a.record_decision(DecisionRecord(ticker="A.KS", trade_date=days[0], rating="Buy", action="Buy", entry_price=100.0, thesis=text,
                                     evidence=[EvidenceItem(source="market", summary="종가 100")]))
    a.record_order(OrderRecord(order_id=f"paper-{days[0]}-A.KS", ticker="A.KS", trade_date=days[0], side="Buy", quantity=10,
                               filled_quantity=10, filled_price=100.0, fill_date=days[1], status="filled", market="KRX"))
    api, _ = _client(data, monkeypatch)
    d = api.get("/api/decisions", params={"market": "KRX", "date": days[0]}).json()[0]
    assert d["thesis"] == text                                    # the record's text as it was
    assert d["card"]["kind"] == "memo" and d["card"]["complete"] and d["card"]["target"] == 480.0
    assert d["price_ctx"] == {"close": 100.0, "avg_price": 100.0, "stop_loss_pct": -0.08, "realized": {}}
    stored = a.decisions_where(ticker="A.KS")
    assert [x.thesis for x in stored] == [text]                  # nothing written back


@pytest.mark.unit
def test_review_text_of_many_paragraphs_with_labels_is_kept_whole_as_raw():
    """review 1R #1: the model's own text runs to the end — paragraphs and labels of its own — and is never parsed."""
    raw = "첫 문단\n\n둘째 문단\n**Thesis**: 서비스 마진은 오른다\n**Action**: Hold"
    c = thesis_card(f"**Rating**: REVIEW\n\n**사유**: 등급 줄 없음\n\n**원문**: {raw}")
    assert (c["kind"], c["reason"], c["raw"], c["extra"], c["complete"]) == ("raw", "등급 줄 없음", raw, [], True)
    c = thesis_card("**Rating**: REVIEW\n\n**사유**: 원문 없음")
    assert (c["kind"], c["raw"], c["missing"], c["complete"]) == ("raw", None, ["원문"], False)


@pytest.mark.unit
def test_no_line_of_a_check_or_the_prices_is_dropped():
    """review 1R #2: a check's evidence on two lines stays with it; lines under the prices go to extra; a block whose
    first line is not a check is not read (the card shows the whole text)."""
    head = "**Rating**: Hold\n\n**Action**: Hold\n\n**Reasons**: none\n\n"
    c = thesis_card(head + "**Invalidation checks**:\n- [x] 조건 — 근거 첫 줄\n근거 둘째 줄\n\n**Reasoning**: r")
    assert c["checks"] == [{"condition": "조건", "met": True, "evidence": "근거 첫 줄\n근거 둘째 줄"}] and c["complete"]
    c = thesis_card(head + "**Invalidation checks**:\n앞선 설명\n- [ ] 조건\n\n**Reasoning**: r")
    assert "Invalidation checks" in c["missing"] and not c["complete"]
    c = thesis_card("**Thesis**: a\n\n**Price / Target / Downside**: 1 / 2 / 3 (upside 100%)\n덧붙인 줄")
    assert (c["price"], c["target"], c["downside"]) == (1.0, 2.0, 3.0)
    assert {"label": "Price / Target / Downside", "text": "(upside 100%)\n덧붙인 줄"} in c["extra"] and not c["complete"]
    # review 2R #1: text after the numbers on their own line alone is kept too
    c = thesis_card("**Thesis**: a\n\n**Price / Target / Downside**: 1 / 2 / 3 (upside 100%)")
    assert c["price"] == 1.0 and c["extra"] == [{"label": "Price / Target / Downside", "text": "(upside 100%)"}]
    assert not c["complete"]


@pytest.mark.unit
def test_a_catalyst_date_is_a_date():
    """review 1R #5: brackets that close an event's own name are not its date."""
    c = thesis_card("**Thesis**: a\n\n**Catalysts**: FDA 승인 (경구용); 실적 (2027-01); 주총 (2027-03-20)")
    assert c["catalysts"] == [{"event": "FDA 승인 (경구용)", "date": None}, {"event": "실적", "date": "2027-01"},
                              {"event": "주총", "date": "2027-03-20"}]
