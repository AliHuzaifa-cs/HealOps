from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from healops.events import Incident, Observation
from healops.types import IncidentState, IncidentType, ObservationKind, Severity


def make_observation(**overrides):
    values = {
        "app_name": "demo-app",
        "kind": ObservationKind.METRIC,
        "name": "latency_ms",
        "value": 950.0,
    }
    values.update(overrides)
    return Observation(**values)


def make_incident(**overrides):
    values = {
        "app_name": "demo-app",
        "incident_type": IncidentType.HIGH_LATENCY,
        "severity": Severity.HIGH,
        "summary": "p95 latency above threshold",
        "evidence": (make_observation(),),
    }
    values.update(overrides)
    return Incident(**values)


def test_observation_gets_utc_timestamp_by_default():
    obs = make_observation()
    assert obs.timestamp.tzinfo is not None


def test_observation_is_immutable():
    obs = make_observation()
    with pytest.raises(FrozenInstanceError):
        obs.value = 1.0


def test_observation_rejects_empty_names():
    with pytest.raises(ValueError):
        make_observation(app_name="  ")
    with pytest.raises(ValueError):
        make_observation(name="")


def test_observation_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        make_observation(timestamp=datetime(2026, 10, 10, 12, 0))


def test_observation_accepts_explicit_utc_timestamp():
    ts = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert make_observation(timestamp=ts).timestamp == ts


def test_health_observation_can_be_boolean():
    obs = make_observation(kind=ObservationKind.HEALTH, name="alive", value=False)
    assert obs.value is False


def test_incident_starts_open_with_unique_id():
    first, second = make_incident(), make_incident()
    assert first.state is IncidentState.OPEN
    assert first.incident_id != second.incident_id


def test_incident_requires_evidence():
    with pytest.raises(ValueError):
        make_incident(evidence=())


def test_incident_rejects_empty_summary():
    with pytest.raises(ValueError):
        make_incident(summary="")


def test_incident_is_immutable():
    incident = make_incident()
    with pytest.raises(FrozenInstanceError):
        incident.state = IncidentState.RESOLVED