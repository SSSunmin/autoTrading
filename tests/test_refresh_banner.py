"""The cache-refresh failure on the status bar and the settings (fix/refresh-banner): only symbols that still fail.
A failure in the refresh log is resolved — counted apart, the log left as it was — once the symbol's KIS file holds a
final row dated after that refresh's last final day (a later update: the daily loop's, which logs no line). A
provisional row, a missing file, a line without its last final day or not of KIS resolve nothing; each market is judged on its
own line."""
from __future__ import annotations

import json

import pytest

from tests.test_db_store_api import _client, _kis, store  # noqa: F401 (the fixture)

LFD = "2026-11-12"          # the failed run's last final day


def _line(market, failed, *, lfd=LFD, at="2026-11-13T21:30:15+09:00", source="kis"):
    return {"at": at, "source": source, "market": market, "last_final_day": lfd, "symbols": 504,
            "failed": [{"symbol": s, "error": f"NoMarketDataError: {s}"} for s in failed]}


def _data(tmp_path, log, files, provisional=None):
    data = tmp_path / "data"
    for bench in ("^KS11", "^GSPC"):
        _kis(data, bench, [(d, 1, 1) for d in ("2026-11-11", "2026-11-12")])
    for symbol, days in files.items():
        _kis(data, symbol, [(d, 1, 1) for d in days])
    if provisional is not None:
        (data / "cache" / "_provisional.json").write_text(json.dumps(provisional), encoding="utf-8")
    (data / "cache" / "_refresh_log.jsonl").write_text("".join(json.dumps(x) + "\n" for x in log), encoding="utf-8")
    return data


def _refresh(api, market):
    h = api.get("/api/health", params={"market": market}).json()["refresh"]
    assert api.get("/api/data/status").json()["refresh"][market] == h        # the bar and the settings: one answer
    return (h["failed_count"], h["failed_symbols"], h["resolved_count"], h["resolved_symbols"])


@pytest.mark.unit
def test_a_failure_the_cache_since_caught_up_on_leaves_the_bar_and_stays_counted(tmp_path, monkeypatch, store):  # noqa: F811
    """The trial's shape: BRK-B's file has the 11-13 final row and a provisional 11-16; BF-B's ends on the failed
    run's own last final day; ZTS has no file. Only BRK-B is resolved, and the log is byte for byte as it was."""
    data = _data(tmp_path, [_line("US", ["BRK-B", "BF-B", "ZTS"])],
                 {"BRK-B": ["2026-11-12", "2026-11-13", "2026-11-16"], "BF-B": ["2026-11-11", LFD]},
                 {"2026-11-16": ["BRK-B"]})
    before = (data / "cache" / "_refresh_log.jsonl").read_bytes()
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "US") == (2, ["BF-B", "ZTS"], 1, ["BRK-B"])
    h = api.get("/api/health", params={"market": "US"}).json()["refresh"]
    assert h["failed_error"] == "NoMarketDataError: BF-B"       # the error of one that still fails
    assert (data / "cache" / "_refresh_log.jsonl").read_bytes() == before


@pytest.mark.unit
def test_every_failure_caught_up_on_no_failure_left(tmp_path, monkeypatch, store):  # noqa: F811
    data = _data(tmp_path, [_line("US", ["BRK-B", "BF-B"])],
                 {"BRK-B": [LFD, "2026-11-13"], "BF-B": [LFD, "2026-11-16"]})
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "US") == (0, [], 2, ["BRK-B", "BF-B"])
    assert api.get("/api/health", params={"market": "US"}).json()["refresh"]["failed_error"] is None


@pytest.mark.unit
@pytest.mark.parametrize(("days", "provisional"), [
    ([LFD], None),                                       # not updated since: ends on the failed run's day
    (["2026-11-10"], None),                              # behind it
    ([LFD, "2026-11-13"], {"2026-11-13": ["BRK-B"]}),    # past it only by a provisional row
])
def test_no_false_resolution(tmp_path, monkeypatch, days, provisional):
    data = _data(tmp_path, [_line("US", ["BRK-B"])], {"BRK-B": days}, provisional)
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "US") == (1, ["BRK-B"], 0, [])


@pytest.mark.unit
def test_a_line_without_its_last_final_day_resolves_nothing(tmp_path, monkeypatch):
    data = _data(tmp_path, [_line("US", ["BRK-B"], lfd=None)], {"BRK-B": [LFD, "2026-11-16"]})
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "US") == (1, ["BRK-B"], 0, [])


@pytest.mark.unit
def test_a_line_not_of_kis_resolves_nothing(tmp_path, monkeypatch):
    """A Yahoo refresh's failure is not judged on the KIS file, however far it reaches (review 1R #2)."""
    data = _data(tmp_path, [_line("US", ["BRK-B"], source="yahoo")], {"BRK-B": [LFD, "2026-11-16"]})
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "US") == (1, ["BRK-B"], 0, [])


@pytest.mark.unit
def test_a_long_file_is_read_from_its_tail(tmp_path, monkeypatch):
    """Five years of rows (well past the tail read): the last final row still found, a provisional last one skipped."""
    days = [f"{y}-{m:02d}-{d:02d}" for y in range(2021, 2026) for m in range(1, 13) for d in (1, 15)]
    data = _data(tmp_path, [_line("US", ["BRK-B", "BF-B"])],
                 {"BRK-B": [*days, LFD, "2026-11-13", "2026-11-16"], "BF-B": [*days, LFD, "2026-11-16"]},
                 {"2026-11-16": ["BRK-B", "BF-B"]})
    assert (data / "cache" / "BRK-B-KIS-data.csv").stat().st_size > 2048
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "US") == (1, ["BF-B"], 1, ["BRK-B"])


@pytest.mark.unit
def test_each_market_is_judged_on_its_own_latest_line(tmp_path, monkeypatch):
    """KRX's caught-up file resolves KRX's failure only; US keeps its own; an older line of a market is not read."""
    log = [_line("US", ["005930.KS"], at="2026-11-13T21:00:00+09:00"),        # older US line: superseded
           _line("KRX", ["005930.KS"], at="2026-11-13T19:30:00+09:00"),
           _line("US", ["AAPL"], at="2026-11-13T21:30:00+09:00")]
    data = _data(tmp_path, log, {"005930.KS": [LFD, "2026-11-13"], "AAPL": [LFD]})
    api, _ = _client(data, monkeypatch)
    assert _refresh(api, "KRX") == (0, [], 1, ["005930.KS"])
    assert _refresh(api, "US") == (1, ["AAPL"], 0, [])
