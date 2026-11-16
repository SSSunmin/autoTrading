"""The daily loop prices through ``execution.Prices(source="kis")`` (the executor's and the API's path, scripts/daily_loop
.py): a name with a KIS file reads exactly what the loop's own KIS reader read before (the provisional row of the day
included), and a name without one — Yahoo's adjusted prices, where a live run trades the real ones — is named in the
run's ``notes``, one line, never silently; no fallback, no line. Both stores; no network (tests/test_daily_loop.py's
fake KIS client)."""

import pandas as pd
import pytest

from agent_trading_core import kis
from agent_trading_core.audit import OrderRecord, market_audit
from tests.test_daily_loop import FakeManager, _market, at, dl, final

D1, D2 = "2026-08-04", "2026-08-05"
FALLBACK = "가격 Yahoo 폴백"


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, monkeypatch):
    monkeypatch.setenv("TRADING_STORE", request.param)
    return request.param


def _old_kis_read(path) -> pd.DataFrame:
    """What the loop's ``KisPrices.frame`` (removed) made of a KIS file: the reference for the same bytes."""
    raw = kis.read_cache(path)
    f = pd.DataFrame({"Open": raw["Open"].to_numpy(dtype=float), "Close": raw["Close"].to_numpy(dtype=float)},
                     index=raw["Date"].dt.strftime("%Y-%m-%d"))
    return f[~f.index.duplicated(keep="last")].sort_index()


def _decide(client, day, data):
    client.today = day
    return dl.run_stage("KRX", "decide", day, data, client, at(day, "15:40"))


@pytest.mark.unit
def test_a_kis_file_reads_as_before_with_the_days_provisional_row(store, tmp_path):
    client = _market()
    r = _decide(client, D1, tmp_path)                    # today's bar written as a provisional row
    assert r.status == "ok" and r.counts["provisional"] > 0
    cache = tmp_path / "cache"
    p = dl.Prices(cache, source="kis")
    names = ["^KS11", *dl.SETUPS["KRX"]["universe"]]
    for t in names:
        new, old = p.frame(t), _old_kis_read(kis.cache_path(t, cache))
        assert new.index.equals(old.index) and new["Open"].equals(old["Open"]) and new["Close"].equals(old["Close"]), t
    info = p.info("^KS11")
    assert info == {"source": "kis", "as_of": D1, "provisional": True}
    # the provisional close is the moving one, not the final bar (FakeKis moves today's Close by 1%)
    assert p.close_asof("^KS11", D1) == pytest.approx(final(client, "^KS11", D1) * client.moved_close)
    assert not [n for n in r.notes if n.startswith(FALLBACK)]                        # every name had a KIS file


@pytest.mark.unit
def test_a_held_name_without_a_kis_file_is_named_in_the_runs_notes(store, tmp_path):
    client = _market()
    assert _decide(client, D1, tmp_path).status == "ok"                             # open orders for D2's open
    audit = market_audit(tmp_path / "audit", "KRX")
    # a holding from before the KIS cache: only its Yahoo file (the fill stage refreshes open orders' names only)
    audit.record_order(OrderRecord(order_id="paper-2026-07-31-ZZZ.KS", ticker="ZZZ.KS", trade_date="2026-07-31",
                                   side="Buy", quantity=1, filled_quantity=1, filled_price=20_000.0,
                                   fill_date="2026-08-03", status="filled"))
    days = pd.bdate_range("2026-07-01", D2)
    pd.DataFrame({"Date": days.strftime("%Y-%m-%d"), "Open": 20_000.0, "Close": 20_000.0}).to_csv(
        tmp_path / "cache" / "ZZZ.KS-YFin-data.csv", index=False)
    client.today = D2
    r = dl.run_stage("KRX", "fill", D2, tmp_path, client, at(D2, "09:10"))
    assert r.status == "ok"
    assert [n for n in r.notes if n.startswith(FALLBACK)] == [
        f"{FALLBACK}(KIS 파일 없음): ZZZ.KS — {D2} 실행, Yahoo 마지막 행 {D2}"]
    run = [x for x in audit.runs() if (x.stage, x.trade_date) == ("fill", D2)][-1]
    assert run.notes == r.notes                                                       # the recorded line, not only returned


def _holding(tmp_path, kis_text=None):
    """``ZZZ.KS`` held from before the KIS cache: its Yahoo file only, or (``kis_text``) a KIS file with that text."""
    audit = market_audit(tmp_path / "audit", "KRX")
    audit.record_order(OrderRecord(order_id="paper-2026-07-31-ZZZ.KS", ticker="ZZZ.KS", trade_date="2026-07-31",
                                   side="Buy", quantity=1, filled_quantity=1, filled_price=20_000.0,
                                   fill_date="2026-08-03", status="filled"))
    days = pd.bdate_range("2026-07-01", D2)
    pd.DataFrame({"Date": days.strftime("%Y-%m-%d"), "Open": 20_000.0, "Close": 20_000.0}).to_csv(
        tmp_path / "cache" / "ZZZ.KS-YFin-data.csv", index=False)
    if kis_text is not None:
        kis.cache_path("ZZZ.KS", tmp_path / "cache").write_text(kis_text, encoding="utf-8")
    return audit


@pytest.mark.unit
def test_a_price_file_that_fails_the_run_fails_the_check_too_and_the_record_is_still_written(store, tmp_path):
    client = _market()
    assert _decide(client, D1, tmp_path).status == "ok"
    audit = _holding(tmp_path, kis_text="")                              # an empty KIS file: unreadable
    client.today = D2
    r = dl.run_stage("KRX", "fill", D2, tmp_path, client, at(D2, "09:10"))
    assert r.status == "failed" and "EmptyDataError" in r.error
    assert [n for n in r.notes if n.startswith("가격 폴백 확인 실패")] == [
        "가격 폴백 확인 실패: EmptyDataError: No columns to parse from file"]
    saved = [x for x in audit.runs() if (x.stage, x.trade_date) == ("fill", D2)]
    assert [(x.status, x.notes) for x in saved] == [("failed", r.notes)]


@pytest.mark.unit
def test_the_dry_run_says_a_fallback_on_stderr(store, tmp_path, capsys):
    client = _market()
    assert _decide(client, D1, tmp_path).status == "ok"                 # the day's screen, recorded
    _holding(tmp_path)
    capsys.readouterr()
    assert dl.dry_run("KRX", D1, tmp_path, llm=FakeManager()) == 0
    err = capsys.readouterr().err
    assert err.splitlines() == [f"{FALLBACK}(KIS 파일 없음): ZZZ.KS — {D1} 실행, Yahoo 마지막 행 {D2}"]   # its file runs to D2


@pytest.mark.unit
def test_the_dry_run_keeps_its_table_when_the_check_meets_an_unreadable_file(store, tmp_path, capsys):
    client = _market()
    assert _decide(client, D1, tmp_path).status == "ok"
    # a name of the book the model's call never reads (an order blocked, never held) with an empty KIS file: the
    # check after the paid calls meets it; a held one would stop agent_decide's replay before any call instead
    market_audit(tmp_path / "audit", "KRX").record_order(OrderRecord(
        order_id="paper-2026-07-31-ZZZ.KS", ticker="ZZZ.KS", trade_date="2026-07-31", side="Buy", quantity=0,
        status="blocked"))
    kis.cache_path("ZZZ.KS", tmp_path / "cache").write_text("", encoding="utf-8")
    capsys.readouterr()
    assert dl.dry_run("KRX", D1, tmp_path, llm=FakeManager()) == 0
    out = capsys.readouterr()
    assert out.err.splitlines() == ["가격 폴백 확인 실패: EmptyDataError: No columns to parse from file"]
    assert "| 종목 | 종류 | 등급 |" in out.out and "드라이런" in out.out


@pytest.mark.unit
def test_fallback_notes_lists_each_yahoo_name_once_and_nothing_else(tmp_path):
    for t, v in (("KIS.KS", "KIS"), ("YF.KS", "YFin"), ("BOTH.KS", "KIS"), ("BOTH.KS", "YFin")):
        pd.DataFrame({"Date": ["2026-08-03", "2026-08-04"], "Open": 1.0, "Close": 1.0}).to_csv(
            tmp_path / f"{t}-{v}-data.csv", index=False)
    p = dl.Prices(tmp_path, source="kis")
    assert dl.fallback_notes(p, ["KIS.KS", "YF.KS", "BOTH.KS", "NONE.KS", "YF.KS"], D1) == [
        f"{FALLBACK}(KIS 파일 없음): YF.KS — {D1} 실행, Yahoo 마지막 행 2026-08-04"]
    assert dl.fallback_notes(p, ["KIS.KS", "BOTH.KS", "NONE.KS"], D1) == []
