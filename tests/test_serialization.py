import json
from datetime import UTC, datetime

import pytest

from healops.events import Incident, Observation
from healops.exceptions import RecordError
from healops.outcomes import ActionResult, PolicyDecision, VerificationResult
from healops.records import ActionProposal, Diagnosis
from healops.serialization import (
    RECORD_TYPES,
    record_from_dict,
    record_from_json,
    record_to_dict,
    record_to_json,
)
from healops.types import (
    ActionType,
    IncidentType,
    ObservationKind,
    PolicyVerdict,
    Severity,
    VerificationOutcome,
)

T1 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 10, 12, 0, 5, tzinfo=UTC)


def observation():
    return Observation(
        app_name="demo-app",
        kind=ObservationKind.METRIC,
        name="latency_ms",
        value=950.0,
        timestamp=T1,
        details={"source": "probe-1"},
    )


def samples():
    obs = observation()
    return [
        obs,
        Observation(
            app_name="demo-app",
            kind=ObservationKind.HEALTH,
            name="alive",
            value=False,
            timestamp=T1,
        ),
        Incident(
            app_name="demo-app",
            incident_type=IncidentType.HIGH_LATENCY,
            severity=Severity.HIGH,
            summary="latency above threshold",
            evidence=(obs,),
            detected_at=T1,
        ),
        Diagnosis(
            incident_id="inc-1",
            summary="slow after reload",
            confidence=0.7,
            probable_cause="new model is slower",
            evidence_used=("latency_ms 950",),
            created_at=T1,
        ),
        ActionProposal(
            incident_id="inc-1",
            diagnosis_id="diag-1",
            action_type=ActionType.ROLLBACK_MODEL,
            rationale="regressed after deployment",
            expected_outcome="latency recovers",
            params={"target": "v1"},
            risks=("brief downtime",),
            created_at=T1,
        ),
        PolicyDecision(
            proposal_id="prop-1",
            verdict=PolicyVerdict.NEEDS_APPROVAL,
            reasons=("rollback needs approval",),
            decided_at=T1,
        ),
        ActionResult(
            proposal_id="prop-1",
            dry_run=True,
            completed=True,
            message="dry run only",
            started_at=T1,
            ended_at=T2,
        ),
        VerificationResult(
            execution_id="exec-1",
            outcome=VerificationOutcome.VERIFIED,
            checks_passed=("health ok",),
            verified_at=T2,
        ),
    ]


@pytest.mark.parametrize("record", samples(), ids=lambda r: type(r).__name__)
def test_every_record_survives_a_json_round_trip(record):
    text = record_to_json(record)
    assert record_from_json(type(record), text) == record


@pytest.mark.parametrize("record", samples(), ids=lambda r: type(r).__name__)
def test_output_is_plain_json_data(record):
    data = record_to_dict(record)
    assert json.loads(json.dumps(data)) == data


def test_enums_and_times_become_plain_text():
    data = record_to_dict(samples()[2])
    assert data["incident_type"] == "high_latency"
    assert data["state"] == "open"
    assert data["detected_at"].startswith("2026-10-10T12:00:00")
    assert data["evidence"][0]["name"] == "latency_ms"


def test_json_is_a_single_line():
    assert "\n" not in record_to_json(samples()[2])


def test_every_record_type_is_covered_by_the_samples():
    assert {type(r) for r in samples()} == set(RECORD_TYPES)


def test_times_come_back_timezone_aware():
    restored = record_from_json(Observation, record_to_json(observation()))
    assert restored.timestamp.tzinfo is not None


def test_unknown_action_in_incoming_data_is_rejected():
    data = record_to_dict(samples()[4])
    data["action_type"] = "delete_everything"
    with pytest.raises(RecordError):
        record_from_dict(ActionProposal, data)


def test_record_rules_still_apply_to_incoming_data():
    data = record_to_dict(samples()[2])
    data["evidence"] = []
    with pytest.raises(RecordError):
        record_from_dict(Incident, data)


def test_verified_without_checks_is_rejected_when_loading():
    data = record_to_dict(samples()[7])
    data["checks_passed"] = []
    with pytest.raises(RecordError):
        record_from_dict(VerificationResult, data)


def test_naive_time_in_incoming_data_is_rejected():
    data = record_to_dict(observation())
    data["timestamp"] = "2026-10-10T12:00:00"
    with pytest.raises(RecordError):
        record_from_dict(Observation, data)


def test_bad_json_and_non_objects_are_rejected():
    with pytest.raises(RecordError, match="invalid JSON"):
        record_from_json(Observation, "{not json")
    with pytest.raises(RecordError, match="object"):
        record_from_json(Observation, "[1, 2]")


def test_non_record_types_are_refused():
    with pytest.raises(RecordError):
        record_to_dict(object())
    with pytest.raises(RecordError):
        record_from_dict(dict, {})