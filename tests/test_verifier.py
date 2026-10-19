from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from healops.agents.mock import MockDiagnosisAgent
from healops.config import HealOpsConfig, VerificationConfig
from healops.events import Incident, Observation
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleStatus, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.outcomes import ActionResult
from healops.registry import ApplicationRegistry
from healops.types import (
    IncidentType,
    ObservationKind,
    Severity,
    VerificationOutcome,
)
from healops.verification.base import Verifier
from healops.verification.postconditions import PostconditionVerifier

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
V, F, P, I = (
    VerificationOutcome.VERIFIED,
    VerificationOutcome.FAILED,
    VerificationOutcome.PARTIAL,
    VerificationOutcome.INCONCLUSIVE,
)


class ScriptedApp:
    """Replies check by check. Each list is consumed one entry per check."""

    def __init__(self, health=(True, True, True), metrics=None, drift=None):
        self._health = list(health)
        self._metrics = list(metrics) if metrics is not None else None
        self._drift = list(drift) if drift is not None else None
        self.health_calls = 0
        self.restarts = 0

    def get_health(self) -> bool:
        self.health_calls += 1
        value = self._health.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def restart_component(self, component: str) -> str:
        self.restarts += 1
        return "restarted"


class MetricsApp(ScriptedApp):
    def get_metrics(self):
        value = self._metrics.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class DriftApp(ScriptedApp):
    def get_drift_scores(self):
        return self._drift.pop(0)


class OnlyHealth:
    def get_health(self) -> bool:
        return True


class Sleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def config(**extra):
    values = {
        "thresholds": {"latency_ms": 500, "error_rate": 0.05, "drift_score": 0.3},
        "metric_degradation": {"f1_score": {"baseline": 0.9, "max_change": 0.1}},
        "verification": {"settle_seconds": 5, "checks": 3, "interval_seconds": 10},
    }
    values.update(extra)
    return HealOpsConfig.model_validate(values)


def make_incident(incident_type, names=("health",), kind=ObservationKind.HEALTH):
    evidence = tuple(
        Observation(app_name="demo-app", kind=kind, name=name, value=False)
        for name in names
    )
    return Incident(
        app_name="demo-app",
        incident_type=incident_type,
        severity=Severity.HIGH,
        summary="something is wrong",
        evidence=evidence,
    )


ACTION = ActionResult(
    proposal_id="prop-1",
    dry_run=False,
    completed=True,
    message="restarted",
    started_at=T0,
    ended_at=T0,
)


def check(app, incident, cfg=None):
    registry = ApplicationRegistry()
    application = registry.register("demo-app", app)
    sleeper = Sleeper()
    verifier = PostconditionVerifier(cfg or config(), sleep=sleeper)
    result = verifier.verify(application, incident, ACTION)
    return result, sleeper


def latency_incident():
    return make_incident(
        IncidentType.HIGH_LATENCY, ("latency_ms",), ObservationKind.METRIC
    )


def test_verifier_follows_the_verifier_protocol():
    verifier: Verifier = PostconditionVerifier(config(), sleep=Sleeper())
    assert verifier is not None


def test_a_healthy_application_in_every_check_is_verified():
    app = ScriptedApp()
    result, sleeper = check(app, make_incident(IncidentType.HEALTH_FAILURE))
    assert result.outcome is V
    assert result.execution_id == ACTION.execution_id
    assert "health: ok in 3/3 checks" in result.checks_passed
    assert app.health_calls == 3
    assert sleeper.calls == [5, 10, 10]


def test_an_unhealthy_application_in_every_check_fails():
    result, _ = check(
        ScriptedApp(health=(False, False, False)), make_incident(IncidentType.HEALTH_FAILURE)
    )
    assert result.outcome is F
    assert any("health: breached in 3/3" in line for line in result.checks_failed)


def test_one_good_reading_is_not_enough():
    result, _ = check(
        ScriptedApp(health=(False, True, True)), make_incident(IncidentType.HEALTH_FAILURE)
    )
    assert result.outcome is P
    assert result.checks_passed
    assert result.checks_failed


def test_a_failing_health_probe_counts_as_unhealthy_without_crashing():
    boom = RuntimeError("probe died password=hunter2")
    result, _ = check(
        ScriptedApp(health=(boom, boom, boom)), make_incident(IncidentType.HEALTH_FAILURE)
    )
    assert result.outcome is F
    assert "hunter2" not in " ".join(result.checks_failed)


def test_good_latency_and_health_in_every_check_is_verified():
    app = MetricsApp(metrics=[{"latency_ms": 100.0}] * 3)
    result, _ = check(app, latency_incident())
    assert result.outcome is V
    assert "latency_ms: ok in 3/3 checks" in result.checks_passed


def test_latency_still_too_high_fails_and_shows_the_latest_value():
    app = MetricsApp(metrics=[{"latency_ms": 900.0}] * 3)
    result, _ = check(app, latency_incident())
    assert result.outcome is F
    assert any("latest 900" in line for line in result.checks_failed)


def test_latency_fixed_but_application_unhealthy_is_not_recovered():
    app = MetricsApp(health=(False, False, False), metrics=[{"latency_ms": 100.0}] * 3)
    result, _ = check(app, latency_incident())
    assert result.outcome is not V


def test_latency_exactly_at_the_limit_counts_as_recovered():
    app = MetricsApp(metrics=[{"latency_ms": 500.0}] * 3)
    assert check(app, latency_incident())[0].outcome is V


def test_a_metric_that_is_not_reported_is_inconclusive_never_verified():
    app = MetricsApp(metrics=[{"other": 1.0}] * 3)
    result, _ = check(app, latency_incident())
    assert result.outcome is I
    assert any("not observed" in line for line in result.checks_failed)


def test_an_application_without_metrics_cannot_prove_a_latency_recovery():
    result, _ = check(OnlyHealth(), latency_incident())
    assert result.outcome is I


def test_a_metrics_callback_that_fails_gives_no_proof():
    boom = RuntimeError("metrics down")
    app = MetricsApp(metrics=[boom, boom, boom])
    assert check(app, latency_incident())[0].outcome is I


def test_a_model_metric_back_inside_its_limit_is_verified():
    incident = make_incident(
        IncidentType.PERFORMANCE_DEGRADATION, ("f1_score",), ObservationKind.METRIC
    )
    good = MetricsApp(metrics=[{"f1_score": 0.88}] * 3)
    bad = MetricsApp(metrics=[{"f1_score": 0.60}] * 3)
    assert check(good, incident)[0].outcome is V
    assert check(bad, incident)[0].outcome is F


def test_labels_that_have_not_arrived_yet_are_inconclusive():
    incident = make_incident(
        IncidentType.PERFORMANCE_DEGRADATION, ("f1_score",), ObservationKind.METRIC
    )
    app = MetricsApp(metrics=[{"latency_ms": 100.0}] * 3)
    assert check(app, incident)[0].outcome is I


def test_drift_is_judged_only_for_the_features_in_the_incident():
    incident = make_incident(IncidentType.DATA_DRIFT, ("age",), ObservationKind.DRIFT)
    app = DriftApp(drift=[{"age": 0.1, "income": 0.9}] * 3)
    assert check(app, incident)[0].outcome is V
    still = DriftApp(drift=[{"age": 0.8, "income": 0.1}] * 3)
    assert check(still, incident)[0].outcome is F


def test_an_incident_type_without_a_recovery_check_is_inconclusive():
    incident = make_incident(IncidentType.DATA_QUALITY, ("nulls",), ObservationKind.METRIC)
    result, _ = check(ScriptedApp(), incident)
    assert result.outcome is I
    assert "no recovery check is defined for data_quality" in result.checks_failed[0]


def test_every_check_takes_a_fresh_reading():
    app = ScriptedApp(health=(False, True, True))
    check(app, make_incident(IncidentType.HEALTH_FAILURE))
    assert app.health_calls == 3


def test_the_limits_come_from_the_same_config_as_the_detector():
    app = MetricsApp(metrics=[{"latency_ms": 300.0}] * 3)
    strict = config(thresholds={"latency_ms": 200})
    assert check(app, latency_incident(), cfg=strict)[0].outcome is F


def test_default_verification_settings_fit_inside_the_cap():
    settings = VerificationConfig()
    window = settings.settle_seconds + (settings.checks - 1) * settings.interval_seconds
    assert window <= settings.max_window_seconds


def test_a_window_longer_than_the_cap_is_rejected():
    with pytest.raises(ValidationError, match="longer than max_window_seconds"):
        HealOpsConfig.model_validate(
            {"verification": {"checks": 3, "interval_seconds": 200, "max_window_seconds": 300}}
        )


@pytest.mark.parametrize(
    "bad",
    [{"checks": 0}, {"checks": 21}, {"interval_seconds": -1}, {"max_window_seconds": 0}],
)
def test_bad_verification_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"verification": bad})


def test_a_misspelled_verification_key_is_rejected():
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate({"verification": {"chekcs": 3}})


def build_orchestrator(app, cfg):
    registry = ApplicationRegistry()
    registry.register("demo-app", app)
    return Orchestrator(
        registry=registry,
        agent=MockDiagnosisAgent(),
        policy=PolicyEngine(cfg, history=ActionHistory(), clock=lambda: T0),
        approvals=ApprovalQueue(600, clock=lambda: T0),
        notifier=SafeNotifier([RecordingNotifier()]),
        dry_run=False,
        clock=lambda: T0,
        verifier=PostconditionVerifier(cfg, sleep=Sleeper()),
    )


def loop_config():
    return config(
        min_confidence_for_auto=0.3,
        policies={"restart_component": {"verdict": "allow", "cooldown_seconds": 0}},
    )


def test_the_whole_loop_resolves_an_incident_with_real_evidence():
    app = ScriptedApp(health=(True, True, True))
    orchestrator = build_orchestrator(app, loop_config())
    ready = orchestrator.handle(make_incident(IncidentType.HEALTH_FAILURE))
    result = orchestrator.verify(orchestrator.execute(ready))
    assert result.status is HandleStatus.RESOLVED
    assert app.restarts == 1


def test_the_whole_loop_does_not_resolve_when_the_application_stays_down():
    app = ScriptedApp(health=(False, False, False))
    orchestrator = build_orchestrator(app, loop_config())
    ready = orchestrator.handle(make_incident(IncidentType.HEALTH_FAILURE))
    result = orchestrator.verify(orchestrator.execute(ready))
    assert result.status is not HandleStatus.RESOLVED
    assert result.verification.outcome is F