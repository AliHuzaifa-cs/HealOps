from datetime import UTC, datetime

import pytest

from healops.agents.base import AgentResult
from healops.agents.mock import MockDiagnosisAgent
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import AlertKind, RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleResult, HandleStatus, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.records import ActionProposal, Diagnosis
from healops.registry import ApplicationRegistry
from healops.types import (
    ActionType,
    IncidentState,
    IncidentType,
    ObservationKind,
    Severity,
)

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class RecordingAdapter:
    def __init__(self, fail_with=None, reply="restarted"):
        self.calls = []
        self._fail_with = fail_with
        self._reply = reply

    def get_health(self) -> bool:
        return True

    def restart_component(self, component: str) -> str:
        self.calls.append(("restart_component", component))
        if self._fail_with:
            raise RuntimeError(self._fail_with)
        return self._reply

    def reload_model(self) -> str:
        self.calls.append(("reload_model",))
        return "reloaded"


def incident(incident_type=IncidentType.HEALTH_FAILURE):
    obs = Observation(
        app_name="demo-app", kind=ObservationKind.HEALTH, name="health", value=False
    )
    return Incident(
        app_name="demo-app",
        incident_type=incident_type,
        severity=Severity.CRITICAL,
        summary="health check failed 3 times in a row",
        evidence=(obs,),
    )


def allow_restart():
    return HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.3,
            "policies": {"restart_component": {"verdict": "allow"}},
        }
    )


class ScriptedAgent:
    """Returns one fixed proposal, so every action type can be tried."""

    def __init__(self, action, params=None, cause="a component is down"):
        self._action = action
        self._params = params or {}
        self._cause = cause

    def diagnose(self, context):
        diagnosis = Diagnosis(
            incident_id=context.incident.incident_id,
            summary="scripted",
            confidence=0.9,
            probable_cause=self._cause,
            evidence_used=("health = False",) if self._cause else (),
            missing_information=() if self._cause else ("raw error logs",),
        )
        proposal = ActionProposal(
            incident_id=diagnosis.incident_id,
            diagnosis_id=diagnosis.diagnosis_id,
            action_type=self._action,
            rationale="scripted",
            expected_outcome="scripted",
            params=self._params,
        )
        return AgentResult(diagnosis, proposal)


def build(adapter=None, config=None, agent=None, dry_run=False):
    adapter = adapter or RecordingAdapter()
    config = config or allow_restart()
    history = ActionHistory()
    registry = ApplicationRegistry()
    registry.register("demo-app", adapter)
    recorder = RecordingNotifier()
    orchestrator = Orchestrator(
        registry=registry,
        agent=agent or MockDiagnosisAgent(),
        policy=PolicyEngine(config, history=history, clock=lambda: T0),
        approvals=ApprovalQueue(600, clock=lambda: T0),
        notifier=SafeNotifier([recorder]),
        dry_run=dry_run,
        clock=lambda: T0,
    )
    return orchestrator, adapter, history, recorder


def ready(orchestrator, inc=None):
    result = orchestrator.handle(inc or incident())
    assert result.status is HandleStatus.READY
    return result


def test_a_live_action_runs_the_adapter_callback_once():
    orchestrator, adapter, history, _ = build()
    result = orchestrator.execute(ready(orchestrator))
    assert result.status is HandleStatus.EXECUTED
    assert result.incident.state is IncidentState.EXECUTED
    assert adapter.calls == [("restart_component", "application")]
    assert result.action_result.completed is True
    assert result.action_result.dry_run is False
    assert result.action_result.message == "restarted"
    assert len(history) == 1


def test_dry_run_is_the_default_and_never_calls_the_application():
    config = allow_restart()
    adapter = RecordingAdapter()
    registry = ApplicationRegistry()
    registry.register("demo-app", adapter)
    orchestrator = Orchestrator(
        registry=registry,
        agent=MockDiagnosisAgent(),
        policy=PolicyEngine(config, history=ActionHistory(), clock=lambda: T0),
        approvals=ApprovalQueue(600, clock=lambda: T0),
        notifier=SafeNotifier([]),
    )
    result = orchestrator.execute(ready(orchestrator))
    assert adapter.calls == []
    assert result.status is HandleStatus.ESCALATED


def test_dry_run_logs_the_action_and_tells_a_person_it_was_only_a_test():
    orchestrator, adapter, history, recorder = build(dry_run=True)
    result = orchestrator.execute(ready(orchestrator))
    assert adapter.calls == []
    assert result.status is HandleStatus.ESCALATED
    assert result.incident.state is IncidentState.ESCALATED
    assert "dry-run" in result.reason
    assert "nothing was changed" in result.reason
    assert result.action_result.dry_run is True
    assert result.action_result.completed is False
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED
    assert recorder.alerts[-1].title.startswith("[DRY-RUN]")
    assert len(history) == 0


def test_a_dry_run_is_never_reported_as_resolved():
    orchestrator, _, _, _ = build(dry_run=True)
    result = orchestrator.execute(ready(orchestrator))
    assert result.incident.state is not IncidentState.RESOLVED


def test_a_failing_callback_escalates_and_is_not_retried():
    adapter = RecordingAdapter(fail_with="connection refused")
    orchestrator, adapter, history, recorder = build(adapter=adapter)
    result = orchestrator.execute(ready(orchestrator))
    assert result.status is HandleStatus.ESCALATED
    assert len(adapter.calls) == 1
    assert "failed (RuntimeError)" in result.reason
    assert "not retried" in result.reason
    assert result.action_result.completed is False
    assert "connection refused" in result.action_result.message
    assert len(history) == 1
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED


def test_secrets_in_callback_errors_and_replies_are_hidden():
    adapter = RecordingAdapter(fail_with="login failed password=hunter2")
    orchestrator, _, _, _ = build(adapter=adapter)
    failed = orchestrator.execute(ready(orchestrator))
    assert "hunter2" not in failed.action_result.message

    adapter = RecordingAdapter(reply="restarted token=abc123XYZ")
    orchestrator, _, _, _ = build(adapter=adapter)
    done = orchestrator.execute(ready(orchestrator))
    assert "abc123XYZ" not in done.action_result.message


def test_the_same_result_cannot_be_executed_twice():
    orchestrator, adapter, _, _ = build()
    result = ready(orchestrator)
    orchestrator.execute(result)
    with pytest.raises(ValueError, match="already been executed"):
        orchestrator.execute(result)
    assert len(adapter.calls) == 1


def test_only_ready_results_can_be_executed():
    orchestrator, _, _, _ = build(config=HealOpsConfig.model_validate({}))
    parked = orchestrator.handle(incident())
    assert parked.status is HandleStatus.PARKED
    with pytest.raises(ValueError, match="ready to execute"):
        orchestrator.execute(parked)


def test_an_approved_action_can_then_be_executed():
    orchestrator, adapter, _, _ = build(config=HealOpsConfig.model_validate({}))
    parked = orchestrator.handle(incident())
    approved = orchestrator.approve(
        parked.incident.incident_id, parked.proposal.proposal_id, "ali@example.com"
    )
    result = orchestrator.execute(approved)
    assert result.status is HandleStatus.EXECUTED
    assert adapter.calls == [("restart_component", "application")]


def test_a_second_incident_is_blocked_by_the_cooldown_of_the_first_run():
    orchestrator, adapter, _, _ = build()
    orchestrator.execute(ready(orchestrator))
    second = orchestrator.handle(incident())
    assert second.status is HandleStatus.ESCALATED
    assert "cooldown" in second.reason
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    ("action", "words"),
    [
        (ActionType.ESCALATE, "chose to escalate"),
        (ActionType.NO_ACTION, "no action"),
        (ActionType.RUN_DIAGNOSTIC, "diagnostic"),
    ],
)
def test_framework_actions_hand_the_incident_to_a_person_without_touching_the_app(
    action, words
):
    agent = ScriptedAgent(action)
    orchestrator, adapter, history, recorder = build(agent=agent)
    result = orchestrator.execute(ready(orchestrator))
    assert result.status is HandleStatus.ESCALATED
    assert words in result.reason
    assert adapter.calls == []
    assert len(history) == 0
    assert recorder.alerts[-1].kind is AlertKind.ESCALATED


def test_a_restart_without_a_component_is_refused_before_anything_runs():
    agent = ScriptedAgent(ActionType.RESTART_COMPONENT, params={})
    orchestrator, adapter, history, _ = build(agent=agent)
    result = orchestrator.execute(ready(orchestrator))
    assert result.status is HandleStatus.ESCALATED
    assert "component" in result.reason
    assert adapter.calls == []
    assert len(history) == 0


def test_reload_runs_the_reload_callback():
    config = HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.3,
            "policies": {"reload_model": {"verdict": "allow"}},
        }
    )
    agent = ScriptedAgent(ActionType.RELOAD_MODEL)
    orchestrator, adapter, _, _ = build(config=config, agent=agent)
    result = orchestrator.execute(ready(orchestrator))
    assert result.status is HandleStatus.EXECUTED
    assert adapter.calls == [("reload_model",)]


def test_an_executed_status_must_match_the_executed_state():
    with pytest.raises(ValueError, match="needs the incident in state"):
        HandleResult(incident(), HandleStatus.EXECUTED)