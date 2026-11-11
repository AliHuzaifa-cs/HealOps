import pytest


@pytest.fixture(autouse=True)
def _no_token_from_the_environment(monkeypatch):
    """A token set in the shell that runs the tests must not change what the tests expect."""
    monkeypatch.delenv("HEALOPS_ACTION_TOKEN", raising=False)
