from datetime import UTC, datetime

import pytest

from healops.agents.mock import MockDiagnosisAgent
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import AlertKind, RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleResult, HandleStatus, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.outcomes import VerificationResult
from healops.registry import ApplicationRegistry
from healops.types import (
    ActionType,
    IncidentState,
    IncidentType,
    ObservationKind,
    Severity,
    VerificationOutcome,
)
from healops.verification.base import NoVerifier, Verifier

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class Adapter:
    def __init__(self):
        self.calls = []

    def get_health(self) -> bool:
        return True

    def restart_component(self, component: str) -> str:
        self.calls.append("restart_component")
        return "restarted"

    def reload_model(self) -> str:
        self.calls.append("reload_model")
        return "reloaded"


class ScriptedVerifier:
    """Answers each verification with the next outcome in the list."""

    def __init__(self, *outcomes, detail="health still failing"):
        self._outcomes = list(outcomes)
        self._detail = detail
        self.seen = []

    def verify(self, application, incident, result):
        self.seen.append((incident.state, result.completed))
        outcome = self._outcomes.pop(0)
        passed = ("health ok",) if outcome in (
            VerificationOutcome.VERIFIED,
            VerificationOutcome.PARTIAL,
        ) else ()
        failed = (self._detail,) if outcome in (
            VerificationOutcome.FAILED,
            VerificationOutcome.PARTIAL,
        ) else ()
        return VerificationResult(
            execution_id=result.execution_id,
            outcome=outcome,
            checks_passed=passed,
            checks_failed=failed,
        )


class CrashingVerifier:
    def verify(self, application, incident, result):
        raise RuntimeError("probe crashed password=hunter2")


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


def config(reload_verdict="allow"):
    return HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.3,
            "policies": {
                "restart_component": {"verdict": "allow", "cooldown_seconds": 0},
                "reload_model": {"verdict": reload_verdict, "cooldown_seconds": 0},
            },
        }
    )


def build(verifier=None, cfg=None, max_attempts=3, resolved=None, agent=None):
    adapter = Adapter()
    registry = ApplicationRegistry()
    registry.register("demo-app", adapter)
    recorder = RecordingNotifier()
    cfg = cfg or config()
    orchestrator = Orchestrator(
        registry=registry,
        agent=agent or MockDiagnosisAgent(),
        policy=PolicyEngine(cfg, history=ActionHistory(), clock=lambda: T0),
        approvals=ApprovalQueue(600, clock=lambda: T0),
        notifier=SafeNotifier([recorder]),
        dry_run=False,
        clock=lambda: T0,
        verifier=verifier,
        max_recovery_attempts=max_attempts,
        on_resolved=resolved,
    )
    return orchestrator, adapter, recorder


def executed(orchestrator, inc=None):
    ready = orchestrator.handle(inc or incident())
    assert ready.status is HandleStatus.READY
    result = orchestrator.execute(ready)
    assert result.status is HandleStatus.EXECUTED
    return result


F, V, P = (
    VerificationOutcome.FAILED,
    VerificationOutcome.VERIFIED,
    VerificationOutcome.PARTIAL,
)


def test_a_verified_recovery_resolves_the_incident_and_releases_it():
    released = []
    verifier = ScriptedVerifier(V)
    orchestrator, _, recorder = build(verifier, resolved=released.append)
    done = executed(orchestrator)
    result = orchestrator.verify(done)
    assert result.status is HandleStatus.RESOLVED
    assert result.incident.state is IncidentState.RESOLVED
    assert released == [result.incident.incident_id]
    assert result.verification.outcome is V
    assert "health ok" in result.reason
    assert recorder.alerts == []


def test_the_verifier_sees_the_incident_while_it_is_being_verified():
    verifier = ScriptedVerifier(V)
    orchestrator, _, _ = build(verifier)
    orchestrator.verify(executed(orchestrator))
    assert verifier.seen == [(IncidentState.VERIFYING, True)]


def test_a_failed_recovery_starts_a_new_round_with_a_different_action():
    verifier = ScriptedVerifier(F, V)
    orchestrator, adapter, _ = build(verifier)
    second = orchestrator.verify(executed(orchestrator))
    assert second.status is HandleStatus.READY
    assert second.proposal.action_type is ActionType.RELOAD_MODEL
    assert second.verification.outcome is F
    final = orchestrator.verify(orchestrator.execute(second))
    assert final.status is HandleStatus.RESOLVED
    assert adapter.calls == ["restart_component", "reload_model"]


def test_the_agent_is_told_what_already_failed():
    seen = []

    class SpyAgent(MockDiagnosisAgent):
        def diagnose(self, context):
            seen.append(context.previous_attempts)
            return super().diagnose(context)

    verifier = ScriptedVerifier(F)
    orchestrator, _, _ = build(verifier, agent=SpyAgent())
    orchestrator.verify(executed(orchestrator))
    assert seen[0] == ()
    attempt = seen[1][0]
    assert attempt.action_type is ActionType.RESTART_COMPONENT
    assert attempt.outcome is F
    assert "health still failing" in attempt.summary


def test_a_partial_recovery_is_treated_like_a_failure_and_tries_again():
    orchestrator, _, _ = build(ScriptedVerifier(P))
    result = orchestrator.verify(executed(orchestrator))
    assert result.status is HandleStatus.READY
    assert result.proposal.action_type is ActionType.RELOAD_MODEL


def test_repeated_failures_stop_at_the_attempt_limit_and_alert_a_person():
    orchestrator, adapter, recorder = build(ScriptedVerifier(F, F), max_attempts=2)
    second = orchestrator.verify(executed(orchestrator))
    final = orchestrator.verify(orchestrator.execute(second))
    assert final.status is HandleStatus.ESCALATED
    assert "attempt limit" in final.reason
    assert final.verification.outcome is F
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED
    assert len(adapter.calls) == 2


def test_a_new_round_that_needs_approval_waits_for_a_person():
    orchestrator, _, recorder = build(
        ScriptedVerifier(F), cfg=config(reload_verdict="needs_approval")
    )
    result = orchestrator.verify(executed(orchestrator))
    assert result.status is HandleStatus.PARKED
    assert recorder.alerts[-1].kind is AlertKind.APPROVAL_NEEDED


@pytest.mark.parametrize(
    "outcome",
    [VerificationOutcome.INCONCLUSIVE, VerificationOutcome.ESCALATION_REQUIRED],
)
def test_unclear_results_go_to_a_person_without_another_attempt(outcome):
    released = []
    orchestrator, adapter, recorder = build(ScriptedVerifier(outcome), resolved=released.append)
    result = orchestrator.verify(executed(orchestrator))
    assert result.status is HandleStatus.ESCALATED
    assert outcome.value in result.reason
    assert released == []
    assert adapter.calls == ["restart_component"]
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED


def test_without_a_verifier_recovery_is_never_claimed():
    orchestrator, _, _ = build()
    result = orchestrator.verify(executed(orchestrator))
    assert result.status is HandleStatus.ESCALATED
    assert result.incident.state is not IncidentState.RESOLVED


def test_a_crashing_verifier_escalates_without_leaking_its_message():
    orchestrator, _, recorder = build(CrashingVerifier())
    result = orchestrator.verify(executed(orchestrator))
    assert result.status is HandleStatus.ESCALATED
    assert "RuntimeError" in result.reason
    assert "hunter2" not in result.reason
    assert "hunter2" not in " ".join(recorder.alerts[-1].details)


def test_secrets_in_failed_checks_are_hidden_from_the_next_diagnosis():
    seen = []

    class SpyAgent(MockDiagnosisAgent):
        def diagnose(self, context):
            seen.append(context.previous_attempts)
            return super().diagnose(context)

    verifier = ScriptedVerifier(F, detail="login rejected password=hunter2")
    orchestrator, _, _ = build(verifier, agent=SpyAgent())
    orchestrator.verify(executed(orchestrator))
    assert "hunter2" not in seen[1][0].summary


def test_a_result_can_only_be_verified_once():
    orchestrator, _, _ = build(ScriptedVerifier(V))
    done = executed(orchestrator)
    orchestrator.verify(done)
    with pytest.raises(ValueError, match="already been verified"):
        orchestrator.verify(done)


def test_only_executed_results_can_be_verified():
    orchestrator, _, _ = build(ScriptedVerifier(V))
    ready = orchestrator.handle(incident())
    with pytest.raises(ValueError, match="executed result"):
        orchestrator.verify(ready)


def test_the_same_incident_keeps_its_identity_across_rounds():
    orchestrator, _, _ = build(ScriptedVerifier(F))
    done = executed(orchestrator)
    second = orchestrator.verify(done)
    assert second.incident.incident_id == done.incident.incident_id


def test_the_attempt_limit_must_be_at_least_one():
    with pytest.raises(ValueError):
        build(max_attempts=0)


def test_no_verifier_default_says_inconclusive_and_follows_the_protocol():
    verifier: Verifier = NoVerifier()
    orchestrator, _, _ = build()
    done = executed(orchestrator)
    registered = ApplicationRegistry()
    registered.register("demo-app", Adapter())
    outcome = verifier.verify(registered.get("demo-app"), done.incident, done.action_result)
    assert outcome.outcome is VerificationOutcome.INCONCLUSIVE


def test_a_resolved_status_must_match_the_resolved_state():
    with pytest.raises(ValueError, match="needs the incident in state"):
        HandleResult(incident(), HandleStatus.RESOLVED)