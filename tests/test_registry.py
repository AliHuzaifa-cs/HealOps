import pytest

from healops.exceptions import (
    ApplicationAlreadyRegisteredError,
    HealOpsError,
    UnknownApplicationError,
)
from healops.interfaces import HealthProvider, Retrainer
from healops.registry import ApplicationRegistry
from healops.types import ActionType


class BatchAppAdapter:
    def get_health(self) -> bool:
        return True

    def request_retraining(self) -> str:
        return "retraining started"


class BrokenAdapter:
    def get_helth(self) -> bool:  # typo
        return True


def test_register_returns_the_application_with_its_capabilities():
    registry = ApplicationRegistry()
    app = registry.register("batch-app", BatchAppAdapter())
    assert app.name == "batch-app"
    assert app.capabilities.has(HealthProvider)
    assert app.capabilities.has(Retrainer)
    assert ActionType.REQUEST_RETRAINING in app.capabilities.supported_actions()


def test_registered_application_can_be_found_by_name():
    registry = ApplicationRegistry()
    adapter = BatchAppAdapter()
    registry.register("batch-app", adapter)
    assert registry.get("batch-app").adapter is adapter
    assert "batch-app" in registry
    assert len(registry) == 1


def test_registering_the_same_name_twice_is_rejected():
    registry = ApplicationRegistry()
    registry.register("batch-app", BatchAppAdapter())
    with pytest.raises(ApplicationAlreadyRegisteredError):
        registry.register("batch-app", BatchAppAdapter())
    assert len(registry) == 1


def test_unknown_application_raises_a_clear_error():
    registry = ApplicationRegistry()
    with pytest.raises(UnknownApplicationError, match="missing-app"):
        registry.get("missing-app")


def test_broken_adapter_is_rejected_and_not_stored():
    registry = ApplicationRegistry()
    with pytest.raises(ValueError):
        registry.register("broken", BrokenAdapter())
    assert "broken" not in registry
    assert len(registry) == 0


def test_empty_name_is_rejected():
    registry = ApplicationRegistry()
    with pytest.raises(ValueError):
        registry.register("  ", BatchAppAdapter())


def test_names_are_sorted():
    registry = ApplicationRegistry()
    registry.register("zeta", BatchAppAdapter())
    registry.register("alpha", BatchAppAdapter())
    assert registry.names() == ("alpha", "zeta")


def test_registries_do_not_share_state():
    first, second = ApplicationRegistry(), ApplicationRegistry()
    first.register("batch-app", BatchAppAdapter())
    assert "batch-app" not in second


def test_registry_errors_share_one_base_class():
    assert issubclass(ApplicationAlreadyRegisteredError, HealOpsError)
    assert issubclass(UnknownApplicationError, HealOpsError)