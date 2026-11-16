import pytest


@pytest.fixture
def jsonl_store(monkeypatch):
    """A test about the JSON-lines files themselves (a line cut short, the files from before the market axis): the
    JSONL store whatever ``TRADING_STORE`` the run has. List it before the fixtures that open a book."""
    monkeypatch.setenv("TRADING_STORE", "jsonl")


@pytest.fixture(autouse=True)
def _no_llm_trial(monkeypatch):
    """TRADING_LLM_TRIAL=1 in the shell would make the loop's default decider the model, and a test that passes no
    fake client would call it for real: every test starts with the trial off (tests/test_llm_trial.py turns it on)."""
    monkeypatch.delenv("TRADING_LLM_TRIAL", raising=False)
