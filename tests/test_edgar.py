"""SEC EDGAR financials and filings in the LLM snapshot of a US stock (agent_trading_core/edgar.py,
agent.fundamentals_section) with a fake SEC session and fixed answers: the ticker → CIK table, point in time (a
value or filing filed on or after the decision day is left out, an amendment before it replaces the original), the
quarters (Q4 = annual − nine months), the tag candidates' order, PER · PBR by hand, the flag off leaving the request
as it was (fm-v4, no SEC call), the failure kinds as a "재무 데이터 없음" section with the decision still asked and the
reason in the run's notes, the request spacing, and the User-Agent in no exception, section, cache file or log. One
test replays real answers cut down under tests/fixtures/edgar_aapl/ (AAPL, fetched 2026-11-17 — public filings, no
User-Agent in them). No network."""

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from agent_trading_core import agent, edgar
from tests.test_daily_loop import _market, dl
from tests.test_dart import Capture

UA = "EdgarTestBot contact-" + "q" * 12 + "@example.invalid"     # never a real contact
DAY = "2026-11-13"
CIK = 1234567
FIXTURES = Path(__file__).parent / "fixtures" / "edgar_aapl"


@pytest.fixture(autouse=True)
def _no_real_user_agent(tmp_path, monkeypatch):
    """The worktree .env holds the real SEC_USER_AGENT: every test reads an empty env file instead."""
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("TRADING_ENV_FILE", str(env))
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)


def at(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=ZoneInfo("America/New_York"))


def f(start, end, val, filed, form="10-Q", accn=None, fy=2026, fp="Q3"):
    return {"start": start, "end": end, "val": val, "accn": accn or f"0001234567-{filed[2:4]}-{filed[5:7]}{filed[8:]}",
            "fy": fy, "fp": fp, "form": form, "filed": filed} | ({} if start else {"start": None})


def i(end, val, filed, form="10-Q", accn=None):
    r = f(None, end, val, filed, form, accn)
    r.pop("start")
    return r


M = 1_000_000


def series(scale):
    """Quarters of a company with a December year-end: FY2025 in a 10-K (filed 2026-02-20) with its nine months from
    the Q3 10-Q, then Q1 · Q2 · Q3 2026; the year before as each filing's comparative."""
    return [
        f("2024-07-01", "2024-09-30", 80 * scale, "2025-11-01"),                 # Q3 2024 (comparative)
        f("2025-01-01", "2025-09-30", 240 * scale, "2025-11-01"),                # nine months 2025
        f("2024-01-01", "2024-09-30", 200 * scale, "2025-11-01"),
        f("2025-01-01", "2025-12-31", 340 * scale, "2026-02-20", "10-K", fp="FY", fy=2025),
        f("2024-01-01", "2024-12-31", 290 * scale, "2026-02-20", "10-K", fp="FY", fy=2025),
        f("2025-07-01", "2025-09-30", 90 * scale, "2025-11-01"),                 # Q3 2025
        f("2026-01-01", "2026-03-31", 95 * scale, "2026-05-01"),                 # Q1 2026
        f("2025-01-01", "2025-03-31", 76 * scale, "2026-05-01"),
        f("2026-04-01", "2026-06-30", 105 * scale, "2026-08-01"),                # Q2 2026
        f("2025-04-01", "2025-06-30", 84 * scale, "2026-08-01"),
        f("2026-07-01", "2026-09-30", 110 * scale, "2026-11-02"),                # Q3 2026
        f("2026-07-01", "2026-09-30", 999 * scale, "2026-11-13"),                # filed on the day: not known
    ]


def facts(**over) -> dict:
    g = {
        "Revenues": {"units": {"USD": series(10 * M)}},
        "OperatingIncomeLoss": {"units": {"USD": series(2 * M)}},
        "NetIncomeLoss": {"units": {"USD": series(M)}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            {**r, "val": r["val"] / M / 100} for r in series(M)]}},
        "Assets": {"units": {"USD": [i("2026-09-30", 3_000 * M, "2026-11-02"), i("2025-12-31", 2_900 * M, "2026-02-20")]}},
        "Liabilities": {"units": {"USD": [i("2026-09-30", 1_000 * M, "2026-11-02")]}},
        "StockholdersEquity": {"units": {"USD": [i("2026-09-30", 2_000 * M, "2026-11-02")]}},
    }
    g.update(over)
    return {"cik": CIK, "entityName": "TEST CORP", "facts": {"us-gaap": {k: v for k, v in g.items() if v is not None},
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                i("2026-10-25", 100 * M, "2026-11-02")]}}}}}


SUBMISSIONS = {"cik": str(CIK), "name": "TEST CORP", "filings": {"recent": {
    "accessionNumber": ["0001234567-26-001113", "0001234567-26-001112", "0009999999-26-000001", "0001234567-26-001102",
                        "0001234567-26-000901"],
    "filingDate": ["2026-11-13", "2026-11-12", "2026-11-10", "2026-11-02", "2026-09-01"],
    "reportDate": ["", "2026-11-12", "", "2026-09-30", ""],
    "form": ["8-K", "8-K", "4", "10-Q", "8-K"],
    "items": ["2.02", "5.02,9.01", "", "", "8.01"]}}}
TICKERS = {"0": {"cik_str": CIK, "ticker": "TEST", "title": "TEST CORP"},
           "1": {"cik_str": 7654321, "ticker": "BRK-B", "title": "BERKSHIRE"}}


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        if isinstance(self._body, (bytes, str)):    # what requests raises on a body that is not JSON
            raise json.JSONDecodeError("not json", str(self._body), 0)
        return self._body


class FakeSec:
    """company_tickers · companyfacts · submissions by URL; ``fail``: url part → an exception or a Resp."""

    def __init__(self, facts_body=None, fail=None):
        self.facts, self.fail, self.calls = facts_body or facts(), fail or {}, []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers))
        for part, what in self.fail.items():
            if part in url:
                if isinstance(what, Exception):
                    raise what
                return what
        if url.endswith("company_tickers.json"):
            return Resp(TICKERS)
        if "/companyfacts/" in url:
            return Resp(self.facts)
        if "/submissions/" in url:
            return Resp(SUBMISSIONS)
        return Resp({}, 404)


def client(tmp_path, session=None, **kw):
    return edgar.EdgarClient(tmp_path / "edgar", user_agent=UA, session=session or FakeSec(),
                             today=date(2026, 11, 17), sleep=lambda s: None, **kw)


@pytest.mark.unit
def test_the_ticker_table_maps_class_tickers_and_pads_the_cik(tmp_path):
    s = FakeSec()
    c = client(tmp_path, s)
    assert c.cik("brk.b") == (7654321, "BERKSHIRE") and c.cik("TEST") == (CIK, "TEST CORP")
    assert edgar.cik10(320193) == "0000320193"
    with pytest.raises(edgar.EdgarError) as e:
        c.cik("NOPE")
    assert e.value.kind == edgar.NO_CIK
    assert len(s.calls) == 1 and s.calls[0][1]["User-Agent"] == UA     # the table once, then the saved file
    edgar.build(c, "TEST", DAY, 50.0)
    urls = [u for u, _ in s.calls]
    assert f"{edgar.DATA_URL}/api/xbrl/companyfacts/CIK0001234567.json" in urls
    assert f"{edgar.DATA_URL}/submissions/CIK0001234567.json" in urls


@pytest.mark.unit
def test_quarters_q4_from_the_year_debt_ratio_and_per_pbr_by_hand(tmp_path):
    text, summary = edgar.build(client(tmp_path), "TEST", DAY, 50.0)
    # Q3 2026 revenue 1,100M against Q3 2025 900M → +22.2%; the 999 filed on the day is not known
    assert "| 2026-07-01~2026-09-30 | $1,100M | +22.2% | $220M | +22.2% | $110M | +22.2% |" in text
    assert "$9,990M" not in text and "$999M" not in text
    # Q4 2025 = the year 3,400 − nine months 2,400 = 1,000; the year before 2,900 − 2,000 = 900 → +11.1%
    assert ("| 2025-10-01~2025-12-31 | $1,000M | +11.1% | $200M | +11.1% | $100M | +11.1% | "
            "10-K 0001234567-26-0220 − 10-Q 0001234567-25-1101 |") in text
    # four quarters 1,000 + 950 + 1,050 + 1,100 = 4,100M revenue; net 410M
    assert "최근 4분기 합계: 매출 $4,100M, 영업이익 $820M, 순이익 $410M" in text
    assert "| 부채비율(부채총계 ÷ 자본총계, 같은 기말) | 50.0% |" in text
    # 100M shares at $50: $5,000M → PER 5,000 / 410 = 12.2, PBR 5,000 / 2,000 = 2.50, BPS $20.00, EPS 4.10
    assert "| 시가총액(종가 × 유통주식수) | $5,000M |" in text and "| 100,000,000주 |" in text
    assert "| PER(시가총액 ÷ 최근 4분기 순이익) | 12.2배 |" in text and "| PBR(시가총액 ÷ 자본총계) | 2.50배 |" in text
    assert "| BPS(자본총계 ÷ 유통주식수) | $20.00 |" in text and "근사) | $4.10 |" in text
    assert "PER 12.2배" in summary and "부채비율 50.0%" in summary
    assert "계산 안 함(스냅샷 종가 없음)" in edgar.build(client(tmp_path), "TEST", DAY, None)[0]


@pytest.mark.unit
def test_point_in_time_amendments_and_filings(tmp_path):
    amended = series(M) + [f("2026-07-01", "2026-09-30", 120 * M, "2026-11-10", "10-Q/A")]
    text, _ = edgar.build(client(tmp_path, FakeSec(facts(NetIncomeLoss={"units": {"USD": amended}}))), "TEST", DAY, 50.0)
    # the amendment before the day, with its own source beside the others' (only net income was amended)
    assert ("| $120M | +33.3% | 매출 10-Q 0001234567-26-1102 제출 2026-11-02; 영업이익 10-Q 0001234567-26-1102 제출 "
            "2026-11-02; 순이익 10-Q/A 0001234567-26-1110 제출 2026-11-10; EPS 10-Q 0001234567-26-1102 제출 2026-11-02 |") in text
    # every figure of the derived Q4 row rests on the same 10-K and 10-Q: one source
    assert "| 10-K 0001234567-26-0220 − 10-Q 0001234567-25-1101 |" in text
    # the day before the Q3 filing: the latest quarter is Q2, Q3's figures are not known yet
    early, _ = edgar.build(client(tmp_path / "b"), "TEST", "2026-11-02", 50.0)
    assert "- 최신 분기: 2026-04-01~2026-06-30" in early and "$1,100M" not in early
    # the filings: before the day only, newest first, the insider's Form 4 counted out
    assert "- 2026-11-12 8-K (items 5.02,9.01) (accession 0001234567-26-001112)" in text
    assert "- 2026-11-02 10-Q (accession 0001234567-26-001102)" in text
    assert "2.02" not in text and "0001234567-26-000901" not in text            # on the day · older than 30 days
    assert "2건 중 최근 2건, 내부자 보고 Form 3 · 4 · 5 · 144 1건 제외" in text
    assert text.index("2026-11-12 8-K") < text.index("2026-11-02 10-Q")


@pytest.mark.unit
def test_the_tag_candidates_in_order(tmp_path):
    both = facts(RevenueFromContractWithCustomerExcludingAssessedTax={"units": {"USD": series(7 * M)}})
    assert "매출 Revenues," in edgar.build(client(tmp_path, FakeSec(both)), "TEST", DAY, 50.0)[0]
    # the first candidate without the latest quarter gives way to the next that has it
    old = [r for r in series(10 * M) if r["end"] < "2026-01-01"]
    swap = facts(Revenues={"units": {"USD": old}},
                 RevenueFromContractWithCustomerExcludingAssessedTax={"units": {"USD": series(7 * M)}})
    text, _ = edgar.build(client(tmp_path / "b", FakeSec(swap)), "TEST", DAY, 50.0)
    assert "매출 RevenueFromContractWithCustomerExcludingAssessedTax," in text and "| $770M |" in text
    # no Liabilities: the balance sheet's identity, named; no OperatingIncomeLoss: N/A with "태그 없음"
    ident = facts(Liabilities=None, OperatingIncomeLoss=None, LiabilitiesAndStockholdersEquity={"units": {"USD": [
        i("2026-09-30", 3_000 * M, "2026-11-02")]}})
    text, _ = edgar.build(client(tmp_path / "c", FakeSec(ident)), "TEST", DAY, 50.0)
    assert "| 부채총계 | $1,000M | LiabilitiesAndStockholdersEquity − StockholdersEquity, 2026-09-30" in text
    assert "영업이익 태그 없음" in text and "| 부채비율(부채총계 ÷ 자본총계, 같은 기말) | 50.0% |" in text
    assert "영업이익 N/A(태그 없음)" in text and "매출 $4,100M" in text


@pytest.mark.unit
def test_an_old_single_share_count_or_one_per_class_gives_no_multiples(tmp_path):
    stale = facts()
    stale["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"] = [i("2010-01-27", 5 * M, "2010-02-03")]
    text, _ = edgar.build(client(tmp_path, FakeSec(stale)), "TEST", DAY, 50.0)
    assert "계산 안 함(dei:EntityCommonStockSharesOutstanding 최근 값 없음(마지막 2010-01-27" in text and "PER(" not in text
    per_class = facts()
    per_class["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"] = [
        i("2026-10-25", 60 * M, "2026-11-02", accn="0001234567-26-001102"),
        i("2026-10-25", 40 * M, "2026-11-02", accn="0001234567-26-001102")]
    text, _ = edgar.build(client(tmp_path / "b", FakeSec(per_class)), "TEST", DAY, 50.0)
    assert "표지 주식 수가 여러 값(종류주식) — 합산하지 않음" in text


FAILURES = [
    ({"company_tickers": requests.ConnectTimeout("no answer")}, "네트워크 ConnectTimeout"),
    ({"company_tickers": Resp({}, 403)}, "접근 거부 HTTP 403"),
    ({"companyfacts": Resp({}, 429)}, "호출 한도 HTTP 429"),
    ({"companyfacts": Resp({}, 404)}, "XBRL 재무 없음 HTTP 404"),
    ({"submissions": Resp({"filings": {"recent": {"form": ["8-K"]}}})}, "응답 형식 submissions"),
    ({"companyfacts": Resp("<html>", 200)}, "응답 형식 JSON 아님"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("fail", "why"), FAILURES)
def test_a_failure_is_a_section_saying_so(tmp_path, monkeypatch, fail, why):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    section, got = agent.fundamentals_section("US", "TEST", DAY, 1.0, tmp_path, client=client(tmp_path, FakeSec(fail=fail)))
    assert got == why and section.title == "재무 스냅샷(SEC EDGAR)"
    assert section.text == f"재무 데이터 없음({why}) — 이 종목은 가격 스냅샷만으로 판단한다."


@pytest.mark.unit
def test_no_cik_no_tag_and_no_user_agent(tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    assert agent.fundamentals_section("US", "NOPE", DAY, 1.0, tmp_path, client=client(tmp_path))[1] == "CIK 없음"
    bare = facts(Revenues=None, NetIncomeLoss=None)
    why = agent.fundamentals_section("US", "TEST", DAY, 1.0, tmp_path, client=client(tmp_path / "b", FakeSec(bare)))[1]
    assert why == "태그 없음 순이익 · 매출 분기 값 없음"
    s = FakeSec()
    c = edgar.EdgarClient(tmp_path / "c", session=s, sleep=lambda x: None)        # no SEC_USER_AGENT anywhere
    whys = [agent.fundamentals_section("US", t, DAY, 1.0, tmp_path, client=c)[1] for t in ("TEST", "BRK-B")]
    assert whys == ["SEC_USER_AGENT 미설정"] * 2 and s.calls == []             # nothing requested, not even once


@pytest.mark.unit
def test_after_a_block_or_a_timeout_the_run_asks_sec_nothing_more(tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    for fail in (Resp({}, 403), requests.ReadTimeout("slow")):
        s = FakeSec(fail={"company_tickers": fail})
        c = client(tmp_path / type(fail).__name__, s)
        [agent.fundamentals_section("US", t, DAY, 1.0, tmp_path, client=c) for t in ("TEST", "BRK-B", "TEST")]
        assert len(s.calls) == 1
    s = FakeSec(fail={"companyfacts": Resp({}, 404)})                  # one company without XBRL: the others go on
    c = client(tmp_path / "404", s)
    agent.fundamentals_section("US", "TEST", DAY, 1.0, tmp_path, client=c)
    agent.fundamentals_section("US", "BRK-B", DAY, 1.0, tmp_path, client=c)
    assert c.down is None and sum("/companyfacts/" in u for u, _ in s.calls) == 2


@pytest.mark.unit
def test_requests_are_spaced_under_ten_a_second(tmp_path):
    now, slept = [100.0], []

    def sleep(sec):
        slept.append(sec)
        now[0] += sec

    c = edgar.EdgarClient(tmp_path / "edgar", user_agent=UA, session=FakeSec(), today=date(2026, 11, 17),
                          clock=lambda: now[0], sleep=sleep)
    edgar.build(c, "TEST", DAY, 50.0)                    # three requests back to back on a stopped clock
    assert c.stats["calls"] == 3 and slept == [pytest.approx(edgar.EDGAR["min_interval"])] * 2
    assert edgar.EDGAR["min_interval"] >= 0.1


@pytest.mark.unit
def test_a_cached_answer_serves_a_day_only_when_fetched_on_or_after_it(tmp_path):
    s = FakeSec()
    edgar.build(client(tmp_path, s), "TEST", DAY, 50.0)
    edgar.build(client(tmp_path, s), "TEST", "2026-11-17", 50.0)         # fetched 11-17: good for 11-13 and 11-17
    assert len(s.calls) == 3
    later = edgar.EdgarClient(tmp_path / "edgar", user_agent=UA, session=s, today=date(2026, 11, 18), sleep=lambda x: None)
    edgar.build(later, "TEST", "2026-11-18", 50.0)                       # fetched before 11-18: asked again
    assert len(s.calls) == 5 and later.stats["cache_hits"] == 1          # the ticker table is still young


@pytest.mark.unit
def test_the_flag_off_asks_sec_nothing_and_the_us_request_stays(tmp_path, monkeypatch):
    assert dl.LLM["fundamentals"] is False and agent.prompt_version() == "fm-v4"
    monkeypatch.setattr(edgar, "EdgarClient", lambda *a, **k: pytest.fail("no SEC client with the flag off"))
    assert agent.fundamentals_section("US", "AAPL", DAY, 1.0, tmp_path) == (None, None)
    assert agent.fundamentals_client("US", tmp_path) is None
    llm, market = Capture(), _market(market="US")
    market.today = "2026-08-04"
    r = dl.run_stage("US", "decide", "2026-08-04", tmp_path, market, at("2026-08-04", "16:10"), decider="llm",
                     llm=llm)
    assert r.status == "ok" and llm.requests and not any("재무" in n for n in r.notes)
    assert all(q["system"][0]["text"] == agent.SYSTEM_PROMPT for q in llm.requests)
    assert not any("재무 스냅샷" in q["messages"][0]["content"] for q in llm.requests)


@pytest.mark.unit
def test_the_flag_on_adds_the_us_section_under_fm_v5(tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    seen = []
    monkeypatch.setattr(edgar, "build", lambda c, t, d, close: seen.append(type(c).__name__) or
                        (f"섹션 {t} {d} 종가 {close:g}", f"요약 {t}"))
    llm, market = Capture(), _market(market="US")
    market.today = "2026-08-04"
    r = dl.run_stage("US", "decide", "2026-08-04", tmp_path, market, at("2026-08-04", "16:10"), decider="llm",
                     llm=llm)
    assert r.status == "ok" and llm.requests and any("fm-v5" in n for n in r.notes)
    assert set(seen) == {"EdgarClient"} and len(seen) == len(llm.requests)
    for q in llm.requests:
        text = q["messages"][0]["content"]
        assert q["system"][0]["text"] == agent.SYSTEM_PROMPT + agent.FUNDAMENTALS_PROMPT
        assert "## 재무 스냅샷(SEC EDGAR)\n\n섹션 " in text and "## 재무 스냅샷(DART)" not in text
        assert text.index("## 선별") < text.index("## 재무 스냅샷(SEC EDGAR)") < text.index("## 포트폴리오")
    from agent_trading_core.audit import market_audit

    d = market_audit(tmp_path / "audit", "US").decisions()
    assert d and all(x.settings["prompt_version"] == "fm-v5" for x in d)
    assert "SEC EDGAR" in agent.FUNDAMENTALS_PROMPT and "DART" in agent.FUNDAMENTALS_PROMPT
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\b[A-Z]{2,5}\b(?<!SEC)(?<!DART)(?<!EDGAR)(?<!BPS)(?<!EPS)(?<!PER)(?<!PBR)",
                         agent.FUNDAMENTALS_PROMPT.replace("8-K", ""))


@pytest.mark.unit
def test_the_user_agent_is_in_no_exception_section_cache_or_log(tmp_path, monkeypatch, caplog):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    caplog.set_level(logging.DEBUG)
    seen = []
    for k, (fail, _) in enumerate(FAILURES + [({}, "")]):
        s = FakeSec(fail=fail)
        c = client(tmp_path / str(k), s)
        try:
            edgar.build(c, "TEST", DAY, 50.0)
        except edgar.EdgarError as e:
            seen += [str(e), repr(e), e.detail]
            assert e.__cause__ is None
        section, why = agent.fundamentals_section("US", "TEST", DAY, 50.0, tmp_path, client=client(tmp_path / f"s{k}", s))
        seen += [section.text, section.summary, why or ""]
        assert all(h["User-Agent"] == UA for _, h in s.calls)              # sent where it belongs, the header
    files = [p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json")]
    assert files and not any(UA in x for x in seen + files) and UA not in caplog.text
    assert not any(UA in p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.json"))


class Recorded:
    """The real answers of tests/fixtures/edgar_aapl, by URL."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        name = ("company_tickers.json" if url.endswith("company_tickers.json") else
                "companyfacts.json" if "/companyfacts/" in url else "submissions.json")
        return Resp(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


@pytest.mark.unit
def test_a_recorded_aapl_answer_gives_the_figures_checked_by_hand(tmp_path):
    """AAPL on 2026-11-12 (fiscal year to late September): the 10-K filed 2026-11-03 gives the September quarter as
    the year less the nine months; each figure below was recomputed from the full answer apart from edgar.py."""
    s = Recorded()
    text, summary = edgar.build(client(tmp_path, s), "AAPL", "2026-11-12", 354.88)
    assert len(s.calls) == 3
    assert ("| 2026-06-28~2026-09-26 | $114,477M | +11.7% | $35,631M | +9.9% | $30,711M | +11.8% | "
            "10-K 0000320193-26-000026 − 10-Q 0000320193-26-000020 |") in text
    assert "| 2025-09-28~2025-12-27 | $143,756M |" in text
    assert "최근 4분기 합계: 매출 $478,834M, 영업이익 $158,063M, 순이익 $132,175M" in text
    assert "매출 RevenueFromContractWithCustomerExcludingAssessedTax, 영업이익 OperatingIncomeLoss, 순이익 NetIncomeLoss" in text
    assert "| 부채비율(부채총계 ÷ 자본총계, 같은 기말) | 260.9% |" in text and "| 14,532,182,000주 |" in text
    assert "| PER(시가총액 ÷ 최근 4분기 순이익) | 39.0배 |" in text and "| PBR(시가총액 ÷ 자본총계) | 46.01배 |" in text
    assert "근사) | $8.97 |" in text and "| BPS(자본총계 ÷ 유통주식수) | $7.71 |" in text
    assert "- 2026-11-03 10-K (accession 0000320193-26-000026)" in text and "PER 39.0배" in summary
    # the day of the 10-K: it is not known yet, the June quarter is the latest
    before, _ = edgar.build(client(tmp_path / "b", Recorded()), "AAPL", "2026-11-03", 354.88)
    assert "- 최신 분기: 2026-03-29~2026-06-27" in before and "0000320193-26-000026" not in before


@pytest.mark.unit
def test_a_company_whose_last_quarter_is_long_gone_has_no_figures(tmp_path):
    old = [r for r in series(M) if r["end"] < "2025-01-01"]          # last quarter ended 2024-12-31, 682 days before
    stale = facts(Revenues={"units": {"USD": old}}, NetIncomeLoss={"units": {"USD": old}})
    with pytest.raises(edgar.EdgarError) as e:
        edgar.build(client(tmp_path, FakeSec(stale)), "TEST", DAY, 50.0)
    assert (e.value.kind, e.value.detail) == (edgar.NO_TAG, "최근 분기 값 없음(마지막 분기 끝 2024-12-31)")


@pytest.mark.unit
def test_a_first_net_income_tag_left_behind_does_not_hide_the_latest_quarters(tmp_path):
    old = [r for r in series(M) if r["end"] < "2025-01-01"]
    moved = facts(NetIncomeLoss={"units": {"USD": old}}, ProfitLoss={"units": {"USD": series(M)}})
    text, _ = edgar.build(client(tmp_path, FakeSec(moved)), "TEST", DAY, 50.0)
    assert "- 최신 분기: 2026-07-01~2026-09-30" in text and "순이익 ProfitLoss" in text and "순이익 $410M" in text


@pytest.mark.unit
def test_revenue_sets_the_latest_quarter_when_net_income_is_only_old(tmp_path):
    old = [r for r in series(M) if r["end"] < "2025-01-01"]
    text, _ = edgar.build(client(tmp_path, FakeSec(facts(NetIncomeLoss={"units": {"USD": old}}))), "TEST", DAY, 50.0)
    assert "- 최신 분기: 2026-07-01~2026-09-30" in text and "| $1,100M | +22.2% |" in text


@pytest.mark.unit
def test_an_8k_or_a_proxy_tagging_the_same_concept_is_not_the_source(tmp_path):
    later = series(M) + [f("2026-07-01", "2026-09-30", 110 * M, "2026-11-09", "DEF 14A", fy=None, fp=None),
                         f("2026-07-01", "2026-09-30", 111 * M, "2026-11-10", "8-K")]
    text, _ = edgar.build(client(tmp_path, FakeSec(facts(NetIncomeLoss={"units": {"USD": later}}))), "TEST", DAY, 50.0)
    assert "DEF 14A" not in text.split("최근 30일")[0] and "$111M" not in text
    assert "| 2026-07-01~2026-09-30 | $1,100M | +22.2% | $220M | +22.2% | $110M | +22.2% | 10-Q 0001234567-26-1102" in text


@pytest.mark.unit
def test_no_net_income_tag_at_all_says_so_in_the_per_cell(tmp_path):
    text, _ = edgar.build(client(tmp_path, FakeSec(facts(NetIncomeLoss=None))), "TEST", DAY, 50.0)
    assert "순이익 태그 없음" in text and "순이익 N/A(태그 없음)" in text
    assert "| PER(시가총액 ÷ 최근 4분기 순이익) | N/A(태그 없음) |" in text and "| PBR(시가총액 ÷ 자본총계) | 2.50배 |" in text
