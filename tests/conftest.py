import pytest


@pytest.fixture(autouse=True)
def no_live_independent_check(monkeypatch):
    """Tests that mock the planner's model must not also feed the checker; it reports 'unavailable'."""
    from agent import check

    monkeypatch.setattr(check, "independent_answer", lambda question: None)
