import pytest

from healops.events import Incident, Observation
from healops.exceptions import InvalidTransitionError
from healops.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    advance,
    can_transition,
)
from healops.types import IncidentState, IncidentType, ObservationKind, Severity

S = IncidentState


def make_incident(state=S.OPEN):
    obs = Observation(
        app_name="demo-app",
        kind=ObservationKind.METRIC,
        name="latency_ms",
        value=950.0,
    )
    return Incident(
        app_name="demo-app",
        incident_type=IncidentType.HIGH_LATENCY,
        severity=Severity.HIGH,
        summary="p95 latency above threshold",
        evidence=(obs,),
        state=state,
    )


def test_every_state_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(IncidentState)


def test_resolved_and_escalated_are_the_only_terminal_states():
    assert TERMINAL_STATES == {S.RESOLVED, S.ESCALATED}


def test_happy_path_reaches_resolved():
    incident = make_incident()
    for target in [
        S.DIAGNOSED,
        S.PROPOSED,
        S.APPROVED,
        S.EXECUTING,
        S.EXECUTED,
        S.VERIFYING,
        S.RESOLVED,
    ]:
        incident = advance(incident, target)
    assert incident.state is S.RESOLVED


def test_executed_cannot_jump_straight_to_resolved():
    assert not can_transition(S.EXECUTED, S.RESOLVED)
    with pytest.raises(InvalidTransitionError):
        advance(make_incident(S.EXECUTED), S.RESOLVED)


def test_action_cannot_run_without_approval():
    assert not can_transition(S.PROPOSED, S.EXECUTING)
    assert not can_transition(S.DENIED, S.EXECUTING)


def test_approval_path_and_rejection_path():
    assert can_transition(S.PROPOSED, S.AWAITING_APPROVAL)
    assert can_transition(S.AWAITING_APPROVAL, S.APPROVED)
    assert can_transition(S.AWAITING_APPROVAL, S.DENIED)


def test_failed_verification_can_retry_or_escalate():
    assert can_transition(S.VERIFYING, S.DIAGNOSED)
    assert can_transition(S.VERIFYING, S.ESCALATED)


@pytest.mark.parametrize("terminal", [S.RESOLVED, S.ESCALATED])
def test_terminal_states_cannot_be_left(terminal):
    for target in IncidentState:
        assert not can_transition(terminal, target)


def test_advance_returns_new_incident_and_keeps_original():
    original = make_incident()
    moved = advance(original, S.DIAGNOSED)
    assert original.state is S.OPEN
    assert moved.state is S.DIAGNOSED
    assert moved.incident_id == original.incident_id
    assert moved.evidence == original.evidence


def test_error_message_names_both_states():
    with pytest.raises(InvalidTransitionError, match="executed.*resolved"):
        advance(make_incident(S.EXECUTED), S.RESOLVED)