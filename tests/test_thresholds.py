import pytest

from healops.config import ThresholdsConfig
from healops.detection.base import Detector
from healops.detection.thresholds import (
    ThresholdDetector,
    ThresholdRule,
    build_threshold_detector,
)
from healops.events import Observation
from healops.types import IncidentType, ObservationKind, Severity


def obs(kind, name, value, app="demo-app"):
    return Observation(app_name=app, kind=kind, name=name, value=value)


def health(value, app="demo-app"):
    return obs(ObservationKind.HEALTH, "health", value, app)


def metric(name, value, app="demo-app"):
    return obs(ObservationKind.METRIC, name, value, app)


def drift(name, value, app="demo-app"):
    return obs(ObservationKind.DRIFT, name, value, app)


def detector(**overrides):
    values = {
        "latency_ms": 500,
        "error_rate": 0.05,
        "drift_score": 0.3,
        "consecutive_breaches": 3,
    }
    values.update(overrides)
    return build_threshold_detector(ThresholdsConfig(**values))


def feed(det, observations_per_check):
    found = []
    for batch in observations_per_check:
        found.extend(det.evaluate(batch))
    return found


def test_detector_follows_the_detector_protocol():
    det: Detector = detector()
    assert det.evaluate([]) == []


def test_health_failure_after_three_failed_checks():
    incidents = feed(detector(), [[health(False)]] * 3)
    assert len(incidents) == 1
    assert incidents[0].incident_type is IncidentType.HEALTH_FAILURE
    assert incidents[0].severity is Severity.CRITICAL
    assert len(incidents[0].evidence) == 3


def test_two_failed_checks_are_not_enough():
    assert feed(detector(), [[health(False)]] * 2) == []


def test_a_healthy_check_in_between_resets_the_count():
    checks = [[health(False)], [health(False)], [health(True)], [health(False)]]
    assert feed(detector(), checks) == []


def test_high_latency_after_three_slow_checks():
    incidents = feed(detector(), [[metric("latency_ms", v)] for v in (600, 700, 800)])
    assert len(incidents) == 1
    assert incidents[0].incident_type is IncidentType.HIGH_LATENCY
    assert "latency_ms" in incidents[0].summary
    assert "800" in incidents[0].summary


def test_a_value_exactly_at_the_limit_is_not_a_breach():
    assert feed(detector(), [[metric("latency_ms", 500)]] * 5) == []


def test_one_slow_spike_does_not_create_an_incident():
    checks = [[metric("latency_ms", v)] for v in (900, 100, 120, 110)]
    assert feed(detector(), checks) == []


def test_high_error_rate_is_detected_with_high_severity():
    incidents = feed(detector(), [[metric("error_rate", 0.2)]] * 3)
    assert incidents[0].incident_type is IncidentType.HIGH_ERROR_RATE
    assert incidents[0].severity is Severity.HIGH


def test_a_threshold_left_unset_is_not_monitored():
    det = detector(latency_ms=None)
    assert feed(det, [[metric("latency_ms", 99999)]] * 5) == []


def test_data_drift_is_detected_with_low_severity():
    incidents = feed(detector(), [[drift("age", 0.9)]] * 3)
    assert incidents[0].incident_type is IncidentType.DATA_DRIFT
    assert incidents[0].severity is Severity.LOW
    assert "age" in incidents[0].summary


def test_each_feature_is_tracked_separately():
    checks = [[drift("age", 0.9), drift("income", 0.1)]] * 3
    incidents = feed(detector(), checks)
    assert len(incidents) == 1
    assert incidents[0].evidence[0].name == "age"


def test_each_application_is_tracked_separately():
    checks = [[metric("latency_ms", 900, "app-a"), metric("latency_ms", 100, "app-b")]] * 3
    incidents = feed(detector(), checks)
    assert [i.app_name for i in incidents] == ["app-a"]


def test_a_continuing_problem_creates_only_one_incident():
    assert len(feed(detector(), [[health(False)]] * 10)) == 1


def test_release_lets_the_same_problem_be_detected_again():
    det = detector()
    first = feed(det, [[health(False)]] * 3)
    det.release(first[0].incident_id)
    second = feed(det, [[health(False)]] * 3)
    assert len(second) == 1
    assert second[0].incident_id != first[0].incident_id


def test_releasing_an_unknown_incident_is_harmless():
    detector().release("does-not-exist")


def test_evidence_holds_only_the_breaching_readings_that_triggered_it():
    det = detector(consecutive_breaches=2)
    incidents = feed(det, [[metric("latency_ms", v)] for v in (100, 900, 700)])
    assert [o.value for o in incidents[0].evidence] == [900, 700]


def test_boolean_values_in_metrics_are_not_treated_as_numbers():
    assert feed(detector(), [[metric("latency_ms", True)]] * 5) == []


def test_unrelated_observations_are_ignored():
    assert feed(detector(), [[metric("cpu_percent", 99.0)]] * 5) == []


def test_consecutive_breaches_of_one_fires_immediately():
    incidents = detector(consecutive_breaches=1).evaluate([health(False)])
    assert len(incidents) == 1


def test_rule_without_a_limit_is_rejected_unless_it_is_health():
    with pytest.raises(ValueError):
        ThresholdRule(
            kind=ObservationKind.METRIC,
            incident_type=IncidentType.HIGH_LATENCY,
            severity=Severity.LOW,
        )
    with pytest.raises(ValueError):
        ThresholdRule(
            kind=ObservationKind.HEALTH,
            incident_type=IncidentType.HEALTH_FAILURE,
            severity=Severity.LOW,
            limit=1.0,
        )


def test_detector_can_be_built_from_rules_directly():
    rule = ThresholdRule(
        kind=ObservationKind.METRIC,
        incident_type=IncidentType.PERFORMANCE_DEGRADATION,
        severity=Severity.MEDIUM,
        observation_name="mae",
        limit=10.0,
    )
    det = ThresholdDetector([rule], consecutive_breaches=1)
    assert len(det.evaluate([metric("mae", 12.0)])) == 1