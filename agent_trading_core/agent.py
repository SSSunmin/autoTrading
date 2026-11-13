"""The LLM fund manager (docs/01 §12): one model call per ticker, a structured answer, a decision record.

Two calls, both ``claude-opus-5-5`` (``config.LLM``) with structured output:

- ``decide_candidate``: a ticker that passed the screen → ``schemas.InvestmentMemo`` (Buy/Overweight need
  reward/risk ≥ 2, checked by the schema)
- ``review_holding``: a ticker in the book → ``schemas.SellReview`` (Hold / Trim / Exit with one of the seven
  ``SellReason``)

The answer is never trusted to be well formed: a refusal, a ``max_tokens`` stop, JSON the schema rejects and every
API error end as an ``Outcome`` with ``review`` set, which ``to_record`` turns into a REVIEW decision — the executor
makes it a ``review`` exception, never an order and never a Hold. The response (or the error) is kept as a JSON file
under ``raw_dir`` whatever happened.

The request: the fixed ``SYSTEM_PROMPT`` with a cache breakpoint (nothing that changes per ticker or day is in it),
the ticker's snapshot in the user message, ``output_config`` = effort + the schema as JSON schema. ``messages.parse``
is not used: it validates inside the SDK and raises with the response gone, so a rejected memo would leave neither
its text nor its token counts. ``messages.create`` with the same ``transform_schema`` and ``structured.validate_text``
after it is the same check with the response kept. No server-side refusal fallback either: it is a beta parameter
(``client.beta.messages``) and would route a decline to another model; a refusal here is a REVIEW for a person.

The hard rules (sizing, stops, drawdown and loss halts, order caps) are the executor's; the model is told they exist
and that it decides inside them.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd
from stockstats import wrap

from agent_trading_core.audit import DecisionRecord, EvidenceItem
from agent_trading_core.config import LLM
from agent_trading_core.rating import RATING_REVIEW
from agent_trading_core.schemas import (
    InvestmentMemo,
    SellAction,
    SellReason,
    SellReview,
    render_investment_memo,
    render_sell_review,
)
from agent_trading_core.snapshot import DEFAULT_SNAPSHOT_INDICATORS, _fmt
from agent_trading_core.structured import validate_text

ROOT = Path(__file__).resolve().parents[1]
PROMPT_VERSION = "fm-v1"        # bump with any change to SYSTEM_PROMPT or the user message layout

# USD per million tokens, claude-opus-5-5 (2026-10 price list): input, output, cache read, 5-minute cache write
PRICE = {"input_tokens": 4.00, "output_tokens": 20.00, "cache_read_input_tokens": 0.20,
         "cache_creation_input_tokens": 5.00}
PRICE_TOLERANCE = 0.01       # a memo's current_price may differ this much from the snapshot's latest close

SYSTEM_PROMPT = """당신은 롱온리 주식 재량 운용 펀드매니저다. 이 시스템에서 매수·매도 판단은 당신이 내리고, 당신의 결정은 그대로 주문이 된다(지금은 모의투자 장부). 결정 하나는 투자위원회(IC)에 올리는 문서처럼 쓴다.

## 운용 환경
- 리스크팀의 운용 가이드라인(하드 룰)은 실행기가 집행한다: 종목당 비중 상한과 균등 사이징, 손절·트레일링 스톱·보유 기간 만료, 드로다운 단계 축소와 −20% 정지, 일일 손실 정지, 시장 국면 필터, 주문당·일일 주문 금액 상한. 당신은 그 안에서 판단하고, 당신의 비중 권고는 리스크팀 한도로 잘린다. 하드 룰 집행은 당신 몫이 아니므로 매도 사유 hard_rule은 고르지 않는다.
- 등급이 주문이 되는 방식: Buy·Overweight → 보유하지 않은 종목이면 매수 주문(다음 거래일 시가 체결). Hold → 주문 없음. Underweight·Sell → 보유 중이면 전량 매도. 매도 검토의 Trim·Exit는 매도 주문이 되며, 실행기는 아직 부분 매도를 하지 않으므로 Trim도 보유 전량 매도로 집행된다.

## 요청 두 가지
요청은 사용자 메시지 첫 줄에 적혀 온다.

1) 신규 후보 — 투자 메모(InvestmentMemo). 매니저의 사고 흐름을 그대로 밟는다: 아이디어가 왜 지금 나왔나 → 사업을 두 문장으로 → 실적·재무 점검 → 밸류에이션(상승여력 대 하락위험) → 갭을 닫을 촉매와 시점 → 베어 케이스와 논지가 깨지는 조건 → 확신도와 비중 → 매도 계획.
   매수(Buy·Overweight)는 다음을 모두 충족할 때다.
   - 사업을 두 문장으로 설명할 수 있다.
   - 목표가와 하락 시나리오 가격이 숫자로 있고, 상승여력/하락위험이 2:1 이상이다(통상 3:1). 2:1 미만의 Buy·Overweight는 스키마가 거부한다.
   - 12개월 안에 갭을 닫을 구체적인 촉매가 있다.
   - 베어 케이스를 공정하게 서술하고 그것이 왜 틀렸는지 답할 수 있다.
   - 논지가 깨지는 조건(invalidation)을 가격이 아니라 사실로 미리 적는다. 예: "다음 분기 영업이익률 12% 미만".
   - 포트폴리오에 같은 베팅(같은 섹터·같은 매크로 노출)이 이미 있지 않다.
   근거가 한쪽으로 기울면 그쪽으로 결정하고, 얼마나 기울었는지는 확신도와 비중으로 표현한다. Hold는 따져 본 뒤에도 근거가 팽팽하거나 판단하기에 너무 얇을 때만 고른다.

2) 보유 종목 — 매도 검토(SellReview). "지금 팔 이유가 생겼나"를 묻는다. 매도 사유는 일곱 가지다: target_reached(목표가 도달로 상승여력이 하락위험보다 작아짐), thesis_broken(미리 적은 invalidation 조건 충족), better_alternative(같은 리스크로 상승여력이 더 큰 대안), overweight(올라서 비중 한도 초과), catalyst_gone(기대한 촉매가 지나갔고 다음이 없음), would_not_rebuy("오늘 처음 본다면 사겠나"에 아니오), hard_rule(실행기 몫 — 고르지 않는다).
   - 가격 하락 자체는 매도 사유가 아니다. 사실이 변했는지 본다.
   - 사유 하나면 검토, 둘이면 매도가 기준이다. Hold면 reasons는 비워도 되고, Trim·Exit면 사유를 하나 이상 적는다.
   - 매수 당시 메모가 주어지면 그 invalidation을 하나씩 checks에 옮겨 점검한다. 메모가 없으면(규칙 베이스라인이 산 종목) 지금 기준으로 논지가 깨지는 조건을 세우고 그것을 점검한다. 충족된 조건이 있으면 thesis_broken을 사유에 넣는다.

## 숫자 규칙
- 가격·지표·수익률·거래량·보유 수량·평가 금액 같은 숫자는 사용자 메시지의 검증 스냅샷에 있는 값만 쓴다. current_price는 스냅샷의 최신 종가다. 통화는 스냅샷에 적힌 통화다.
- 스냅샷에 없는 재무 숫자(매출, 이익, PER 같은 배수)를 지어내지 않는다. 밸류에이션에 그런 숫자가 필요하면 "스냅샷에 없음"이라고 밝히고, 어떤 방법으로 목표가와 하락 가격을 잡았는지 적는다.
- 사업 설명, 경쟁 구도, 촉매 같은 정성 판단은 당신의 지식으로 하되, 날짜가 있는 촉매는 기준일 이후로 잡는다.
- 과거 결정과 결과(기억)가 주어지면 같은 실수를 되풀이하지 않도록 반영한다.

## 출력
- 모든 서술 필드는 한국어로 쓴다. 등급·행동·매도 사유 값은 스키마의 영문 값을 그대로 쓴다.
- 가격 필드는 숫자 하나다. 범위나 퍼센트를 쓰지 않는다.
"""

CANDIDATE = "candidate"
HOLDING = "holding"
REQUEST_LINE = {CANDIDATE: "요청: 신규 후보 — 투자 메모(InvestmentMemo)",
                HOLDING: "요청: 보유 종목 — 매도 검토(SellReview)"}
SCHEMA = {CANDIDATE: InvestmentMemo, HOLDING: SellReview}


# --- the snapshot a decision is made from ------------------------------------------------------------------------

@dataclass
class Section:
    source: str             # EvidenceItem.source: market, portfolio, memory, other
    title: str
    text: str
    summary: str            # one sentence, the decision card's evidence line
    close: float | None = None    # market section: the latest close, the price a memo must be written against


@dataclass
class Snapshot:
    """What the model sees about one ticker on one day; also the decision's evidence list."""

    ticker: str
    as_of: str
    currency: str
    sections: list[Section] = field(default_factory=list)

    def render(self) -> str:
        head = f"종목: {self.ticker}\n기준일: {self.as_of}\n통화: {self.currency}"
        return "\n\n".join([head] + [f"## {s.title}\n\n{s.text}" for s in self.sections])

    @property
    def close(self) -> float | None:
        return next((s.close for s in self.sections if s.close is not None), None)

    def evidence(self, ref: str | None = None) -> list[EvidenceItem]:
        return [EvidenceItem(source=s.source, summary=s.summary, as_of=self.as_of, ref=ref or s.title)
                for s in self.sections]


def market_section(frame: pd.DataFrame, as_of: str, provisional: bool = False, look_back: int = 20) -> Section:
    """Prices up to ``as_of``: the latest OHLCV row, ``snapshot.DEFAULT_SNAPSHOT_INDICATORS`` on it, returns and the
    last ``look_back`` closes. ``frame``: Date, Open, High, Low, Close, Volume (a KIS or Yahoo cache file).

    ponytail: the same table as ``snapshot.build_verified_market_snapshot``, which reads only the Yahoo files; give
    that function a loader and drop this one."""
    df = frame.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df[df["Date"] <= pd.Timestamp(as_of)].dropna(subset=["Date", "Close"]).sort_values("Date")
    if df.empty:
        raise ValueError(f"{as_of} 이전 가격 행 없음")
    stock = wrap(df.copy())
    ind = {}
    for name in DEFAULT_SNAPSHOT_INDICATORS:
        try:
            stock[name]
            ind[name] = _fmt(stock.iloc[-1][name])
        except Exception as exc:  # an indicator the history is too short for
            ind[name] = f"N/A ({type(exc).__name__})"
    last, closes = df.iloc[-1], df["Close"].astype(float)

    def ret(n: int) -> str:
        return f"{closes.iloc[-1] / closes.iloc[-1 - n] - 1:+.1%}" if len(closes) > n else "N/A"

    rows = [f"- 최신 행: {_fmt(last['Date'])}" + (" (장 마감 직후 KIS 당일 값 — 확정 전 임시 행)" if provisional else ""),
            "", "| 항목 | 값 |", "|---|---:|"]
    rows += [f"| {k} | {_fmt(last.get(k))} |" for k in ("Open", "High", "Low", "Close", "Volume")]
    rows += [f"| 수익률 {n}거래일 | {ret(n)} |" for n in (5, 20, 60, 120)]
    rows += ["", "| 지표 | 값 |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in ind.items()]
    recent = df.tail(look_back)
    rows += ["", f"최근 {len(recent)}거래일 종가: "
             + ", ".join(f"{d:%m-%d} {_fmt(c)}" for d, c in zip(recent["Date"], recent["Close"], strict=True))]
    summary = (f"{_fmt(last['Date'])} 종가 {_fmt(last['Close'])}, 20거래일 {ret(20)}, 60거래일 {ret(60)}, "
               f"RSI {ind.get('rsi', 'N/A')}, 50일선 {ind.get('close_50_sma', 'N/A')}")
    return Section("market", "검증 시세 스냅샷", "\n".join(rows), summary, float(last["Close"]))


# --- one model call ------------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    """One model call. ``output`` is the validated answer; otherwise ``review`` says why there is none."""

    kind: str                               # CANDIDATE or HOLDING
    ticker: str
    output: InvestmentMemo | SellReview | None
    review: str | None
    usage: dict[str, int]
    seconds: float
    raw: str | None = None                  # the kept response file


def make_client() -> anthropic.Anthropic:
    """The SDK client; the key is ANTHROPIC_API_KEY from the environment, after the repository ``.env`` or
    ``TRADING_ENV_FILE`` (neither overrides it). A missing key is named, never a value."""
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("TRADING_ENV_FILE") or ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY 없음 — 환경변수나 저장소 루트 .env(또는 TRADING_ENV_FILE)에 넣는다")
    return anthropic.Anthropic(timeout=float(LLM["timeout"]))


def request(kind: str, user_text: str) -> dict:
    """The ``messages.create`` arguments: everything but the user message is the same for every ticker and day."""
    return {
        "model": LLM["model"],
        "max_tokens": LLM["max_tokens"],
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
        "output_config": {"effort": LLM["effort"],
                          "format": {"type": "json_schema", "schema": anthropic.transform_schema(SCHEMA[kind])}},
    }


def _usage(message) -> dict[str, int]:
    u = getattr(message, "usage", None)
    return {k: int(getattr(u, k, None) or 0) for k in PRICE}


def call(client, kind: str, ticker: str, user_text: str, raw_dir: str | Path | None = None,
         close: float | None = None) -> Outcome:
    """One request; REVIEW (``review`` set) on a refusal, a ``max_tokens`` stop, an answer the schema rejects or an
    API error (its class only — the message stays in the raw file). Beyond the schema (``check``): a memo whose
    ``current_price`` is off the snapshot's ``close`` (the 2:1 gate is computed from it), a sell review naming
    ``hard_rule`` (the executor's)."""
    started, message, error = time.monotonic(), None, None
    try:
        message = client.messages.create(**request(kind, user_text))
    except anthropic.RateLimitError as e:
        error, review = e, "api: RateLimitError"
    except anthropic.APIStatusError as e:
        error, review = e, f"api: {type(e).__name__}({e.status_code})"
    except anthropic.APIConnectionError as e:          # APITimeoutError included
        error, review = e, f"api: {type(e).__name__}"
    except anthropic.AnthropicError as e:
        error, review = e, f"api: {type(e).__name__}"
    output = None
    if message is not None:
        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            review = f"refusal({getattr(details, 'category', None) or 'none'})"
        elif message.stop_reason == "max_tokens":
            review = f"max_tokens({LLM['max_tokens']})"
        else:
            output, why = validate_text(SCHEMA[kind], text)
            review = f"schema: {why}" if output is None else check(output, close)
            output = output if review is None else None
    out = Outcome(kind, ticker, output, review, _usage(message), round(time.monotonic() - started, 2))
    if raw_dir is not None:
        try:
            out.raw = str(_keep(Path(raw_dir), out, user_text, message, error))
        except OSError:              # a full disk or a read-only folder: the decision goes on, "보관 안 함"
            out.raw = None
    return out


def _keep(raw_dir: Path, out: Outcome, user_text: str, message, error) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{out.ticker}-{out.kind}-{datetime.now():%H%M%S%f}.json"
    body = {"ticker": out.ticker, "kind": out.kind, "model": LLM["model"], "effort": LLM["effort"],
            "prompt_version": PROMPT_VERSION, "seconds": out.seconds, "review": out.review, "usage": out.usage,
            "user": user_text,
            "response": message.to_dict() if message is not None else None,
            "error": None if error is None else {"type": type(error).__name__, "message": str(error)[:2000]}}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def check(output: InvestmentMemo | SellReview, close: float | None) -> str | None:
    """Why a schema-valid answer is still not tradeable, or None."""
    if isinstance(output, InvestmentMemo):
        if close is None:
            return "price: 스냅샷 종가 없음 — current_price를 대조할 수 없음"
        if abs(output.current_price / close - 1) > PRICE_TOLERANCE:
            return f"price: current_price {output.current_price:g} ≠ 스냅샷 종가 {close:g}"
    elif SellReason.HARD_RULE in output.reasons:
        return "schema: hard_rule은 실행기 몫 — 매니저 사유가 아님"
    return None


def decide_candidate(ticker: str, snapshot: Snapshot, memory: str, *, client, raw_dir=None) -> Outcome:
    """A screened ticker not held: an ``InvestmentMemo`` or REVIEW."""
    text = "\n\n".join([REQUEST_LINE[CANDIDATE], snapshot.render(), f"## 기억(이 종목의 지난 결정과 결과)\n\n{memory}"])
    return call(client, CANDIDATE, ticker, text, raw_dir, snapshot.close)


def review_holding(ticker: str, snapshot: Snapshot, position: str, memory: str, *, client, raw_dir=None) -> Outcome:
    """A ticker in the book: a ``SellReview`` or REVIEW."""
    text = "\n\n".join([REQUEST_LINE[HOLDING], snapshot.render(), f"## 보유 현황과 매수 당시 메모\n\n{position}",
                        f"## 기억(이 종목의 지난 결정과 결과)\n\n{memory}"])
    return call(client, HOLDING, ticker, text, raw_dir)


# --- the decision record -------------------------------------------------------------------------------------------

ACTION = {"Buy": "Buy", "Overweight": "Buy", "Hold": "Hold", "Underweight": "Sell", "Sell": "Sell"}
# Trim is Underweight: the executor sells an Underweight in full (no partial sells yet), the thesis says so
REVIEW_RATING = {SellAction.HOLD: "Hold", SellAction.TRIM: "Underweight", SellAction.EXIT: "Sell"}


def settings() -> dict:
    return {"decider": LLM["model"], "effort": LLM["effort"], "prompt_version": PROMPT_VERSION}


def to_record(out: Outcome, *, market: str, day: str, screen_run_id: str | None,
              evidence: list[EvidenceItem]) -> DecisionRecord:
    """The decision as the audit log and the executor read it (``source="agent"``)."""
    base = {"market": market, "ticker": out.ticker, "trade_date": day, "screen_run_id": screen_run_id,
            "evidence": evidence, "usage": out.usage, "source": "agent",
            "settings": settings() | {"kind": out.kind, "seconds": out.seconds}
                        | ({"raw": out.raw} if out.raw else {})}
    if out.output is None:
        return DecisionRecord(**base, rating=RATING_REVIEW, action="Sell" if out.kind == HOLDING else None,
                              thesis=f"**Rating**: {RATING_REVIEW}\n\n**사유**: {out.review}\n\n"
                                     f"**원문**: {out.raw or '보관 안 함'}")
    if isinstance(out.output, InvestmentMemo):
        m = out.output
        return DecisionRecord(**base, rating=m.rating.value, action=ACTION[m.rating.value], entry_price=m.current_price,
                              position_sizing=None if m.sizing_pct is None else f"{m.sizing_pct:g}% of equity",
                              confidence=m.conviction, thesis=render_investment_memo(m))
    r = out.output
    rating = REVIEW_RATING[r.action]
    thesis = f"**Rating**: {rating}\n\n" + render_sell_review(r)
    if r.action == SellAction.TRIM:
        thesis += "\n\n실행기는 부분 매도를 하지 않아 Trim을 보유 전량 매도(Underweight)로 집행한다."
    return DecisionRecord(**base, rating=rating, action=ACTION[rating], sell_reason=r.reasons[0] if r.reasons else None,
                          position_sizing=None if r.trim_to_pct is None else f"trim to {r.trim_to_pct:g}% of equity",
                          thesis=thesis)


def cost_usd(usage: dict[str, int]) -> float:
    return sum(usage.get(k, 0) * p for k, p in PRICE.items()) / 1_000_000
