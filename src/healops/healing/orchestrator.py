"""The orchestrator: runs one incident through the recovery lifecycle.

It connects the pieces without deciding anything itself: the agent proposes,
the policy engine permits, people approve, and every move follows the incident
state machine. It covers diagnosis, the policy decision, parking for approval,
escalation, running the approved action (or only logging it in dry-run mode) and
verifying that the application really recovered. A failed recovery starts a new
round of diagnosis, up to a limit, and anything unclear goes to a person.

Every step is written to the audit log. Before a real action runs, the intent
is written first: if that cannot be recorded, the action does not run.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import cast

from healops.agents.base import DiagnosisAgent, DiagnosisContext, PreviousAttempt
from healops.audit import AuditLog, AuditStage, MemoryAuditLog, build_event
from healops.capabilities import ACTION_REQUIREMENTS
from healops.events import Incident, Observation, _now
from healops.healing.approval import ApprovalQueue, PendingApproval
from healops.healing.history import ActionHistory
from healops.healing.notify import Alert, AlertKind, NotificationResult, SafeNotifier
from healops.healing.policy import PolicyEngine
from healops.interfaces import ComponentRestarter
from healops.lifecycle import advance
from healops.outcomes import ActionResult, PolicyDecision, VerificationResult
from healops.records import ActionProposal, Diagnosis
from healops.redaction import redact_text
from healops.registry import ApplicationRegistry
from healops.types import ActionType, IncidentState, PolicyVerdict, VerificationOutcome
from healops.verification.base import NoVerifier, Verifier

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

MAX_MESSAGE_LENGTH = 200

# HealOps' own actions change nothing in the application. Each one hands the
# incident to a person, because the problem is still there.
_FRAMEWORK_REASONS: dict[ActionType, str] = {
    ActionType.ESCALATE: "the agent chose to escalate",
    ActionType.NO_ACTION: "the agent recommended no action, but the problem is not resolved",
    ActionType.RUN_DIAGNOSTIC: (
        "a diagnostic was requested, but HealOps cannot collect more evidence automatically yet"
    ),
}

_METHOD_NAMES: dict[ActionType, str] = {
    ActionType.RELOAD_MODEL: "reload_model",
    ActionType.ROLLBACK_MODEL: "rollback_model",
    ActionType.REQUEST_RETRAINING: "request_retraining",
    ActionType.REQUEST_REDEPLOYMENT: "request_redeployment",
}


class HandleStatus(StrEnum):
    READY = "ready_to_execute"
    PARKED = "awaiting_approval"
    EXECUTED = "executed"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


_STATE_FOR_STATUS = {
    HandleStatus.READY: IncidentState.APPROVED,
    HandleStatus.PARKED: IncidentState.AWAITING_APPROVAL,
    HandleStatus.EXECUTED: IncidentState.EXECUTED,
    HandleStatus.RESOLVED: IncidentState.RESOLVED,
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
    action_result: ActionResult | None = None
    verification: VerificationResult | None = None

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
        history: ActionHistory | None = None,
        dry_run: bool = True,
        clock: Clock = _now,
        verifier: Verifier | None = None,
        max_recovery_attempts: int = 3,
        on_resolved: Callable[[str], None] | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        if max_recovery_attempts < 1:
            raise ValueError("max_recovery_attempts must be at least 1")
        self._registry = registry
        self._agent = agent
        self._policy = policy
        self._approvals = approvals
        self._notifier = notifier
        # Use the policy engine's own history so retry limits and cooldowns see
        # every action that really ran.
        if history is None:
            history = policy.history if policy.history is not None else ActionHistory()
        self._history = history
        self._dry_run = dry_run
        self._clock = clock
        self._verifier = verifier if verifier is not None else NoVerifier()
        self._max_attempts = max_recovery_attempts
        self._on_resolved = on_resolved
        self._attempts: dict[str, list[PreviousAttempt]] = {}
        self._recent: dict[str, tuple[Observation, ...]] = {}
        self._executed: set[str] = set()
        self._verified: set[str] = set()
        self.audit: AuditLog = audit if audit is not None else MemoryAuditLog()
        self.audit_failures: list[str] = []

    def _audit(
        self,
        stage: AuditStage,
        incident: Incident,
        record: object | None = None,
        data: dict | None = None,
        note: str | None = None,
    ) -> bool:
        """Write an audit event. A failure is noted and logged but never stops recovery."""
        try:
            self._write_audit(stage, incident, record, data, note)
        except Exception as error:  # noqa: BLE001 - observing must not break healing
            message = f"audit write failed at {stage.value} ({type(error).__name__})"
            self.audit_failures.append(message)
            logger.warning(message)
            return False
        return True

    def _write_audit(
        self,
        stage: AuditStage,
        incident: Incident,
        record: object | None,
        data: dict | None,
        note: str | None,
    ) -> None:
        event = build_event(
            stage, incident.incident_id, incident.app_name, record=record, data=data, note=note
        )
        self.audit.append(event)

    def handle(
        self, incident: Incident, recent_observations: Sequence[Observation] = ()
    ) -> HandleResult:
        """Take a freshly detected incident as far as it can go without a person."""
        if incident.state is not IncidentState.OPEN:
            raise ValueError("handle() needs an incident in the open state")
        self._recent[incident.incident_id] = tuple(recent_observations)
        self._audit(AuditStage.INCIDENT_OPENED, incident, record=incident)
        return self._round(incident)

    def _round(self, incident: Incident) -> HandleResult:
        """One round of diagnosis and decision, from an open or just-verified incident."""
        application = self._registry.get(incident.app_name)
        available = application.capabilities.supported_actions()
        context = DiagnosisContext(
            incident=incident,
            available_actions=available,
            previous_attempts=tuple(self._attempts.get(incident.incident_id, ())),
            recent_observations=self._recent.get(incident.incident_id, ()),
        )

        try:
            agent_result = self._agent.diagnose(context)
        except Exception as error:  # noqa: BLE001 - a broken agent must not crash recovery
            reason = f"the diagnosis agent failed ({type(error).__name__})"
            return self._escalate(incident, reason)

        diagnosis, proposal = agent_result.diagnosis, agent_result.proposal
        incident = advance(incident, IncidentState.DIAGNOSED)
        self._audit(AuditStage.DIAGNOSED, incident, record=diagnosis)
        if proposal is None:
            return self._escalate(incident, "the agent proposed no action", diagnosis)

        incident = advance(incident, IncidentState.PROPOSED)
        self._audit(AuditStage.PROPOSED, incident, record=proposal)
        decision = self._policy.evaluate(
            proposal, diagnosis, available, app_name=incident.app_name
        )
        self._audit(AuditStage.POLICY_DECIDED, incident, record=decision)

        if decision.verdict is PolicyVerdict.DENY:
            incident = advance(incident, IncidentState.DENIED)
            return self._escalate(
                incident, "; ".join(decision.reasons), diagnosis, proposal, decision
            )

        if decision.verdict is PolicyVerdict.NEEDS_APPROVAL:
            incident = advance(incident, IncidentState.AWAITING_APPROVAL)
            pending = self._approvals.add(incident, proposal, decision)
            self._audit(
                AuditStage.APPROVAL_REQUESTED,
                incident,
                data={
                    "proposal_id": proposal.proposal_id,
                    "action": proposal.action_type.value,
                    "expires_at": pending.expires_at.isoformat(),
                },
            )
            alert = Alert(
                kind=AlertKind.APPROVAL_NEEDED,
                incident_id=incident.incident_id,
                app_name=incident.app_name,
                title=f"{proposal.action_type.value} needs your approval",
                details=(
                    f"incident: {incident.summary}",
                    f"diagnosis: {diagnosis.summary} (confidence {diagnosis.confidence:.2f})",
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

    def execute(self, ready: HandleResult) -> HandleResult:
        """Run the approved action, or only log it in dry-run mode.

        A result can be executed once. A failing callback is never retried
        automatically: the incident goes to a person.
        """
        if ready.status is not HandleStatus.READY or ready.proposal is None:
            raise ValueError("execute() needs a result that is ready to execute")
        if ready.proposal.proposal_id in self._executed:
            raise ValueError("this result has already been executed")
        self._executed.add(ready.proposal.proposal_id)

        proposal = ready.proposal
        action = proposal.action_type
        incident = advance(ready.incident, IncidentState.EXECUTING)
        context = (ready.diagnosis, proposal, ready.decision)

        if action in _FRAMEWORK_REASONS:
            return self._escalate(incident, _FRAMEWORK_REASONS[action], *context)

        if self._dry_run:
            result = self._action_result(
                proposal, True, False, f"dry-run: {action.value} was not run", self._clock()
            )
            reason = f"dry-run: {action.value} would have run (nothing was changed)"
            self._audit(AuditStage.ACTION_FINISHED, incident, record=result)
            return self._escalate(
                incident, reason, *context, action_result=result, dry_run=True
            )

        application = self._registry.get(incident.app_name)
        call, problem = self._prepare(application.adapter, proposal)
        if call is None:
            result = self._action_result(proposal, False, False, problem, self._clock())
            return self._escalate(incident, problem, *context, action_result=result)

        started = self._clock()
        try:
            # Write-ahead: the intent is recorded before anything changes.
            self._write_audit(
                AuditStage.ACTION_STARTING,
                incident,
                None,
                {
                    "proposal_id": proposal.proposal_id,
                    "action": action.value,
                    "params": proposal.params,
                    "dry_run": False,
                },
                None,
            )
        except Exception as error:  # noqa: BLE001 - no record means no action
            self.audit_failures.append(
                f"audit write failed at {AuditStage.ACTION_STARTING.value} "
                f"({type(error).__name__})"
            )
            reason = "the audit log could not be written, so the action was not run"
            result = self._action_result(proposal, False, False, reason, started)
            return self._escalate(incident, reason, *context, action_result=result)

        self._history.record(incident.app_name, incident.incident_id, action, started)
        try:
            returned = call()
        except Exception as error:  # noqa: BLE001 - a failing callback must not crash recovery
            message = f"{type(error).__name__}: {error}"
            result = self._action_result(proposal, False, False, message, started)
            reason = (
                f"{action.value} failed ({type(error).__name__}); "
                "it was not retried automatically"
            )
            self._audit(AuditStage.ACTION_FINISHED, incident, record=result)
            return self._escalate(incident, reason, *context, action_result=result)

        message = str(returned) if returned else f"{action.value} completed"
        result = self._action_result(proposal, False, True, message, started)
        incident = advance(incident, IncidentState.EXECUTED)
        self._audit(AuditStage.ACTION_FINISHED, incident, record=result)
        return HandleResult(
            incident,
            HandleStatus.EXECUTED,
            ready.diagnosis,
            proposal,
            ready.decision,
            reason=f"{action.value} ran",
            action_result=result,
        )

    def verify(self, executed: HandleResult) -> HandleResult:
        """Check whether the application really recovered after the action.

        Verified: the incident is resolved. Failed or partial: another round of
        diagnosis (the failed action counts as tried), up to the attempt limit.
        Anything else, or too many failures: a person takes over.
        """
        if (
            executed.status is not HandleStatus.EXECUTED
            or executed.action_result is None
            or executed.proposal is None
        ):
            raise ValueError("verify() needs an executed result")
        if executed.proposal.proposal_id in self._verified:
            raise ValueError("this result has already been verified")
        self._verified.add(executed.proposal.proposal_id)

        proposal = executed.proposal
        incident = advance(executed.incident, IncidentState.VERIFYING)
        application = self._registry.get(incident.app_name)
        context = (executed.diagnosis, proposal, executed.decision)
        try:
            verification = self._verifier.verify(application, incident, executed.action_result)
        except Exception as error:  # noqa: BLE001 - a broken verifier must not crash recovery
            reason = f"verification could not run ({type(error).__name__})"
            return self._escalate(
                incident, reason, *context, action_result=executed.action_result
            )

        outcome = verification.outcome
        self._audit(AuditStage.VERIFIED, incident, record=verification)
        if outcome is VerificationOutcome.VERIFIED:
            incident = advance(incident, IncidentState.RESOLVED)
            self._audit(AuditStage.RESOLVED, incident, note="recovery verified")
            if self._on_resolved is not None:
                self._on_resolved(incident.incident_id)
            return HandleResult(
                incident,
                HandleStatus.RESOLVED,
                executed.diagnosis,
                proposal,
                executed.decision,
                reason="recovery verified: " + "; ".join(verification.checks_passed),
                action_result=executed.action_result,
                verification=verification,
            )

        if outcome in (VerificationOutcome.FAILED, VerificationOutcome.PARTIAL):
            detail = "; ".join(verification.checks_failed) or outcome.value
            attempts = self._attempts.setdefault(incident.incident_id, [])
            attempts.append(
                PreviousAttempt(
                    proposal.action_type,
                    outcome,
                    redact_text(f"{proposal.action_type.value}: {detail}")[:MAX_MESSAGE_LENGTH],
                )
            )
            if len(attempts) < self._max_attempts:
                return replace(self._round(incident), verification=verification)
            reason = (
                f"recovery was not verified after {len(attempts)} attempt(s); "
                "the attempt limit was reached"
            )
        else:
            reason = f"verification was {outcome.value}; a person must check"
        return self._escalate(
            incident,
            reason,
            *context,
            action_result=executed.action_result,
            verification=verification,
        )

    def _prepare(
        self, adapter: object, proposal: ActionProposal
    ) -> tuple[Callable[[], object] | None, str]:
        """Return the call to make, or None and the reason it cannot be made."""
        action = proposal.action_type
        if not isinstance(adapter, ACTION_REQUIREMENTS[action]):
            return None, f"the application does not support {action.value}"
        if action is ActionType.RESTART_COMPONENT:
            component = proposal.params.get("component")
            if not component:
                return None, "restart_component needs a 'component' parameter"
            restarter = cast(ComponentRestarter, adapter)
            return (lambda: restarter.restart_component(component)), ""
        return getattr(adapter, _METHOD_NAMES[action]), ""

    def _action_result(
        self,
        proposal: ActionProposal,
        dry_run: bool,
        completed: bool,
        message: str,
        started: datetime,
    ) -> ActionResult:
        ended = max(self._clock(), started)
        clean = redact_text(message)[:MAX_MESSAGE_LENGTH] or "no message"
        return ActionResult(
            proposal_id=proposal.proposal_id,
            dry_run=dry_run,
            completed=completed,
            message=clean,
            started_at=started,
            ended_at=ended,
        )

    def approve(self, incident_id: str, proposal_id: str, approver: str) -> HandleResult:
        """A person approved the waiting action. Raises if it is unknown or expired."""
        resolution = self._approvals.approve(incident_id, proposal_id, approver)
        pending = resolution.pending
        incident = advance(pending.incident, IncidentState.APPROVED)
        self._audit(
            AuditStage.APPROVAL_RESOLVED,
            incident,
            data={"proposal_id": proposal_id, "approved": True, "approver": approver},
        )
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
        self._audit(
            AuditStage.APPROVAL_RESOLVED,
            resolution.pending.incident,
            data={
                "proposal_id": proposal_id,
                "approved": False,
                "approver": approver,
                "reason": reason,
            },
        )
        return self._escalate_pending(resolution.pending, why)

    def expire_overdue_approvals(self) -> list[HandleResult]:
        """Escalate every incident whose approval time ran out. Never approves anything."""
        results = []
        for resolution in self._approvals.expire_overdue():
            self._audit(
                AuditStage.APPROVAL_RESOLVED,
                resolution.pending.incident,
                data={
                    "proposal_id": resolution.pending.proposal.proposal_id,
                    "approved": False,
                    "approver": resolution.approver,
                    "reason": resolution.reason,
                },
            )
            results.append(
                self._escalate_pending(resolution.pending, resolution.reason or "timed out")
            )
        return results

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
        action_result: ActionResult | None = None,
        dry_run: bool = False,
        verification: VerificationResult | None = None,
    ) -> HandleResult:
        incident = advance(incident, IncidentState.ESCALATED)
        self._audit(AuditStage.ESCALATED, incident, note=reason)
        details = [f"incident: {incident.summary}", f"reason: {reason}"]
        if diagnosis is not None:
            details.append(f"diagnosis: {diagnosis.summary}")
        if proposal is not None:
            details.append(f"proposed action: {proposal.action_type.value}")
        alert = Alert(
            kind=AlertKind.ESCALATED,
            incident_id=incident.incident_id,
            app_name=incident.app_name,
            title=(
                ("[DRY-RUN] " if dry_run else "")
                + f"{incident.incident_type.value} on {incident.app_name} needs a person"
            ),
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
            action_result,
            verification,
        )