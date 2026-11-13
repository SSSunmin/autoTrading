import pytest


@pytest.fixture
def jsonl_store(monkeypatch):
    """A test about the JSON-lines files themselves (a line cut short, the files from before the market axis): the
    JSONL store whatever ``TRADING_STORE`` the run has. List it before the fixtures that open a book."""
    monkeypatch.setenv("TRADING_STORE", "jsonl")
