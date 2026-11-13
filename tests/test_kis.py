"""KIS loader (agent_trading_core/kis.py) with a fake HTTP session: token cache and re-issue, call spacing and the
throttle retry, 100-row windows, response → frame, settings errors that name variables but never values, the
KIS cache file, and the comparison helpers in scripts/kis_check.py. No network."""

import importlib.util
import json
import os
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from agent_trading_core import kis
from agent_trading_core.errors import NoMarketDataError, VendorNotConfiguredError, VendorUnavailableError

_spec = importlib.util.spec_from_file_location("kis_check", Path(__file__).resolve().parents[1] / "scripts" / "kis_check.py")
kc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kc)

KEY, SECRET = "PSfakeappkey0000000000000000000000000", "fakesecret" * 18
CREDS = {"appkey": KEY, "appsecret": SECRET, "env": "paper"}
T0 = 1_800_000_000.0     # 2027-01-15 KST, a fixed "now"


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeSession:
    """Answers POST /oauth2/tokenP with a token expiring at ``expires`` and GETs from a queue (or a function)."""

    def __init__(self, gets=(), expires="2027-01-16 08:00:00"):
        self.posts, self.calls, self.gets, self.expires = [], [], list(gets), expires

    def post(self, url, json, timeout):
        self.posts.append((url, json))
        return Resp({"access_token": f"tok{len(self.posts)}", "access_token_token_expired": self.expires,
                     "token_type": "Bearer", "expires_in": 86400})

    def get(self, url, params, timeout, headers):
        self.calls.append((url, dict(params), dict(headers)))
        g = self.gets.pop(0) if self.gets else {"rt_cd": "0", "output2": []}
        return Resp(g(params) if callable(g) else g)


class Clock:
    def __init__(self, t=T0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _client(tmp_path, session, clock=None):
    clock = clock or Clock()
    return kis.KisClient(CREDS, session=session, token_file=tmp_path / ".kis_token.json", now=clock, sleep=clock.sleep)


@pytest.mark.unit
def test_token_is_issued_once_reused_from_the_file_and_reissued_near_expiry(tmp_path):
    s, clock = FakeSession(), Clock()
    c = _client(tmp_path, s, clock)
    assert c.token() == "tok1" and c.token() == "tok1"
    assert len(s.posts) == 1 and c.stats["token_issued"] == 1 and c.stats["token_reused"] == 1
    saved = (tmp_path / ".kis_token.json").read_text(encoding="utf-8")
    assert KEY not in saved and SECRET not in saved                       # the token file holds no credential
    # a new process: the file's token is reused, no second issuance (KIS sends a notice per issuance)
    s2 = FakeSession()
    c2 = _client(tmp_path, s2, clock)
    assert c2.token() == "tok1" and s2.posts == []
    # within TOKEN_MARGIN of the stated expiry (KST) a new token is issued
    expires = json.loads(saved)["expires_at"]
    clock.t = expires - kis.TOKEN_MARGIN + 1
    assert c2.token() == "tok1" and len(s2.posts) == 1 and c2.stats["token_issued"] == 1
    # a different app key does not get the saved token
    s3 = FakeSession()
    c3 = kis.KisClient({**CREDS, "appkey": "PSotherkey"}, session=s3, token_file=tmp_path / ".kis_token.json",
                       now=clock, sleep=clock.sleep)
    assert c3.token() == "tok1" and len(s3.posts) == 1                    # FakeSession numbers its own tokens
    assert "PSotherkey" not in (tmp_path / ".kis_token.json").read_text(encoding="utf-8")


@pytest.mark.unit
def test_a_rejected_token_is_reissued_once_then_the_call_repeats(tmp_path):
    ok = {"rt_cd": "0", "output2": []}
    s = FakeSession([{"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}, ok])
    c = _client(tmp_path, s)
    assert c.get("/p", "TR", {}) == ok
    assert len(s.posts) == 2 and [h["authorization"] for _, _, h in s.calls] == ["Bearer tok1", "Bearer tok2"]
    s2 = FakeSession([{"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "x"}] * 2)   # still rejected: no loop
    with pytest.raises(VendorUnavailableError, match="EGW00123"):
        _client(tmp_path / "b", s2).get("/p", "TR", {})
    assert len(s2.posts) == 2
    # throttled to the last retry, then a token error: re-issue without using up a retry, no AssertionError
    thr = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당"}
    s3 = FakeSession([thr] * kis.RETRIES + [{"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "x"}, ok])
    assert _client(tmp_path / "c", s3).get("/p", "TR", {}) == ok and len(s3.posts) == 2
    s4 = FakeSession([{"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "x"}] + [thr] * (kis.RETRIES + 1))
    c4 = _client(tmp_path / "d", s4)
    with pytest.raises(VendorUnavailableError, match="EGW00201"):
        c4.get("/p", "TR", {})
    assert c4.stats["throttled"] == kis.RETRIES          # all throttle retries kept after the re-issue


@pytest.mark.unit
def test_a_network_failure_is_a_vendor_error_without_the_url_or_key(tmp_path):
    class Down(FakeSession):
        def get(self, url, params, timeout, headers):
            raise requests.ConnectionError(f"{url}?appkey={headers['appkey']}")
    with pytest.raises(VendorUnavailableError, match="ConnectionError") as e:
        _client(tmp_path, Down()).get("/p", "TR", {})
    assert KEY not in str(e.value) and "openapivts" not in str(e.value)


@pytest.mark.unit
def test_expiry_is_read_as_korean_time(tmp_path):
    # 2027-01-16 08:00:00 KST = 2027-01-15 23:00:00 UTC
    c = kis.KisClient(CREDS, session=FakeSession(), token_file=tmp_path / "t.json", now=lambda: 0.0,
                      sleep=lambda _: None)
    c.token()
    assert c._token[1] == pd.Timestamp("2027-01-15 23:00:00", tz="UTC").timestamp()


@pytest.mark.unit
def test_calls_are_spaced_and_a_throttled_answer_is_retried(tmp_path):
    ok = {"rt_cd": "0", "msg_cd": "MCA00000", "output2": [{"stck_bsop_date": "20260731"}]}
    s, clock = FakeSession([{"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}, ok, ok]), Clock()
    c = _client(tmp_path, s, clock)
    assert c.get("/p", "TR", {}) == ok
    assert c.stats["throttled"] == 1 and len(s.calls) == 2
    c.get("/p", "TR", {})                                                 # immediately after: waits CALL_INTERVAL
    assert clock.slept[-1] == pytest.approx(kis.CALL_INTERVAL)
    hdr = s.calls[-1][2]
    assert hdr["tr_id"] == "TR" and hdr["authorization"] == "Bearer tok1" and hdr["custtype"] == "P"


@pytest.mark.unit
def test_an_error_answer_raises_with_the_code_and_never_the_key(tmp_path):
    s = FakeSession([{"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 서비스 코드 입니다"}])
    with pytest.raises(VendorUnavailableError) as e:
        _client(tmp_path, s).get("/p", "FHKUP03500100", {})
    assert "OPSQ0002" in str(e.value) and KEY not in str(e.value) and SECRET not in str(e.value)
    # a throttle that never clears is not returned as data either
    s2 = FakeSession([{"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당"}] * (kis.RETRIES + 1))
    with pytest.raises(VendorUnavailableError, match="EGW00201"):
        _client(tmp_path, s2).get("/p", "TR", {})


@pytest.mark.unit
def test_windows_stay_under_each_endpoints_row_cap_and_cover_the_range_once():
    a, b = date(2024, 1, 1), date(2024, 5, 9)                             # exactly 130 days
    assert kis.date_chunks(a, b, 130) == [(a, b)]
    assert kis.date_chunks(a, b + timedelta(days=1), 130) == [(a, b), (b + timedelta(days=1), b + timedelta(days=1))]
    for days, cap in (kis.INDEX_WINDOW, kis.STOCK_WINDOW):
        chunks = kis.date_chunks(date(2021, 11, 13), date(2026, 11, 13), days)
        assert chunks[0][0] == date(2021, 11, 13) and chunks[-1][1] == date(2026, 11, 13)
        for (_, prev_end), (nxt, _) in pairwise(chunks):
            assert nxt == prev_end + timedelta(days=1)                    # no gap, no overlap
        # even with no holiday a window has fewer weekdays than the cap, so a full page means truncation
        assert max(int(np.busday_count(x, y + timedelta(days=1))) for x, y in chunks) < cap


@pytest.mark.unit
def test_a_page_at_the_row_cap_raises_instead_of_leaving_a_gap(tmp_path):
    # what the index endpoint did with a 140-day window on 2026-11-13: the 50 newest rows, the rest dropped
    days = pd.bdate_range(end="2024-05-17", periods=50)[::-1]
    full = {"rt_cd": "0", "output2": [{"stck_bsop_date": d.strftime("%Y%m%d"), "bstp_nmix_prpr": "2700"} for d in days]}
    with pytest.raises(VendorUnavailableError, match="상한 50"):
        _client(tmp_path, FakeSession([full])).index_daily("^KS11", date(2024, 3, 19), date(2024, 5, 17))


@pytest.mark.unit
def test_rows_become_the_yahoo_frame_shape():
    rows = [  # newest first as KIS sends them, a blank padding row, and a repeated date from two windows
        {"stck_bsop_date": "20260731", "bstp_nmix_oprc": "5657.79", "bstp_nmix_hgpr": "6630.77",
         "bstp_nmix_lwpr": "5629.76", "bstp_nmix_prpr": "6595.45", "acml_vol": "434445"},
        {"stck_bsop_date": "20260730", "bstp_nmix_oprc": "5681.77", "bstp_nmix_hgpr": "5976.82",
         "bstp_nmix_lwpr": "5547.41", "bstp_nmix_prpr": "5593.56", "acml_vol": "378859"},
        {"stck_bsop_date": "", "bstp_nmix_prpr": ""},
        {"stck_bsop_date": "20260729", "bstp_nmix_oprc": "", "bstp_nmix_hgpr": "", "bstp_nmix_lwpr": "",
         "bstp_nmix_prpr": "", "acml_vol": ""},                            # a date with no close: dropped
        {"stck_bsop_date": "20260731", "bstp_nmix_oprc": "5657.79", "bstp_nmix_hgpr": "6630.77",
         "bstp_nmix_lwpr": "5629.76", "bstp_nmix_prpr": "6595.45", "acml_vol": "434445"},
    ]
    df = kis.to_frame(rows, kis.INDEX_FIELDS, "^KS11")
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert list(df["Date"].dt.strftime("%Y-%m-%d")) == ["2026-07-30", "2026-07-31"]
    assert df["Date"].dt.tz is None and (df["Date"] == df["Date"].dt.normalize()).all()
    assert df["Close"].tolist() == [5593.56, 6595.45] and df["Volume"].dtype == "int64" and df["Open"].dtype == float
    gap = kis.to_frame([{"stck_bsop_date": "20260730", "stck_oprc": "10", "stck_hgpr": "11", "stck_lwpr": "9",
                         "stck_clpr": "10", "acml_vol": "1"},
                        {"stck_bsop_date": "20260731", "stck_oprc": "", "stck_hgpr": "", "stck_lwpr": "",
                         "stck_clpr": "12", "acml_vol": ""}], kis.STOCK_FIELDS, "X")
    assert gap["Open"].tolist() == [10.0, 10.0] and gap["Close"].tolist() == [10.0, 12.0]   # filled forward
    with pytest.raises(NoMarketDataError):
        kis.to_frame([{"stck_bsop_date": ""}], kis.STOCK_FIELDS, "005930.KS")


@pytest.mark.unit
def test_index_and_stock_requests_walk_the_windows(tmp_path):
    def answer(p):
        return {"rt_cd": "0", "output2": [{"stck_bsop_date": p["FID_INPUT_DATE_2"], "stck_clpr": "100",
                                            "stck_oprc": "99", "stck_hgpr": "101", "stck_lwpr": "98", "acml_vol": "7"}]}
    s = FakeSession([answer] * 3)
    df = _client(tmp_path, s).stock_daily("005930.KS", date(2024, 1, 1), date(2024, 9, 1))
    params = [c[1] for c in s.calls]
    assert [(p["FID_INPUT_DATE_1"], p["FID_INPUT_DATE_2"]) for p in params] == [
        ("20240101", "20240509"), ("20240510", "20240901")]
    assert {p["FID_INPUT_ISCD"] for p in params} == {"005930"} and {p["FID_ORG_ADJ_PRC"] for p in params} == {"0"}
    assert len(df) == 2


@pytest.mark.unit
def test_us_pages_backward_from_the_end_date(tmp_path):
    def page(p):  # 3 rows per page ending at BYMD, as the endpoint does with 100
        end = pd.Timestamp(p["BYMD"])
        days = [end - pd.Timedelta(days=i) for i in range(3)]
        return {"rt_cd": "0", "output2": [{"xymd": d.strftime("%Y%m%d"), "open": "1", "high": "1", "low": "1",
                                           "clos": "2", "tvol": "3"} for d in days]}
    s = FakeSession([page] * 5)
    df = _client(tmp_path, s).us_daily("AAPL", "NAS", date(2026, 7, 1), date(2026, 7, 8))
    assert [c[1]["BYMD"] for c in s.calls] == ["20260708", "20260705", "20260702"]
    assert df["Date"].min() == pd.Timestamp("2026-07-01") and df["Date"].max() == pd.Timestamp("2026-07-08")
    assert {c[1]["EXCD"] for c in s.calls} == {"NAS"} and {c[1]["MODP"] for c in s.calls} == {"1"}


@pytest.mark.unit
def test_settings_name_the_missing_variable_and_refuse_a_live_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(kis, "ROOT", tmp_path)                            # no repository .env
    for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ENV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KIS_APP_KEY", KEY)
    with pytest.raises(VendorNotConfiguredError) as e:
        kis.credentials()
    assert "KIS_APP_SECRET" in str(e.value) and "KIS_ENV" in str(e.value) and KEY not in str(e.value)
    monkeypatch.setenv("KIS_APP_SECRET", SECRET)
    monkeypatch.setenv("KIS_ENV", "real")
    with pytest.raises(VendorNotConfiguredError, match="paper") as e:
        kis.credentials()
    assert SECRET not in str(e.value)
    monkeypatch.setenv("KIS_ENV", "paper")
    assert kis.credentials() == {"appkey": KEY, "appsecret": SECRET, "env": "paper"}
    # the .env file fills what the environment lacks, and does not override it
    (tmp_path / ".env").write_text("KIS_APP_KEY=fromfile\nKIS_ACCOUNT_NO=12345678\n", encoding="utf-8")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "x")                             # registers the restore …
    monkeypatch.delenv("KIS_ACCOUNT_NO")                                  # … of whatever load_dotenv adds
    assert kis.credentials()["appkey"] == KEY
    assert os.environ["KIS_ACCOUNT_NO"] == "12345678"


@pytest.mark.unit
def test_kis_cache_sits_beside_the_yahoo_file_and_cuts_at_the_as_of_date(tmp_path):
    yahoo = tmp_path / "005930.KS-YFin-data.csv"
    yahoo.write_text("Date,Open,High,Low,Close,Volume\n2026-07-31 00:00:00+09:00,1,1,1,1,1\n", encoding="utf-8")
    before = yahoo.read_bytes()

    class Stub:
        def stock_daily(self, symbol, start, end):
            return pd.DataFrame({"Date": pd.to_datetime(["2026-07-30", "2026-07-31", "2026-08-03"]),
                                 "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": [10.0, 11.0, 12.0], "Volume": 5})

    df = kis.load_kis_ohlcv("005930.KS", "2026-07-31", str(tmp_path), Stub())
    assert (tmp_path / "005930.KS-KIS-data.csv").exists() and yahoo.read_bytes() == before
    assert df["Close"].tolist() == [10.0, 11.0] and str(df["Date"].dtype).startswith("datetime64")
    # written today → read from the file, no client needed
    assert kis.load_kis_ohlcv("005930.KS", "2026-08-03", str(tmp_path))["Close"].iloc[-1] == 12.0
    with pytest.raises(ValueError, match="국내"):
        kis.load_kis_ohlcv("AAPL", "2026-08-03", str(tmp_path / "x"), Stub())


@pytest.mark.unit
def test_check_adds_kis_as_a_third_column_and_measures_close_gaps():
    check = pd.DataFrame({"date": ["2026-07-30", "2026-07-31"], "yfinance": [5593.56, 6595.45],
                          "krx": [5593.56, 6595.45], "diff_pct": [0.0, 0.0]})
    k = pd.Series([5593.56, 6661.40], index=pd.to_datetime(["2026-07-30", "2026-07-31"]))
    out = kc.add_kis(kc.add_kis(check, k), k)                            # re-running replaces, not duplicates
    assert list(out.columns) == ["date", "yfinance", "krx", "diff_pct", "kis", "diff_kis_pct"]
    assert out["diff_kis_pct"].iloc[0] == 0.0 and out["diff_kis_pct"].iloc[1] == pytest.approx((6661.40 / 6595.45 - 1) * 100)
    y = pd.Series([100.0, 100.0, 100.0], index=pd.to_datetime(["2024-01-02", "2024-01-03", "2023-12-28"]))
    kk = pd.Series([100.0, 102.0], index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    d = kc.close_diff(y, kk, "2024-01-02", "2026-11-12")
    assert d["days"] == 2 and d["max_abs_pct"] == pytest.approx(2.0) and d["max_date"] == "2024-01-03"
    assert d["median_abs_pct"] == pytest.approx(1.0) and d["over_1pct_days"] == 1
    assert kc.close_diff(y, kk, "2025-01-01", "2026-11-12") is None
    y2 = pd.Series([100.0, 100.0], index=pd.to_datetime(["2024-01-02", "2024-02-01"]))
    k2 = pd.Series([97.0, 100.0], index=pd.to_datetime(["2024-01-02", "2024-02-01"]))
    m = kc.monthly_gap({"A": (y, kk), "B": (y2, k2)}, "2024-01-02", "2026-11-12")
    assert m["month"].tolist() == ["2024-01", "2024-02"] and m["symbols"].tolist() == [2, 1]
    assert m["median_pct"].iloc[0] == pytest.approx(-1.0) and m["kis_higher"].tolist() == [1, 0]
