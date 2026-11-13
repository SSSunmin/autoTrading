"""A sale needs a named reason; a met invalidation forces thesis_broken; a trim names its target."""

import pytest
from pydantic import ValidationError

from agent_trading_core.schemas import (
    InvalidationCheck,
    SellAction,
    SellReason,
    SellReview,
    render_sell_review,
)

OK = InvalidationCheck(condition="Operating margin below 12%", met=False, evidence="Q3 margin 14.2%")
MET = InvalidationCheck(condition="Operating margin below 12%", met=True, evidence="Q3 margin 10.8%")


@pytest.mark.unit
def test_a_hold_needs_no_reason():
    r = SellReview(checks=[OK], reasoning="thesis intact", action=SellAction.HOLD)
    assert r.reasons == []


@pytest.mark.unit
def test_an_exit_without_a_reason_is_refused():
    with pytest.raises(ValidationError, match="SellReason"):
        SellReview(checks=[OK], reasoning="price fell 8%", action=SellAction.EXIT)


@pytest.mark.unit
def test_a_met_invalidation_must_be_named_as_thesis_broken():
    with pytest.raises(ValidationError, match="thesis_broken"):
        SellReview(checks=[MET], reasons=[SellReason.OVERWEIGHT], reasoning="x", action=SellAction.TRIM, trim_to_pct=3)
    r = SellReview(checks=[MET], reasons=[SellReason.THESIS_BROKEN], reasoning="margin broke", action=SellAction.EXIT)
    assert r.action == SellAction.EXIT


@pytest.mark.unit
def test_a_trim_names_its_target_weight():
    with pytest.raises(ValidationError, match="trim_to_pct"):
        SellReview(checks=[OK], reasons=[SellReason.OVERWEIGHT], reasoning="x", action=SellAction.TRIM)


@pytest.mark.unit
def test_render_lists_checks_and_reasons():
    text = render_sell_review(SellReview(checks=[OK, MET], reasons=[SellReason.THESIS_BROKEN, SellReason.TARGET_REACHED],
                                         reasoning="both", action=SellAction.EXIT, revised_target="₩250,000"))
    assert text.startswith("**Action**: Exit")
    assert "thesis_broken, target_reached" in text and "- [x] Operating margin" in text and "250000" in text
