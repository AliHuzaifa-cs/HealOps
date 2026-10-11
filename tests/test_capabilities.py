import pytest

from healops.capabilities import (
    ACTION_REQUIREMENTS,
    FRAMEWORK_ACTIONS,
    Capabilities,
)
from healops.interfaces import (
    HealthProvider,
    MetricsProvider,
    ModelRollbacker,
    Retrainer,
)
from healops.types import ActionType


class ApiAppAdapter:
    def get_health(self) -> bool:
        return True

    def get_metrics(self) -> dict[str, float]:
        return {"latency_ms": 120.0}

    def reload_model(self) -> str:
        return "model reloaded"

    def rollback_model(self) -> str:
        return "rolled back"


class BatchAppAdapter:
    def get_health(self) -> bool:
        return True

    def request_retraining(self) -> str:
        return "retraining started"


class MisspelledAdapter:
    def get_helth(self) -> bool:  # typo: should be get_health
        return True


def test_every_action_type_is_covered_exactly_once():
    covered = set(ACTION_REQUIREMENTS) | set(FRAMEWORK_ACTIONS)
    assert covered == set(ActionType)
    assert not set(ACTION_REQUIREMENTS) & set(FRAMEWORK_ACTIONS)


def test_discover_finds_what_the_adapter_implements():
    caps = Capabilities.discover(ApiAppAdapter())
    assert caps.has(HealthProvider)
    assert caps.has(MetricsProvider)
    assert caps.has(ModelRollbacker)
    assert not caps.has(Retrainer)


def test_api_app_can_reload_and_rollback_but_not_retrain():
    actions = Capabilities.discover(ApiAppAdapter()).supported_actions()
    assert ActionType.RELOAD_MODEL in actions
    assert ActionType.ROLLBACK_MODEL in actions
    assert ActionType.REQUEST_RETRAINING not in actions


def test_batch_app_cannot_rollback_but_can_retrain():
    actions = Capabilities.discover(BatchAppAdapter()).supported_actions()
    assert ActionType.REQUEST_RETRAINING in actions
    assert ActionType.ROLLBACK_MODEL not in actions


def test_retraining_does_not_imply_redeployment():
    actions = Capabilities.discover(BatchAppAdapter()).supported_actions()
    assert ActionType.REQUEST_REDEPLOYMENT not in actions


def test_framework_actions_are_always_available():
    for adapter in (ApiAppAdapter(), BatchAppAdapter()):
        actions = Capabilities.discover(adapter).supported_actions()
        assert FRAMEWORK_ACTIONS <= actions


def test_adapter_with_no_capability_is_rejected():
    with pytest.raises(ValueError, match="no HealOps capability"):
        Capabilities.discover(object())


def test_misspelled_method_is_caught_instead_of_silently_ignored():
    with pytest.raises(ValueError, match="MisspelledAdapter"):
        Capabilities.discover(MisspelledAdapter())


def test_names_are_sorted_and_readable():
    assert Capabilities.discover(BatchAppAdapter()).names() == (
        "HealthProvider",
        "Retrainer",
    )


def test_unknown_capability_class_is_rejected():
    class Fake:
        pass

    with pytest.raises(ValueError):
        Capabilities(provided=frozenset({Fake}))