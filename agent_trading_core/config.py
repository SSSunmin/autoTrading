"""Explicit settings for the core. Nothing here is read from a global; pass the dict around.

TradingAgents keeps a process-wide config behind context variables. This package
takes the few values it needs as plain arguments, so a trading program can hold
its own settings object and hand the relevant piece to each call.
"""

import os

HOME = os.path.join(os.path.expanduser("~"), ".agent_trading_core")

# Alpha benchmark per exchange suffix. ``""`` is the default for unsuffixed
# (US-listed) tickers. Korea: KOSPI for .KS, KOSDAQ for .KQ.
DEFAULT_BENCHMARK_MAP = {
    ".KS":  "^KS11",
    ".KQ":  "^KQ11",
    ".T":   "^N225",
    ".HK":  "^HSI",
    ".SS":  "000001.SS",
    ".SZ":  "399001.SZ",
    ".TW":  "^TWII",
    ".NS":  "^NSEI",
    ".L":   "^FTSE",
    ".DE":  "^GDAXI",
    ".PA":  "^FCHI",
    ".TO":  "^GSPTSE",
    ".AX":  "^AXJO",
    "":     "SPY",
}


# Market rules the execution and backtest layers need, one entry per market. The two books are run apart
# (separate cash, separate limits, no FX). Keys shared by every entry: currency, benchmark, benchmark_key,
# benchmark_name_ko, timezone, session, settlement, price_limit, halt_rules, sell_tax, trade_unit,
# fractional_shares, pdt_rule. Market-only keys (KRX VI, US regulatory fees) stay in their own entry.
#
# KRX: verified 2026-11-13 against public sources (docs/01 §11-2). The static-VI reference price and the
# tick table are not settled there; confirm against the KRX business regulations before relying on them.
# US: verified 2026-11-13 (docs/01 §11-3).
MARKETS: dict[str, dict] = {}
MARKETS["KRX"] = {
    "price_limit": 0.30,                 # daily limit vs reference price, since 2015
    "vi_dynamic_kospi200": 0.03,         # dynamic volatility interruption, KOSPI200 names
    "vi_dynamic_other": 0.06,            # dynamic VI, other KOSPI and KOSDAQ names
    "vi_dynamic_close_kospi200": 0.02,   # 15:20-15:30
    "vi_dynamic_close_other": 0.04,
    "vi_static": 0.10,                   # static VI vs reference price (definition to confirm)
    "vi_halt_minutes": 2,                # single-price auction after a VI
    "sell_tax_kospi": 0.0020,            # 2026: 0.05% transaction tax + 0.15% rural special tax
    "sell_tax_kosdaq": 0.0020,           # 2026: 0.20%, no rural special tax
    "sell_tax_konex": 0.0010,
    "trade_unit": 1,
    "short_selling": "allowed_since_2025-03-31",
    # common keys
    "currency": "KRW",
    "benchmark": "^KS11",
    "benchmark_key": "kospi",
    "benchmark_name_ko": "코스피",
    "timezone": "Asia/Seoul",
    "session": "09:00-15:30",
    "settlement": "T+2",
    "halt_rules": "VI 동적 3%/6%·정적 10% → 2분 단일가, 시장 서킷브레이커 8/15/20%",
    # "sell_tax" (common key) is set from sell_tax_kospi below: the backtest universe is KOSPI
    "fractional_shares": False,
    "pdt_rule": "해당 없음",            # string, not None: the settings screen prints every value
}
# one number for the simulator (sell_tax) and paper fills (execution.py reads sell_tax_kospi)
MARKETS["KRX"]["sell_tax"] = MARKETS["KRX"]["sell_tax_kospi"]
KRX_MARKET = MARKETS["KRX"]              # backend/main.py imports this name

MARKETS["US"] = {
    "currency": "USD",
    "benchmark": "^GSPC",
    "benchmark_key": "sp500",
    "benchmark_name_ko": "S&P 500",
    "timezone": "America/New_York",
    "session": "09:30-16:00",            # regular session only; pre/after-market not used
    "settlement": "T+1",                 # since 2024-05-28 (SEC Rule 15c6-1 amendment)
    "price_limit": None,                 # no daily limit; LULD bands instead
    "halt_rules": "LULD Tier 1(S&P 500) $3 초과 ±5% 밴드 15초 이탈 → 5분 정지, 시장 서킷브레이커 7/13/20%",
    "sell_tax": 0.0,                     # no transaction tax
    # Regulatory fees on sells. Rates vary over the backtest window; one rate is applied throughout (below).
    # SEC Section 31, the rate in force on 2026-11-13: $20.60 per $1M of sales from 2026-04-04 (FY2026 fee rate advisory, 2026-02-27; $0.00
    # from 2025-05-14, $27.80 before). https://www.finra.org/rules-guidance/notices/information-notice-20260317
    "sec_fee_rate": 20.60 / 1_000_000,
    # FINRA TAF: $0.000195 per share sold, max $9.79 per trade — the regular 2026 rate. Unlike the SEC fee this is
    # not the rate in force today: the 2026 Q4 fee holiday ($0) is not applied (set by
    # SR-FINRA-2026-021). https://www.finra.org/rules-guidance/rulebooks/corporate-organization/section-1-member-regulatory-fees
    "taf_per_share": 0.000195,
    "taf_max": 9.79,
    "trade_unit": 1,
    "fractional_shares": True,           # brokers offer it; the paper book trades whole shares only
    # The $25,000 pattern-day-trader minimum was replaced by intraday margin standards on 2026-06-04
    # (SR-FINRA-2025-017, Regulatory Notice 26-10). The book holds overnight, so it does not bind either way.
    "pdt_rule": "2026-06-04 폐지(FINRA 4210 일중 증거금 기준으로 대체), 이전: 마진 계좌 $25,000 미만 5영업일 내 데이트레이드 4회 제한",
}


# Auto-execution guard rails (docs/02 v0.3, 화면 7). Amounts in KRW, limits as fractions.
# The default mode is paper trading: the plan (§8-1) runs the same auto loop on paper before
# real orders, and nothing real happens until an admin switches the mode.
# ponytail: constants until the settings screen edits them; the operations log then holds changes.
AUTO_EXECUTION = {
    "auto_daily_order_cap": 100_000_000,   # 일일 주문 금액 상한
    "auto_daily_loss_limit": -0.02,        # 일일 손실이 이 아래면 자동 정지
    "auto_order_max": 30_000_000,          # 주문당 금액 상한
    "kill_switch_reset": "admin",          # 재가동 권한
    "default_mode": "paper",               # auto | manual | paper
}


# Paper-trading gate (docs/02 §8-1, docs/05): the same auto loop runs on paper and goes live only when every
# criterion holds over the whole period. Thresholds are the 2nd-measurement rules-on drawdown with room.
PAPER_TRADING = {
    "min_weeks": 8,                      # 최소 모의 운용 기간
    "max_drawdown": -0.10,               # 기간 중 최대 드로다운 한도 (2차 실측 규칙 켬 −19%, 국면 필터 −16%보다 보수적)
    "max_daily_loss_breaches": 2,        # 일일 손실 한도(−2%) 위반 허용 횟수
    "max_stop_outs": 0,                  # −20% 정지 발동 허용 횟수
    "max_review_rate": 0.05,             # 등급을 읽지 못한(REVIEW) 결정 비율
    "max_unexplained_orders": 0,         # 결정 기록 없이 나간 주문 (감사 무결성)
}


def default_config() -> dict:
    """The settings the memory, settlement and backtest layers read.

    - ``data_cache_dir``: where OHLCV downloads are cached per symbol.
    - ``memory_log_path``: the decision log; None disables it.
    - ``memory_log_max_entries``: cap on settled entries kept; None keeps all.
    - ``holding_period_days``: trading days after entry over which a decision is scored.
    - ``benchmark_ticker``: overrides ``benchmark_map`` for every ticker when set.
    - ``results_dir``: where backtest runs keep their own logs.
    """
    return {
        "data_cache_dir": os.path.join(HOME, "cache"),
        "memory_log_path": os.path.join(HOME, "memory", "trading_memory.md"),
        "memory_log_max_entries": None,
        "holding_period_days": 5,
        "benchmark_ticker": None,
        "benchmark_map": dict(DEFAULT_BENCHMARK_MAP),
        "results_dir": os.path.join(HOME, "results"),
    }
