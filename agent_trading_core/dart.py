"""DART OpenAPI (금융감독원 전자공시) financials and filings for the LLM snapshot of a KRX stock (docs/01 §12-8).

Public filings, read only. The key is ``DART_API_KEY`` from the environment, after the repository ``.env`` or
``TRADING_ENV_FILE`` (neither overrides it). It goes into the query string and nowhere else: ``requests`` puts the
full URL, key included, in its exception messages, so a failed call is raised with its kind and the exception's
class only, and the cache keeps response bodies, which carry no key.

Endpoints (https://opendart.fss.or.kr/guide/main.do, read 2026-11-16):
  corpCode.xml          ZIP of CORPCODE.xml: corp_code, corp_name, stock_code (blank when unlisted), modify_date
  list.json             filings of one corp_code by receipt date (bgn_de · end_de YYYYMMDD; pblntf_ty A = periodic),
                        page_count up to 100, newest first
  fnlttSinglAcnt.json   key accounts of one periodic report (bsns_year, reprt_code 11013 Q1 · 11012 H1 · 11014 Q3 ·
                        11011 annual), CFS and OFS rows. In a quarterly or half-year report an income-statement
                        row's thstrm_amount is the three months and thstrm_add_amount the year to date, frmtrm_* the
                        same for the year before (measured 2026-11-16 on 005930: H1 171.5 + Q1 133.9 = 305.4 조)
  stockTotqySttus.json  shares by class of one periodic report: istc_totqy issued. ``se`` is the filer's own text
                        ("보통주", "의결권 있는 주식 (보통주)", "의결권 있는 보통주" on 2026-11-16, line breaks
                        inside), so the common row is the one naming 보통주
Status: 000 ok, 013 no data, 020 over the request limit (20,000 a day as a rule), 010 · 011 · 012 · 901 key or IP
refused, 100 · 101 bad request, 800 maintenance, 900 undefined.

Point in time: the section of day D holds only what was received before D — filings with rcept_dt < D, and a
report's figures only when their receipt number (whose first eight digits are the receipt date) is before D. A
report that comes back under a later receipt (a correction filed on or after D) is left out, not shown as known.

ponytail: December year-end companies only (a periodic report's period month must be 03 · 06 · 09 · 12 and match its
kind); another year-end needs bsns_year mapped from the period, which the guide does not state.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

from agent_trading_core.config import DART
from agent_trading_core.files import replace_file

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://opendart.fss.or.kr/api"
KST = ZoneInfo("Asia/Seoul")

# failure kinds, the section's "재무 데이터 없음(<kind>)"
NO_KEY, LIMIT, NETWORK, NO_CORP, API = "키 없음", "호출 한도", "네트워크", "기업 코드 없음", "응답 오류"
NO_DATA = "013"
STATUS_KIND = {"020": LIMIT, "800": NETWORK, "900": NETWORK}   # others but 000 · 013: API with the code

# (kind in report_nm, period month) → reprt_code, quarter
PERIODS = {("분기보고서", "03"): ("11013", 1), ("반기보고서", "06"): ("11012", 2), ("분기보고서", "09"): ("11014", 3),
           ("사업보고서", "12"): ("11011", 4)}
CODE_OF_QUARTER = {q: code for code, q in PERIODS.values()}
REPORT_NM = re.compile(r"(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")
REVENUE = {"매출액", "수익(매출액)", "영업수익", "매출"}


class DartError(Exception):
    """A section that cannot be built: ``kind`` is one of the failure kinds, ``detail`` never holds the key."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind, self.detail = kind, detail
        super().__init__(kind + (f" {detail}" if detail else ""))


def api_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("TRADING_ENV_FILE") or ROOT / ".env", override=False)
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise DartError(NO_KEY, "DART_API_KEY — 환경변수나 저장소 루트 .env(또는 TRADING_ENV_FILE)")
    return key


def amount(text) -> int | None:
    """'86,061,747,000,000' → int; '-', '' or None → None."""
    s = str(text or "").replace(",", "").strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


def receipt_day(rcept_no: str) -> str:
    return f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"


@dataclass(frozen=True)
class Report:
    """One periodic report as the filing list names it."""

    year: int
    quarter: int          # 1 · 2 · 3 · 4 (annual)
    code: str             # reprt_code
    name: str             # 분기보고서 (2026.03)
    rcept_no: str         # the latest receipt of the period
    receipts: frozenset[str] = frozenset()   # every receipt of the period in the list (original, corrections)


class DartClient:
    """The calls and their cache under ``cache_dir`` (``data/cache/dart``). ``session`` and ``today`` (KST) are
    injectable for tests; the key is read at the first call that needs the network."""

    def __init__(self, cache_dir: str | Path, *, key: str | None = None, session=None, today: date | None = None):
        self.dir = Path(cache_dir)
        self._key, self.session = key, session or requests.Session()
        self.today = today or datetime.now(KST).date()
        self.stats = {"calls": 0, "cache_hits": 0}
        self.down: DartError | None = None   # no key, the limit, the network: the rest of the run is not asked

    def _get(self, path: str, params: dict) -> requests.Response:
        if self.down is not None:
            raise self.down
        try:
            if self._key is None:
                self._key = api_key()
            self.stats["calls"] += 1
            # urllib3 logs each request line, query string and key included, at DEBUG: kept above it meanwhile
            wire = logging.getLogger("urllib3.connectionpool")
            level = wire.level
            wire.setLevel(max(level, logging.INFO))     # setLevel, not .level =: it clears isEnabledFor's cache
            try:
                r = self.session.get(f"{BASE_URL}/{path}", params={"crtfc_key": self._key, **params},
                                     timeout=float(DART["timeout"]))
            except requests.RequestException as e:   # the message holds the URL with the key: the class only
                raise DartError(NETWORK, type(e).__name__) from None
            finally:
                wire.setLevel(level)
            if r.status_code != 200:
                raise DartError(NETWORK, f"HTTP {r.status_code}")
            return r
        except DartError as e:
            self.down = e
            raise

    def _down_if(self, e: DartError) -> None:
        if e.kind in (NO_KEY, LIMIT, NETWORK):
            self.down = e

    def _json(self, path: str, params: dict) -> dict:
        try:
            body = self._get(path, params).json()
        except ValueError:
            raise DartError(API, f"{path} JSON 아님") from None
        status = str(body.get("status", ""))
        if status not in ("000", NO_DATA):
            e = DartError(STATUS_KIND.get(status, API), status)
            self._down_if(e)
            raise e
        return body

    def _cached(self, rel: str, path: str, params: dict, keep) -> dict:
        """The body from ``rel`` under the cache, else from the API, written there when ``keep(body)``."""
        file = self.dir / rel
        if file.exists():
            self.stats["cache_hits"] += 1
            return json.loads(file.read_text(encoding="utf-8"))
        body = self._json(path, params)
        if keep(body):
            file.parent.mkdir(parents=True, exist_ok=True)
            replace_file(file, lambda tmp: Path(tmp).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8"))
        return body

    # --- corp codes ----------------------------------------------------------
    def corp_codes(self) -> dict[str, list[str]]:
        """stock_code → [corp_code, corp_name] of listed companies, from ``corp_codes.json`` when it is younger than
        ``DART["corp_codes_max_age_days"]``; an older file is used when the download fails."""
        file = self.dir / "corp_codes.json"
        saved = json.loads(file.read_text(encoding="utf-8")) if file.exists() else None
        if saved and (self.today - date.fromisoformat(saved["fetched"])).days < DART["corp_codes_max_age_days"]:
            self.stats["cache_hits"] += 1
            return saved["codes"]
        try:
            r = self._get("corpCode.xml", {})
            try:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    root = ElementTree.fromstring(z.read(z.namelist()[0]))
            except zipfile.BadZipFile:            # an error comes back as a JSON or XML body, not a ZIP
                status = re.search(rb"<status>(\d+)</status>|\"status\"\s*:\s*\"(\d+)\"", r.content)
                code = next((g.decode() for g in status.groups() if g), "") if status else ""
                e = DartError(STATUS_KIND.get(code, API), code or "corpCode ZIP 아님")
                self._down_if(e)
                raise e from None
        except DartError:
            if saved:
                return saved["codes"]
            raise
        codes = {s: [e.findtext("corp_code", "").strip(), e.findtext("corp_name", "").strip()]
                 for e in root.iter("list") if (s := (e.findtext("stock_code") or "").strip())}
        self.dir.mkdir(parents=True, exist_ok=True)
        replace_file(file, lambda tmp: Path(tmp).write_text(json.dumps(
            {"fetched": self.today.isoformat(), "codes": codes}, ensure_ascii=False), encoding="utf-8"))
        return codes

    def corp(self, ticker: str) -> tuple[str, str]:
        """(corp_code, corp_name) of a KRX ticker (``005930.KS``)."""
        hit = self.corp_codes().get(ticker.split(".")[0])
        if not hit:
            raise DartError(NO_CORP, ticker)
        return hit[0], hit[1]

    # --- lists and reports ---------------------------------------------------
    def filings(self, corp_code: str, day: str, days: int, periodic: bool) -> list[dict]:
        """Filings received in the ``days`` before ``day`` (the day itself excluded), one page of 100, newest first.
        Kept once the window's last day is past (its list no longer grows)."""
        end = date.fromisoformat(day) - timedelta(days=1)
        params = {"corp_code": corp_code, "bgn_de": f"{end - timedelta(days=days - 1):%Y%m%d}",
                  "end_de": f"{end:%Y%m%d}", "page_count": "100"} | ({"pblntf_ty": "A"} if periodic else {})
        body = self._cached(f"list/{corp_code}/{'A' if periodic else 'all'}-{days}-{end:%Y%m%d}.json", "list.json",
                            params, keep=lambda b: end < self.today)
        return [r for r in body.get("list") or [] if str(r.get("rcept_dt", "")) < f"{date.fromisoformat(day):%Y%m%d}"]

    def accounts(self, corp_code: str, report: Report) -> list[dict]:
        """Key-account rows of ``report``; kept per receipt number (013 — not out yet — is not kept)."""
        body = self._cached(f"fin/{corp_code}/{report.year}-{report.code}-{report.rcept_no}.json", "fnlttSinglAcnt.json",
                            {"corp_code": corp_code, "bsns_year": str(report.year), "reprt_code": report.code},
                            keep=lambda b: b.get("status") == "000")
        return body.get("list") or []

    def shares(self, corp_code: str, report: Report) -> list[dict]:
        body = self._cached(f"shares/{corp_code}/{report.year}-{report.code}-{report.rcept_no}.json",
                            "stockTotqySttus.json",
                            {"corp_code": corp_code, "bsns_year": str(report.year), "reprt_code": report.code},
                            keep=lambda b: b.get("status") == "000")
        return body.get("list") or []


def periodic_reports(rows: list[dict]) -> dict[tuple[int, int], Report]:
    """(year, quarter) → the period's latest report among ``rows`` (a correction replaces the original), with every
    receipt of the period. Nothing for a company whose annual report is not a December period: its quarterly
    reports would pass the month check under another fiscal year."""
    out: dict[tuple[int, int], Report] = {}
    for r in rows:
        m = REPORT_NM.search(str(r.get("report_nm", "")))
        if m and m.group(1) == "사업보고서" and m.group(3) != "12":
            return {}
        if not m or (m.group(1), m.group(3)) not in PERIODS:
            continue
        code, q = PERIODS[(m.group(1), m.group(3))]
        key, no = (int(m.group(2)), q), str(r["rcept_no"])
        old = out.get(key)
        receipts = (old.receipts if old else frozenset()) | {no}
        latest = max(no, old.rcept_no) if old else no
        out[key] = Report(key[0], q, code, f"{m.group(1)} ({m.group(2)}.{m.group(3)})", latest, receipts)
    return out


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", str(name)).replace("(손실)", "")


def pick(rows: list[dict], fs_div: str, item: str) -> dict | None:
    """The row of ``item`` (revenue · operating · net · assets · liabilities · equity) in the ``fs_div`` statements."""
    for r in rows:
        if r.get("fs_div") != fs_div:
            continue
        n = _norm(r.get("account_nm", ""))
        if {"revenue": n in REVENUE, "operating": n == "영업이익", "net": n == "당기순이익",
                "assets": n == "자산총계", "liabilities": n == "부채총계", "equity": n == "자본총계"}[item]:
            return r
    return None


def quarter_values(item: str, fs_div: str, year: int, q: int, fin: dict) -> tuple[int | None, int | None, str | None]:
    """(three months, the same three months a year before, the receipt it rests on) for ``item`` in quarter ``q``
    of ``year``. ``fin``: (year, quarter) → account rows. Q4 is the annual report less the Q3 report's year to date."""
    if q < 4:
        row = pick(fin.get((year, q)) or [], fs_div, item)
        if row is None:
            return None, None, None
        return amount(row.get("thstrm_amount")), amount(row.get("frmtrm_amount")), row.get("rcept_no")
    annual, q3 = pick(fin.get((year, 4)) or [], fs_div, item), pick(fin.get((year, 3)) or [], fs_div, item)
    if annual is None or q3 is None:
        return None, None, None
    cur, ytd = amount(annual.get("thstrm_amount")), amount(q3.get("thstrm_add_amount"))
    prev, prev_ytd = amount(annual.get("frmtrm_amount")), amount(q3.get("frmtrm_add_amount"))
    return (None if cur is None or ytd is None else cur - ytd,
            None if prev is None or prev_ytd is None else prev - prev_ytd,
            f"{annual.get('rcept_no')} − {q3.get('rcept_no')}")


def last_quarters(year: int, q: int, n: int = 4) -> list[tuple[int, int]]:
    out = []
    for _ in range(n):
        out.append((year, q))
        year, q = (year - 1, 4) if q == 1 else (year, q - 1)
    return out


def needed(quarters: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """The reports the quarters are computed from: Q4 needs the annual and the Q3 report."""
    return {p for y, q in quarters for p in ([(y, 4), (y, 3)] if q == 4 else [(y, q)])}


def eok(v: int | None) -> str:
    return "N/A" if v is None else f"{v / 1e8:,.0f}억"


def growth(cur: int | None, prev: int | None) -> str:
    if cur is None or prev is None or prev == 0:
        return "N/A"
    if prev < 0:
        return "흑자전환" if cur > 0 else "적자지속"
    return f"{cur / prev - 1:+.1%}" if cur >= 0 else "적자전환"


def common_shares(rows: list[dict], before: str) -> dict | None:
    """The common-stock row of a stockTotqySttus answer received before ``before`` (YYYYMMDD)."""
    for r in rows:
        se = re.sub(r"\s+", "", str(r.get("se", "")))
        if "보통주" in se and str(r.get("rcept_no", "")) < before:
            return r
    return None


def months(q: int) -> str:
    return {1: "1~3월", 2: "4~6월", 3: "7~9월", 4: "10~12월"}[q]


def build(client: DartClient, ticker: str, day: str, close: float | None) -> tuple[str, str]:
    """(text, summary) of the section for ``ticker`` on ``day``; raises ``DartError`` when there is none."""
    corp_code, corp_name = client.corp(ticker)
    d = f"{date.fromisoformat(day):%Y%m%d}"
    reports = periodic_reports(client.filings(corp_code, day, DART["periodic_days"], periodic=True))
    def known(key: tuple[int, int]) -> list[dict]:
        # only the rows filed under one of the period's receipts in the list (all received before the day): a later
        # correction, or another report answered for that year and code, is not known as this period on the day
        return [r for r in client.accounts(corp_code, reports[key]) if str(r.get("rcept_no", "")) in reports[key].receipts]

    fin: dict[tuple[int, int], list[dict]] = {}
    for key in sorted(reports, reverse=True):          # the latest report with figures, then only what it needs
        if rows := known(key):
            fin[key] = rows
            break
    if fin:
        for key in sorted(needed(last_quarters(*max(fin))) - set(fin)):
            if key in reports and (rows := known(key)):
                fin[key] = rows
    recent = client.filings(corp_code, day, DART["recent_days"], periodic=False)
    own = list(dict.fromkeys((r["rcept_dt"], re.sub(r"\s+", " ", str(r["report_nm"])).strip(), r["rcept_no"])
                             for r in recent if r.get("flr_nm") == corp_name))
    lines = ["- 출처: DART 전자공시(opendart.fss.or.kr) — 기준일 전날까지 접수된 보고서 · 공시만. 금액은 억원(반올림)."]
    summary_parts = []
    if not fin:
        lines.append(f"- 정기보고서 재무: 없음(기준일 전 {DART['periodic_days']}일 안에 접수된 12월 결산 정기보고서의 주요계정 없음)")
    else:
        latest = max(fin)
        rep, rows = reports[latest], fin[latest]
        fs_div = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
        fs_nm = "연결재무제표" if fs_div == "CFS" else "재무제표(별도)"
        lines.append(f"- 최신 정기보고서: {rep.name}, 접수 {receipt_day(rep.rcept_no)}, 접수번호 {rep.rcept_no}, {fs_nm} 기준")
        quarters = last_quarters(*latest)
        lines += ["", "| 분기 | 매출액 | 전년 동기 대비 | 영업이익 | 전년 동기 대비 | 순이익 | 전년 동기 대비 | 출처 접수번호 |",
                  "|---|---:|---:|---:|---:|---:|---:|---|"]
        ttm, ttm_ok = {"revenue": 0, "operating": 0, "net": 0}, {"revenue": True, "operating": True, "net": True}
        for y, q in quarters:
            cells, refs = [], []
            for item in ("revenue", "operating", "net"):
                cur, prev, ref = quarter_values(item, fs_div, y, q, fin)
                cells += [eok(cur), growth(cur, prev)]
                refs += [ref] if ref else []
                if cur is None:
                    ttm_ok[item] = False
                else:
                    ttm[item] += cur
            lines.append(f"| {y} {q}분기({months(q)}) | " + " | ".join(cells) + f" | {(refs or ['없음'])[0]} |")
        lines.append("- 4분기 = 사업보고서 연간 − 3분기보고서 누적. 순이익은 당기순이익(비지배지분 포함).")
        if not ttm_ok["revenue"] and all(pick(r, fs_div, "revenue") is None for r in fin.values()):
            lines.append("- 매출액 N/A: 이 회사의 주요계정에 매출액 계정이 없다(금융회사는 이자수익 · 순이자손익 등으로 공시).")
        assets, liab, equity = (amount((pick(rows, fs_div, k) or {}).get("thstrm_amount"))
                                for k in ("assets", "liabilities", "equity"))
        debt = f"{liab / equity:.1%}" if liab is not None and equity and equity > 0 else "N/A"
        lines += ["", f"| 재무상태({rep.name} 기말) | 값 |", "|---|---:|", f"| 자산총계 | {eok(assets)} |",
                  f"| 부채총계 | {eok(liab)} |", f"| 자본총계(비지배지분 포함) | {eok(equity)} |",
                  f"| 부채비율(부채총계 ÷ 자본총계) | {debt} |"]
        ttm_text = {k: eok(v) if ttm_ok[k] else "N/A(4분기 중 빈 분기)" for k, v in ttm.items()}
        lines.append(f"- 최근 4분기 합계: 매출액 {ttm_text['revenue']}, 영업이익 {ttm_text['operating']}, "
                     f"순이익 {ttm_text['net']}")
        common = common_shares(client.shares(corp_code, rep), d)
        shares = amount(common.get("istc_totqy")) if common else None
        if close is None or not shares:
            lines.append("- 주당 지표 · PER · PBR: 계산 안 함(" + ("스냅샷 종가 없음" if close is None else
                         f"{rep.name}의 보통주 발행주식총수 없음") + ")")
            per = None
        else:
            cap = close * shares
            eps = ttm["net"] / shares if ttm_ok["net"] else None
            bps = equity / shares if equity else None
            per = cap / ttm["net"] if ttm_ok["net"] and ttm["net"] > 0 else None
            pbr = cap / equity if equity and equity > 0 else None
            per_text = (f"{per:.1f}배" if per is not None else "N/A(4분기 중 빈 분기)" if not ttm_ok["net"]
                        else "N/A(최근 4분기 순이익 ≤ 0)")
            lines += ["", f"| 주당 지표 · 배수(스냅샷 종가 {close:,.0f}원) | 값 |", "|---|---:|",
                      f"| 보통주 발행주식총수({rep.name}, 접수번호 {common['rcept_no']}, 자기주식 포함 · 우선주 제외) | {shares:,}주 |",
                      f"| 시가총액(종가 × 보통주) | {eok(round(cap))} |",
                      f"| EPS(최근 4분기 순이익 ÷ 보통주) | {'N/A' if eps is None else f'{eps:,.0f}원'} |",
                      f"| BPS(자본총계 ÷ 보통주) | {'N/A' if bps is None else f'{bps:,.0f}원'} |",
                      f"| PER(시가총액 ÷ 최근 4분기 순이익) | {per_text} |",
                      f"| PBR(시가총액 ÷ 자본총계) | {'N/A' if pbr is None else f'{pbr:.2f}배'} |"]
        net_ttm = ttm_text["net"]
        summary_parts.append(f"DART {rep.name}(접수 {receipt_day(rep.rcept_no)}) 최근 4분기 매출 {ttm_text['revenue']}, "
                             f"순이익 {net_ttm}, 부채비율 {debt}" + ("" if per is None else f", PER {per:.1f}배"))
    n = DART["disclosures_max"]
    lines += ["", f"최근 {DART['recent_days']}일 회사 제출 공시(기준일 전날까지 {len(own)}건 중 최근 {min(n, len(own))}건):"]
    lines += [f"- {receipt_day(no)} {title} (접수번호 {no})" for _, title, no in own[:n]] or ["- 없음"]
    summary_parts.append(f"최근 {DART['recent_days']}일 회사 공시 {len(own)}건")
    return "\n".join(lines), "; ".join(summary_parts)


def missing_text(kind: str) -> str:
    return f"재무 데이터 없음({kind}) — 이 종목은 가격 스냅샷만으로 판단한다."
