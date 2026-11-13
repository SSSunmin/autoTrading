"""KRX hand downloads (scripts/ingest_krx_constituents.py): KOSPI200 snapshots → membership history, the two KOSPI
index files → one ascending series, the yfinance check, and the snapshot names behind display_name. No network."""

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from agent_trading_core import krx_names
from agent_trading_core.universe import load_history, members_on

_spec = importlib.util.spec_from_file_location(
    "ingest_krx_constituents", Path(__file__).resolve().parents[1] / "scripts" / "ingest_krx_constituents.py")
ik = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ik)

SNAP_HEADER = "종목코드,종목명,종가,대비,등락률,상장시가총액"
INDEX_HEADER = "일자,종가,대비,등락률,시가,고가,저가,거래량,거래대금,상장시가총액"


def _write(path: Path, header: str, rows: list[str]) -> Path:
    # as KRX serves it: cp949, quoted fields, no trailing newline
    path.write_bytes("\r\n".join([header, *rows]).encode("cp949"))
    return path


def _snap(tmp_path, ymd, members):
    return _write(tmp_path / f"kospi200_{ymd}.csv", SNAP_HEADER,
                  [f'"{code}","{name}","1000","0","0.00","1.0"' for code, name in members])


@pytest.mark.unit
def test_snapshots_become_start_add_remove_dated_on_the_later_snapshot(tmp_path):
    a = _snap(tmp_path, "20240102", [("005930", "삼성전자"), ("003410", "쌍용C&E"), ("000660", "SK하이닉스")])
    b = _snap(tmp_path, "20240614", [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("042670", "HD현대인프라코어")])
    snaps = ik.read_snapshots([b, a])                       # file order does not matter
    assert [d for d, _ in snaps] == [date(2024, 1, 2), date(2024, 6, 14)]
    assert snaps[0][1]["003410.KS"] == "쌍용C&E"            # cp949 → str, no mojibake
    rows = ik.build_history(snaps, date(2024, 1, 2), date(2026, 11, 12))
    assert [(r["date"], r["ticker"], r["action"]) for r in rows] == [
        ("2024-01-02", "000660.KS", "start"), ("2024-01-02", "003410.KS", "start"), ("2024-01-02", "005930.KS", "start"),
        ("2024-06-14", "003410.KS", "remove"), ("2024-06-14", "042670.KS", "add")]
    out = tmp_path / "h.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8")
    h = load_history(out)
    # no look-ahead: the June change is not seen the day before its snapshot, only from that date on
    assert members_on(h, "2024-06-13") == {"000660.KS", "003410.KS", "005930.KS"}
    assert members_on(h, "2024-06-14") == {"000660.KS", "005930.KS", "042670.KS"}
    # the start snapshot is the last one on or before --start; one after --end is ignored
    assert ik.build_history(snaps, date(2024, 3, 1), date(2024, 6, 13))[-1]["action"] == "start"
    with pytest.raises(SystemExit):
        ik.build_history(snaps, date(2023, 12, 1), date(2026, 11, 12))


@pytest.mark.unit
def test_an_unexpected_header_stops_the_run(tmp_path):
    p = _write(tmp_path / "kospi200_20240102.csv", "종목코드,종목명,종가", ['"005930","삼성전자","1000"'])
    with pytest.raises(SystemExit, match="열이 다르다"):
        ik.read_snapshots([p])


def _day(ymd, close, open_=None):
    o = open_ if open_ is not None else close
    return f'"{ymd}","{close}","0","0.00","{o}","{close}","{close}","10.0","20.0","30.0"'


@pytest.mark.unit
def test_index_files_merge_ascending_with_the_overlap_kept_once(tmp_path):
    # KRX lists days newest first; the two downloads share 2025/12/30
    a = _write(tmp_path / "kospi_daily_2024-2025.csv", INDEX_HEADER,
               [_day("2025/12/30", "4214.17"), _day("2025/12/29", "4220.56"), _day("2024/01/02", "2669.81", "2645.47")])
    b = _write(tmp_path / "kospi_daily_2026.csv", INDEX_HEADER, [_day("2026/01/02", "4309.63"), _day("2025/12/30", "4214.17")])
    m = ik.merge_index([a, b])
    assert list(m["Date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2025-12-29", "2025-12-30", "2026-01-02"]
    assert list(m.columns) == ["Date", "Open", "High", "Low", "Close", "Volume", "TradingValue", "MarketCap"]
    assert m.loc[0, "Open"] == 2645.47 and m.loc[0, "Close"] == 2669.81
    # the same date with different values in the two files is not silently resolved
    c = _write(tmp_path / "kospi_daily_x.csv", INDEX_HEADER, [_day("2025/12/30", "4300.00")])
    with pytest.raises(SystemExit, match="겹치는 날짜"):
        ik.merge_index([a, c])


@pytest.mark.unit
def test_check_lists_every_date_and_the_percent_gap():
    official = pd.DataFrame({"Date": pd.to_datetime(["2026-07-30", "2026-07-31"]), "Close": [5593.56, 6595.45]})
    yahoo = pd.Series([5593.56, 6661.40, 6000.0], index=pd.to_datetime(["2026-07-30", "2026-07-31", "2026-08-03"]))
    c = ik.check_against(official, yahoo).set_index("date")
    assert c.loc["2026-07-30", "diff_pct"] == pytest.approx(0.0)
    assert c.loc["2026-07-31", "diff_pct"] == pytest.approx((6661.40 / 6595.45 - 1) * 100)
    assert pd.isna(c.loc["2026-08-03", "krx"]) and pd.isna(c.loc["2026-08-03", "diff_pct"])   # only one side


@pytest.mark.unit
def test_snapshot_names_cover_members_the_kind_list_lacks(tmp_path, monkeypatch):
    h = tmp_path / "kospi200_history.csv"
    h.write_text("date,ticker,action,name\n2024-01-02,003410.KS,start,쌍용C&E\n2024-01-02,005930.KS,start,삼성전자(옛)\n"
                 "2024-06-14,003410.KS,remove,쌍용C&E\n", encoding="utf-8")
    names = krx_names.load_history_names(h)
    assert names == {"003410.KS": "쌍용C&E", "005930.KS": "삼성전자(옛)"}
    assert krx_names.load_history_names(tmp_path / "none.csv") == {}
    monkeypatch.setattr(krx_names, "_listed", {"000123.KS": "케이아이엔디"})
    monkeypatch.setattr(krx_names, "_history_names", names)
    assert krx_names.display_name("005930.KS") == "삼성전자"     # NAMES_KO first
    assert krx_names.display_name("000123.KS") == "케이아이엔디"   # then the KIND list
    assert krx_names.display_name("003410.KS") == "쌍용C&E"      # then the snapshot names (delisted)
    assert krx_names.display_name("999999.KS") is None
