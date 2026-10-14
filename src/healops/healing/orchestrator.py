"""The orchestrator: runs one incident through the recovery lifecycle.

It connects the pieces without deciding anything itself: the agent proposes,
the policy engine permits, people approve, and every move follows the incident
state machine. This version covers diagnosis, the policy decision, parking for
approval and escalation. Running the action and verifying it come next.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from healops.agents.base import DiagnosisAgent, DiagnosisContext, PreviousAttempt
from healops.events import Incident, Observation
from healops.healing.approval import ApprovalQueue, PendingApproval
from healops.healing.notify import Alert, AlertKind, NotificationResult, SafeNotifier
from healops.healing.policy import PolicyEngine
from healops.lifecycle import advance
from healops.outcomes import PolicyDecision
from healops.records import ActionProposal, Diagnosis
from healops.registry import ApplicationRegistry
from healops.types import IncidentState, PolicyVerdict


class HandleStatus(StrEnum):
    READY = "ready_to_execute"
    PARKED = "awaiting_approval"
    ESCALATED = "escalated"


_STATE_FOR_STATUS = {
    HandleStatus.READY: IncidentState.APPROVED,
    HandleStatus.PARKED: IncidentState.AWAITING_APPROVAL,
    HandleStatus.ESCALATED: IncidentState.ESCALATED,
}


@dataclass(frozen=True, slots=True)
class HandleResult:
    """Where an incident stands after the orchestrator touched it, and why."""

    incident: Incident
    status: HandleStatus
    diagnosis: Diagnosis | None = None
    proposal: ActionProposal | None = None
    decision: PolicyDecision | None = None
    notifications: tuple[NotificationResult, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        expected = _STATE_FOR_STATUS[self.status]
        if self.incident.state is not expected:
            raise ValueError(
                f"status {self.status.value} needs the incident in state {expected.value}, "
                f"not {self.incident.state.value}"
            )


class Orchestrator:
    """Handles incidents one at a time. Create one per HealOps agent."""

    def __init__(
        self,
        registry: ApplicationRegistry,
        agent: DiagnosisAgent,
        policy: PolicyEngine,
        approvals: ApprovalQueue,
        notifier: SafeNotifier,
    ) -> None:
        self._registry = registry
        self._agent = agent
        self._policy = policy
        self._approvals = approvals
        self._notifier = notifier
        self._attempts: dict[str, list[PreviousAttempt]] = {}

    def handle(
        self, incident: Incident, recent_observations: Sequence[Observation] = ()
    ) -> HandleResult:
        """Take a freshly detected incident as far as it can go without a person."""
        if incident.state is not IncidentState.OPEN:
            raise ValueError("handle() needs an incident in the open state")
        application = self._registry.get(incident.app_name)
        available = application.capabilities.supported_actions()
        context = DiagnosisContext(
            incident=incident,
            available_actions=available,
            previous_attempts=tuple(self._attempts.get(incident.incident_id, ())),
            recent_observations=tuple(recent_observations),
        )

        try:
            agent_result = self._agent.diagnose(context)
        except Exception as error:  # noqa: BLE001 - a broken agent must not crash recovery
            reason = f"the diagnosis agent failed ({type(error).__name__})"
            return self._escalate(incident, reason)

        diagnosis, proposal = agent_result.diagnosis, agent_result.proposal
        incident = advance(incident, IncidentState.DIAGNOSED)
        if proposal is None:
            return self._escalate(incident, "the agent proposed no action", diagnosis)

        incident = advance(incident, IncidentState.PROPOSED)
        decision = self._policy.evaluate(
            proposal, diagnosis, available, app_name=incident.app_name
        )

        if decision.verdict is PolicyVerdict.DENY:
            incident = advance(incident, IncidentState.DENIED)
            return self._escalate(
                incident, "; ".join(decision.reasons), diagnosis, proposal, decision
            )

        if decision.verdict is PolicyVerdict.NEEDS_APPROVAL:
            incident = advance(incident, IncidentState.AWAITING_APPROVAL)
            pending = self._approvals.add(incident, proposal, decision)
            alert = Alert(
                kind=AlertKind.APPROVAL_NEEDED,
                incident_id=incident.incident_id,
                app_name=incident.app_name,
                title=f"{proposal.action_type.value} needs your approval",
                details=(
                    f"incident: {incident.summary}",
                    f"proposal_id: {proposal.proposal_id}",
                    f"why approval: {'; '.join(decision.reasons)}",
                    f"approve before: {pending.expires_at.isoformat()}",
                ),
            )
            return HandleResult(
                incident,
                HandleStatus.PARKED,
                diagnosis,
                proposal,
                decision,
                self._notifier.send(alert),
            )

        incident = advance(incident, IncidentState.APPROVED)
        return HandleResult(incident, HandleStatus.READY, diagnosis, proposal, decision)

    def approve(self, incident_id: str, proposal_id: str, approver: str) -> HandleResult:
        """A person approved the waiting action. Raises if it is unknown or expired."""
        resolution = self._approvals.approve(incident_id, proposal_id, approver)
        pending = resolution.pending
        incident = advance(pending.incident, IncidentState.APPROVED)
        return HandleResult(
            incident,
            HandleStatus.READY,
            proposal=pending.proposal,
            decision=pending.decision,
            reason=f"approved by {approver}",
        )

    def reject(
        self, incident_id: str, proposal_id: str, approver: str, reason: str | None = None
    ) -> HandleResult:
        """A person refused the waiting action, so the incident is escalated."""
        resolution = self._approvals.reject(incident_id, proposal_id, approver, reason)
        why = f"rejected by {approver}" + (f": {reason}" if reason else "")
        return self._escalate_pending(resolution.pending, why)

    def expire_overdue_approvals(self) -> list[HandleResult]:
        """Escalate every incident whose approval time ran out. Never approves anything."""
        return [
            self._escalate_pending(resolution.pending, resolution.reason or "timed out")
            for resolution in self._approvals.expire_overdue()
        ]

    def _escalate_pending(self, pending: PendingApproval, reason: str) -> HandleResult:
        incident = advance(pending.incident, IncidentState.DENIED)
        return self._escalate(
            incident, reason, proposal=pending.proposal, decision=pending.decision
        )

    def _escalate(
        self,
        incident: Incident,
        reason: str,
        diagnosis: Diagnosis | None = None,
        proposal: ActionProposal | None = None,
        decision: PolicyDecision | None = None,
    ) -> HandleResult:
        incident = advance(incident, IncidentState.ESCALATED)
        details = [f"incident: {incident.summary}", f"reason: {reason}"]
        if proposal is not None:
            details.append(f"proposed action: {proposal.action_type.value}")
        alert = Alert(
            kind=AlertKind.ESCALATED,
            incident_id=incident.incident_id,
            app_name=incident.app_name,
            title=f"{incident.incident_type.value} on {incident.app_name} needs a person",
            details=tuple(details),
        )
        return HandleResult(
            incident,
            HandleStatus.ESCALATED,
            diagnosis,
            proposal,
            decision,
            self._notifier.send(alert),
            reason,
        )