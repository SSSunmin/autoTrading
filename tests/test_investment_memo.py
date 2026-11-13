"""The investment memo enforces the manager's flow: no Buy without asymmetry, a catalyst and an invalidation."""

import pytest
from pydantic import ValidationError

from agent_trading_core.rating import parse_rating
from agent_trading_core.schemas import Catalyst, InvestmentMemo, PortfolioRating, render_investment_memo


def _memo(**over):
    base = {
        "business_summary": "Makes memory chips and phones. Earns most from DRAM and HBM.",
        "thesis": "HBM demand is underpriced after the Q3 margin print.",
        "bull_case": "HBM4 share gains lift margins through 2027.",
        "bear_case": "DRAM downcycle starts earlier than consensus.",
        "valuation_basis": "P/B 1.4x vs 10y median 1.6x; peers 2.1x.",
        "current_price": 200000, "price_target": 280000, "downside_price": 170000,
        "catalysts": [Catalyst(event="Q4 earnings", expected_by="2027-01")],
        "invalidation": ["Operating margin below 12% for two quarters"],
        "rating": PortfolioRating.OVERWEIGHT, "conviction": 0.65, "sizing_pct": 5,
        "exit_plan": "Trim half at target; exit on invalidation; review at horizon end.",
        "time_horizon": "6-12 months",
    }
    base.update(over)
    return InvestmentMemo(**base)


@pytest.mark.unit
def test_reward_risk_is_computed_from_the_three_prices():
    memo = _memo()
    assert memo.upside_pct == pytest.approx(0.40)
    assert memo.downside_pct == pytest.approx(-0.15)
    assert memo.reward_risk == pytest.approx(40 / 15)


@pytest.mark.unit
def test_a_buy_without_two_to_one_asymmetry_is_refused():
    with pytest.raises(ValidationError, match="reward/risk"):
        _memo(price_target=220000, downside_price=170000)   # 10% up, 15% down


@pytest.mark.unit
def test_a_hold_may_lack_asymmetry():
    memo = _memo(rating=PortfolioRating.HOLD, price_target=210000, downside_price=170000)
    assert memo.rating == PortfolioRating.HOLD


@pytest.mark.unit
def test_a_buy_without_a_catalyst_or_invalidation_is_refused():
    with pytest.raises(ValidationError):
        _memo(catalysts=[])
    with pytest.raises(ValidationError):
        _memo(invalidation=[])


@pytest.mark.unit
def test_prices_written_as_text_are_read():
    memo = _memo(current_price="₩200,000", price_target="280000", downside_price="170,000")
    assert memo.current_price == 200000


@pytest.mark.unit
def test_the_rendered_memo_opens_with_a_rating_the_parser_reads():
    text = render_investment_memo(_memo())
    assert text.startswith("**Rating**: Overweight")
    assert parse_rating(text) == "Overweight"
    assert "reward/risk 2.7:1" in text and "Q4 earnings (2027-01)" in text
