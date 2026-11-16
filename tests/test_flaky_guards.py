"""What makes a test fail now and then under load, made certain (flaky-sqlite): a cache file another process holds
when the loop rewrites it (Windows refuses the move), and a failed test that says why the loop's run failed."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_trading_core import files, kis
from agent_trading_core.audit import OperationRecord, market_audit
from tests.test_daily_loop import _market, at, dl

ROOT = Path(__file__).resolve().parents[1]


def _held(monkeypatch, name):
    """``os.replace`` onto ``name`` refused as Windows refuses it while a scanner or an indexer holds the file."""
    real = os.replace

    def replace(src, dst):
        if Path(dst).name == name:
            raise PermissionError(5, "액세스가 거부되었습니다", str(src))
        return real(src, dst)

    monkeypatch.setattr(files.os, "replace", replace)
    return real


@pytest.mark.unit
def test_a_cache_file_held_by_another_process_fails_the_stage_with_its_reason_and_the_next_run_goes_through(
        tmp_path, monkeypatch, _failed_runs):
    """The refused-executor test's last run, with the index's cache file held: the run fails (never a stale 'ok'),
    says which file, and once it is let go the same stage runs. The failed test of that kind is
    ``assert 'failed' == 'ok'`` — the reason is in the run's error, which the report now shows."""
    audit = market_audit(tmp_path / "audit", "KRX")
    client = _market(today="2026-08-04")
    assert dl.run_stage("KRX", "decide", "2026-08-04", tmp_path, client, at("2026-08-04", "15:40")).status == "ok"
    audit.record_operation(OperationRecord(kind="halt", operator="admin", role="admin", reason="점검",
                                           target="manual", after={"keep_exits": False}, market="KRX"))
    audit.record_operation(OperationRecord(kind="resume", operator="admin", role="admin", reason="재개", market="KRX"))
    real = _held(monkeypatch, kis.cache_path("^KS11", tmp_path / "cache").name)
    r = dl.run_stage("KRX", "fill", "2026-08-04", tmp_path, client, at("2026-08-04", "16:00"))
    assert r.status == "failed" and not r.final
    assert "^KS11" in r.error and "KIS 캐시를 바꾸지 못했다" in r.error
    assert _failed_runs == [f"KRX fill 2026-08-04: {r.error}"]
    monkeypatch.setattr(files.os, "replace", real)
    again = dl.run_stage("KRX", "fill", "2026-08-04", tmp_path, client, at("2026-08-04", "16:05"))
    assert again.status == "ok", again.error


FAILING = '''
from agent_trading_core.audit import RunRecord, market_audit


def test_fails_after_a_failed_run(tmp_path):
    market_audit(tmp_path / "audit", "KRX").record_run(RunRecord(
        market="KRX", stage="fill", trade_date="2026-08-04", started_at="2026-08-04T07:00:00Z", status="failed",
        error="갱신 실패 ^KS11 OSError: 이전 파일 그대로"))
    assert "failed" == "ok"
'''


@pytest.mark.unit
def test_a_failed_test_reports_the_loop_runs_that_failed_in_it(tmp_path):
    """The conftest hook, through a real pytest run: the failure report carries every failed run's error."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text(FAILING, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "TRADING_STORE": os.environ.get("TRADING_STORE", "sqlite"),
           "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path),
                        str(tmp_path / "tests")], check=False, capture_output=True, text=True, encoding="utf-8",
                       env=env,
                       cwd=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "loop runs recorded as failed" in r.stdout
    assert "KRX fill 2026-08-04: 갱신 실패 ^KS11 OSError: 이전 파일 그대로" in r.stdout


def _refused(monkeypatch, times):
    """``os.replace`` refused ``times`` times (a scanner holding the file for a moment), then let through."""
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= times:
            raise PermissionError(5, "액세스가 거부되었습니다", str(src))
        return real(src, dst)

    monkeypatch.setattr(files.os, "replace", replace)
    monkeypatch.setattr(files, "REPLACE_WAIT", 0.0)
    return calls


@pytest.mark.unit
def test_a_file_held_for_a_moment_is_still_replaced(tmp_path, monkeypatch, caplog):
    """Under load the move was refused for a few ms (13 ms measured) and the loop's run failed on it: the move is
    tried again, so the new file is in place and nothing is kept back."""
    path = tmp_path / "A.KS-KIS-data.csv"
    path.write_text("old", encoding="utf-8")
    calls = _refused(monkeypatch, 3)                                  # three refusals, then the scanner lets go
    files.replace_file(path, lambda tmp: Path(tmp).write_text("new", encoding="utf-8"))
    assert path.read_text(encoding="utf-8") == "new" and len(calls) == 4
    assert "Kept the cached" not in caplog.text and not list(tmp_path.glob("*.tmp"))


@pytest.mark.unit
def test_a_file_held_past_the_tries_keeps_the_old_one_and_says_so(tmp_path, monkeypatch, caplog):
    path = tmp_path / "A.KS-KIS-data.csv"
    path.write_text("old", encoding="utf-8")
    calls = _refused(monkeypatch, 10**6)                              # never let go: tried a bounded number of times
    files.replace_file(path, lambda tmp: Path(tmp).write_text("new", encoding="utf-8"))
    assert path.read_text(encoding="utf-8") == "old" and len(calls) == files.REPLACE_TRIES == 10
    assert "Kept the cached A.KS-KIS-data.csv" in caplog.text and not list(tmp_path.glob("*.tmp"))
