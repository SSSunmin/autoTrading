"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# LLMs sometimes write a placeholder string ("None", "N/A", ...) into an optional
# numeric field instead of omitting it. Coerce those to None so the structured
# call validates instead of erroring (#1058). Pydantic still parses real numeric
# strings ("189.5") to float.
_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    """Normalise an LLM-written optional numeric field before validation.

    Three shapes show up in practice: a placeholder string ("None", "N/A") in
    place of an omitted value (#1058); a percentage where a price was asked for
    ("15%", #1288); and a human-formatted price ("$1,234.50"). A percentage
    cannot be salvaged into an absolute level -- reading "15%" as 15 would put a
    stop at $15 on a $600 stock -- so it is dropped like a placeholder, leaving
    one bad field to null out instead of failing the whole proposal. A formatted
    price is reduced to its number.

    Anything that is not a single number is dropped the same way. A range
    ("150-160") or a hedge ("around 150") would otherwise reach pydantic, fail
    validation, and discard the whole decision, losing every field the model got
    right along with the price.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in _NULLISH_FLOAT or text.endswith("%"):
        return None
    cleaned = text.replace(",", "").lstrip("$€£¥₩").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(StrEnum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(StrEnum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Conflicting arguments alone are not a "
            "reason to Hold: commit to the stronger side, sized by how "
            "decisively it wins. Choose Hold only when the evidence is still "
            "balanced after weighing, or too thin to support a call."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including sizing guidance relative to a standard allocation. The "
            "research team does not see the caller's holdings; the trader and "
            "portfolio manager apply the actual position."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        description=(
            "Optional entry price target as an absolute number in the instrument's "
            "quote currency (e.g. 189.5), never a percentage or a range. Omit it "
            "if you cannot state a specific level."
        ),
    )
    stop_loss: float | None = Field(
        default=None,
        description=(
            "Optional stop-loss as an absolute price in the instrument's quote "
            "currency (e.g. 172.0), never a percentage. Convert a percentage "
            "distance to the price level it implies, or omit it."
        ),
    )
    position_sizing: str | None = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )

    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    # Named even when absent, so a reader can tell a level the trader chose not
    # to give from one the schema never asked for.
    for label, value in (("Entry Price", proposal.entry_price),
                         ("Stop Loss", proposal.stop_loss),
                         ("Position Sizing", proposal.position_sizing)):
        parts.extend(["", f"**{label}**: {value if value is not None and value != '' else 'not provided'}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate. "
            "Conflicting arguments alone are not a reason to Hold: commit to the "
            "stronger side, sized by how decisively it wins. Choose Hold only "
            "when the evidence is still balanced after weighing, or too thin to "
            "support a call."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: float | None = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )

    @field_validator("price_target", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    # Named even when absent: a missing line reads as a field nobody asked for,
    # so a reader cannot tell "no target" from "target not reported".
    target = decision.price_target if decision.price_target is not None else "not provided"
    parts.extend(["", f"**Price Target**: {target}"])
    parts.extend(["", f"**Time Horizon**: {decision.time_horizon or 'not provided'}"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------


class SentimentBand(StrEnum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    overall_band: SentimentBand = Field(
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when all sources are genuinely silent or non-committal."
        ),
    )
    overall_score: float = Field(
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "Use 'low' when one or more sources returned a placeholder or fewer "
            "than 5 data points; 'medium' when data is present but sparse; "
            "'high' when all three sources returned substantive data."
        ),
    )
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite message "
            "counts, ratios, notable posts); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )


def render_sentiment_report(report: SentimentReport) -> str:
    """Render a SentimentReport to the markdown shape the rest of the system expects.

    The structured header (band + score + confidence) is prepended to the
    narrative so the saved report is both human-readable and machine-parseable
    without regex.
    """
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** "
        f"(Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])


# ---------------------------------------------------------------------------
# Investment memo: the fund manager's decision flow, enforced by the schema
# ---------------------------------------------------------------------------

# A discretionary manager does not buy without a target, a downside case and a
# catalyst; the schema makes every one of those a required field, so a model that
# skips a step fails structured output and the decision is quarantined as REVIEW.
MIN_REWARD_RISK_FOR_BUY = 2.0


class Catalyst(BaseModel):
    event: str = Field(description="What closes the gap between price and value, e.g. 'Q4 earnings'")
    expected_by: str | None = Field(default=None, description="When, YYYY-MM or YYYY-MM-DD")


class InvestmentMemo(BaseModel):
    """One manager's investment memo for one instrument, as an investment committee reads it."""

    business_summary: str = Field(description="What the company does and how it earns, in two sentences")
    thesis: str = Field(description="Why the current price is wrong, in one paragraph")
    bull_case: str = Field(description="The strongest case for the position")
    bear_case: str = Field(description="The strongest case against it, stated fairly")
    valuation_basis: str = Field(description="Method and multiples used, vs own history and peers")
    current_price: float = Field(gt=0, description="Price the memo is written against, quote currency")
    price_target: float = Field(gt=0, description="12-month target, quote currency")
    downside_price: float = Field(gt=0, description="Price in the bear scenario, quote currency")
    catalysts: list[Catalyst] = Field(min_length=1, description="At least one dated catalyst")
    invalidation: list[str] = Field(
        min_length=1,
        description="Facts, not prices, that would break the thesis, e.g. 'operating margin below 12%'",
    )
    rating: PortfolioRating
    conviction: float = Field(ge=0, le=1, description="0 to 1")
    sizing_pct: float | None = Field(default=None, ge=0, le=100, description="Recommended weight, % of equity")
    exit_plan: str = Field(description="What to do at target, on invalidation, and at horizon end")
    time_horizon: str = Field(description="e.g. '6-12 months'")

    @field_validator("current_price", "price_target", "downside_price", mode="before")
    @classmethod
    def _price_text_to_float(cls, v):
        return _coerce_optional_float(v)

    @property
    def upside_pct(self) -> float:
        return (self.price_target - self.current_price) / self.current_price

    @property
    def downside_pct(self) -> float:
        return (self.downside_price - self.current_price) / self.current_price

    @property
    def reward_risk(self) -> float | None:
        """Upside over downside magnitude; None when there is no downside stated below the price."""
        if self.downside_pct >= 0:
            return None
        return self.upside_pct / -self.downside_pct

    @model_validator(mode="after")
    def _buy_needs_asymmetry(self):
        """A Buy or Overweight needs at least MIN_REWARD_RISK_FOR_BUY to one, like an IC would ask."""
        if self.rating in (PortfolioRating.BUY, PortfolioRating.OVERWEIGHT):
            rr = self.reward_risk
            if rr is None or rr < MIN_REWARD_RISK_FOR_BUY:
                raise ValueError(
                    f"{self.rating.value} requires reward/risk >= {MIN_REWARD_RISK_FOR_BUY}, "
                    f"got {rr if rr is not None else 'no downside below current price'}"
                )
        if self.downside_price > self.price_target:
            raise ValueError("downside_price must not exceed price_target")
        return self


def render_investment_memo(memo: InvestmentMemo) -> str:
    """The memo as a reader sees it, rating first so the parser reads it as the call."""
    rr = memo.reward_risk
    lines = [
        f"**Rating**: {memo.rating.value}",
        "",
        f"**Business**: {memo.business_summary}",
        "",
        f"**Thesis**: {memo.thesis}",
        "",
        f"**Bull case**: {memo.bull_case}",
        "",
        f"**Bear case**: {memo.bear_case}",
        "",
        f"**Valuation**: {memo.valuation_basis}",
        f"**Price / Target / Downside**: {memo.current_price:g} / {memo.price_target:g} / {memo.downside_price:g}"
        f" (upside {memo.upside_pct:+.1%}, downside {memo.downside_pct:+.1%}"
        + (f", reward/risk {rr:.1f}:1)" if rr is not None else ")"),
        "",
        "**Catalysts**: " + "; ".join(
            f"{c.event}" + (f" ({c.expected_by})" if c.expected_by else "") for c in memo.catalysts),
        "**Invalidation**: " + "; ".join(memo.invalidation),
        "",
        f"**Conviction**: {memo.conviction:.2f}"
        + (f" · **Sizing**: {memo.sizing_pct:g}% of equity" if memo.sizing_pct is not None else ""),
        f"**Time Horizon**: {memo.time_horizon}",
        f"**Exit Plan**: {memo.exit_plan}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sell review: the manager's periodic "is there a reason to sell?" on a holding
# ---------------------------------------------------------------------------


class SellReason(StrEnum):
    """The seven reasons a discretionary manager sells (docs/01 §12-2). Price alone is not one."""

    TARGET_REACHED = "target_reached"          # upside now smaller than downside
    THESIS_BROKEN = "thesis_broken"            # a pre-written invalidation condition is met
    BETTER_ALTERNATIVE = "better_alternative"  # same risk, more upside elsewhere
    OVERWEIGHT = "overweight"                  # position grew past its limit
    CATALYST_GONE = "catalyst_gone"            # the event passed, no next one
    WOULD_NOT_REBUY = "would_not_rebuy"        # "would I buy it today?" is no
    HARD_RULE = "hard_rule"                    # stop, trailing stop, horizon expiry, protection


class InvalidationCheck(BaseModel):
    condition: str = Field(description="The invalidation condition as written in the memo")
    met: bool
    evidence: str = Field(description="What the latest data says about it, one sentence")


class SellAction(StrEnum):
    HOLD = "Hold"
    TRIM = "Trim"
    EXIT = "Exit"


class SellReview(BaseModel):
    """One review of one holding against its memo. Reasons drive the action, not the price move."""

    checks: list[InvalidationCheck] = Field(min_length=1, description="Every memo invalidation, checked")
    reasons: list[SellReason] = Field(default_factory=list, description="Sell reasons that apply now")
    reasoning: str = Field(description="Why these reasons do or do not add up to a sale")
    action: SellAction
    trim_to_pct: float | None = Field(default=None, ge=0, le=100, description="Target weight after a Trim")
    revised_target: float | None = Field(default=None, description="New price target if the memo is updated")

    @field_validator("revised_target", mode="before")
    @classmethod
    def _price_text_to_float(cls, v):
        return _coerce_optional_float(v)

    @model_validator(mode="after")
    def _reasons_match_action(self):
        """No sale without a named reason; a met invalidation is a reason; a Trim names its target."""
        if self.action != SellAction.HOLD and not self.reasons:
            raise ValueError(f"{self.action.value} needs at least one SellReason")
        if any(c.met for c in self.checks) and SellReason.THESIS_BROKEN not in self.reasons:
            raise ValueError("an invalidation condition is met: thesis_broken must be among the reasons")
        if self.action == SellAction.TRIM and self.trim_to_pct is None:
            raise ValueError("Trim needs trim_to_pct")
        return self


def render_sell_review(review: SellReview) -> str:
    lines = [f"**Action**: {review.action.value}"
             + (f" (to {review.trim_to_pct:g}%)" if review.trim_to_pct is not None else ""),
             "",
             "**Reasons**: " + (", ".join(r.value for r in review.reasons) or "none"),
             "",
             "**Invalidation checks**:"]
    for c in review.checks:
        lines.append(f"- [{'x' if c.met else ' '}] {c.condition} — {c.evidence}")
    lines += ["", f"**Reasoning**: {review.reasoning}"]
    if review.revised_target is not None:
        lines.append(f"**Revised Target**: {review.revised_target:g}")
    return "\n".join(lines)
