import pytest

from healops.interfaces import (
    ComponentRestarter,
    DriftProvider,
    HealthProvider,
    MetricsProvider,
    ModelReloader,
    ModelRollbacker,
    Redeployer,
    Retrainer,
)

ALL_CAPABILITIES = [
    HealthProvider,
    MetricsProvider,
    DriftProvider,
    ComponentRestarter,
    ModelReloader,
    ModelRollbacker,
    Retrainer,
    Redeployer,
]


class ApiAppAdapter:
    """An online API app: health, metrics, reload and rollback."""

    def get_health(self) -> bool:
        return True

    def get_metrics(self) -> dict[str, float]:
        return {"latency_ms": 120.0}

    def reload_model(self) -> str:
        return "model reloaded"

    def rollback_model(self) -> str:
        return "rolled back to v1"


class BatchAppAdapter:
    """A nightly batch model: no API, so only health and retraining."""

    def get_health(self) -> bool:
        return True

    def request_retraining(self) -> str:
        return "retraining started"


class NotAnAdapter:
    pass


def supported(adapter):
    return {cap for cap in ALL_CAPABILITIES if isinstance(adapter, cap)}


def test_there_are_eight_distinct_capabilities():
    assert len(set(ALL_CAPABILITIES)) == 8


def test_api_app_supports_exactly_what_it_implements():
    assert supported(ApiAppAdapter()) == {
        HealthProvider,
        MetricsProvider,
        ModelReloader,
        ModelRollbacker,
    }


def test_batch_app_is_not_forced_to_provide_api_metrics():
    adapter = BatchAppAdapter()
    assert supported(adapter) == {HealthProvider, Retrainer}
    assert not isinstance(adapter, MetricsProvider)


def test_object_with_no_methods_supports_nothing():
    assert supported(NotAnAdapter()) == set()


@pytest.mark.parametrize("capability", ALL_CAPABILITIES)
def test_every_capability_can_be_checked_at_runtime(capability):
    assert not isinstance(NotAnAdapter(), capability)


def test_retraining_and_redeployment_are_separate_capabilities():
    assert Retrainer is not Redeployer
    assert not isinstance(BatchAppAdapter(), Redeployer)