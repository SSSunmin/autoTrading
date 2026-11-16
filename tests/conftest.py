import pytest


@pytest.fixture
def jsonl_store(monkeypatch):
    """A test about the JSON-lines files themselves (a line cut short, the files from before the market axis): the
    JSONL store whatever ``TRADING_STORE`` the run has. List it before the fixtures that open a book."""
    monkeypatch.setenv("TRADING_STORE", "jsonl")


@pytest.fixture(autouse=True)
def _no_holiday_calendar(tmp_path_factory, monkeypatch):
    """Every test starts with no holiday calendar (every weekday a session, the loop before the calendar): the fake
    markets' bars are business days, and the tracked lists under data/calendar/ would close some of them.
    tests/test_market_calendar.py reads the tracked lists, or a test calendar of its own."""
    monkeypatch.setenv("TRADING_CALENDAR_DIR", str(tmp_path_factory.mktemp("no_calendar")))


@pytest.fixture(autouse=True)
def _no_llm_trial(monkeypatch):
    """TRADING_LLM_TRIAL=1 in the shell would make the loop's default decider the model, and a test that passes no
    fake client would call it for real: every test starts with the trial off (tests/test_llm_trial.py turns it on)."""
    monkeypatch.delenv("TRADING_LLM_TRIAL", raising=False)


@pytest.fixture(autouse=True)
def _failed_runs(monkeypatch):
    """Every loop run recorded as failed in the test, with its error (flaky-sqlite: a loop test failed once on
    ``assert 'failed' == 'ok'`` and the run's reason was lost). Shown in the report of a test that fails."""
    from agent_trading_core.audit import AuditLog

    seen: list[str] = []
    real = AuditLog.record_run

    def record_run(self, run):
        if run.status == "failed":
            seen.append(f"{run.market} {run.stage} {run.trade_date}: {run.error}")
        return real(self, run)

    monkeypatch.setattr(AuditLog, "record_run", record_run)
    return seen


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    seen = getattr(item, "funcargs", {}).get("_failed_runs")
    if report.when == "call" and report.failed and seen:
        report.sections.append(("loop runs recorded as failed", "\n".join(seen)))
