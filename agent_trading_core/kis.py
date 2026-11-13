"""KIS Developers (한국투자증권 Open API) daily prices: KOSPI index, KRX stocks, US stocks (docs/01 §11-4).

Quotations only — no order, balance or account call is made here. Paper (모의투자) environment only: any other
``KIS_ENV`` is refused before a request is sent.

- Credentials come from the environment or the repository's ``.env`` (``KIS_APP_KEY`` · ``KIS_APP_SECRET`` ·
  ``KIS_ENV``; ``TRADING_ENV_FILE`` names another file); a missing one raises ``VendorNotConfiguredError`` naming the
  variable, never a value.
- The access token is cached in ``data/.kis_token.json`` (gitignored; under ``TRADING_DATA_DIR`` when set) until shortly before it expires: KIS sends a
  notice on every issuance and allows one per minute; a token lives a day.
- Calls are spaced ``CALL_INTERVAL`` apart; a throttled answer (``EGW00201``) is retried after a pause. At the
  official samples' 0.5 s a paper account was throttled on 388 of 962 calls, at 1.0 s on 14 of 29 (2026-11-13),
  at 1.1 s on 0 of 15 — so the spacing is 1.1 s.
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
  US index     GET /uapi/overseas-price/v1/quotations/inquire-daily-chartprice      FHKST03030100 (N, SPX, 100 rows/call)

Daily use (``update_cache`` · ``load_ohlcv``, scripts/refresh_cache.py): a cache is brought up to the last bar whose
day has passed (``last_final_day``) by fetching from its last date on, not five years again. The last date is fetched again and its
close compared: KIS re-adjusts past prices after a split or rights issue, and a changed close means the whole
history is taken again rather than joined onto rows on the old basis.

Today's bar (``fetch_today_close``, scripts/daily_loop.py): the 15:35 decision needs the day's close before it is
final. It is written into the cache as a provisional row and its date listed in ``<cache_dir>/_provisional.json``;
the next ``update_cache`` drops the listed rows, fetches those days as final bars and takes the symbol off the list.
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
from agent_trading_core.files import locked, replace_file
from agent_trading_core.symbols import kis_us_symbol, safe_ticker_component

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path(os.environ.get("TRADING_DATA_DIR") or ROOT / "data") / ".kis_token.json"
BASE_URL = {"paper": "https://openapivts.koreainvestment.com:29443"}   # 실전(openapi…:9443) deliberately absent
CALL_INTERVAL = 1.1            # seconds between calls on a paper account (1.0 was throttled half the time)
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
US_INDEX_PATH = "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
US_INDEX_FIELDS = {"stck_bsop_date": "Date", "ovrs_nmix_oprc": "Open", "ovrs_nmix_hgpr": "High",
                   "ovrs_nmix_lwpr": "Low", "ovrs_nmix_prpr": "Close", "acml_vol": "Volume"}
US_INDEX_WINDOW = (130, 100)          # FHKST03030100 also stops at 100 rows (2026-01-01 ~ 11-13 came back as 100)
US_INDEX_CODES = {"^GSPC": "SPX"}
US_EXCHANGES = ("NAS", "NYS", "AMS")  # a symbol asked on the wrong exchange comes back with no rows, not an error
EXCHANGE_FILE = "_kis_us_exchange.json"
BACKFILL_DAYS = 5 * 365
CLOSE_TOL = 1e-6                      # relative change of the overlap day's close that counts as a re-adjustment
PROVISIONAL_FILE = "_provisional.json"   # {"YYYY-MM-DD": [symbols whose row of that day is provisional]}


def credentials() -> dict[str, str]:
    """The KIS settings from the environment, after the repository ``.env`` or ``TRADING_ENV_FILE`` (neither
    overrides it)."""
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("TRADING_ENV_FILE") or ROOT / ".env", override=False)
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise VendorNotConfiguredError(f"KIS 설정 없음: {', '.join(missing)} — 환경변수나 저장소 루트 .env(또는 TRADING_ENV_FILE)에 넣는다")
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
            page = self.get(US_PATH, "HHDFS76240000", {"AUTH": "", "EXCD": exchange, "SYMB": kis_us_symbol(symbol), "GUBN": "0",
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

    def us_index_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        rows = self._windows(US_INDEX_PATH, "FHKST03030100", US_INDEX_WINDOW, start, end,
                             {"FID_COND_MRKT_DIV_CODE": "N", "FID_INPUT_ISCD": US_INDEX_CODES[symbol]}, symbol)
        return to_frame(rows, US_INDEX_FIELDS, symbol)

    def us_exchange(self, symbol: str) -> str | None:
        """The first of NAS · NYS · AMS that has rows for ``symbol`` (one call each until found)."""
        for ex in US_EXCHANGES:
            page = self.get(US_PATH, "HHDFS76240000", {"AUTH": "", "EXCD": ex, "SYMB": kis_us_symbol(symbol), "GUBN": "0",
                                                       "BYMD": "", "MODP": "1"}).get("output2") or []
            if any(str(r.get("xymd", "")).isdigit() for r in page):
                return ex
        return None


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


def market_of(symbol: str) -> str:
    return "KRX" if symbol in INDEX_CODES or symbol.endswith((".KS", ".KQ")) else "US"


def last_final_day(market: str, now: datetime | None = None) -> date:
    """The latest day whose bar is final: the day before today in the market's time zone. KIS keeps moving today's
    bar after the close — on 2026-11-13 005930's close read 207,000 at 17:47 and 207,500 at 19:19 (the same under
    J · UN · NX: the alternative exchange's after-market runs to 20:00) — while past days hold the KRX close. A fetch
    stops at the day before, so a moving bar never reaches a cache (and never trips the re-adjustment check)."""
    from agent_trading_core.config import MARKETS

    local = (now or datetime.now(KST)).astimezone(ZoneInfo(MARKETS[market]["timezone"]))
    return local.date() - timedelta(days=1)


def cache_path(symbol: str, cache_dir: str | Path) -> Path:
    return Path(cache_dir) / f"{safe_ticker_component(symbol)}-KIS-data.csv"


def _us_exchange(client: KisClient, symbol: str, cache_dir: str | Path) -> str:
    """The symbol's exchange, found once and kept in ``<cache_dir>/_kis_us_exchange.json``."""
    path = Path(cache_dir) / EXCHANGE_FILE
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if symbol not in known:
        ex = client.us_exchange(symbol)
        if ex is None:
            raise NoMarketDataError(symbol, symbol, f"KIS {'/'.join(US_EXCHANGES)} 어디에도 시세 없음")
        known[symbol] = ex
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, lambda tmp: Path(tmp).write_text(json.dumps(known, indent=1, sort_keys=True),
                                                            encoding="utf-8"))
    return known[symbol]


def fetch(client: KisClient, symbol: str, start: date, end: date, cache_dir: str | Path) -> pd.DataFrame:
    """Daily bars of a Yahoo-style symbol from ``start`` to ``end``: ``^KS11`` · ``.KS``/``.KQ`` · ``^GSPC`` · a US
    stock (its exchange looked up once)."""
    if symbol in INDEX_CODES:
        df = client.index_daily(symbol, start, end)
    elif symbol.endswith((".KS", ".KQ")):
        df = client.stock_daily(symbol, start, end)
    elif symbol in US_INDEX_CODES:
        df = client.us_index_daily(symbol, start, end)
    elif symbol.startswith("^") or "." in symbol:
        raise ValueError(f"{symbol}: KIS 로더가 모르는 심볼(지수는 {sorted(INDEX_CODES | US_INDEX_CODES)})")
    else:
        df = client.us_daily(symbol, _us_exchange(client, symbol, cache_dir), start, end)
    return df[df["Date"] <= pd.Timestamp(end)].reset_index(drop=True)


def read_cache(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, parse_dates=["Date"])


def provisional(cache_dir: str | Path) -> dict[str, list[str]]:
    """``_provisional.json``: per date, the symbols whose cached row of that day is provisional."""
    path = Path(cache_dir) / PROVISIONAL_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _mark_provisional(cache_dir: str | Path, symbol: str, day: str | None) -> None:
    """Take ``symbol`` off every date of the list, then (``day`` given) list it under ``day``."""
    path = Path(cache_dir) / PROVISIONAL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        marks = {d: [s for s in syms if s != symbol] for d, syms in provisional(cache_dir).items()}
        if day is not None:
            marks[day] = sorted([*marks.get(day, []), symbol])
        marks = {d: syms for d, syms in sorted(marks.items()) if syms}
        replace_file(path, lambda tmp: Path(tmp).write_text(json.dumps(marks, indent=1), encoding="utf-8"))
        if provisional(cache_dir) != marks:
            raise OSError(f"{PROVISIONAL_FILE}를 바꾸지 못했다(다른 프로세스가 파일을 열고 있음?)")


def _write_checked(symbol: str, path: Path, merged: pd.DataFrame) -> None:
    """Check ``merged`` (dates unique and rising, every close present), replace the file in one move, read it back."""
    if not (merged["Date"].is_unique and merged["Date"].is_monotonic_increasing and merged["Close"].notna().all()):
        raise ValueError(f"{symbol}: KIS 캐시 검사 실패(날짜 중복·역순 또는 종가 빈 칸) — 파일을 바꾸지 않는다")
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda tmp: merged.to_csv(tmp, index=False, encoding="utf-8"))
    back = read_cache(path)
    if len(back) != len(merged) or back["Date"].iloc[-1] != merged["Date"].iloc[-1] or \
            not (back["Close"].to_numpy() == merged["Close"].to_numpy()).all():   # a refetch keeps span and rows
        # replace_file keeps the old file when another process holds it open (Windows) and only logs it
        raise OSError(f"{symbol}: KIS 캐시를 바꾸지 못했다(다른 프로세스가 파일을 열고 있음?) — 이전 파일 그대로")


def update_cache(symbol: str, cache_dir: str | Path, client: KisClient, *, since: str | None = None,
                 start: str | None = None, now: datetime | None = None) -> dict:
    """Bring ``<symbol>-KIS-data.csv`` up to ``last_final_day`` and say what changed.

    No file: the full history from ``start`` (default five years back) — status ``full``. A file: its last date
    (or ``since``) to the last final day, joined with a fetched date's new values winning — ``append``; nothing
    to fetch — ``current``; a fetch that returns no row at all, though it asks for the last cached day again —
    ``empty`` (halted, delisted or a wrong code, not a holiday). Rows dated after the last final day were not final
    when written and are dropped (``trim`` when nothing else changed). ``since`` later than the last date starts at
    the last date instead, so no gap opens. If the earliest refetched day's close moved (KIS re-adjusted its past
    prices) the whole span of the file is taken again — ``refetch``. The file is replaced in one move after the new
    frame is checked (dates unique and rising, every close present) and read back; a failure, or a file another
    process holds open, raises and leaves the old file as it was. Rows listed as provisional (``fetch_today_close``)
    are dropped first, so their days are fetched as final bars; the symbol then leaves the list."""
    path = cache_path(symbol, cache_dir)
    end = last_final_day(market_of(symbol), now)
    marked = [d for d, syms in provisional(cache_dir).items() if symbol in syms]
    old = read_cache(path) if path.exists() else None
    written = 0 if old is None else len(old)
    if old is not None:
        keep = (old["Date"] <= pd.Timestamp(end)) & ~old["Date"].isin(pd.to_datetime(marked))
        old = old[keep].reset_index(drop=True)
    if old is None or old.empty:
        first = date.fromisoformat(start) if start else end - timedelta(days=BACKFILL_DAYS)
        new = fetch(client, symbol, first, end, cache_dir)
        status, merged = "full", new
    else:
        last = old["Date"].iloc[-1].date()
        new, quiet = old.iloc[0:0], "current"
        if since is not None or last < end:
            try:
                new = fetch(client, symbol, min(date.fromisoformat(since), last) if since else last, end, cache_dir)
            except NoMarketDataError:  # not even the last cached day came back
                quiet = "empty"
        if new.empty and len(old) == written:
            if marked:                 # listed, but the row never reached the file
                _mark_provisional(cache_dir, symbol, None)
            return {"symbol": symbol, "status": quiet, "rows_added": 0, "last": str(last)}
        both = new.merge(old[["Date", "Close"]], on="Date", suffixes=("", "_old"))
        if not both.empty and abs(both["Close"].iloc[0] / both["Close_old"].iloc[0] - 1) > CLOSE_TOL:
            new = fetch(client, symbol, old["Date"].iloc[0].date(), end, cache_dir)
            status, merged = "refetch", new
        else:
            status = "append" if len(new) else "trim"
            merged = (pd.concat([old, new], ignore_index=True).drop_duplicates("Date", keep="last")
                      .sort_values("Date").reset_index(drop=True))
    _write_checked(symbol, path, merged)
    if marked:
        _mark_provisional(cache_dir, symbol, None)
    return {"symbol": symbol, "status": status, "rows_added": len(merged) - written,
            "last": merged["Date"].iloc[-1].strftime("%Y-%m-%d")}


def fetch_today_close(market: str, tickers: list[str], cache_dir: str | Path, client: KisClient,
                      now: datetime | None = None) -> dict:
    """Today's bar (the market's local date) of each ticker, written into its cache as a provisional row. Before
    the close its Close is the latest trade; after it, the close as KIS has it so far (``last_final_day``).

    The date is listed in ``_provisional.json`` before the row is written: a crash in between leaves a listed date
    with no row, which ``update_cache`` simply fetches — never an unlisted provisional row it would keep as final.
    A ticker needs a cache file (``update_cache`` first). Returns the day, the tickers written, the ones with no
    row of that day (a holiday when the benchmark is among them) and the failures."""
    from agent_trading_core.config import MARKETS

    day = (now or datetime.now(KST)).astimezone(ZoneInfo(MARKETS[market]["timezone"])).date()
    out: dict = {"date": day.isoformat(), "written": [], "missing": [], "failed": []}
    for t in tickers:
        try:
            path = cache_path(t, cache_dir)
            if not path.exists():
                raise FileNotFoundError(f"{t}: KIS 캐시 없음 — update_cache 먼저")
            try:
                bar = fetch(client, t, day, day, cache_dir)
            except NoMarketDataError:
                bar = None
            if bar is None or bar.empty or bar["Date"].iloc[-1] != pd.Timestamp(day):
                out["missing"].append(t)
                continue
            old = read_cache(path)
            merged = (pd.concat([old[old["Date"] != pd.Timestamp(day)], bar.tail(1)], ignore_index=True)
                      .sort_values("Date").reset_index(drop=True))
            _mark_provisional(cache_dir, t, day.isoformat())
            _write_checked(t, path, merged)
            out["written"].append(t)
        except Exception as e:  # one ticker's failure is reported, the rest still run
            out["failed"].append({"symbol": t, "error": f"{type(e).__name__}: {e}"[:300]})
    return out


def load_ohlcv(symbol: str, as_of_date: str, cache_dir: str, client: KisClient | None = None,
               start: str | None = None) -> pd.DataFrame:
    """``yahoo.load_ohlcv`` from KIS: cached as ``<symbol>-KIS-data.csv`` beside the Yahoo files (never over them).
    Fresh means the file's last row reaches the as-of date or the last final bar, whichever is earlier — the file's
    write time plays no part (``yahoo._cache_is_fresh`` is not used). Otherwise ``update_cache`` runs. Rows after
    ``as_of_date`` are dropped.

    ponytail: on a weekend or holiday the last final day has no bar, so each load makes one incremental call that
    returns nothing; a trading calendar would skip it."""
    from agent_trading_core.yahoo import assert_ohlcv_not_stale

    path = cache_path(symbol, cache_dir)
    want = min(pd.Timestamp(as_of_date).date(), last_final_day(market_of(symbol)))
    if not path.exists() or read_cache(path)["Date"].iloc[-1].date() < want:
        update_cache(symbol, cache_dir, client or KisClient(), start=start)
    data = read_cache(path)
    data = data[data["Date"] <= pd.Timestamp(as_of_date)].reset_index(drop=True)
    if data.empty:
        raise NoMarketDataError(symbol, symbol, f"no KIS rows on or before {as_of_date}")
    assert_ohlcv_not_stale(data, as_of_date, symbol)
    return data
