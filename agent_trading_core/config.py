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


# Korean market rules the execution and backtest layers need. Verified 2026-11-13
# against public sources (docs/01 §11-2). The static-VI reference price and the
# tick table are not settled there; confirm against the KRX business regulations
# before relying on them.
# ponytail: a plain dict until a second market needs the same shape.
KRX_MARKET = {
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
