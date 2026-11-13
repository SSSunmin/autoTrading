"""Yahoo Finance access: retry, empty-vs-outage reporting, cached point-in-time OHLCV,
closes for settlement and the company profile.

Merged from TradingAgents' ``vendors/yahoo/common.py``, ``ohlcv.py``, ``market.get_closes``
and ``fundamentals.get_company_profile``. The process-wide config lookup is replaced
by an explicit ``cache_dir`` argument.
"""

import logging
import math
import os
import time

import pandas as pd
import requests
import yfinance as yf
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from agent_trading_core.config import default_config
from agent_trading_core.errors import NoMarketDataError, VendorUnavailableError
from agent_trading_core.files import replace_file
from agent_trading_core.symbols import normalize_symbol, safe_ticker_component

logger = logging.getLogger(__name__)

YAHOO_HOST = "https://query2.finance.yahoo.com"

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale: generous enough to span long holiday weekends, tight
# enough to catch a year-old frame yfinance occasionally returns.
MAX_OHLCV_STALE_DAYS = 10

# How long a same-day cache that does not yet reach the requested day may be
# reused before it is refetched. Yahoo publishes a partial daily candle during
# market hours, whose Close is not the closing price.
OHLCV_CACHE_TTL_SECONDS = 900


# --- shared -----------------------------------------------------------------

def price_text(value: float) -> str:
    """A price with six significant digits and at least two decimals."""
    if not math.isfinite(value):
        return str(value)
    decimals = 2 if value == 0 else max(2, 5 - math.floor(math.log10(abs(value))))
    whole, _, fraction = f"{value:.{decimals}f}".partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def vendor_reachable(url: str, timeout: float = 5.0) -> bool:
    """Whether the vendor answers at all, for telling silence from an outage."""
    try:
        requests.head(url, timeout=timeout, allow_redirects=True)
        return True
    except requests.RequestException:
        return False


def raise_for_empty(symbol: str, canonical: str, what: str) -> None:
    """Report an empty Yahoo answer as an absence, or as an outage if it is one."""
    if not vendor_reachable(YAHOO_HOST):
        raise VendorUnavailableError(f"Yahoo Finance is unreachable; no {what} was retrieved")
    raise NoMarketDataError(symbol, canonical, f"no {what}")


# yfinance answers some failed requests with an empty result, which would read as
# "no data" for a symbol nobody checked; raised, the failure is reported as one.
yf.config.debug.hide_exceptions = False


def _answered_empty(exc: Exception) -> bool:
    """Whether Yahoo answered that it has nothing: no such symbol (404), or a
    price window with no prices in it."""
    if isinstance(exc, YFPricesMissingError):
        reason = exc.yahoo_reason
        return "status_code" not in (exc.debug_info or "") and (
            reason is None or reason.startswith("Data doesn't exist"))
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Run a yfinance call with exponential backoff on rate limits.

    A rate limit that outlasts the retries, or any other failure, is raised as
    VendorUnavailableError. Yahoo answering that it has nothing returns None.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError as exc:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning("Yahoo Finance rate limited, retrying in %.0fs (attempt %d/%d)",
                               delay, attempt + 1, max_retries)
                time.sleep(delay)
            else:
                raise VendorUnavailableError(
                    f"Yahoo Finance rate limited after {max_retries} retries: {exc}"
                ) from exc
        except Exception as exc:
            if _answered_empty(exc):
                return None
            raise VendorUnavailableError(f"Yahoo Finance request failed: {type(exc).__name__}") from exc


# --- OHLCV -------------------------------------------------------------------

def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _local_midnight(value) -> pd.Timestamp:
    if pd.isna(value):
        return pd.NaT
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def _normalize_dates(dates) -> pd.Series:
    """Naive, midnight-normalized dates, normalized per element so a series with
    mixed UTC offsets (five years of bars across daylight-saving changes) keeps
    each bar's own local date."""
    return pd.to_datetime(pd.Series(dates).map(_local_midnight))


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    data = _ensure_date_column(data)
    data["Date"] = _normalize_dates(data["Date"])
    data = data.dropna(subset=["Date"]).copy()
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    return data


def _fill_price_gaps(data: pd.DataFrame) -> pd.DataFrame:
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data = data.dropna(subset=["Close"]).copy()
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def assert_ohlcv_not_stale(
    data: pd.DataFrame,
    as_of_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than as_of_date.

    Guards the dangerous case of present-but-stale rows: a vendor returning a
    year-old frame that would otherwise feed wrong prices to the agent.
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol, canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def _cache_is_fresh(data_file, as_of_dt, now) -> bool:
    written = pd.Timestamp.fromtimestamp(os.path.getmtime(data_file))
    if written.date() != now.date():
        return False
    return as_of_dt.date() < now.date() or (now - written).total_seconds() <= OHLCV_CACHE_TTL_SECONDS


def load_ohlcv(symbol: str, as_of_date: str, cache_dir: str | None = None,
               fill_gaps: bool = True) -> pd.DataFrame:
    """OHLCV up to and including ``as_of_date``, cached per symbol, never later.

    Downloads 5 years up to today once per day per symbol. Rows after
    ``as_of_date`` are dropped so a backtest never sees future prices. With
    ``fill_gaps`` False the frame is returned as the vendor reported it, for
    callers that quote the numbers themselves.
    """
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)
    cache_dir = cache_dir or default_config()["data_cache_dir"]
    as_of_dt = pd.to_datetime(as_of_date).normalize()

    now = pd.Timestamp.today()
    start_str = (now - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    # yfinance ``end`` is exclusive; ask for tomorrow so today's row is included.
    end_str = (now + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(cache_dir, exist_ok=True)
    data_file = os.path.join(cache_dir, f"{safe_symbol}-YFin-data.csv")

    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns and _cache_is_fresh(data_file, as_of_dt, now):
            data = cached

    if data is None:
        downloaded = yf_retry(lambda: yf.Ticker(canonical).history(
            start=start_str, end=end_str, auto_adjust=True, actions=False,
        ))
        if downloaded is None:
            raise_for_empty(symbol, canonical, "price rows")
        downloaded = _ensure_date_column(downloaded.reset_index())
        if downloaded.empty or "Close" not in downloaded.columns:
            raise_for_empty(symbol, canonical, "price rows")
        replace_file(data_file, lambda temp: downloaded.to_csv(temp, index=False, encoding="utf-8"))
        data = downloaded

    data = _clean_dataframe(data)
    data = data[data["Date"] <= as_of_dt]

    if not data.empty and pd.isna(data["Close"].iloc[-1]):
        settled = data["Close"].notna().to_numpy().nonzero()[0]
        if settled.size == 0:
            raise NoMarketDataError(symbol, canonical, "no bar in range has a closing price")
        logger.warning("%s: %d trailing bar(s) through %s have no closing price; using %s as the latest close.",
                       canonical, len(data) - settled[-1] - 1,
                       data["Date"].iloc[-1].date(), data["Date"].iloc[settled[-1]].date())

    data = _fill_price_gaps(data) if fill_gaps else data.dropna(subset=["Close"]).copy()
    assert_ohlcv_not_stale(data, as_of_date, symbol, canonical)
    return data


# --- closes and profile --------------------------------------------------------

def get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Daily closes from ``start_date`` up to, not including, ``end_date``."""
    canonical = normalize_symbol(symbol)
    history = yf_retry(lambda: yf.Ticker(canonical).history(start=start_date, end=end_date))
    return history["Close"] if history is not None and "Close" in history else pd.Series(dtype=float)


def get_company_profile(ticker: str) -> dict:
    """Yahoo's current profile for ``ticker``: name, sector, industry and the like."""
    return yf_retry(lambda: yf.Ticker(normalize_symbol(ticker)).info) or {}
