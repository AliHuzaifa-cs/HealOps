"""Human approval: incidents wait here, without blocking anything else.

When the policy says NEEDS_APPROVAL, the incident is parked in an ApprovalQueue.
A person later calls `approve` or `reject`. If nobody answers in time,
`expire_overdue` hands the incident back so it can be escalated. Silence is
never treated as approval.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from healops.events import Incident, _now, _require_aware, _require_text
from healops.exceptions import (
    ApprovalError,
    ApprovalExpiredError,
    NoPendingApprovalError,
    StateStoreError,
)
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal
from healops.types import IncidentState, PolicyVerdict

log = logging.getLogger("healops.approval")

TIMEOUT_APPROVER = "system:timeout"

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """An action waiting for a person to say yes or no."""

    incident: Incident
    proposal: ActionProposal
    decision: PolicyDecision
    requested_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """How a pending approval ended: who decided, and why."""

    pending: PendingApproval
    approved: bool
    approver: str
    resolved_at: datetime = field(default_factory=_now)
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.approver, "approver")
        _require_aware(self.resolved_at, "resolved_at")


class PendingStore(Protocol):
    """Where waiting approvals are saved, so a restart does not lose them."""

    def save_pending(self, pending: PendingApproval) -> None: ...

    def delete_pending(self, incident_id: str) -> None: ...


class ApprovalQueue:
    """Parked incidents, keyed by incident id. Create one per HealOps agent."""

    def __init__(
        self, timeout_seconds: int, clock: Clock = _now, store: PendingStore | None = None
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self._timeout = timedelta(seconds=timeout_seconds)
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._store = store
        self._pending: dict[str, PendingApproval] = {}

    def add(
        self, incident: Incident, proposal: ActionProposal, decision: PolicyDecision
    ) -> PendingApproval:
        """Park an incident until a person decides."""
        if decision.verdict is not PolicyVerdict.NEEDS_APPROVAL:
            raise ApprovalError("only a NEEDS_APPROVAL decision can wait for approval")
        if decision.proposal_id != proposal.proposal_id:
            raise ApprovalError("the decision does not belong to this proposal")
        if proposal.incident_id != incident.incident_id:
            raise ApprovalError("the proposal does not belong to this incident")
        if incident.state is not IncidentState.AWAITING_APPROVAL:
            raise ApprovalError("the incident must be in the awaiting_approval state")
        if incident.incident_id in self._pending:
            raise ApprovalError("this incident is already waiting for approval")

        now = self._clock()
        pending = PendingApproval(incident, proposal, decision, now, now + self._timeout)
        self._pending[incident.incident_id] = pending
        if self._store is not None:
            try:
                self._store.save_pending(pending)
            except StateStoreError:
                # Still waiting in memory; it just would not survive a restart.
                log.warning("could not save a waiting approval; it will not survive a restart")
        return pending

    def restore(self, pending: PendingApproval) -> None:
        """Put back an approval that was saved before a restart (it keeps its own deadline)."""
        if pending.incident.state is not IncidentState.AWAITING_APPROVAL:
            raise ApprovalError("only an incident awaiting approval can be restored")
        if pending.incident.incident_id in self._pending:
            raise ApprovalError("this incident is already waiting for approval")
        self._pending[pending.incident.incident_id] = pending

    def _forget(self, incident_id: str) -> None:
        """Remove the saved copy first: if that fails, nothing is approved or rejected."""
        if self._store is not None:
            self._store.delete_pending(incident_id)

    def approve(self, incident_id: str, proposal_id: str, approver: str) -> ApprovalResolution:
        """Approve one specific proposal. Raises if it is unknown or has expired."""
        _require_text(approver, "approver")
        pending = self._find(incident_id, proposal_id)
        if self._clock() >= pending.expires_at:
            raise ApprovalExpiredError(
                f"the approval for incident {incident_id} expired at {pending.expires_at.isoformat()}"
            )
        self._forget(incident_id)
        del self._pending[incident_id]
        return ApprovalResolution(pending, True, approver, self._clock())

    def reject(
        self, incident_id: str, proposal_id: str, approver: str, reason: str | None = None
    ) -> ApprovalResolution:
        """Reject one specific proposal. Rejecting is always allowed, even after expiry."""
        _require_text(approver, "approver")
        pending = self._find(incident_id, proposal_id)
        self._forget(incident_id)
        del self._pending[incident_id]
        return ApprovalResolution(pending, False, approver, self._clock(), reason)

    def expire_overdue(self) -> list[ApprovalResolution]:
        """Remove every approval whose time is up. None of them is ever approved."""
        now = self._clock()
        overdue = [p for p in self._pending.values() if now >= p.expires_at]
        resolutions = []
        for pending in overdue:
            try:
                self._forget(pending.incident.incident_id)
            except StateStoreError:
                log.warning("could not clear a saved approval; will try again later")
                continue
            del self._pending[pending.incident.incident_id]
            resolutions.append(
                ApprovalResolution(
                    pending,
                    False,
                    TIMEOUT_APPROVER,
                    now,
                    f"approval timed out after {self._timeout_seconds} seconds",
                )
            )
        return resolutions

    def pending(self) -> tuple[PendingApproval, ...]:
        """Everything still waiting, oldest first."""
        return tuple(sorted(self._pending.values(), key=lambda p: p.requested_at))

    def get(self, incident_id: str) -> PendingApproval | None:
        return self._pending.get(incident_id)

    def __len__(self) -> int:
        return len(self._pending)

    def _find(self, incident_id: str, proposal_id: str) -> PendingApproval:
        pending = self._pending.get(incident_id)
        if pending is None:
            raise NoPendingApprovalError(f"nothing is waiting for approval for incident {incident_id}")
        if pending.proposal.proposal_id != proposal_id:
            raise NoPendingApprovalError(
                f"incident {incident_id} is waiting for a different proposal"
            )
        return pending