"""Allowed incident state transitions (the state machine from the design diagram)."""

from dataclasses import replace
from types import MappingProxyType

from healops.events import Incident
from healops.exceptions import InvalidTransitionError
from healops.types import IncidentState

S = IncidentState

ALLOWED_TRANSITIONS = MappingProxyType(
    {
        S.OPEN: frozenset({S.DIAGNOSED, S.ESCALATED}),
        S.DIAGNOSED: frozenset({S.PROPOSED, S.ESCALATED}),
        S.PROPOSED: frozenset({S.APPROVED, S.AWAITING_APPROVAL, S.DENIED}),
        S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.DENIED}),
        S.APPROVED: frozenset({S.EXECUTING}),
        S.DENIED: frozenset({S.ESCALATED}),
        S.EXECUTING: frozenset({S.EXECUTED, S.ESCALATED}),
        S.EXECUTED: frozenset({S.VERIFYING}),
        S.VERIFYING: frozenset({S.RESOLVED, S.DIAGNOSED, S.ESCALATED}),
        S.RESOLVED: frozenset(),
        S.ESCALATED: frozenset(),
    }
)

TERMINAL_STATES = frozenset(
    state for state, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def can_transition(current: IncidentState, target: IncidentState) -> bool:
    """Return True if an incident may move from `current` to `target`."""
    return target in ALLOWED_TRANSITIONS[current]


def advance(incident: Incident, target: IncidentState) -> Incident:
    """Return a new Incident in `target` state, or raise if the move is not allowed."""
    if not can_transition(incident.state, target):
        raise InvalidTransitionError(
            f"cannot move incident {incident.incident_id} "
            f"from {incident.state.value} to {target.value}"
        )
    return replace(incident, state=target)