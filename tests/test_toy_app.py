import pytest

from healops.capabilities import Capabilities
from healops.interfaces import (
    ComponentRestarter,
    DriftProvider,
    HealthProvider,
    MetricsProvider,
    ModelReloader,
    ModelRollbacker,
    Retrainer,
)
from healops.testing import COMPONENT, Fault, ToyApp
from healops.types import ActionType


def test_a_fresh_app_is_healthy_with_normal_numbers():
    app = ToyApp()
    assert app.get_health() is True
    assert app.get_metrics() == {"latency_ms": 100.0, "error_rate": 0.01, "f1_score": 0.90}
    assert app.get_drift_scores() == {"input_distribution": 0.05}


def test_it_implements_the_capabilities_it_claims():
    caps = Capabilities.discover(ToyApp())
    for capability in (
        HealthProvider,
        MetricsProvider,
        DriftProvider,
        ComponentRestarter,
        ModelReloader,
        ModelRollbacker,
        Retrainer,
    ):
        assert caps.has(capability)
    actions = caps.supported_actions()
    assert ActionType.RESTART_COMPONENT in actions
    assert ActionType.ROLLBACK_MODEL in actions


@pytest.mark.parametrize(
    ("fault", "check"),
    [
        (Fault.CRASH, lambda a: a.get_health() is False),
        (Fault.MODEL_CORRUPT, lambda a: a.get_health() is False),
        (Fault.SLOW, lambda a: a.get_metrics()["latency_ms"] == 900.0),
        (Fault.BAD_MODEL, lambda a: a.get_metrics()["error_rate"] == 0.20),
        (Fault.BAD_MODEL, lambda a: a.get_metrics()["f1_score"] == 0.62),
        (Fault.DRIFT, lambda a: a.get_drift_scores()["input_distribution"] == 0.80),
    ],
)
def test_each_fault_shows_up_in_the_right_reading(fault, check):
    app = ToyApp()
    app.inject(fault)
    assert check(app)


def test_a_fault_leaves_the_unrelated_readings_normal():
    app = ToyApp()
    app.inject(Fault.SLOW)
    assert app.get_health() is True
    assert app.get_metrics()["error_rate"] == 0.01
    assert app.get_drift_scores()["input_distribution"] == 0.05


@pytest.mark.parametrize(
    ("fault", "cure"),
    [
        (Fault.CRASH, lambda a: a.restart_component(COMPONENT)),
        (Fault.SLOW, lambda a: a.restart_component(COMPONENT)),
        (Fault.MODEL_CORRUPT, lambda a: a.reload_model()),
        (Fault.BAD_MODEL, lambda a: a.rollback_model()),
    ],
)
def test_the_matching_cure_fixes_the_fault(fault, cure):
    app = ToyApp()
    app.inject(fault)
    cure(app)
    assert app.faults == set()


@pytest.mark.parametrize(
    ("fault", "wrong_cure"),
    [
        (Fault.CRASH, lambda a: a.reload_model()),
        (Fault.CRASH, lambda a: a.rollback_model()),
        (Fault.SLOW, lambda a: a.reload_model()),
        (Fault.MODEL_CORRUPT, lambda a: a.restart_component(COMPONENT)),
        (Fault.BAD_MODEL, lambda a: a.restart_component(COMPONENT)),
        (Fault.BAD_MODEL, lambda a: a.reload_model()),
    ],
)
def test_a_wrong_cure_changes_nothing(fault, wrong_cure):
    app = ToyApp()
    app.inject(fault)
    wrong_cure(app)
    assert app.faults == {fault}


def test_drift_is_not_cured_by_anything_automatic():
    app = ToyApp()
    app.inject(Fault.DRIFT)
    app.restart_component(COMPONENT)
    app.reload_model()
    app.rollback_model()
    app.request_retraining()
    assert app.faults == {Fault.DRIFT}


def test_two_faults_need_two_different_cures():
    app = ToyApp()
    app.inject(Fault.CRASH)
    app.inject(Fault.BAD_MODEL)
    app.restart_component(COMPONENT)
    assert app.faults == {Fault.BAD_MODEL}
    app.rollback_model()
    assert app.faults == set()


def test_an_unknown_component_is_refused_and_not_logged():
    app = ToyApp()
    with pytest.raises(ValueError, match="unknown component"):
        app.restart_component("database")
    assert app.actions == []


def test_every_action_is_recorded_in_order():
    app = ToyApp()
    app.reload_model()
    app.restart_component(COMPONENT)
    assert app.actions == ["reload_model", f"restart_component:{COMPONENT}"]


def test_only_a_fault_can_be_injected_and_clear_all_works():
    app = ToyApp()
    with pytest.raises(TypeError):
        app.inject("crash")  # type: ignore[arg-type]
    app.inject(Fault.CRASH)
    app.inject(Fault.DRIFT)
    app.clear_all_faults()
    assert app.get_health() is True
    assert app.faults == set()