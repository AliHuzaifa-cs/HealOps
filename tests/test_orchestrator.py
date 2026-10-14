from datetime import UTC, datetime, timedelta

import pytest

from healops.agents.base import AgentResult, DiagnosisContext
from healops.agents.mock import MockDiagnosisAgent
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.exceptions import (
    ApprovalExpiredError,
    NoPendingApprovalError,
    UnknownApplicationError,
)
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import AlertKind, RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleResult, HandleStatus, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.records import Diagnosis
from healops.registry import ApplicationRegistry
from healops.types import (
    IncidentState,
    IncidentType,
    ObservationKind,
    Severity,
)

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class ApiAdapter:
    def get_health(self) -> bool:
        return True

    def restart_component(self, component: str) -> str:
        return "restarted"

    def reload_model(self) -> str:
        return "reloaded"

    def rollback_model(self) -> str:
        return "rolled back"


class BrokenNotifier:
    def notify(self, alert):
        raise RuntimeError("smtp down")


def observation():
    return Observation(
        app_name="demo-app", kind=ObservationKind.HEALTH, name="health", value=False
    )


def incident(incident_type=IncidentType.HEALTH_FAILURE, state=IncidentState.OPEN):
    return Incident(
        app_name="demo-app",
        incident_type=incident_type,
        severity=Severity.CRITICAL,
        summary="health check failed 3 times in a row",
        evidence=(observation(),),
        state=state,
    )


def build(config=None, agent=None, notifiers=None, clock=None, timeout=600):
    clock = clock or FakeClock()
    config = config or HealOpsConfig.model_validate({})
    registry = ApplicationRegistry()
    registry.register("demo-app", ApiAdapter())
    recorder = RecordingNotifier()
    orchestrator = Orchestrator(
        registry=registry,
        agent=agent or MockDiagnosisAgent(),
        policy=PolicyEngine(config, history=ActionHistory(), clock=clock),
        approvals=ApprovalQueue(timeout, clock=clock),
        notifier=SafeNotifier(notifiers if notifiers is not None else [recorder]),
    )
    return orchestrator, recorder, clock


def allow_restart(**extra):
    values = {
        "min_confidence_for_auto": 0.3,
        "policies": {"restart_component": {"verdict": "allow"}},
    }
    values.update(extra)
    return HealOpsConfig.model_validate(values)


def test_an_allowed_action_is_ready_to_execute_without_bothering_anyone():
    orchestrator, recorder, _ = build(allow_restart())
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.READY
    assert result.incident.state is IncidentState.APPROVED
    assert result.proposal.action_type.value == "restart_component"
    assert recorder.alerts == []


def test_an_unlisted_action_waits_for_approval_and_alerts_a_person():
    orchestrator, recorder, _ = build()
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.PARKED
    assert result.incident.state is IncidentState.AWAITING_APPROVAL
    assert len(recorder.alerts) == 1
    alert = recorder.alerts[0]
    assert alert.kind is AlertKind.APPROVAL_NEEDED
    assert result.proposal.proposal_id in " ".join(alert.details)
    assert any("approve before" in line for line in alert.details)


def test_low_confidence_turns_an_allowed_action_into_a_request_for_approval():
    config = HealOpsConfig.model_validate(
        {"policies": {"restart_component": {"verdict": "allow"}}}
    )
    orchestrator, _, _ = build(config)
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.PARKED
    assert any("confidence" in reason for reason in result.decision.reasons)


def test_a_denied_action_escalates_with_the_reasons_and_alerts_a_person():
    config = HealOpsConfig.model_validate(
        {"policies": {"restart_component": {"verdict": "deny"}}}
    )
    orchestrator, recorder, _ = build(config)
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.ESCALATED
    assert result.incident.state is IncidentState.ESCALATED
    assert "configuration denies restart_component" in result.reason
    assert recorder.alerts[0].kind is AlertKind.ESCALATED


def test_a_crashing_agent_escalates_instead_of_crashing_the_loop():
    class CrashingAgent:
        def diagnose(self, context):
            raise RuntimeError("model server unreachable password=hunter2")

    orchestrator, recorder, _ = build(agent=CrashingAgent())
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.ESCALATED
    assert "RuntimeError" in result.reason
    assert "hunter2" not in result.reason
    assert "hunter2" not in " ".join(recorder.alerts[0].details)


def test_an_agent_that_proposes_nothing_escalates():
    class SilentAgent:
        def diagnose(self, context):
            diagnosis = Diagnosis(
                incident_id=context.incident.incident_id,
                summary="no idea",
                confidence=0.1,
                missing_information=("raw error logs",),
            )
            return AgentResult(diagnosis, None)

    orchestrator, _, _ = build(agent=SilentAgent())
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.ESCALATED
    assert "no action" in result.reason
    assert result.diagnosis is not None


def test_only_open_incidents_can_be_handled():
    orchestrator, _, _ = build()
    with pytest.raises(ValueError, match="open"):
        orchestrator.handle(incident(state=IncidentState.DIAGNOSED))


def test_an_incident_for_an_unknown_application_is_an_error():
    orchestrator, _, _ = build()
    stranger = Incident(
        app_name="other-app",
        incident_type=IncidentType.HEALTH_FAILURE,
        severity=Severity.HIGH,
        summary="x",
        evidence=(
            Observation(
                app_name="other-app",
                kind=ObservationKind.HEALTH,
                name="health",
                value=False,
            ),
        ),
    )
    with pytest.raises(UnknownApplicationError):
        orchestrator.handle(stranger)


def test_the_agent_only_sees_what_the_application_can_do_and_the_context_given():
    seen = {}

    class SpyAgent:
        def diagnose(self, context: DiagnosisContext):
            seen["actions"] = context.available_actions
            seen["recent"] = context.recent_observations
            return MockDiagnosisAgent().diagnose(context)

    orchestrator, _, _ = build(agent=SpyAgent())
    recent = (observation(),) * 2
    orchestrator.handle(incident(), recent_observations=recent)
    assert "rollback_model" in {a.value for a in seen["actions"]}
    assert "request_retraining" not in {a.value for a in seen["actions"]}
    assert seen["recent"] == recent


def test_approving_makes_the_action_ready_and_records_who_approved():
    orchestrator, _, _ = build()
    parked = orchestrator.handle(incident())
    result = orchestrator.approve(
        parked.incident.incident_id, parked.proposal.proposal_id, "ali@example.com"
    )
    assert result.status is HandleStatus.READY
    assert result.incident.state is IncidentState.APPROVED
    assert "ali@example.com" in result.reason


def test_an_approval_cannot_be_reused_and_the_wrong_proposal_is_refused():
    orchestrator, _, _ = build()
    parked = orchestrator.handle(incident())
    incident_id = parked.incident.incident_id
    with pytest.raises(NoPendingApprovalError, match="different proposal"):
        orchestrator.approve(incident_id, "some-other-proposal", "ali")
    orchestrator.approve(incident_id, parked.proposal.proposal_id, "ali")
    with pytest.raises(NoPendingApprovalError, match="nothing is waiting"):
        orchestrator.approve(incident_id, parked.proposal.proposal_id, "ali")


def test_rejecting_escalates_and_alerts_with_the_reason():
    orchestrator, recorder, _ = build()
    parked = orchestrator.handle(incident())
    result = orchestrator.reject(
        parked.incident.incident_id, parked.proposal.proposal_id, "ali", "too risky tonight"
    )
    assert result.status is HandleStatus.ESCALATED
    assert "rejected by ali: too risky tonight" in result.reason
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED


def test_silence_escalates_after_the_deadline_and_never_approves():
    orchestrator, recorder, clock = build(timeout=600)
    orchestrator.handle(incident())
    assert orchestrator.expire_overdue_approvals() == []
    clock.advance(600)
    results = orchestrator.expire_overdue_approvals()
    assert [r.status for r in results] == [HandleStatus.ESCALATED]
    assert "timed out" in results[0].reason
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED


def test_approving_too_late_is_refused_and_the_incident_keeps_waiting():
    orchestrator, _, clock = build(timeout=600)
    parked = orchestrator.handle(incident())
    clock.advance(600)
    with pytest.raises(ApprovalExpiredError):
        orchestrator.approve(parked.incident.incident_id, parked.proposal.proposal_id, "ali")
    results = orchestrator.expire_overdue_approvals()
    assert results[0].status is HandleStatus.ESCALATED


def test_a_broken_notifier_does_not_stop_the_recovery_and_is_reported():
    orchestrator, _, _ = build(notifiers=[BrokenNotifier()])
    result = orchestrator.handle(incident())
    assert result.status is HandleStatus.PARKED
    assert result.notifications[0].delivered is False


def test_the_status_must_match_the_incident_state():
    with pytest.raises(ValueError, match="needs the incident in state"):
        HandleResult(incident(), HandleStatus.ESCALATED)