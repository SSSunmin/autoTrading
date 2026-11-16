"""Exchange holiday calendars from the exchanges' own published lists, into ``data/calendar/<market>-<year>.json``
(agent_trading_core.market_calendar reads them). Run once a year, when the next year's list is out (KRX publishes it
in December, NYSE years ahead); the scheduler never fetches.

Run:  .venv/Scripts/python scripts/fetch_calendar.py --market KRX --year 2027 [--out data/calendar]

- KRX: global.krx.co.kr "Market Holidays" (the page's own OTP + JSON request; open.krx.co.kr answers a script with
  403). KIS Developers 국내휴장일조회 (CTCA0903R) is not served to paper keys (``EGW02006 모의투자 TR 이 아닙니다``,
  2026-11-16) and this project holds paper keys only.
- US: nyse.com "Holidays & Trading Hours" — the holiday table (three years) and its early-close footnotes ("close
  early at 1:00 p.m. … on <dates>").

Writes the file only when it read at least one holiday, and over a file with more holidays only with ``--force``
(a corrected list that dropped a day — correct it before that day: a day already recorded ``skipped(holiday)`` is
final). A file's hand-written ``sessions`` (days whose hours move, from the exchange's notice) are kept. Exit 0
written, 1 the source failed or gave nothing, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 "
                    "Safari/537.36"}
KRX_BASE = "https://global.krx.co.kr"
KRX_PAGE = "/contents/GLB/05/0501/0501110000/GLB0501110000.jsp"
NYSE_PAGE = "https://www.nyse.com/markets/hours-calendars"
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], 1)}


def krx(year: int) -> dict:
    s = requests.Session()
    s.headers.update(UA)
    s.get(KRX_BASE + KRX_PAGE, timeout=20)
    otp = s.get(f"{KRX_BASE}/contents/COM/GenerateOTP.jspx", timeout=20,
                params={"bld": "GLB/05/0501/0501110000/glb0501110000_01", "name": "form"}).text
    r = s.post(f"{KRX_BASE}/contents/GLB/99/GLB99000001.jspx", headers={"Referer": KRX_BASE + KRX_PAGE}, timeout=20,
               data={"search_bas_yy": year, "gridTp": "KRX", "pagePath": KRX_PAGE, "code": otp})
    r.raise_for_status()
    rows = r.json().get("block1", [])
    return {"source": KRX_BASE + KRX_PAGE,
            "holidays": [{"date": x["calnd_dd"], "name": x.get("holdy_eng_nm") or ""} for x in rows
                         if x.get("calnd_dd", "").startswith(f"{year}-")],
            "early_close": {}}


def _day(text: str, year: int) -> str | None:
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})",
                  text)
    return date(year, MONTHS[m.group(1)], int(m.group(2))).isoformat() if m else None


def nyse(year: int) -> dict:
    r = requests.get(NYSE_PAGE, headers=UA, timeout=30)
    r.raise_for_status()
    texts = [html.unescape(t.encode().decode("unicode_escape", "ignore")).encode("latin-1", "ignore").decode(
        "utf-8", "ignore").strip() for t in re.findall(r'"text":"((?:[^"\\]|\\.)*)"', r.text)]
    i = texts.index("Holiday")
    years = [int(t) for t in texts[i + 1:i + 4]]
    if year not in years:
        raise ValueError(f"NYSE 표의 연도 {years}에 {year} 없음")
    col, holidays, k = years.index(year), [], i + 4
    while texts[k] != "Trading Hours":
        name, cells = texts[k], texts[k + 1:k + 4]
        if (d := _day(cells[col], year)) is not None:
            holidays.append({"date": d, "name": name.replace("’", "'")})
        k += 4
    flat = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", r.text)).split())
    early = {}
    for m in re.finditer(r"close early at (\d{1,2}):(\d{2}) p\.m\..{0,80}? on ((?:[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, "
                         r"\d{4}(?:, | and |, and )?)+)", flat):
        at = f"{int(m.group(1)) + 12:02d}:{m.group(2)}"
        for wd, mon, dd, yy in re.findall(r"([A-Z][a-z]+day), ([A-Z][a-z]+) (\d{1,2}), (\d{4})", m.group(3)):
            if int(yy) == year:
                early[date(year, MONTHS[mon], int(dd)).isoformat()] = at
    return {"source": NYSE_PAGE, "holidays": holidays, "early_close": dict(sorted(early.items()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", choices=["KRX", "US"], required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--out", default=str(ROOT / "data" / "calendar"))
    parser.add_argument("--force", action="store_true", help="기존 파일보다 휴장일이 적어도 덮어씀(정정 목록)")
    args = parser.parse_args(argv)
    try:
        got = (krx if args.market == "KRX" else nyse)(args.year)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"{args.market} {args.year}: 출처 실패 — {type(e).__name__}: {e}"[:300], file=sys.stderr)
        return 1
    if not got["holidays"]:
        print(f"{args.market} {args.year}: 휴장일 0건 — 아직 공표 전이거나 출처 형식이 바뀜, 파일 안 씀", file=sys.stderr)
        return 1
    path = Path(args.out) / f"{args.market}-{args.year}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if len(old.get("holidays", [])) > len(got["holidays"]) and not args.force:
        print(f"{path}: 기존 파일의 휴장일이 더 많음 — 덮어쓰지 않음(정정 목록이면 --force)", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"market": args.market, "year": args.year, "source": got["source"],
            "fetched": datetime.now().date().isoformat(), "holidays": got["holidays"],
            "early_close": got["early_close"], **({"sessions": old["sessions"]} if old.get("sessions") else {})}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{path}: 휴장 {len(got['holidays'])} · 조기 폐장 {len(got['early_close'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
