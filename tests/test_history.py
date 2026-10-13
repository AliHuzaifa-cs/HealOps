from datetime import UTC, datetime, timedelta

import pytest

from healops.healing.history import ActionHistory, ActionRecord
from healops.types import ActionType

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def test_empty_history_has_no_attempts_and_no_last_run():
    history = ActionHistory()
    assert history.attempts("inc-1", ActionType.RELOAD_MODEL) == 0
    assert history.last_executed("app", ActionType.RELOAD_MODEL) is None
    assert len(history) == 0


def test_attempts_are_counted_per_incident_and_action():
    history = ActionHistory()
    history.record("app", "inc-1", ActionType.RELOAD_MODEL, T0)
    history.record("app", "inc-1", ActionType.RELOAD_MODEL, T0 + timedelta(minutes=5))
    history.record("app", "inc-1", ActionType.ROLLBACK_MODEL, T0)
    history.record("app", "inc-2", ActionType.RELOAD_MODEL, T0)
    assert history.attempts("inc-1", ActionType.RELOAD_MODEL) == 2
    assert history.attempts("inc-1", ActionType.ROLLBACK_MODEL) == 1
    assert history.attempts("inc-2", ActionType.RELOAD_MODEL) == 1


def test_last_executed_is_the_latest_across_incidents():
    history = ActionHistory()
    history.record("app", "inc-1", ActionType.RELOAD_MODEL, T0)
    history.record("app", "inc-2", ActionType.RELOAD_MODEL, T0 + timedelta(hours=1))
    assert history.last_executed("app", ActionType.RELOAD_MODEL) == T0 + timedelta(hours=1)


def test_last_executed_is_separate_per_application():
    history = ActionHistory()
    history.record("app-a", "inc-1", ActionType.RELOAD_MODEL, T0)
    assert history.last_executed("app-b", ActionType.RELOAD_MODEL) is None


def test_record_validates_its_fields():
    with pytest.raises(ValueError):
        ActionRecord("", "inc-1", ActionType.RELOAD_MODEL, T0)
    with pytest.raises(ValueError):
        ActionRecord("app", "inc-1", ActionType.RELOAD_MODEL, datetime(2026, 10, 12))
    with pytest.raises(TypeError):
        ActionRecord("app", "inc-1", "reload_model", T0)