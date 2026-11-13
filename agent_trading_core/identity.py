"""Prompt context the decision agents share: instrument identity, absent-report and
opening-speaker markers, portfolio block, output language.

From TradingAgents' ``agents/context.py``. The language instruction takes the
language as an argument instead of reading a global config.
"""

import functools
import logging
from collections.abc import Mapping
from typing import Any

from agent_trading_core.date_window import is_historical
from agent_trading_core.yahoo import get_company_profile

logger = logging.getLogger(__name__)


def language_instruction(language: str = "English") -> str:
    """Prompt suffix for a non-English output language; empty for English.

    The labelled lines a program reads (the ``**Rating**:`` line, the
    ``FINAL TRANSACTION PROPOSAL:`` line) keep their English label and value, so
    the rating parser never has to search translated prose.
    """
    if not language or language.strip().lower() == "english":
        return ""
    return (
        f" Write your entire response in {language}, except the labelled lines the format"
        f" asks for (the \"**Rating**:\" line, \"FINAL TRANSACTION PROPOSAL:\"):"
        f" keep their label and value in English, exactly as specified."
    )


def opponent_argument_or_opening(text: str, opponent: str) -> str:
    """Opponent's latest argument, or an explicit opening marker when empty.

    An empty opponent response interpolated into a "refute the opponent" prompt
    makes the model fabricate the other side's position.
    """
    text = (text or "").strip()
    if text:
        return text
    return f"(The {opponent} has not spoken yet — open the debate with your own case.)"


def report_or_absent(text: str, source: str) -> str:
    """An analyst's report, or a marker saying it was never produced.

    A blank interpolated into a labelled section reads as an empty finding, and
    the reading agent fills it in from nothing.
    """
    text = (text or "").strip()
    if text:
        return text
    return f"(No {source} report in this run: it is not available, not an empty finding.)"


def portfolio_context_or_notice(context: str | None) -> str:
    """The caller's portfolio block, or a notice that none was given.

    A run without portfolio context must not read as a flat book.
    """
    if isinstance(context, str) and context.strip():
        return context
    return (
        "Portfolio context: not provided. You do not know the caller's current "
        "holdings or cash, so do not assume a flat book; give direction and "
        "sizing guidance in terms the caller can apply to their own position."
    )


def _clean_identity_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "n/a", "nan", "null"}:
        return None
    return cleaned


def resolve_instrument_identity(ticker: str) -> dict:
    """Deterministic identity (company name, sector, industry, exchange) for a ticker.

    Stops the pipeline from hallucinating a different company when a chart
    pattern suggests a different industry. Fail-open: returns ``{}`` when the
    lookup fails, and the caller falls back to ticker-only context.
    """
    try:
        return _identity(ticker)
    except Exception as exc:
        logger.debug("Could not resolve instrument identity for %s: %s", ticker, exc)
        return {}


@functools.lru_cache(maxsize=256)
def _identity(ticker: str) -> dict:
    info = get_company_profile(ticker)
    identity: dict[str, str] = {}
    name = _clean_identity_value(info.get("longName")) or _clean_identity_value(info.get("shortName"))
    if name:
        identity["company_name"] = name
    for source_key, target_key in (("sector", "sector"), ("industry", "industry"),
                                   ("exchange", "exchange"), ("quoteType", "quote_type")):
        value = _clean_identity_value(info.get(source_key))
        if value:
            identity[target_key] = value
    return identity


def build_instrument_context(
    ticker: str,
    asset_type: str = "stock",
    identity: Mapping[str, str] | None = None,
    trade_date: str | None = None,
) -> str:
    """Describe the exact instrument so agents preserve identity and ticker.

    A run dated in the past gets the current name alone, as a way to tell the
    company apart, since today's sector or exchange may not have held then.
    """
    is_crypto = asset_type == "crypto"
    instrument_label = "asset" if is_crypto else "instrument"
    context = (
        f"The {instrument_label} to analyze is `{ticker}`. "
        "The tools serve this instrument; refer to it by this exact ticker in every report and recommendation, "
        "preserving any exchange suffix (e.g. `.KS`, `.KQ`, `.TO`, `.L`, `.HK`, `.T`, `-USD`)."
    )

    identity = identity or {}
    name = identity.get("company_name") or identity.get("name")
    label = "Name" if is_crypto else "Company"
    details = []
    if is_historical(trade_date):
        if name:
            details.append(
                f"{label}: {name} (its current name, given only to identify it; "
                f"on {trade_date} it may have been named differently)"
            )
    else:
        if name:
            details.append(f"{label}: {name}")
        sector, industry = identity.get("sector"), identity.get("industry")
        if sector and industry:
            details.append(f"Business classification: {sector} / {industry}")
        elif sector:
            details.append(f"Sector: {sector}")
        elif industry:
            details.append(f"Industry: {industry}")
        if identity.get("exchange"):
            details.append(f"Exchange: {identity['exchange']}")

    if details:
        context += (
            f" Resolved identity: {'; '.join(details)}. "
            "Do not substitute a different company or ticker unless a tool "
            "result explicitly disproves this resolved identity."
        )
    if is_crypto:
        context += (
            " Treat it as a crypto asset rather than a company, and do not "
            "assume company fundamentals are available."
        )
    return context


def instrument_context(ticker: str, asset_type: str = "stock", trade_date: str | None = None) -> str:
    """Resolve the identity once and render the full context string."""
    return build_instrument_context(ticker, asset_type, resolve_instrument_identity(ticker), trade_date)
