"""DART financials and filings in the LLM snapshot (agent_trading_core/dart.py, agent.fundamentals_section) with a fake
OpenDART session and fixed answers: the corp-code table, point in time (a report, a correction or a filing received
on or after the decision day is left out), the quarters (Q4 = annual − Q3 year to date) and PER · PBR by hand, the
flag off leaving the request as it was (fm-v6, no DART call), the four failure kinds (and a refused key) as a
"재무 데이터 없음" section with the decision still asked and the reason in the run's notes, and the key in no
exception, section, note, cache file or log. One test replays real answers kept under tests/fixtures/dart_005930/
(005930, received 2026-11-16 — public filings, no key in them). No network."""

import hashlib
import io
import json
import logging
import re
import zipfile
from datetime import date
from pathlib import Path

import pytest
import requests

from agent_trading_core import agent, dart
from tests.test_daily_loop import FakeManager, _market, at, dl

KEY = "dartTESTsecret" + "7" * 26                      # 40 characters, like an issued key
DAY = "2026-11-13"
CORP, NAME, STOCK = "00000001", "테스트전자", "111111"
E8 = 100_000_000                                       # 1억 원


@pytest.fixture(autouse=True)
def _no_real_key(tmp_path, monkeypatch):
    """The worktree .env holds a real DART key: every test reads an empty env file instead."""
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("TRADING_ENV_FILE", str(env))
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)


def corp_zip() -> bytes:
    xml = ("<?xml version='1.0' encoding='UTF-8'?><result>"
           f"<list><corp_code>{CORP}</corp_code><corp_name>{NAME}</corp_name><stock_code>{STOCK}</stock_code></list>"
           "<list><corp_code>00000002</corp_code><corp_name>비상장</corp_name><stock_code> </stock_code></list>"
           "<list><corp_code>00000003</corp_code><corp_name>다른회사</corp_name><stock_code>222222</stock_code></list>"
           "</result>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml.encode("utf-8"))
    return buf.getvalue()


def filing(nm, no, flr=NAME):
    return {"corp_code": CORP, "corp_name": NAME, "report_nm": nm, "rcept_no": no, "rcept_dt": no[:8], "flr_nm": flr}


PERIODIC = [filing("분기보고서 (2025.09)", "20251114000001"), filing("사업보고서 (2025.12)", "20260310000001"),
            filing("분기보고서 (2026.03)", "20260515000001"), filing("반기보고서 (2026.06)", "20260814000001"),
            filing("[기재정정]반기보고서 (2026.06)", "20261020000001"),
            filing("분기보고서 (2026.09)", "20261113000001")]       # on the day: not known yet
RECENT = [filing("주요사항보고서(자기주식취득결정)", "20261112000002"),
          filing("임원ㆍ주요주주특정증권등소유상황보고서", "20261112000003", flr="홍길동"),
          filing("기업설명회(IR)개최(안내공시)", "20261113000004"),
          filing("[기재정정]반기보고서 (2026.06)", "20261020000001")]


def accounts(no, rev, op, net, assets=None, liab=None, equity=None):
    """Key-account rows: income-statement items as (three months, year to date, a year before, its year to date)."""
    rows = [{"rcept_no": no, "fs_div": "CFS", "sj_div": "IS", "account_nm": name,
             "thstrm_amount": f"{v[0] * E8:,}", "thstrm_add_amount": f"{v[1] * E8:,}",
             "frmtrm_amount": f"{v[2] * E8:,}", "frmtrm_add_amount": f"{v[3] * E8:,}"}
            for name, v in (("매출액", rev), ("영업이익", op), ("당기순이익(손실)", net))]
    rows += [{"rcept_no": no, "fs_div": "CFS", "sj_div": "BS", "account_nm": name, "thstrm_amount": f"{v * E8:,}"}
             for name, v in (("자산총계", assets), ("부채총계", liab), ("자본총계", equity)) if v is not None]
    rows += [{"rcept_no": no, "fs_div": "OFS", "sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "1"}]
    return rows


# (bsns_year, reprt_code) → rows. Revenue 3 months: 2025 Q3 100, Q4 420 − 300 = 120, 2026 Q1 110, Q2 130 (억)
ACCOUNTS = {
    ("2025", "11014"): accounts("20251114000001", (100, 300, 80, 250), (10, 30, 8, 25), (8, 24, 6, 20)),
    ("2025", "11011"): accounts("20260310000001", (420, 0, 340, 0), (42, 0, 33, 0), (34, 0, 26, 0)),
    ("2026", "11013"): accounts("20260515000001", (110, 110, 100, 100), (11, 11, 10, 10), (9, 9, 7, 7),
                                assets=280, liab=90, equity=190),
    # the correction filed 2026-10-20 is what the API answers for the half year
    ("2026", "11012"): accounts("20261020000001", (130, 240, 105, 205), (13, 24, 10, 20), (11, 20, 8, 15),
                                assets=300, liab=100, equity=200),
    ("2026", "11014"): accounts("20261113000001", (999, 999, 1, 1), (99, 99, 1, 1), (99, 99, 1, 1)),
}
SHARES = [{"rcept_no": "20261020000001", "se": "의결권 있는 주식\n(보통주)", "istc_totqy": "1,000,000"},
          {"rcept_no": "20261020000001", "se": "의결권 없는 주식\n(우선주)", "istc_totqy": "100,000"},
          {"rcept_no": "20261020000001", "se": "합계", "istc_totqy": "1,100,000"}]


class Resp:
    def __init__(self, body=None, content=b"", status=200):
        self.body, self.content, self.status_code = body, content, status

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


class FakeDart:
    """Answers like OpenDART from the tables above — every filing whatever the dates asked, so the date cut is the
    client's own. ``fail``: path → an exception to raise or a response to return instead."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], dict(fail or {})

    def get(self, url, params, timeout):
        path = url.rsplit("/", 1)[1]
        self.calls.append((path, dict(params)))
        if path in self.fail:
            f = self.fail[path]
            if isinstance(f, Exception):
                raise f
            return f
        if path == "corpCode.xml":
            return Resp(content=corp_zip())
        if path == "list.json":
            return Resp({"status": "000", "list": PERIODIC if params.get("pblntf_ty") == "A" else RECENT})
        if path == "fnlttSinglAcnt.json":
            rows = ACCOUNTS.get((params["bsns_year"], params["reprt_code"]))
            return Resp({"status": "000", "list": rows} if rows else {"status": "013", "message": "조회된 데이타가 없습니다."})
        if path == "stockTotqySttus.json":
            ok = (params["bsns_year"], params["reprt_code"]) == ("2026", "11012")
            return Resp({"status": "000", "list": SHARES} if ok else {"status": "013"})
        raise AssertionError(path)


def client(tmp_path, session=None, today=date(2026, 11, 16), key=KEY):
    return dart.DartClient(tmp_path / "dart", key=key, session=session or FakeDart(), today=today)


@pytest.mark.unit
def test_the_corp_code_table_maps_listed_tickers_and_is_kept_for_a_week(tmp_path):
    s = FakeDart()
    c = client(tmp_path, s)
    assert c.corp("111111.KS") == (CORP, NAME) and c.corp("222222.KQ")[0] == "00000003"
    with pytest.raises(dart.DartError) as e:
        c.corp("333333.KS")                           # not listed in the table
    assert e.value.kind == dart.NO_CORP
    assert [p for p, _ in s.calls] == ["corpCode.xml"]        # one download for the three lookups... and the file
    assert client(tmp_path, s, today=date(2026, 11, 22)).corp("111111.KS")[0] == CORP
    assert [p for p, _ in s.calls] == ["corpCode.xml"]        # six days on: still the file
    client(tmp_path, s, today=date(2026, 11, 23)).corp("111111.KS")
    assert [p for p, _ in s.calls] == ["corpCode.xml"] * 2    # seven days on: downloaded again
    # a failed download with a file on disk uses the file; without one it is the failure's kind
    down = FakeDart(fail={"corpCode.xml": requests.ConnectionError("down")})
    assert client(tmp_path, down, today=date(2027, 1, 1)).corp("111111.KS")[0] == CORP
    with pytest.raises(dart.DartError) as e:
        client(tmp_path / "other", down).corp("111111.KS")
    assert e.value.kind == dart.NETWORK


@pytest.mark.unit
def test_the_section_holds_only_what_was_received_before_the_day(tmp_path):
    s = FakeDart()
    text, summary = dart.build(client(tmp_path, s), "111111.KS", DAY, 50_000.0)
    lists = [p for path, p in s.calls if path == "list.json"]
    assert {p["end_de"] for p in lists} == {"20261112"}       # asked up to the day before
    # the Q3 2026 report (received on the day) is neither asked nor shown; the half-year correction of 10-20 is
    assert ("2026", "11014") not in {(p["bsns_year"], p["reprt_code"]) for path, p in s.calls if path == "fnlttSinglAcnt.json"}
    assert "최신 정기보고서: 반기보고서 (2026.06), 접수 2026-10-20, 접수번호 20261020000001" in text
    assert "999" not in text and "2026 3분기" not in text
    # filings: the company's own before the day — not the insider's, not the one received on the day
    assert "2026-11-12 주요사항보고서(자기주식취득결정) (접수번호 20261112000002)" in text
    assert "홍길동" not in text and "임원ㆍ주요주주" not in text and "기업설명회" not in text
    assert "기준일 전날까지 2건 중 최근 2건" in text and "최근 30일 회사 공시 2건" in summary

    # a day before the correction: the list names the original of 08-14, but the API answers with the 10-20
    # correction — figures not known on the day, so the half year is left out and Q1 is the latest
    early = dart.build(client(tmp_path / "early", FakeDart()), "111111.KS", "2026-10-20", 50_000.0)[0]
    assert "최신 정기보고서: 분기보고서 (2026.03), 접수 2026-05-15" in early
    assert "1,300억" not in early and "20261020000001" not in early
    assert "2026 1분기(1~3월) | 110억" in early


@pytest.mark.unit
def test_quarters_debt_ratio_and_per_pbr_by_hand(tmp_path):
    text, summary = dart.build(client(tmp_path), "111111.KS", DAY, 50_000.0)
    # Q4 2025 = annual − Q3 year to date: revenue 420 − 300 = 120, a year before 340 − 250 = 90 → +33.3%;
    # operating 42 − 30 = 12 against 33 − 25 = 8 → +50.0%; net 34 − 24 = 10 against 26 − 20 = 6 → +66.7%
    assert ("| 2025 4분기(10~12월) | 120억 | +33.3% | 12억 | +50.0% | 10억 | +66.7% | "
            "20260310000001 − 20251114000001 |") in text
    assert "| 2026 2분기(4~6월) | 130억 | +23.8% | 13억 | +30.0% | 11억 | +37.5% | 20261020000001 |" in text
    assert "| 2025 3분기(7~9월) | 100억 | +25.0% |" in text and "| 2026 1분기(1~3월) | 110억 | +10.0% |" in text
    # four quarters of net income 8 + 10 + 9 + 11 = 38억; equity 200억, liabilities 100억 → 50.0%
    assert "최근 4분기 합계: 매출액 460억, 영업이익 46억, 순이익 38억" in text
    assert "| 부채비율(부채총계 ÷ 자본총계) | 50.0% |" in text
    # 1,000,000 common shares (the preferred left out) at 50,000원: 500억 → PER 500/38 = 13.2, PBR 500/200 = 2.50
    assert "| 1,000,000주 |" in text and "| 시가총액(종가 × 보통주) | 500억 |" in text
    assert "| EPS(최근 4분기 순이익 ÷ 보통주) | 3,800원 |" in text and "| BPS(자본총계 ÷ 보통주) | 20,000원 |" in text
    assert "| PER(시가총액 ÷ 최근 4분기 순이익) | 13.2배 |" in text and "| PBR(시가총액 ÷ 자본총계) | 2.50배 |" in text
    assert "PER 13.2배" in summary
    # no close: no per-share figures; a loss over four quarters: no PER
    assert "계산 안 함(스냅샷 종가 없음)" in dart.build(client(tmp_path), "111111.KS", DAY, None)[0]
    assert (dart.growth(-5, 10), dart.growth(5, -10), dart.growth(-5, -10), dart.growth(5, 0)) == \
        ("적자전환", "흑자전환", "적자지속", "N/A")


@pytest.mark.unit
def test_the_flag_off_leaves_the_prompt_and_asks_dart_nothing(tmp_path, monkeypatch):
    assert dl.LLM["fundamentals"] is False and agent.prompt_version() == agent.PROMPT_VERSION == "fm-v6"
    # fm-v6 is this exact system prompt: a change to it without a version bump turns this red
    assert hashlib.sha256(agent.system_prompt().encode()).hexdigest() == FM_V6_SHA
    monkeypatch.setattr(dart, "DartClient", lambda *a, **k: pytest.fail("no DART client with the flag off"))
    assert agent.fundamentals_section("KRX", "111111.KS", DAY, 1.0, tmp_path) == (None, None)
    llm, market = Capture(), _market()
    market.today = "2026-08-04"
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, market, at("2026-08-04", "15:40"), decider="llm", llm=llm)
    assert r.status == "ok" and llm.requests and not any("재무" in n for n in r.notes)
    assert all(q["system"][0]["text"] == agent.SYSTEM_PROMPT for q in llm.requests)
    assert not any("재무 스냅샷" in q["messages"][0]["content"] for q in llm.requests)
    assert any("fm-v6" in n for n in r.notes)


@pytest.mark.unit
def test_the_prompt_at_half_is_fm_v4_word_for_word():
    """The Underweight words follow the setting: with 0.5's words the system prompt is fm-v4's, byte for byte."""
    now = "Underweight → 보유 중이면 " + agent.underweight_words(1.0)
    assert agent.system_prompt().count(now) == 1
    half = agent.system_prompt().replace(now, "Underweight → 보유 중이면 " + agent.underweight_words(0.5))
    assert hashlib.sha256(half.encode()).hexdigest() == FM_V4_SHA


# sha256 of SYSTEM_PROMPT fm-v6 (fix/uw-full, 2026-11-23: Underweight sells the whole holding, fraction 1.0). fm-v4
# (fix/post-trial-core: the partial sells, Underweight 50%) was FM_V4_SHA; fm-v1, the 2026-11-16 trial's prompt
# (develop fcfca78), was 1c9e39e9e901c2c0afdd21f2edac626a13ca99623f7f0914da6f046697979cb7
FM_V6_SHA = "6c79341689fcf84beebc7fb0eef213086981a73f11e53c5b0d34a56d711d512b"
FM_V4_SHA = "0418d9cb27675e7bd89916c5ef8b3c732a425b54de5b062b5c65704fcc884c02"


class Capture(FakeManager):
    def __init__(self):
        super().__init__()
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        return super().create(**kw)


@pytest.mark.unit
def test_the_flag_on_adds_the_dart_section_to_krx_under_fm_v5(tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    monkeypatch.setattr(dart, "build", lambda c, t, d, close: (f"섹션 {t} {d} 종가 {close:g}", f"요약 {t}"))
    llm, market = Capture(), _market()
    market.today = "2026-08-04"
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, market, at("2026-08-04", "15:40"), decider="llm", llm=llm)
    assert r.status == "ok" and llm.requests and any("fm-v7" in n for n in r.notes)
    for q in llm.requests:
        text = q["messages"][0]["content"]
        assert q["system"][0]["text"] == agent.SYSTEM_PROMPT + agent.FUNDAMENTALS_PROMPT
        close = re.search(r"\| Close \| ([\d,.]+) \|", text).group(1).replace(",", "")
        assert "## 재무 스냅샷(DART)\n\n섹션 " in text and f"2026-08-04 종가 {float(close):g}" in text
        assert text.index("## 선별") < text.index("## 재무 스냅샷(DART)") < text.index("## 포트폴리오")
    d = market_audit_decisions(tmp_path)
    assert all(x.settings["prompt_version"] == "fm-v7" for x in d)
    assert all(any(e.source == "fundamentals" and e.summary.startswith("요약 ") for e in x.evidence) for x in d)
    # the system prompt has no date or ticker: one cache prefix for every call
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{6}\.K[SQ]", agent.FUNDAMENTALS_PROMPT)
    # US takes the SEC EDGAR section (tests/test_edgar.py), not DART's; no User-Agent here, so it says so
    section, why = agent.fundamentals_section("US", "AAPL", DAY, 1.0, tmp_path)
    assert section.title == "재무 스냅샷(SEC EDGAR)" and why == "SEC_USER_AGENT 미설정"


def market_audit_decisions(data):
    from agent_trading_core.audit import market_audit

    return market_audit(data / "audit", "KRX").decisions()


@pytest.mark.unit
@pytest.mark.parametrize("case, want", [
    ("no_key", "키 없음"),
    ("limit", "호출 한도 020"),
    ("network", "네트워크 ConnectionError"),
    ("no_corp", "기업 코드 없음"),
    ("refused", "응답 오류 010"),
])
def test_each_failure_is_a_no_data_section_never_a_stop(tmp_path, monkeypatch, case, want):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    fail = {"limit": {"list.json": Resp({"status": "020", "message": "요청 제한을 초과하였습니다."})},
            "network": {"list.json": requests.ConnectionError("down")},
            "refused": {"list.json": Resp({"status": "010", "message": "등록되지 않은 키입니다."})}}.get(case)
    c = client(tmp_path, FakeDart(fail), key=None if case == "no_key" else KEY)
    section, why = agent.fundamentals_section("KRX", "333333.KS" if case == "no_corp" else "111111.KS", DAY, 1.0,
                                              tmp_path, client=c)
    assert why == want and section.text == f"재무 데이터 없음({want}) — 이 종목은 가격 스냅샷만으로 판단한다."
    assert section.summary == f"재무 데이터 없음({want})" and section.source == "fundamentals"


@pytest.mark.unit
def test_without_a_key_every_decision_is_still_asked_and_the_run_says_why(tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: pytest.fail("no request without a key"))
    llm, market = Capture(), _market()
    market.today = "2026-08-04"
    r = dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, market, at("2026-08-04", "15:40"), decider="llm", llm=llm)
    asked = [t for t, _ in llm.asked]
    assert r.status == "ok" and asked and r.counts["llm_calls"] == len(asked) and "llm_fallback" not in r.counts
    assert f"재무 데이터 없음(키 없음) {len(asked)}종목: {', '.join(asked)}" in r.notes
    assert all("재무 데이터 없음(키 없음)" in q["messages"][0]["content"] for q in llm.requests)


@pytest.mark.unit
def test_the_key_is_in_no_exception_section_note_cache_file_or_log(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    seen = []
    url = f"https://opendart.fss.or.kr/api/list.json?crtfc_key={KEY}&corp_code={CORP}"
    for fail in ({"list.json": requests.ConnectionError(f"Max retries exceeded with url: {url}")},
                 {"list.json": requests.Timeout(f"Read timed out. (url: {url})")},
                 {"list.json": Resp({"status": "500"}, status=500)},
                 {"list.json": Resp({"status": "010", "message": "등록되지 않은 키입니다."})},
                 {"fnlttSinglAcnt.json": Resp(None)},                       # not JSON
                 {"corpCode.xml": Resp(content=b"<result><status>020</status></result>")},
                 {}):
        s = FakeDart(fail)
        c = client(tmp_path / str(len(seen)), s)
        try:
            dart.build(c, "111111.KS", DAY, 50_000.0)
        except dart.DartError as e:
            seen += [str(e), repr(e), e.detail]
        section, why = agent.fundamentals_section("KRX", "111111.KS", DAY, 50_000.0, tmp_path, client=c)
        seen += [section.text, section.summary, why or ""]
        assert all(p.get("crtfc_key") == KEY for _, p in s.calls)          # the key was sent: the scan means something
    files = [p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json")]
    assert files and len(seen) > 20
    assert not [x for x in seen + files + [caplog.text] if KEY in x]



FIXTURES = Path(__file__).parent / "fixtures" / "dart_005930"


class Recorded:
    """OpenDART answers recorded for 005930 (삼성전자, corp 00126380) on 2026-11-16, by path and report."""

    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        path = url.rsplit("/", 1)[1]
        self.calls.append(path)
        if path == "corpCode.xml":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("CORPCODE.xml", "<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
                                           "<stock_code>005930</stock_code></list></result>".encode())
            return Resp(content=buf.getvalue())
        name = {"list.json": f"list-{'A' if params.get('pblntf_ty') == 'A' else 'all'}"}.get(path) or \
            f"{path.split('.')[0]}-{params['bsns_year']}-{params['reprt_code']}"
        file = FIXTURES / f"{name}.json"
        return Resp(json.loads(file.read_text(encoding="utf-8")) if file.exists() else {"status": "013"})


@pytest.mark.unit
def test_recorded_answers_of_005930_give_the_section_checked_against_the_raw_rows(tmp_path):
    s = Recorded()
    text, summary = dart.build(client(tmp_path, s), "005930.KS", DAY, 208_000.0)
    assert summary.startswith("DART 반기보고서 (2026.06)(접수 2026-08-14) 최근 4분기 매출 4,852,720억")
    assert "최신 정기보고서: 반기보고서 (2026.06), 접수 2026-08-14, 접수번호 20260814003699, 연결재무제표 기준" in text
    # Q2 2026: 171,499,470백만 vs 74,566,317백만 a year before; Q4 2025: 333,605,938 − 239,768,567 = 93,837,371백만
    assert "| 2026 2분기(4~6월) | 1,714,995억 | +130.0% |" in text
    assert "| 2025 4분기(10~12월) | 938,374억 | +23.8% | 200,737억 |" in text
    assert "20260310002820 − 20251114002447" in text
    # liabilities 1,801,708억 ÷ equity 5,793,097억 → 31.1%; 5,846,278,608 common shares (preferred 802,371,203 left out)
    assert "| 부채비율(부채총계 ÷ 자본총계) | 31.1% |" in text and "| 5,846,278,608주 |" in text
    # four quarters of net income 716,245 + 472,253 + 196,417 + 122,257 = 1,507,172억; cap 208,000 × shares
    assert "순이익 1,507,172억" in text and "| PER(시가총액 ÷ 최근 4분기 순이익) | 8.1배 |" in text
    assert "| PBR(시가총액 ÷ 자본총계) | 2.10배 |" in text
    # the company's own filings only: no insider report (filed by persons), nothing from 11-13 on
    assert "임원ㆍ주요주주" not in text and "2026-11-13" not in text
    assert s.calls.count("fnlttSinglAcnt.json") == 4 and s.calls.count("stockTotqySttus.json") == 1


@pytest.mark.unit
def test_a_company_with_another_year_end_gets_no_quarters(tmp_path):
    # a June year-end: its 분기보고서 (2025.09) and (2026.03) would pass the month check as Q3 and Q1
    june = [filing("사업보고서 (2025.06)", "20250915000001"), filing("분기보고서 (2025.09)", "20251114000001"),
            filing("반기보고서 (2025.12)", "20260216000001"), filing("분기보고서 (2026.03)", "20260515000001")]

    class June(FakeDart):
        def get(self, url, params, timeout):
            if url.endswith("list.json") and params.get("pblntf_ty") == "A":
                self.calls.append(("list.json", dict(params)))
                return Resp({"status": "000", "list": june})
            return super().get(url, params, timeout)

    s = June()
    text, _ = dart.build(client(tmp_path, s), "111111.KS", DAY, 50_000.0)
    assert "정기보고서 재무: 없음" in text and "| 분기 |" not in text and "PER" not in text
    assert "fnlttSinglAcnt.json" not in [p for p, _ in s.calls]
    assert "주요사항보고서(자기주식취득결정)" in text                 # the filings are still listed


@pytest.mark.unit
def test_after_a_network_failure_the_run_asks_dart_nothing_more(tmp_path, monkeypatch):
    monkeypatch.setitem(dl.LLM, "fundamentals", True)
    s = FakeDart(fail={"corpCode.xml": requests.ConnectTimeout("no answer")})
    c = client(tmp_path, s)
    whys = [agent.fundamentals_section("KRX", t, DAY, 1.0, tmp_path, client=c)[1] for t in ("111111.KS", "222222.KS")]
    assert whys == ["네트워크 ConnectTimeout"] * 2 and len(s.calls) == 1   # one timeout, not one per ticker
    # a refused answer of one ticker (not the network) does not stop the others
    s2 = FakeDart(fail={"stockTotqySttus.json": Resp({"status": "100"})})
    c2 = client(tmp_path / "b", s2)
    assert agent.fundamentals_section("KRX", "111111.KS", DAY, 1.0, tmp_path, client=c2)[1] == "응답 오류 100"
    assert c2.down is None


@pytest.mark.unit
def test_the_urllib3_request_line_is_not_logged_at_debug(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    wire = logging.getLogger("urllib3.connectionpool")

    class Logs(FakeDart):
        def get(self, url, params, timeout):     # what urllib3 does under requests: the request line at DEBUG
            wire.debug('https://opendart.fss.or.kr:443 "GET %s?crtfc_key=%s HTTP/1.1" 200', url, params["crtfc_key"])
            return super().get(url, params, timeout)

    wire.debug("warm %s", "x")                  # a DEBUG line before: isEnabledFor(DEBUG) is cached True
    before = wire.level
    dart.build(client(tmp_path, Logs()), "111111.KS", DAY, 50_000.0)
    assert KEY not in caplog.text and wire.level == before
    wire.debug("after %s", KEY)                    # the level is back: the guard is only around the request
    assert KEY in caplog.text


@pytest.mark.unit
def test_rows_under_a_receipt_the_list_does_not_name_are_not_that_period(tmp_path, monkeypatch):
    # the API answers 2026 Q1 with rows of an earlier, other filing (received before the day, but not the period's)
    other = accounts("20250515000009", (777, 777, 1, 1), (77, 77, 1, 1), (7, 7, 1, 1))
    monkeypatch.setitem(ACCOUNTS, ("2026", "11013"), other)
    text, _ = dart.build(client(tmp_path), "111111.KS", DAY, 50_000.0)
    assert "| 2026 1분기(1~3월) | N/A | N/A |" in text and "777억" not in text
    assert "N/A(4분기 중 빈 분기)" in text                         # no four-quarter sum, so no PER either
