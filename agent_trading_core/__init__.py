"""agent_trading_core: the judgment-safety and scoring layer extracted from TradingAgents.

What is here, and what each part protects against:

- ``date_window``   point-in-time rules: a tool never serves data past the run date
- ``yahoo``         cached OHLCV filtered to the run date, stale-frame guard, closes, profile
- ``snapshot``      deterministic price/indicator table the model must quote from
- ``identity``      resolved company identity and the shared prompt markers
- ``schemas``       typed decisions (ResearchPlan, TraderProposal, PortfolioDecision)
- ``rating``        5-tier rating parser; unreadable decisions become REVIEW, never Hold
- ``structured``    structured-output call with free-text fallback
- ``portfolio``     the caller's book as the agents see it
- ``memory``        decision log, settlement against a benchmark, reflection
- ``backtest``      grid runner and rating-bucket scoring for any decide(ticker, date)

Derived from TauricResearch/TradingAgents v0.7.1 (Apache-2.0). See NOTICE.
"""

__version__ = "0.1.0"
