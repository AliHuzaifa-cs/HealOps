from pathlib import Path

import pytest
from pydantic import ValidationError

from healops.config import HealOpsConfig, MetricBaseline, ThresholdsConfig
from healops.detection.thresholds import ThresholdRule, build_threshold_detector
from healops.events import Observation
from healops.loader import load_config
from healops.types import IncidentType, ObservationKind, Severity

EXAMPLE = Path(__file__).parent.parent / "examples" / "selfhealing.example.yaml"


def metric(name, value, app="demo-app"):
    return Observation(app_name=app, kind=ObservationKind.METRIC, name=name, value=value)


def health(value=True):
    return Observation(
        app_name="demo-app", kind=ObservationKind.HEALTH, name="health", value=value
    )


def detector(**metrics):
    return build_threshold_detector(
        ThresholdsConfig(consecutive_breaches=3),
        {name: MetricBaseline(**spec) for name, spec in metrics.items()},
    )


def feed(det, batches):
    found = []
    for batch in batches:
        found.extend(det.evaluate(batch))
    return found


F1 = {"f1_score": {"baseline": 100, "max_change": 20, "higher_is_better": True}}
MAE = {"mae": {"baseline": 10, "max_change": 3, "higher_is_better": False}}


def test_limit_is_below_the_baseline_when_higher_is_better():
    assert MetricBaseline(baseline=0.9, max_change=0.1).limit == pytest.approx(0.8)


def test_limit_is_above_the_baseline_when_lower_is_better():
    spec = MetricBaseline(baseline=12, max_change=3, higher_is_better=False)
    assert spec.limit == 15


def test_degradation_defaults_to_nothing_monitored():
    assert HealOpsConfig.model_validate({}).metric_degradation == {}


def test_degradation_is_read_from_plain_config_data():
    config = HealOpsConfig.model_validate(
        {"metric_degradation": {"f1_score": {"baseline": 0.9, "max_change": 0.1}}}
    )
    assert config.metric_degradation["f1_score"].higher_is_better is True


@pytest.mark.parametrize(
    "bad",
    [
        {"metric_degradation": {"f1": {"baseline": 0.9, "max_change": 0}}},
        {"metric_degradation": {"f1": {"baseline": 0.9, "max_change": -1}}},
        {"metric_degradation": {"f1": {"baseline": 0.9}}},
        {"metric_degradation": {"f1": {"max_change": 0.1}}},
        {"metric_degradation": {"f1": {"baseline": 0.9, "max_chnage": 0.1}}},
        {"metric_degradation": {"  ": {"baseline": 0.9, "max_change": 0.1}}},
    ],
)
def test_bad_degradation_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        HealOpsConfig.model_validate(bad)


def test_the_example_config_includes_a_degradation_baseline():
    config = load_config(EXAMPLE)
    assert config.metric_degradation["f1_score"].baseline == 0.90


def test_a_falling_f1_creates_a_performance_degradation_incident():
    checks = [[metric("f1_score", v)] for v in (70, 60, 50)]
    incidents = feed(detector(**F1), checks)
    assert len(incidents) == 1
    assert incidents[0].incident_type is IncidentType.PERFORMANCE_DEGRADATION
    assert incidents[0].severity is Severity.HIGH
    assert "below" in incidents[0].summary


def test_a_small_dip_within_the_allowed_change_is_not_an_incident():
    assert feed(detector(**F1), [[metric("f1_score", 85)]] * 5) == []


def test_a_value_exactly_at_the_limit_is_not_a_breach():
    assert feed(detector(**F1), [[metric("f1_score", 80)]] * 5) == []


def test_improvement_is_never_a_problem():
    assert feed(detector(**F1), [[metric("f1_score", 130)]] * 5) == []


def test_a_rising_mae_is_degradation_when_lower_is_better():
    incidents = feed(detector(**MAE), [[metric("mae", v)] for v in (14, 15, 16)])
    assert len(incidents) == 1
    assert "above" in incidents[0].summary


def test_a_falling_mae_is_an_improvement():
    assert feed(detector(**MAE), [[metric("mae", 2)]] * 5) == []


def test_missing_readings_neither_count_nor_reset_the_streak():
    checks = [
        [metric("f1_score", 70)],
        [metric("f1_score", 60)],
        [health()],
        [health()],
        [metric("f1_score", 50)],
    ]
    assert len(feed(detector(**F1), checks)) == 1


def test_silence_alone_never_creates_an_incident():
    assert feed(detector(**F1), [[health()]] * 10) == []


def test_several_metrics_are_tracked_independently():
    det = detector(**F1, **MAE)
    checks = [[metric("f1_score", 50), metric("mae", 10)]] * 3
    incidents = feed(det, checks)
    assert len(incidents) == 1
    assert incidents[0].evidence[0].name == "f1_score"


def test_below_cannot_be_used_on_a_health_rule():
    with pytest.raises(ValueError):
        ThresholdRule(
            kind=ObservationKind.HEALTH,
            incident_type=IncidentType.HEALTH_FAILURE,
            severity=Severity.LOW,
            below=True,
        )


def test_detector_without_degradation_settings_still_works():
    det = build_threshold_detector(ThresholdsConfig(latency_ms=500))
    assert feed(det, [[metric("f1_score", 0.1)]] * 5) == []