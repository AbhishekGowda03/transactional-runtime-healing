import pytest


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    """The model plug-in retries transient errors after a pause; tests must not wait."""
    monkeypatch.setenv("TXH_RETRY_DELAYS", "0,0,0")
    monkeypatch.setenv("TXH_IGNORE_DOTENV", "1")      # the developer's own .env must not leak in