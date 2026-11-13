"""Point-in-time universe: membership by date, the simulator's candidate path, the delisted close, the KIND
name loader, and the changes-table parser behind data/universe/sp500_history.csv."""

import importlib.util
import os
from pathlib import Path

import pandas as pd
import pytest

from agent_trading_core import krx_names
from agent_trading_core.errors import NoMarketDataError, VendorUnavailableError
from agent_trading_core.risk import RiskConfig
from agent_trading_core.simulate import run_simulation
from agent_trading_core.universe import load_history, members_on, tickers
from tests.test_simulate import DATES, START, _cfg, _index, _paths

_spec = importlib.util.spec_from_file_location("build_universe", Path(__file__).resolve().parents[1] / "scripts" / "build_universe.py")
bu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bu)

HISTORY_CSV = """date,ticker,action,name
2024-01-02,A,start,Alpha
2024-01-02,B,start,Beta
2024-03-01,C,add,Gamma
2024-04-01,A,remove,Alpha
2024-05-01,B,remove,Beta
2024-05-01,D,add,Delta
"""


@pytest.fixture
def history(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text(HISTORY_CSV, encoding="utf-8")
    return load_history(p)


@pytest.mark.unit
def test_members_on_by_hand(history):
    assert members_on(history, "2023-12-29") == set()              # before the start rows
    assert members_on(history, "2024-01-02") == {"A", "B"}
    assert members_on(history, "2024-02-29") == {"A", "B"}         # the day before an add
    assert members_on(history, "2024-03-01") == {"A", "B", "C"}    # in effect on its own date
    assert members_on(history, "2024-04-01") == {"B", "C"}
    assert members_on(history, "2024-04-30") == {"B", "C"}
    assert members_on(history, "2024-05-01") == {"C", "D"}         # same-day swap
    assert tickers(history) == ["A", "B", "C", "D"]


@pytest.mark.unit
def test_rows_out_of_date_order_are_sorted_by_date(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text("date,ticker,action,name\n2024-05-01,A,remove,Alpha\n2024-01-02,A,start,Alpha\n"
                 "2024-05-01,B,add,Beta\n", encoding="utf-8")
    h = load_history(p)
    assert members_on(h, "2024-04-30") == {"A"}
    assert members_on(h, "2024-05-01") == {"B"}


@pytest.mark.unit
def test_unknown_action_is_rejected(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text("date,ticker,action,name\n2024-01-02,A,join,Alpha\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_history(p)


def _cut(frames, ticker, last):
    f = frames[ticker]
    frames[ticker] = f[f["Date"] <= pd.Timestamp(last)].reset_index(drop=True)
    return frames


@pytest.mark.unit
def test_a_held_name_whose_prices_stop_is_closed_at_its_last_close():
    frames, idx = _paths(), _index()
    # a trade from the uncut run, and its series ended two bars after the entry: up to then both runs are the same
    base = run_simulation(frames, idx, _cfg(label="base", risk=RiskConfig()))
    held = next(t for t in base.trades if t.holding_days >= 5)
    last = DATES[DATES.searchsorted(pd.Timestamp(held.entry_date)) + 2]
    frames = _cut(_paths(), held.ticker, last)
    r = run_simulation(frames, idx, _cfg(label="cut", risk=RiskConfig()))
    d = [t for t in r.trades if t.reason == "delisted"]
    assert len(d) == 1 and d[0].ticker == held.ticker
    first_missing = DATES[DATES.searchsorted(last) + 1]
    assert d[0].exit_date == first_missing.strftime("%Y-%m-%d")       # the first day without a bar
    last_close = float(frames[held.ticker]["Close"].iloc[-1])
    assert d[0].exit_price == pytest.approx(last_close * (1 - 0.0005))  # last close, less slippage
    assert any(e["kind"] == "exit" and e["text"].startswith(f"{held.ticker} delisted") for e in r.events)
    assert r.exits_by_reason()["delisted"] == 1
    # never bought again after its prices stopped
    assert not [t for t in r.trades if t.ticker == held.ticker and t.entry_date > last.strftime("%Y-%m-%d")]


RULE_EXITS = {"stop_loss", "trailing", "expired", "exit_signal", "force"}


@pytest.mark.unit
def test_a_name_that_leaves_the_index_is_held_until_a_rule_sells_it_and_not_bought_again():
    frames, idx = _paths(), _index()
    base = run_simulation(frames, idx, _cfg(label="base", risk=RiskConfig()))
    # a position open across the leave day, sold by a rule three or more bars later in the run where nothing leaves
    held = next(t for t in base.trades if t.holding_days >= 5 and t.reason in RULE_EXITS)
    leave = DATES[DATES.searchsorted(pd.Timestamp(held.entry_date)) + 2]
    every = set(frames)

    def universe_on(d):
        return every - {held.ticker} if d >= leave else every

    r = run_simulation(frames, idx, _cfg(label="u", risk=RiskConfig()), universe_on=universe_on)
    same = [t for t in r.trades if t.ticker == held.ticker and t.entry_date == held.entry_date]
    assert len(same) == 1
    # still held after it left: sold on the same day, for the same rule, as when it never left
    assert same[0].exit_date == held.exit_date and same[0].reason == held.reason
    assert same[0].exit_date > leave.strftime("%Y-%m-%d")
    assert not [t for t in r.trades if t.ticker == held.ticker and t.entry_date > leave.strftime("%Y-%m-%d")]
    assert "delisted" not in r.exits_by_reason()


@pytest.mark.unit
def test_a_name_is_not_bought_before_it_joins_the_index():
    frames, idx = _paths(), _index()
    base = run_simulation(frames, idx, _cfg(label="base", risk=RiskConfig()))
    # a name the uncut run bought late in the window, made a member only from a few bars before that buy
    late = max(base.trades, key=lambda t: t.entry_date)
    join = DATES[DATES.searchsorted(pd.Timestamp(late.entry_date)) - 3]
    every = set(frames)

    def universe_on(d):
        return every if d >= join else every - {late.ticker}

    r = run_simulation(frames, idx, _cfg(label="j", risk=RiskConfig()), universe_on=universe_on)
    entries = [t.entry_date for t in r.trades if t.ticker == late.ticker]
    assert entries                                                         # bought once it is a member
    assert min(entries) > join.strftime("%Y-%m-%d")                         # decided on or after the join close


@pytest.mark.unit
def test_universe_on_with_every_frame_matches_the_fixed_list_path():
    frames, idx = _paths(), _index()
    fixed = run_simulation(frames, idx, _cfg(label="f", risk=RiskConfig()))
    every = set(frames)
    pit = run_simulation(frames, idx, _cfg(label="f", risk=RiskConfig()), universe_on=lambda d: every)
    assert pit.equity.equals(fixed.equity)
    assert [(t.ticker, t.entry_date, t.exit_date) for t in pit.trades] == \
           [(t.ticker, t.entry_date, t.exit_date) for t in fixed.trades]
    # and the fixed path is still the number from before the universe axis (tests/test_markets.py literal)
    assert float(fixed.equity.iloc[-1]) == 96919818.66849957
    assert START == "2025-01-02"


@pytest.mark.unit
def test_display_name_prefers_names_ko_then_the_kind_list(tmp_path, monkeypatch):
    html = ("<table><tr><th>회사명</th><th>시장구분</th><th>종목코드</th></tr>"
            "<tr><td>가짜전자</td><td>유가</td><td>005930</td></tr>"
            "<tr><td>테스트바이오</td><td>코스닥</td><td>123</td></tr>"
            "<tr><td>무시</td><td>기타</td><td>999999</td></tr></table>")
    p = tmp_path / "kind_corplist_2026-11-13.xls"
    p.write_bytes(html.encode("cp949"))
    listed = krx_names.load_listed_names(p)
    assert listed == {"005930.KS": "가짜전자", "000123.KQ": "테스트바이오"}
    monkeypatch.setattr(krx_names, "_listed", listed)
    assert krx_names.display_name("005930.KS") == "삼성전자"       # NAMES_KO wins
    assert krx_names.display_name("000123.KQ") == "테스트바이오"    # then the KIND list
    assert krx_names.display_name("ZZZZ") is None
    assert krx_names.EXIT_REASON_KO["delisted"] == "상장폐지 청산"


@pytest.mark.unit
def test_changes_table_parser_expands_rowspans(tmp_path):
    html = """<table id="changes">
    <tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th rowspan="2">Reason</th></tr>
    <tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
    <tr><td rowspan="2">March 18, 2024</td><td>SMCI</td><td>Supermicro</td><td>WHR</td><td>Whirlpool</td><td>cap</td></tr>
    <tr><td>DECK</td><td>Deckers</td><td>ZION</td><td>Zions</td><td>cap</td></tr>
    <tr><td>July 8, 2024</td><td>BRK.B</td><td>Berkshire</td><td></td><td></td><td>x</td></tr>
    </table>"""
    p = tmp_path / "c.html"
    p.write_text(html, encoding="utf-8")
    import datetime as dt
    d1, d2 = dt.date(2024, 3, 18), dt.date(2024, 7, 8)
    assert bu.sp500_changes(p) == [(d1, "SMCI", "add", "Supermicro"), (d1, "WHR", "remove", "Whirlpool"),
                                   (d1, "DECK", "add", "Deckers"), (d1, "ZION", "remove", "Zions"),
                                   (d2, "BRK-B", "add", "Berkshire")]
    assert bu.sp500_changes(p, {"WHR": "WHRX"})[1] == (d1, "WHRX", "remove", "Whirlpool")


_pb_spec = importlib.util.spec_from_file_location("paper_backtest",
                                                  Path(__file__).resolve().parents[1] / "scripts" / "paper_backtest.py")
pb = importlib.util.module_from_spec(_pb_spec)
_pb_spec.loader.exec_module(pb)


def _cache(tmp_path, last, mtime=None):
    path = tmp_path / "X-YFin-data.csv"
    path.write_text(f"Date,Open,High,Low,Close,Volume\n2024-01-02,1,1,1,1,1\n{last},1,1,1,1,1\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.mark.unit
def test_load_frame_reads_a_name_that_stopped_trading_up_to_its_last_bar(tmp_path, monkeypatch):
    _cache(tmp_path, "2026-08-04")                               # written today: the download that just happened
    calls = []

    def fake(ticker, as_of, cache_dir):
        calls.append(as_of)
        if as_of == "2026-11-12":
            raise NoMarketDataError(ticker, ticker, "stale")
        return pd.DataFrame({"Date": [pd.Timestamp(as_of)]})

    monkeypatch.setattr(pb, "load_ohlcv", fake)
    frame, _, cut = pb.load_frame("X", "2026-11-12", str(tmp_path))
    assert cut == "2026-08-04" and calls == ["2026-11-12", "2026-08-04"]
    assert frame["Date"].iloc[-1] == pd.Timestamp("2026-08-04")


@pytest.mark.unit
@pytest.mark.parametrize("error, mtime", [
    (VendorUnavailableError("Yahoo Finance request failed: down"), None),   # outage: not a delisting
    (NoMarketDataError("X", "X", "stale"), 1_700_000_000),                 # stale cache from another day
])
def test_load_frame_does_not_turn_other_failures_into_a_delisting(tmp_path, monkeypatch, error, mtime):
    _cache(tmp_path, "2026-08-04", mtime)

    def fake(ticker, as_of, cache_dir):           # only the full-range read fails: a re-read would succeed
        if as_of == "2026-11-12":
            raise error
        return pd.DataFrame({"Date": [pd.Timestamp(as_of)]})

    monkeypatch.setattr(pb, "load_ohlcv", fake)
    with pytest.raises(type(error)):
        pb.load_frame("X", "2026-11-12", str(tmp_path))
