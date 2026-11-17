"""SEC EDGAR financials and filings for the LLM snapshot of a US stock (docs/01 §12-9).

Public data, read only, no key. SEC asks every automated request to declare a User-Agent with a contact (company or
name and an e-mail — https://www.sec.gov/os/accessing-edgar-data, read 2026-11-17): it is ``SEC_USER_AGENT`` from the
environment, after the repository ``.env`` or ``TRADING_ENV_FILE`` (neither overrides it), never guessed and never
written anywhere — without it nothing is requested. At most 10 requests a second across all machines
(https://www.sec.gov/privacy.htm#security: over it the IP "may be limited for a brief period", until the rate has been
below the threshold for 10 minutes); requests here are spaced ``EDGAR["min_interval"]`` apart.

Endpoints (https://www.sec.gov/search-filings/edgar-application-programming-interfaces, read 2026-11-17; the CIK is
the 10-digit central index key with its leading zeros):
  www.sec.gov/files/company_tickers.json                  ticker, CIK and name associations
  data.sec.gov/api/xbrl/companyfacts/CIK##########.json   every non-custom-taxonomy fact (us-gaap, dei) of the entity
  data.sec.gov/submissions/CIK##########.json             filing history, "recent" = at least a year or 1,000 filings
The JSON field names are not documented on those pages (nor on the old API page, the FAQ or the privacy page); the
shapes read here were checked on real answers (2026-11-17: AAPL, XOM, V, LLY, MSFT — docs/01 §12-9): company_tickers
``{"0": {"cik_str", "ticker", "title"}}``; companyfacts ``facts.<taxonomy>.<tag>.units.<unit>[{start?, end, val, accn,
fy, fp, form, filed, frame?}]``; submissions ``filings.recent`` columns ``accessionNumber · filingDate · form · items``.
An answer of another shape is the failure "응답 형식", never a guessed figure. ``fy`` and ``fp`` are the filing's fiscal year and period focus (FY, Q1 … Q4), not the value's
(https://www.sec.gov/files/financial-statement-data-sets.pdf, SUB table); a value's own period is its start and end.

Point in time: the section of day D holds only facts and filings with ``filed`` (``filingDate``) before D. A period
reported again (an amendment, a later filing's comparative) takes the latest filed before D. A cached answer serves D
only when it was fetched on or after D (New York date), so every filing before D is in it.

ponytail: a company whose cover page reports shares per class (several values) gets no per-share figures; Q4 diluted
EPS is the annual less the nine months, an approximation the section says.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from agent_trading_core.config import EDGAR
from agent_trading_core.dart import growth
from agent_trading_core.files import replace_file

ROOT = Path(__file__).resolve().parents[1]
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DATA_URL = "https://data.sec.gov"
ET = ZoneInfo("America/New_York")

# failure kinds, the section's "재무 데이터 없음(<kind>)"
NO_UA, LIMIT, DENIED, NETWORK = "SEC_USER_AGENT 미설정", "호출 한도", "접근 거부", "네트워크"
NO_CIK, NO_FACTS, NO_TAG, SHAPE = "CIK 없음", "XBRL 재무 없음", "태그 없음", "응답 형식"
STICKY = (NO_UA, LIMIT, DENIED, NETWORK)     # the rest of the run is not asked

# us-gaap tags per item, first that has the latest quarter wins (docs/01 §12-9 table)
TAGS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"),
    "operating": ("OperatingIncomeLoss",),
    "net": ("NetIncomeLoss", "ProfitLoss"),
    "eps": ("EarningsPerShareDiluted",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"),
}
SHARES_TAG = ("dei", "EntityCommonStockSharesOutstanding")
# the periodic reports figures are taken from: an 8-K's earnings release or a proxy's pay-versus-performance table
# tags the same concepts (seen on MSFT, 2026-11-17) but is not the report the figure belongs to
PERIODIC_FORMS = {f + a for f in ("10-K", "10-Q", "10-KT", "10-QT", "20-F", "40-F") for a in ("", "/A")}
OWNERSHIP_FORMS = {"3", "4", "5", "144", "3/A", "4/A", "5/A", "144/A"}    # insiders' reports, filed under the issuer
QUARTER, NINE, YEAR = (70, 110), (250, 300), (340, 390)                    # days in a duration fact


class EdgarError(Exception):
    """A section that cannot be built: ``kind`` is one of the failure kinds, ``detail`` never holds the User-Agent."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind, self.detail = kind, detail
        super().__init__(kind + (f" {detail}" if detail else ""))


def user_agent() -> str:
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("TRADING_ENV_FILE") or ROOT / ".env", override=False)
    ua = (os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        raise EdgarError(NO_UA)
    return ua


def cik10(cik) -> str:
    return f"{int(cik):010d}"


def norm_ticker(t: str) -> str:
    """``BRK.B`` and ``BRK-B`` alike (SEC writes the class with a hyphen)."""
    return t.strip().upper().replace(".", "-")


class EdgarClient:
    """The calls and their cache under ``cache_dir`` (``data/cache/edgar``). ``session``, ``today`` (New York),
    ``clock`` and ``sleep`` are injectable for tests; the User-Agent is read at the first call that needs the network."""

    def __init__(self, cache_dir: str | Path, *, user_agent: str | None = None, session=None, today: date | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.dir = Path(cache_dir)
        self._ua, self.session = user_agent, session or requests.Session()
        self.today = today or datetime.now(ET).date()
        self.clock, self.sleep, self._last = clock, sleep, None
        self.stats = {"calls": 0, "cache_hits": 0}
        self.down: EdgarError | None = None

    def _get(self, url: str) -> dict:
        if self.down is not None:
            raise self.down
        try:
            if self._ua is None:
                self._ua = user_agent()
            if self._last is not None and (wait := EDGAR["min_interval"] - (self.clock() - self._last)) > 0:
                self.sleep(wait)
            self._last = self.clock()
            self.stats["calls"] += 1
            try:
                r = self.session.get(url, headers={"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"},
                                     timeout=float(EDGAR["timeout"]))
            except requests.RequestException as e:     # the class only: no header or body in the message
                raise EdgarError(NETWORK, type(e).__name__) from None
            if r.status_code == 404:
                raise EdgarError(NO_FACTS if "/companyfacts/" in url else SHAPE, "HTTP 404")
            if r.status_code != 200:
                kind = {403: DENIED, 429: LIMIT}.get(r.status_code, NETWORK)
                raise EdgarError(kind, f"HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError:
                raise EdgarError(SHAPE, "JSON 아님") from None
        except EdgarError as e:
            if e.kind in STICKY:
                self.down = e
            raise

    def _cached(self, rel: str, url: str, fresh_from: date) -> dict:
        """The body saved under ``rel`` when it was fetched on or after ``fresh_from``, else fetched and saved."""
        file = self.dir / rel
        if file.exists():
            saved = json.loads(file.read_text(encoding="utf-8"))
            if date.fromisoformat(saved["fetched"]) >= fresh_from:
                self.stats["cache_hits"] += 1
                return saved["body"]
        body = self._get(url)
        file.parent.mkdir(parents=True, exist_ok=True)
        replace_file(file, lambda tmp: Path(tmp).write_text(json.dumps(
            {"fetched": self.today.isoformat(), "body": body}, ensure_ascii=False), encoding="utf-8"))
        return body

    def ciks(self) -> dict[str, list]:
        """ticker → [CIK, name], from ``company_tickers.json`` younger than ``EDGAR["tickers_max_age_days"]``."""
        body = self._cached("company_tickers.json", TICKERS_URL,
                            self.today - timedelta(days=EDGAR["tickers_max_age_days"] - 1))
        try:
            return {norm_ticker(r["ticker"]): [int(r["cik_str"]), str(r["title"])] for r in body.values()}
        except (AttributeError, KeyError, TypeError, ValueError):
            raise EdgarError(SHAPE, "company_tickers") from None

    def cik(self, ticker: str) -> tuple[int, str]:
        hit = self.ciks().get(norm_ticker(ticker))
        if not hit:
            raise EdgarError(NO_CIK, ticker)
        return hit[0], hit[1]

    def facts(self, cik: int, day: str) -> dict:
        return self._cached(f"facts/CIK{cik10(cik)}.json", f"{DATA_URL}/api/xbrl/companyfacts/CIK{cik10(cik)}.json",
                            date.fromisoformat(day))

    def submissions(self, cik: int, day: str) -> dict:
        return self._cached(f"submissions/CIK{cik10(cik)}.json", f"{DATA_URL}/submissions/CIK{cik10(cik)}.json",
                            date.fromisoformat(day))


# --- facts as known on the day ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    start: date | None
    end: date
    val: float
    form: str
    accn: str
    filed: str
    fy: object = None
    fp: object = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1 if self.start else 0

    @property
    def src(self) -> str:
        return f"{self.form} {self.accn} 제출 {self.filed}"


def known(facts: dict, taxonomy: str, tag: str, unit: str, day: str) -> list[Fact]:
    """The tag's values in periodic reports filed before ``day``, one per period: the latest filed (then accession)."""
    try:
        rows = (((facts.get("facts") or {}).get(taxonomy) or {}).get(tag) or {}).get("units", {}).get(unit) or []
        best: dict[tuple, Fact] = {}
        for r in rows:
            if str(r["filed"]) >= day or r.get("form") not in PERIODIC_FORMS:
                continue
            f = Fact(date.fromisoformat(r["start"]) if r.get("start") else None, date.fromisoformat(r["end"]),
                     float(r["val"]), str(r.get("form", "")), str(r["accn"]), str(r["filed"]), r.get("fy"), r.get("fp"))
            key = (f.start, f.end)
            if key not in best or (f.filed, f.accn) > (best[key].filed, best[key].accn):
                best[key] = f
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EdgarError(SHAPE, f"{taxonomy}:{tag}") from None
    return sorted(best.values(), key=lambda f: (f.end, f.start or f.end))


def _in(n: int, span: tuple[int, int]) -> bool:
    return span[0] <= n <= span[1]


@dataclass(frozen=True)
class Quarter:
    start: date
    end: date
    val: float
    src: str
    derived: bool = False


def quarters(values: list[Fact]) -> dict[date, Quarter]:
    """end → the three months ending then: a quarterly fact, else (a fourth quarter) the year less the nine months
    from the same start."""
    out = {f.end: Quarter(f.start, f.end, f.val, f.src) for f in values if f.start and _in(f.days, QUARTER)}
    nines = [f for f in values if f.start and _in(f.days, NINE)]
    for y in (f for f in values if f.start and _in(f.days, YEAR)):
        if y.end in out:
            continue
        n = next((n for n in nines if n.start == y.start and _in((y.end - n.end).days, QUARTER)), None)
        if n:
            out[y.end] = Quarter(n.end + timedelta(days=1), y.end, y.val - n.val,
                                 f"{y.form} {y.accn} − {n.form} {n.accn}", derived=True)
    return out


def near(series: dict[date, Quarter], target: date, tol: int) -> Quarter | None:
    hits = [q for e, q in series.items() if abs((e - target).days) <= tol]
    return min(hits, key=lambda q: abs((q.end - target).days)) if hits else None


def choose(facts: dict, item: str, day: str, at: date | None) -> tuple[str | None, dict[date, Quarter]]:
    """(the tag, its quarters) of ``item``: the first candidate with a quarter ending at ``at`` (±10 days), else the
    first with any quarter, else none."""
    found = [(tag, quarters(known(facts, "us-gaap", tag, "USD/shares" if item == "eps" else "USD", day)))
             for tag in TAGS[item]]
    if at is not None:
        for tag, q in found:
            if near(q, at, 10):
                return tag, q
    return next(((tag, q) for tag, q in found if q), (None, {}))


def instant(facts: dict, item: str, day: str, at: date) -> tuple[str | None, Fact | None]:
    """The balance-sheet value of ``item`` at the quarter end ``at`` (±10 days), filed before ``day``: the first
    candidate tag that has one. Total liabilities a filer does not tag are LiabilitiesAndStockholdersEquity less the
    equity (the balance sheet's own identity), said so by the tag name."""
    for tag in TAGS[item]:
        vals = [f for f in known(facts, "us-gaap", tag, "USD", day) if f.start is None and abs((f.end - at).days) <= 10]
        if vals:
            return tag, vals[-1]
    if item == "liabilities":
        eq_tag, eq = instant(facts, "equity", day, at)
        total = [f for f in known(facts, "us-gaap", "LiabilitiesAndStockholdersEquity", "USD", day)
                 if f.start is None and eq is not None and f.end == eq.end]
        if total:
            t = total[-1]
            return (f"LiabilitiesAndStockholdersEquity − {eq_tag}",
                    Fact(None, t.end, t.val - eq.val, t.form, t.accn, t.filed, t.fy, t.fp))
    return None, None


def shares_out(facts: dict, day: str, at: date) -> tuple[Fact | None, str | None]:
    """(the cover page's common shares outstanding of the latest filing before ``day``, why there is none). A cover
    date more than ``EDGAR["shares_max_lag_days"]`` before the quarter end ``at`` is not used: a company that went
    over to shares per class (dimensional, so not in companyfacts) keeps only its old single value."""
    vals = known(facts, *SHARES_TAG, "shares", day)
    if not vals:
        return None, "dei:EntityCommonStockSharesOutstanding 없음"
    latest = max(vals, key=lambda f: (f.filed, f.accn))
    if (at - latest.end).days > EDGAR["shares_max_lag_days"]:
        return None, f"dei:EntityCommonStockSharesOutstanding 최근 값 없음(마지막 {latest.end}, 종류주식별 공시 추정)"
    # every value of that filing and date, before one per period is kept: a cover page per class has several
    rows = facts["facts"][SHARES_TAG[0]][SHARES_TAG[1]]["units"]["shares"]
    same = {float(r["val"]) for r in rows if r.get("accn") == latest.accn and r.get("end") == latest.end.isoformat()}
    if len(same) > 1:
        return None, "표지 주식 수가 여러 값(종류주식) — 합산하지 않음"
    return latest, None


def usd(v: float | None) -> str:
    return "N/A" if v is None else f"${v / 1e6:,.0f}M"


def recent_filings(sub: dict, day: str, days: int) -> tuple[list[tuple[str, str, str, str]], int]:
    """(date, form, accession, 8-K items) of the filings in the ``days`` before ``day``, newest first, without the
    insiders' ownership forms; and how many of those were left out."""
    try:
        rec = sub["filings"]["recent"]
        cols = [rec["filingDate"], rec["form"], rec["accessionNumber"], rec.get("items") or [""] * len(rec["form"])]
        if len({len(c) for c in cols}) != 1:
            raise ValueError("columns")
        rows = list(zip(*cols, strict=True))
    except (KeyError, TypeError, ValueError):
        raise EdgarError(SHAPE, "submissions") from None
    first = f"{date.fromisoformat(day) - timedelta(days=days):%Y-%m-%d}"
    window = [(str(d), str(f), str(a), str(i or "")) for d, f, a, i in rows if first <= str(d) < day]
    window.sort(key=lambda r: (r[0], r[2]), reverse=True)
    own = [r for r in window if r[1] not in OWNERSHIP_FORMS]
    return own, len(window) - len(own)


def build(client: EdgarClient, ticker: str, day: str, close: float | None) -> tuple[str, str]:
    """(text, summary) of the section for ``ticker`` on ``day``; raises ``EdgarError`` when there is none."""
    cik, name = client.cik(ticker)
    facts = client.facts(cik, day)
    # the latest quarter of any net income or revenue candidate: a first candidate the filer stopped using does not
    # hold the section back
    ends = [e for item in ("net", "revenue") for tag in TAGS[item]
            for e in quarters(known(facts, "us-gaap", tag, "USD", day))]
    if not ends:
        raise EdgarError(NO_TAG, "순이익 · 매출 분기 값 없음")
    net_tag, net_q = choose(facts, "net", day, max(ends))
    _, rev_q = choose(facts, "revenue", day, max(ends))
    anchor = net_q if near(net_q, max(ends), 10) else rev_q
    last = near(anchor, max(ends), 10) or max(anchor.values(), key=lambda q: q.end)
    if (date.fromisoformat(day) - last.end).days > EDGAR["max_quarter_age_days"]:
        raise EdgarError(NO_TAG, f"최근 분기 값 없음(마지막 분기 끝 {last.end})")
    rows = [last] + [near(anchor, last.end - timedelta(days=91 * k), 20) for k in (1, 2, 3)]
    ends = [r.end if r else last.end - timedelta(days=91 * k) for k, r in enumerate(rows)]
    labels = {"revenue": "매출", "operating": "영업이익", "net": "순이익", "eps": "EPS"}
    picked = {"net": (net_tag, net_q)} | {i: choose(facts, i, day, last.end) for i in ("revenue", "operating", "eps")}

    lines = [f"- 출처: SEC EDGAR XBRL(data.sec.gov) — {name}(CIK {cik10(cik)}), 기준일 전날까지 제출(filed)된 값 · 공시만. "
             "금액은 백만 달러(반올림).",
             f"- 최신 분기: {last.start}~{last.end}, 출처 {last.src}",
             "", "| 분기(기간) | 매출 | 전년 동기 대비 | 영업이익 | 전년 동기 대비 | 순이익 | 전년 동기 대비 | 출처(form accession 제출일) |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    ttm = {"revenue": 0.0, "operating": 0.0, "net": 0.0, "eps": 0.0}
    ttm_ok = dict.fromkeys(ttm, True)
    for k, end in enumerate(ends):
        cells, refs = [], {}
        for item in ("revenue", "operating", "net", "eps"):
            series = picked[item][1]
            q = near(series, end, 10) if rows[k] else None
            prev = near(series, end - timedelta(days=364), 10) if q else None
            if q is None:
                ttm_ok[item] = False
            else:
                ttm[item] += q.val
                refs[item] = q.src
            if item != "eps":
                cells += [usd(q.val if q else None), growth(q.val if q else None, prev.val if prev else None)]
        # one source when every figure of the row rests on it, else each figure's own (an amendment of one tag)
        ref = (next(iter(refs.values())) if len(set(refs.values())) == 1
               else "; ".join(f"{labels[i]} {src}" for i, src in refs.items()) or "없음")
        period = f"{rows[k].start}~{rows[k].end}" if rows[k] else f"(분기 값 없음, ~{end})"
        lines.append(f"| {period} | " + " | ".join(cells) + f" | {ref} |")
    tag_line = ", ".join(f"{label} {picked[i][0] or '태그 없음'}" for i, label in
                         (("revenue", "매출"), ("operating", "영업이익"), ("net", "순이익")))
    lines += [f"- 태그(us-gaap): {tag_line}. 4분기 값이 따로 없으면 연간(10-K) − 9개월 누적(10-Q), 출처에 두 accession.",
              "- 전년 동기 대비 = 364일(±10일) 전에 끝난 같은 길이 분기와 비교."]

    eq_tag, equity = instant(facts, "equity", day, last.end)
    as_tag, assets = instant(facts, "assets", day, last.end)
    li_tag, liab = instant(facts, "liabilities", day, last.end)
    same_date = liab is not None and equity is not None and liab.end == equity.end
    debt = f"{liab.val / equity.val:.1%}" if same_date and equity.val > 0 else "N/A"
    eq_label = "자본총계(비지배지분 포함)" if eq_tag == TAGS["equity"][0] else "자본총계(지배주주)"
    lines += ["", f"| 재무상태(최신 분기 끝 {last.end} 기말) | 값 | 태그 · 출처 |", "|---|---:|---|"]
    for label, tag, f in (("자산총계", as_tag, assets), ("부채총계", li_tag, liab), (eq_label, eq_tag, equity)):
        lines.append(f"| {label} | {usd(f.val if f else None)} | {f'{tag}, {f.end}, {f.src}' if f else '그 기말 값 없음'} |")
    lines.append(f"| 부채비율(부채총계 ÷ 자본총계, 같은 기말) | {debt} | |")
    def gap(item: str) -> str:
        return "N/A(태그 없음)" if picked[item][0] is None else "N/A(4분기 중 빈 분기)"

    ttm_text = {k: usd(v) if ttm_ok[k] else gap(k) for k, v in ttm.items() if k != "eps"}
    lines.append(f"- 최근 4분기 합계: 매출 {ttm_text['revenue']}, 영업이익 {ttm_text['operating']}, 순이익 {ttm_text['net']}")

    sh, why_no = shares_out(facts, day, last.end)
    eps_text = f"${ttm['eps']:,.2f}" if ttm_ok["eps"] else gap("eps")
    per = None
    if close is None or sh is None or not sh.val:
        lines.append(f"- EPS(희석, 최근 4분기 보고값 합): {eps_text}")
        lines.append("- 주당 지표 · PER · PBR: 계산 안 함(" + ("스냅샷 종가 없음" if close is None else why_no or "주식 수 0") + ")")
    else:
        cap = close * sh.val
        per = cap / ttm["net"] if ttm_ok["net"] and ttm["net"] > 0 else None
        pbr = cap / equity.val if equity and equity.val > 0 else None
        bps = equity.val / sh.val if equity else None
        per_text = (f"{per:.1f}배" if per is not None else gap("net") if not ttm_ok["net"]
                    else "N/A(최근 4분기 순이익 ≤ 0)")
        lines += ["", f"| 주당 지표 · 배수(스냅샷 종가 ${close:,.2f}) | 값 |", "|---|---:|",
                  f"| 보통주 유통주식수(dei:EntityCommonStockSharesOutstanding, {sh.end} 기준, {sh.src}) | {sh.val:,.0f}주 |",
                  f"| 시가총액(종가 × 유통주식수) | {usd(cap)} |",
                  f"| EPS(희석, 최근 4분기 보고값 합 — 4분기가 연간 − 9개월이면 근사) | {eps_text} |",
                  f"| BPS(자본총계 ÷ 유통주식수) | {'N/A' if bps is None else f'${bps:,.2f}'} |",
                  f"| PER(시가총액 ÷ 최근 4분기 순이익) | {per_text} |",
                  f"| PBR(시가총액 ÷ 자본총계) | {'N/A' if pbr is None else f'{pbr:.2f}배'} |"]

    own, insiders = recent_filings(client.submissions(cik, day), day, EDGAR["recent_days"])
    n = EDGAR["filings_max"]
    lines += ["", f"최근 {EDGAR['recent_days']}일 제출 공시(기준일 전날까지 {len(own)}건 중 최근 {min(n, len(own))}건, "
                  f"내부자 보고 Form 3 · 4 · 5 · 144 {insiders}건 제외):"]
    lines += [f"- {d} {form}" + (f" (items {items})" if items else "") + f" (accession {acc})"
              for d, form, acc, items in own[:n]] or ["- 없음"]
    summary = (f"SEC {last.src.split(' 제출 ')[0]}(분기 끝 {last.end}) 최근 4분기 매출 {ttm_text['revenue']}, "
               f"순이익 {ttm_text['net']}, 부채비율 {debt}" + ("" if per is None else f", PER {per:.1f}배")
               + f"; 최근 {EDGAR['recent_days']}일 공시 {len(own)}건")
    return "\n".join(lines), summary
