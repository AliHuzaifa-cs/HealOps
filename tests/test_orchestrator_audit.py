from datetime import UTC, datetime, timedelta

from healops.agents.base import AgentResult
from healops.agents.mock import MockDiagnosisAgent
from healops.audit import AuditStage, JsonlAuditLog, MemoryAuditLog, read_events
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.exceptions import AuditWriteError
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import AlertKind, RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleStatus, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.outcomes import VerificationResult
from healops.records import ActionProposal, Diagnosis
from healops.registry import ApplicationRegistry
from healops.types import (
    ActionType,
    IncidentType,
    ObservationKind,
    Severity,
    VerificationOutcome,
)

T0 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
S = AuditStage


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class FailingLog(MemoryAuditLog):
    """Fails for the given stages, or for every stage if none are given."""

    def __init__(self, stages=None, error=None):
        super().__init__()
        self._stages = set(stages) if stages else None
        self._error = error or AuditWriteError("disk full")

    def append(self, event):
        if self._stages is None or event.stage in self._stages:
            raise self._error
        super().append(event)


class SnoopingAdapter:
    """Records what the audit log held at the moment the action ran."""

    def __init__(self, log):
        self._log = log
        self.calls = []
        self.seen_when_called = None

    def get_health(self) -> bool:
        return True

    def restart_component(self, component: str) -> str:
        self.seen_when_called = [e.stage for e in getattr(self._log, "events", [])]
        self.calls.append(component)
        return "restarted"


class BothActions(SnoopingAdapter):
    def reload_model(self) -> str:
        self.calls.append("reload")
        return "reloaded"


class ScriptedVerifier:
    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)

    def verify(self, application, incident, result):
        outcome = self._outcomes.pop(0)
        passed = ("health ok",) if outcome is VerificationOutcome.VERIFIED else ()
        failed = ("health still failing",) if outcome is VerificationOutcome.FAILED else ()
        return VerificationResult(
            execution_id=result.execution_id,
            outcome=outcome,
            checks_passed=passed,
            checks_failed=failed,
        )


def incident():
    obs = Observation(
        app_name="demo-app", kind=ObservationKind.HEALTH, name="health", value=False
    )
    return Incident(
        app_name="demo-app",
        incident_type=IncidentType.HEALTH_FAILURE,
        severity=Severity.CRITICAL,
        summary="health check failed 3 times in a row",
        evidence=(obs,),
    )


def config(restart="allow"):
    return HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.3,
            "policies": {"restart_component": {"verdict": restart, "cooldown_seconds": 0}},
        }
    )


class ScriptedAgent:
    def __init__(self, params):
        self._params = params

    def diagnose(self, context):
        diagnosis = Diagnosis(
            incident_id=context.incident.incident_id,
            summary="scripted",
            confidence=0.9,
            probable_cause="a component is down",
            evidence_used=("health = False",),
        )
        proposal = ActionProposal(
            incident_id=diagnosis.incident_id,
            diagnosis_id=diagnosis.diagnosis_id,
            action_type=ActionType.RESTART_COMPONENT,
            rationale="scripted",
            expected_outcome="scripted",
            params=self._params,
        )
        return AgentResult(diagnosis, proposal)


def build(
    log=None, cfg=None, dry_run=False, verifier=None, agent=None, timeout=600, adapter=None
):
    log = log if log is not None else MemoryAuditLog()
    clock = FakeClock()
    adapter = adapter or SnoopingAdapter(log)
    registry = ApplicationRegistry()
    registry.register("demo-app", adapter)
    recorder = RecordingNotifier()
    orchestrator = Orchestrator(
        registry=registry,
        agent=agent or MockDiagnosisAgent(),
        policy=PolicyEngine(cfg or config(), history=ActionHistory(), clock=clock),
        approvals=ApprovalQueue(timeout, clock=clock),
        notifier=SafeNotifier([recorder]),
        dry_run=dry_run,
        clock=clock,
        verifier=verifier,
        audit=log,
    )
    return orchestrator, adapter, log, recorder, clock


def stages(log):
    return [event.stage for event in log.events]


def test_a_full_recovery_leaves_a_complete_ordered_trail():
    orchestrator, _, log, _, _ = build(verifier=ScriptedVerifier(VerificationOutcome.VERIFIED))
    ready = orchestrator.handle(incident())
    orchestrator.verify(orchestrator.execute(ready))
    assert stages(log) == [
        S.INCIDENT_OPENED,
        S.DIAGNOSED,
        S.PROPOSED,
        S.POLICY_DECIDED,
        S.ACTION_STARTING,
        S.ACTION_FINISHED,
        S.VERIFIED,
        S.RESOLVED,
    ]
    assert len({event.incident_id for event in log.events}) == 1


def test_the_intent_is_written_before_the_action_runs():
    orchestrator, adapter, _, _, _ = build()
    orchestrator.execute(orchestrator.handle(incident()))
    assert adapter.seen_when_called[-1] is S.ACTION_STARTING
    assert S.ACTION_FINISHED not in adapter.seen_when_called


def test_if_the_intent_cannot_be_recorded_the_action_does_not_run():
    log = FailingLog(stages=[S.ACTION_STARTING])
    orchestrator, adapter, _, recorder, _ = build(log=log)
    result = orchestrator.execute(orchestrator.handle(incident()))
    assert adapter.calls == []
    assert result.status is HandleStatus.ESCALATED
    assert "audit log could not be written" in result.reason
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED
    assert result.action_result.completed is False


def test_an_unrecorded_action_is_not_counted_in_the_history():
    log = FailingLog(stages=[S.ACTION_STARTING])
    orchestrator, _, _, _, _ = build(log=log)
    orchestrator.execute(orchestrator.handle(incident()))
    second = orchestrator.handle(incident())
    assert second.status is HandleStatus.READY  # no cooldown was started


def test_any_kind_of_audit_error_stops_the_action():
    log = FailingLog(stages=[S.ACTION_STARTING], error=RuntimeError("weird failure"))
    orchestrator, adapter, _, _, _ = build(log=log)
    result = orchestrator.execute(orchestrator.handle(incident()))
    assert adapter.calls == []
    assert result.status is HandleStatus.ESCALATED


def test_other_audit_failures_do_not_stop_recovery_but_are_noted():
    log = FailingLog(stages=[S.DIAGNOSED, S.PROPOSED, S.POLICY_DECIDED, S.ACTION_FINISHED])
    orchestrator, adapter, _, _, _ = build(log=log)
    result = orchestrator.execute(orchestrator.handle(incident()))
    assert result.status is HandleStatus.EXECUTED
    assert adapter.calls == ["application"]
    assert len(orchestrator.audit_failures) == 4
    assert "audit write failed at diagnosed" in orchestrator.audit_failures[0]


def test_when_nothing_can_be_recorded_nothing_real_runs_and_nothing_crashes():
    orchestrator, adapter, _, recorder, _ = build(log=FailingLog())
    result = orchestrator.execute(orchestrator.handle(incident()))
    assert adapter.calls == []
    assert result.status is HandleStatus.ESCALATED
    assert recorder.alerts


def test_dry_run_records_the_outcome_but_writes_no_intent():
    orchestrator, adapter, log, _, _ = build(dry_run=True)
    orchestrator.execute(orchestrator.handle(incident()))
    assert adapter.calls == []
    assert S.ACTION_STARTING not in stages(log)
    assert stages(log)[-2:] == [S.ACTION_FINISHED, S.ESCALATED]
    finished = next(e for e in log.events if e.stage is S.ACTION_FINISHED)
    assert finished.payload["dry_run"] is True


def test_an_approval_that_is_given_is_recorded_with_who_gave_it():
    orchestrator, _, log, _, _ = build(cfg=config(restart="needs_approval"))
    parked = orchestrator.handle(incident())
    assert S.APPROVAL_REQUESTED in stages(log)
    orchestrator.approve(parked.incident.incident_id, parked.proposal.proposal_id, "ali")
    resolved = next(e for e in log.events if e.stage is S.APPROVAL_RESOLVED)
    assert resolved.payload["approved"] is True
    assert resolved.payload["approver"] == "ali"


def test_a_rejection_is_recorded_with_the_reason_and_then_escalated():
    orchestrator, _, log, _, _ = build(cfg=config(restart="needs_approval"))
    parked = orchestrator.handle(incident())
    orchestrator.reject(
        parked.incident.incident_id, parked.proposal.proposal_id, "ali", "too risky"
    )
    resolved = next(e for e in log.events if e.stage is S.APPROVAL_RESOLVED)
    assert resolved.payload["approved"] is False
    assert resolved.payload["reason"] == "too risky"
    assert stages(log)[-1] is S.ESCALATED


def test_a_timeout_is_recorded_as_the_system_and_never_as_an_approval():
    orchestrator, _, log, _, clock = build(cfg=config(restart="needs_approval"), timeout=60)
    orchestrator.handle(incident())
    clock.advance(60)
    orchestrator.expire_overdue_approvals()
    resolved = next(e for e in log.events if e.stage is S.APPROVAL_RESOLVED)
    assert resolved.payload["approved"] is False
    assert resolved.payload["approver"] == "system:timeout"


def test_a_failed_attempt_and_the_retry_are_both_in_the_trail():
    verifier = ScriptedVerifier(VerificationOutcome.FAILED, VerificationOutcome.VERIFIED)
    cfg = HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.3,
            "policies": {
                "restart_component": {"verdict": "allow", "cooldown_seconds": 0},
                "reload_model": {"verdict": "allow", "cooldown_seconds": 0},
            },
        }
    )
    log = MemoryAuditLog()
    orchestrator, _, _, _, _ = build(
        log=log, cfg=cfg, verifier=verifier, adapter=BothActions(log)
    )
    second = orchestrator.verify(orchestrator.execute(orchestrator.handle(incident())))
    orchestrator.verify(orchestrator.execute(second))
    assert stages(log).count(S.VERIFIED) == 2
    assert stages(log).count(S.ACTION_STARTING) == 2
    assert stages(log)[-1] is S.RESOLVED


def test_secrets_in_action_parameters_never_reach_the_trail():
    agent = ScriptedAgent({"component": "api", "password": "hunter2"})
    orchestrator, _, log, _, _ = build(agent=agent)
    orchestrator.execute(orchestrator.handle(incident()))
    everything = " ".join(str(event.to_dict()) for event in log.events)
    assert "hunter2" not in everything


def test_the_trail_can_be_written_to_a_file_and_read_back_in_order(tmp_path):
    path = tmp_path / "audit" / "healops.jsonl"
    orchestrator, _, _, _, _ = build(
        log=JsonlAuditLog(path), verifier=ScriptedVerifier(VerificationOutcome.VERIFIED)
    )
    orchestrator.verify(orchestrator.execute(orchestrator.handle(incident())))
    events = read_events(path)
    trail = [e.stage for e in events]
    assert trail[0] is S.INCIDENT_OPENED
    assert trail[-1] is S.RESOLVED
    assert len(events) == len(path.read_text(encoding="utf-8").splitlines())


def test_an_orchestrator_without_an_audit_argument_still_keeps_an_in_memory_trail():
    registry = ApplicationRegistry()
    registry.register("demo-app", SnoopingAdapter(MemoryAuditLog()))
    orchestrator = Orchestrator(
        registry=registry,
        agent=MockDiagnosisAgent(),
        policy=PolicyEngine(config(), history=ActionHistory()),
        approvals=ApprovalQueue(600),
        notifier=SafeNotifier([]),
    )
    orchestrator.handle(incident())
    assert S.INCIDENT_OPENED in [e.stage for e in orchestrator.audit.events]