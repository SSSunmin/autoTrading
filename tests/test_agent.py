"""The LLM fund manager (agent_trading_core.agent) without the network: a fake Anthropic client returns fixed
responses. A memo becomes an agent decision; a Buy under 2:1 reward/risk, a refusal, a max_tokens stop and API
errors become REVIEW with the response kept; a sell review maps to Sell/Underweight/Hold with its reason; the system
prompt is one fixed string (the cache prefix), the ticker and day only in the user message."""

import json
import re
from pathlib import Path

import anthropic
import httpx2
import pandas as pd
import pytest
from anthropic.types import Message

from agent_trading_core import agent
from agent_trading_core.config import LLM, MARKETS
from agent_trading_core.rating import RATING_REVIEW
from agent_trading_core.schemas import InvestmentMemo, SellReview

USAGE = {"input_tokens": 900, "output_tokens": 1200, "cache_read_input_tokens": 1500,
         "cache_creation_input_tokens": 0}


def memo(**kw) -> dict:
    """A Buy at 100 with target 130 and downside 90: reward/risk 3."""
    return {"business_summary": "반도체를 만든다. 메모리와 파운드리로 번다.", "thesis": "이익 회복이 가격에 덜 반영됐다.",
            "bull_case": "업황 반등", "bear_case": "재고 조정 장기화", "valuation_basis": "스냅샷에 없음 — 과거 밴드",
            "current_price": 100.0, "price_target": 130.0, "downside_price": 90.0,
            "catalysts": [{"event": "4분기 실적", "expected_by": "2027-01"}],
            "invalidation": ["다음 분기 영업이익률 10% 미만"], "rating": "Buy", "conviction": 0.7, "sizing_pct": 5,
            "exit_plan": "목표가에서 절반, 논지 훼손 시 전량", "time_horizon": "6-12개월"} | kw


def review(**kw) -> dict:
    return {"checks": [{"condition": "영업이익률 10% 미만", "met": True, "evidence": "스냅샷에 없음"}],
            "reasons": ["thesis_broken"], "reasoning": "조건 충족", "action": "Exit"} | kw


def message(text: str = "", stop_reason: str = "end_turn", **kw) -> Message:
    return Message.model_validate({"id": "msg_test", "type": "message", "role": "assistant", "model": LLM["model"],
                                   "content": [{"type": "text", "text": text}] if text else [],
                                   "stop_reason": stop_reason, "stop_sequence": None, "usage": USAGE} | kw)


class FakeClient:
    """``messages.create`` answers from ``answer(request)``: a Message, or an exception to raise."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        out = self.answer(kw)
        if isinstance(out, Exception):
            raise out
        return out


def snap(ticker="005930.KS", day="2026-08-04") -> agent.Snapshot:
    days = pd.bdate_range("2026-01-02", day)
    frame = pd.DataFrame({"Date": days, "Open": 99.0, "High": 101.0, "Low": 98.0,
                          "Close": [100.0 + (i % 7) for i in range(len(days) - 1)] + [100.0], "Volume": 1e6})
    return agent.Snapshot(ticker, day, "KRW", [agent.market_section(frame, day),
                                               agent.Section("portfolio", "포트폴리오", "- 현금 100%", "현금 100%")])


def record(out):
    return agent.to_record(out, market="KRX", day="2026-08-04", screen_run_id="daily_krx_2026-08-04",
                           evidence=snap().evidence())


@pytest.mark.unit
def test_a_memo_becomes_an_agent_decision_with_its_settings_usage_and_evidence(tmp_path):
    client = FakeClient(lambda kw: message(json.dumps(memo())))
    out = agent.decide_candidate("005930.KS", snap(), "지난 결정 없음.", client=client, raw_dir=tmp_path)
    assert isinstance(out.output, InvestmentMemo) and out.review is None and out.usage == USAGE
    r = record(out)
    assert (r.rating, r.action, r.source, r.confidence, r.entry_price) == ("Buy", "Buy", "agent", 0.7, 100.0)
    assert r.position_sizing == "5% of equity" and r.thesis.startswith("**Rating**: Buy") and "reward/risk 3.0:1" in r.thesis
    assert r.settings | {"seconds": 0} == {"decider": "claude-opus-5-5", "effort": "high", "prompt_version": agent.PROMPT_VERSION,
                                           "kind": "candidate", "seconds": 0, "raw": out.raw}
    assert r.usage == USAGE and [e.source for e in r.evidence] == ["market", "portfolio"]
    assert "종가" in r.evidence[0].summary
    kept = json.loads(Path(out.raw).read_text(encoding="utf-8"))
    assert kept["response"]["usage"]["cache_read_input_tokens"] == 1500 and "005930.KS" in kept["user"]
    # the request: structured output from the schema, effort high, no thinking parameter, 16000 tokens
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["max_tokens"] == 16000 and "thinking" not in req
    assert req["output_config"]["effort"] == "high"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": anthropic.transform_schema(InvestmentMemo)}


@pytest.mark.unit
def test_a_buy_under_two_to_one_is_rejected_by_the_schema_and_kept_as_review(tmp_path):
    bad = json.dumps(memo(downside_price=80.0))         # +30% vs -20%: 1.5 to 1
    out = agent.decide_candidate("005930.KS", snap(), "", client=FakeClient(lambda kw: message(bad)), raw_dir=tmp_path)
    assert out.output is None and out.review.startswith("schema: ") and "reward/risk >= 2.0" in out.review
    r = record(out)
    assert (r.rating, r.action, r.tradeable) == (RATING_REVIEW, None, False)
    assert out.raw in r.thesis and r.usage == USAGE                     # the paid tokens are still counted
    assert json.loads(Path(out.raw).read_text(encoding="utf-8"))["response"]["content"][0]["text"] == bad


@pytest.mark.unit
@pytest.mark.parametrize("answer, why", [
    (message(stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}),
     "refusal(cyber)"),
    (message('{"business_summary": "반도', stop_reason="max_tokens"), "max_tokens(16000)"),
    (message("요약: 사겠다"), "schema: "),                                   # free text, not JSON
    (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")),
                              body=None), "api: RateLimitError"),
    (anthropic.InternalServerError("boom", response=httpx2.Response(500, request=httpx2.Request("POST", "https://x")),
                                   body=None), "api: InternalServerError(500)"),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), "api: APITimeoutError"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "api: APIConnectionError"),
])
def test_every_failure_is_a_review_never_a_hold_and_its_raw_is_kept(tmp_path, answer, why):
    out = agent.review_holding("AAPL", snap("AAPL"), "- 보유 10주", "", client=FakeClient(lambda kw: answer),
                               raw_dir=tmp_path)
    assert out.output is None and out.review.startswith(why)
    r = record(out)
    assert r.rating == RATING_REVIEW and r.action == "Sell"     # a holding's review shows on the sell side
    kept = json.loads(Path(out.raw).read_text(encoding="utf-8"))
    assert kept["review"] == out.review and (kept["response"] is not None) == isinstance(answer, Message)


@pytest.mark.unit
def test_a_sell_review_maps_to_sell_underweight_or_hold_with_its_first_reason():
    def run(**kw):
        out = agent.review_holding("AAPL", snap("AAPL"), "", "", client=FakeClient(lambda _: message(json.dumps(review(**kw)))))
        return out, record(out)

    out, r = run()
    assert isinstance(out.output, SellReview)
    assert (r.rating, r.action, r.sell_reason) == ("Sell", "Sell", "thesis_broken") and r.thesis.startswith("**Rating**: Sell")
    _, r = run(action="Trim", trim_to_pct=3, reasons=["overweight", "thesis_broken"])
    assert (r.rating, r.action, r.sell_reason, r.position_sizing) == ("Underweight", "Sell", "overweight", "trim to 3% of equity")
    assert "전량 매도" in r.thesis
    _, r = run(action="Hold", reasons=[], checks=[{"condition": "c", "met": False, "evidence": "e"}])
    assert (r.rating, r.action, r.sell_reason) == ("Hold", "Hold", None)
    # a met invalidation without thesis_broken breaks the schema: REVIEW, not the Hold it asked for
    out, r = run(action="Hold", reasons=[])
    assert out.output is None and r.rating == RATING_REVIEW


@pytest.mark.unit
def test_the_system_prompt_is_one_fixed_cached_block_and_the_ticker_and_day_are_in_the_message():
    a = agent.request(agent.CANDIDATE, "종목: 005930.KS\n기준일: 2026-08-04")
    b = agent.request(agent.HOLDING, "종목: AAPL\n기준일: 2026-11-12")
    assert a["system"] == b["system"] == [{"type": "text", "text": agent.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    assert not re.search(r"\d{4}-\d{2}-\d{2}", agent.SYSTEM_PROMPT)
    import importlib.util

    spec = importlib.util.spec_from_file_location("baseline_krx", Path(__file__).resolve().parents[1] / "scripts" / "baseline_krx.py")
    bk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bk)
    tickers = [t for s in bk.SETUPS.values() for t in s["universe"]] + [m["benchmark"] for m in MARKETS.values()]
    assert not [t for t in tickers if re.search(rf"\b{re.escape(t.split('.')[0])}\b", agent.SYSTEM_PROMPT)]
    assert "005930.KS" in a["messages"][0]["content"] and "AAPL" in b["messages"][0]["content"]
    # the direction (docs/00 §2): the model decides buys and sells; no wording telling it to hold back
    assert not re.search(r"보수적|신중하게|조심스럽게|conservative", agent.SYSTEM_PROMPT)


@pytest.mark.unit
def test_a_missing_key_is_named_and_never_printed(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("TRADING_ENV_FILE", str(tmp_path / "none.env"))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY 없음"):
        agent.make_client()


@pytest.mark.unit
def test_cost_is_priced_per_token_kind():
    assert agent.cost_usd(USAGE) == pytest.approx((900 * 4 + 1200 * 20 + 1500 * 0.2) / 1e6)


@pytest.mark.unit
def test_a_memo_off_the_snapshot_close_and_a_hard_rule_reason_are_reviews():
    # the 2:1 gate passes on the model's own price (8000 → 12000 / 7000) but the close is 100: not tradeable
    off = json.dumps(memo(current_price=8000, price_target=12000, downside_price=7000))
    out = agent.decide_candidate("005930.KS", snap(), "", client=FakeClient(lambda kw: message(off)))
    assert out.output is None and out.review == "price: current_price 8000 ≠ 스냅샷 종가 100"
    assert record(out).rating == RATING_REVIEW
    near = json.dumps(memo(current_price=100.5, price_target=130.5, downside_price=90.5))     # within 1%
    assert agent.decide_candidate("005930.KS", snap(), "", client=FakeClient(lambda kw: message(near))).output
    hard = json.dumps(review(reasons=["hard_rule", "thesis_broken"]))
    out = agent.review_holding("AAPL", snap("AAPL"), "", "", client=FakeClient(lambda kw: message(hard)))
    assert out.output is None and out.review.startswith("schema: hard_rule") and record(out).action == "Sell"
