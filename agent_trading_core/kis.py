"""KIS Developers (한국투자증권 Open API) daily prices: KOSPI index, KRX stocks, US stocks (docs/01 §11-4).

Quotations only — no order, balance or account call is made here. Paper (모의투자) environment only: any other
``KIS_ENV`` is refused before a request is sent.

- Credentials come from the environment or the repository's ``.env`` (``KIS_APP_KEY`` · ``KIS_APP_SECRET`` ·
  ``KIS_ENV``); a missing one raises ``VendorNotConfiguredError`` naming the variable, never a value.
- The access token is cached in ``data/.kis_token.json`` (gitignored) until shortly before it expires: KIS sends a
  notice on every issuance and allows one per minute; a token lives a day.
- Calls are spaced ``CALL_INTERVAL`` apart; a throttled answer (``EGW00201``) is retried after a pause. At the
  official samples' 0.5 s a paper account was throttled on 388 of 962 calls (2026-11-13), so the spacing is 1 s.
- A call returns at most 50 rows (index) or 100 rows (stock), newest first, and silently drops the rest of a wider
  window. Windows are sized below the cap and a page that reaches the cap raises instead of leaving a gap.
- Frames have ``yahoo.load_ohlcv``'s shape: Date (naive midnight), Open, High, Low, Close, Volume, ascending.
  Prices are KIS's adjusted prices (수정주가); Yahoo's ``auto_adjust`` also takes dividends out of past prices,
  and whether KIS does is not stated in the official samples. On 2026-11-13 the gap was larger the further back
  (40 KRX names, median 3.3% in 2024-01, 0.0% in 2026-11; docs/01 §11-4-1).

Endpoints (official samples, github.com/koreainvestment/open-trading-api, examples_llm/):
  token        POST /oauth2/tokenP
  index daily  GET /uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice  FHKUP03500100 (U, 0001 = KOSPI)
  stock daily  GET /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice   FHKST03010100 (J, 100 rows/call)
  US daily     GET /uapi/overseas-price/v1/quotations/dailyprice                     HHDFS76240000 (EXCD NAS/NYS/AMS)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from agent_trading_core.errors import NoMarketDataError, VendorNotConfiguredError, VendorUnavailableError
from agent_trading_core.files import replace_file

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "data" / ".kis_token.json"
BASE_URL = {"paper": "https://openapivts.koreainvestment.com:29443"}   # 실전(openapi…:9443) deliberately absent
CALL_INTERVAL = 1.0            # seconds between calls on a paper account
TOKEN_MARGIN = 600             # re-issue this many seconds before the stated expiry
# (calendar days per call, rows one call returns at most): 60 days ≤ 44 weekdays < 50, 130 days ≤ 94 < 100
INDEX_WINDOW = (60, 50)
STOCK_WINDOW = (130, 100)
THROTTLED = "EGW00201"
TOKEN_ERRORS = {"EGW00121", "EGW00123"}   # 유효하지 않은 token · 기간이 만료된 token
RETRIES = 3
KST = ZoneInfo("Asia/Seoul")
REQUIRED = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ENV")

INDEX_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
STOCK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
US_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
INDEX_FIELDS = {"stck_bsop_date": "Date", "bstp_nmix_oprc": "Open", "bstp_nmix_hgpr": "High",
                "bstp_nmix_lwpr": "Low", "bstp_nmix_prpr": "Close", "acml_vol": "Volume"}
STOCK_FIELDS = {"stck_bsop_date": "Date", "stck_oprc": "Open", "stck_hgpr": "High", "stck_lwpr": "Low",
                "stck_clpr": "Close", "acml_vol": "Volume"}
US_FIELDS = {"xymd": "Date", "open": "Open", "high": "High", "low": "Low", "clos": "Close", "tvol": "Volume"}
INDEX_CODES = {"^KS11": "0001"}


def credentials() -> dict[str, str]:
    """The KIS settings from the environment, after the repository ``.env`` (which does not override it)."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise VendorNotConfiguredError(f"KIS 설정 없음: {', '.join(missing)} — 환경변수나 저장소 루트 .env에 넣는다")
    env = os.environ["KIS_ENV"]
    if env not in BASE_URL:
        raise VendorNotConfiguredError(f"KIS_ENV={env!r}: paper(모의투자)만 지원한다 — 실전 키로 시작하지 않는다")
    return {"appkey": os.environ["KIS_APP_KEY"], "appsecret": os.environ["KIS_APP_SECRET"], "env": env}


def date_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """[start, end] cut into consecutive inclusive windows of at most ``days`` calendar days."""
    out, d = [], start
    while d <= end:
        last = min(d + timedelta(days=days - 1), end)
        out.append((d, last))
        d = last + timedelta(days=1)
    return out


def to_frame(rows: list[dict], fields: dict[str, str], symbol: str) -> pd.DataFrame:
    """KIS output rows → Date (naive), Open, High, Low, Close (float), Volume (int), ascending, one row per date.
    Rows without a date (KIS pads empty slots with blank strings) or without a close are dropped and other gaps
    filled forward, as ``yahoo._fill_price_gaps`` does; none left raises."""
    df = pd.DataFrame([{v: r.get(k, "") for k, v in fields.items()} for r in rows], columns=list(fields.values()))
    df = df[df["Date"].astype(str).str.fullmatch(r"\d{8}")]
    if df.empty:
        raise NoMarketDataError(symbol, symbol, "no price rows from KIS")
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d")
    for c in ("Open", "High", "Low", "Close"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("int64")
    df = df.dropna(subset=["Close"]).drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    if df.empty:
        raise NoMarketDataError(symbol, symbol, "no closing price in the KIS rows")
    df[["Open", "High", "Low"]] = df[["Open", "High", "Low"]].ffill().bfill()
    return df


class KisClient:
    """One credential set, its token, and the call spacing. ``now`` / ``sleep`` are injectable for tests."""

    def __init__(self, creds: dict[str, str] | None = None, *, session=None, token_file: Path = TOKEN_FILE,
                 now=time.time, sleep=time.sleep):
        self.creds = creds or credentials()
        self.base = BASE_URL[self.creds["env"]]
        self.session = session or requests.Session()
        self.token_file = Path(token_file)
        self.now, self.sleep = now, sleep
        self._token: tuple[str, float] | None = None
        self._last_call = 0.0
        self.stats = {"token_issued": 0, "token_reused": 0, "calls": 0, "throttled": 0}

    # --- token ---------------------------------------------------------------
    def _key_id(self) -> str:
        """Which app key a saved token belongs to: a short hash, so a replaced key is not sent an old token."""
        return hashlib.sha256(self.creds["appkey"].encode()).hexdigest()[:12]

    def token(self) -> str:
        if self._token is None and self.token_file.exists():
            saved = json.loads(self.token_file.read_text(encoding="utf-8"))
            if saved.get("env") == self.creds["env"] and saved.get("key_id") == self._key_id():
                self._token = (saved["access_token"], float(saved["expires_at"]))
        if self._token and self.now() < self._token[1] - TOKEN_MARGIN:
            self.stats["token_reused"] += 1
            return self._token[0]
        try:
            r = self.session.post(f"{self.base}/oauth2/tokenP", json={
                "grant_type": "client_credentials", "appkey": self.creds["appkey"],
                "appsecret": self.creds["appsecret"]}, timeout=10)
        except requests.RequestException as e:
            raise VendorUnavailableError(f"KIS 토큰 요청 실패: {type(e).__name__}") from None
        body = _json(r)
        if r.status_code != 200 or "access_token" not in body:
            # the body carries only an error code and message, never the key
            raise VendorUnavailableError(f"KIS 토큰 발급 실패: HTTP {r.status_code} {body.get('error_code', '')} "
                                         f"{body.get('error_description', '')}".strip())
        expires = datetime.strptime(body["access_token_token_expired"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        self._token = (body["access_token"], expires.timestamp())
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        replace_file(self.token_file, lambda tmp: Path(tmp).write_text(json.dumps(
            {"env": self.creds["env"], "key_id": self._key_id(), "access_token": self._token[0],
             "expires_at": self._token[1]}), encoding="utf-8"))
        self.stats["token_issued"] += 1
        return self._token[0]

    # --- calls ---------------------------------------------------------------
    def get(self, path: str, tr_id: str, params: dict) -> dict:
        reissued, attempt = False, 0           # a token re-issue does not use up a throttle retry
        while True:
            wait = self._last_call + CALL_INTERVAL - self.now()
            if wait > 0:
                self.sleep(wait)
            self._last_call = self.now()
            self.stats["calls"] += 1
            headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token()}",
                       "appkey": self.creds["appkey"], "appsecret": self.creds["appsecret"], "tr_id": tr_id,
                       "custtype": "P"}
            try:
                r = self.session.get(f"{self.base}{path}", params=params, timeout=10, headers=headers)
            except requests.RequestException as e:   # no URL or header in the message
                raise VendorUnavailableError(f"KIS {tr_id} 요청 실패: {type(e).__name__}") from None
            body = _json(r)
            if body.get("msg_cd") in TOKEN_ERRORS and not reissued:
                self._token, reissued = None, True    # revoked or expired early: one new token, then the call again
                self.token_file.unlink(missing_ok=True)
                continue
            if body.get("msg_cd") == THROTTLED and attempt < RETRIES:
                self.stats["throttled"] += 1
                self.sleep(1.0 + attempt)
                attempt += 1
                continue
            if r.status_code != 200 or body.get("rt_cd") != "0":
                raise VendorUnavailableError(f"KIS {tr_id} 실패: HTTP {r.status_code} {body.get('msg_cd', '')} "
                                             f"{body.get('msg1', '')}".strip())
            return body

    def _windows(self, path: str, tr_id: str, window: tuple[int, int], start: date, end: date, params: dict,
                 symbol: str) -> list[dict]:
        days, cap = window
        rows = []
        for a, b in date_chunks(start, end, days):
            page = self.get(path, tr_id, {**params, "FID_INPUT_DATE_1": f"{a:%Y%m%d}", "FID_INPUT_DATE_2": f"{b:%Y%m%d}",
                                          "FID_PERIOD_DIV_CODE": "D"}).get("output2") or []
            dated = [r for r in page if str(r.get("stck_bsop_date", "")).isdigit()]
            if len(dated) >= cap:
                raise VendorUnavailableError(f"KIS {tr_id} {symbol} {a}~{b}: {len(dated)}행 = 한 번 상한 {cap} — "
                                             "잘렸을 수 있어 쓰지 않는다")
            rows += dated
        return rows

    def index_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        rows = self._windows(INDEX_PATH, "FHKUP03500100", INDEX_WINDOW, start, end,
                             {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": INDEX_CODES[symbol]}, symbol)
        return to_frame(rows, INDEX_FIELDS, symbol)

    def stock_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """KRX stock by Yahoo-style symbol (``005930.KS``), adjusted prices (FID_ORG_ADJ_PRC 0)."""
        rows = self._windows(STOCK_PATH, "FHKST03010100", STOCK_WINDOW, start, end,
                             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol.split(".")[0],
                              "FID_ORG_ADJ_PRC": "0"}, symbol)
        return to_frame(rows, STOCK_FIELDS, symbol)

    def us_daily(self, symbol: str, exchange: str, start: date, end: date) -> pd.DataFrame:
        """US stock (``exchange`` NAS · NYS · AMS), adjusted prices (MODP 1). The endpoint returns the rows up to
        a base date, newest first, so it pages backward from ``end`` until it passes ``start``."""
        rows, base = [], end
        while base >= start:
            page = self.get(US_PATH, "HHDFS76240000", {"AUTH": "", "EXCD": exchange, "SYMB": symbol, "GUBN": "0",
                                                      "BYMD": f"{base:%Y%m%d}", "MODP": "1"}).get("output2") or []
            dated = [r for r in page if str(r.get("xymd", "")).isdigit()]
            if not dated:
                break
            rows += dated
            earliest = datetime.strptime(min(r["xymd"] for r in dated), "%Y%m%d").date()
            if earliest >= base:
                break
            base = earliest - timedelta(days=1)
        df = to_frame(rows, US_FIELDS, symbol)
        return df[df["Date"] >= pd.Timestamp(start)].reset_index(drop=True)


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


def load_kis_ohlcv(symbol: str, as_of_date: str, cache_dir: str, client: KisClient | None = None,
                   start: str | None = None) -> pd.DataFrame:
    """``yahoo.load_ohlcv`` for KRX symbols from KIS: cached as ``<symbol>-KIS-data.csv`` beside the Yahoo files
    (never over them), reused under ``yahoo._cache_is_fresh`` (written today; for an as-of of today, 15 minutes)
    and otherwise fetched in full (``start`` default: five years before today), rows after ``as_of_date`` dropped.
    ``^KS11`` is the KOSPI index; other symbols must be ``.KS`` / ``.KQ`` stocks.

    ponytail: a refresh refetches all five years (~15 calls a stock at 1 s, ~10 minutes for 40) — fine for a
    backtest with a past as-of, too slow for an intraday as-of every 15 minutes. Before live use, append only the
    days after the cache's last date (docs/00 §7)."""
    from agent_trading_core.symbols import safe_ticker_component
    from agent_trading_core.yahoo import _cache_is_fresh, assert_ohlcv_not_stale

    path = Path(cache_dir) / f"{safe_ticker_component(symbol)}-KIS-data.csv"
    today = datetime.now(KST).date()
    # the Yahoo rule: written today, and for an as-of of today no older than 15 minutes (intraday bars move)
    fresh = path.exists() and _cache_is_fresh(path, pd.Timestamp(as_of_date).normalize(), pd.Timestamp.today())
    if not fresh:
        client = client or KisClient()
        first = date.fromisoformat(start) if start else today - timedelta(days=5 * 365)
        if symbol in INDEX_CODES:
            df = client.index_daily(symbol, first, today)
        elif symbol.endswith((".KS", ".KQ")):
            df = client.stock_daily(symbol, first, today)
        else:
            raise ValueError(f"{symbol}: KIS 로더는 국내 종목·지수만 캐시한다(미국은 KisClient.us_daily에 거래소 코드)")
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, lambda tmp: df.to_csv(tmp, index=False, encoding="utf-8"))
    data = pd.read_csv(path, parse_dates=["Date"])
    data = data[data["Date"] <= pd.Timestamp(as_of_date)].reset_index(drop=True)
    if data.empty:
        raise NoMarketDataError(symbol, symbol, f"no KIS rows on or before {as_of_date}")
    assert_ohlcv_not_stale(data, as_of_date, symbol)
    return data
