from datetime import UTC, datetime, timedelta

import pytest

from healops.agents.base import DiagnosisContext
from healops.agents.fallback import FallbackAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.audit import AuditStage, MemoryAuditLog
from healops.config import HealOpsConfig
from healops.events import Incident, Observation
from healops.healing.approval import ApprovalQueue
from healops.healing.history import ActionHistory
from healops.healing.notify import RecordingNotifier, SafeNotifier
from healops.healing.orchestrator import HandleStatus, Orchestrator
from healops.healing.policy import PolicyEngine
from healops.registry import ApplicationRegistry
from healops.types import (
    ActionType,
    IncidentType,
    ObservationKind,
    PolicyVerdict,
    Severity,
)

T0 = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __call__(self):
        return T0 + timedelta(seconds=0)


class Adapter:
    def get_health(self) -> bool:
        return True

    def restart_component(self, component: str) -> str:
        return "restarted"


class DownAgent:
    """Stands in for an LLM that cannot be reached."""

    def __init__(self, error=None):
        self.calls = 0
        self._error = error or ConnectionError("llm down api_key=sk-secret-123")

    def diagnose(self, context):
        self.calls += 1
        raise self._error


def make_incident():
    obs = Observation(app_name="demo-app", kind=ObservationKind.HEALTH, name="health", value=False)
    return Incident(
        app_name="demo-app",
        incident_type=IncidentType.HEALTH_FAILURE,
        severity=Severity.CRITICAL,
        summary="health check failed 3 times in a row",
        evidence=(obs,),
    )


def make_context(incident=None):
    return DiagnosisContext(
        incident=incident or make_incident(),
        available_actions=frozenset({ActionType.RESTART_COMPONENT, ActionType.ESCALATE}),
    )


def test_a_healthy_primary_agent_is_used_untouched():
    primary = MockDiagnosisAgent()
    agent = FallbackAgent(primary, DownAgent())
    context = make_context()
    result = agent.diagnose(context)
    assert not result.diagnosis.summary.startswith("[FALLBACK")
    assert result.diagnosis.confidence == 0.4
    assert agent.fallback_count == 0
    assert agent.last_error_type is None


def test_when_the_primary_fails_the_fallback_answers_and_says_so():
    agent = FallbackAgent(DownAgent(), MockDiagnosisAgent())
    result = agent.diagnose(make_context())
    assert result.diagnosis.summary.startswith(
        "[FALLBACK: primary agent unavailable (ConnectionError)]"
    )
    assert agent.fallback_count == 1
    assert agent.last_error_type == "ConnectionError"


def test_the_error_message_never_leaks_only_its_type_is_kept():
    agent = FallbackAgent(DownAgent(), MockDiagnosisAgent())
    result = agent.diagnose(make_context())
    assert "sk-secret-123" not in result.diagnosis.summary
    assert "llm down" not in result.diagnosis.summary


def test_fallback_confidence_is_capped_but_never_raised():
    agent = FallbackAgent(DownAgent(), MockDiagnosisAgent(), confidence_cap=0.2)
    assert agent.diagnose(make_context()).diagnosis.confidence == 0.2
    low = FallbackAgent(DownAgent(), MockDiagnosisAgent(), confidence_cap=0.9)
    assert low.diagnose(make_context()).diagnosis.confidence == 0.4


def test_the_proposal_still_matches_the_rewritten_diagnosis():
    result = FallbackAgent(DownAgent(), MockDiagnosisAgent()).diagnose(make_context())
    assert result.proposal is not None
    assert result.proposal.diagnosis_id == result.diagnosis.diagnosis_id
    assert result.proposal.action_type is ActionType.RESTART_COMPONENT


def test_an_agent_result_with_no_proposal_stays_without_one():
    class NoProposal:
        def diagnose(self, context):
            from healops.agents.base import AgentResult

            inner = MockDiagnosisAgent().diagnose(context)
            return AgentResult(inner.diagnosis, None)

    result = FallbackAgent(DownAgent(), NoProposal()).diagnose(make_context())
    assert result.proposal is None
    assert result.diagnosis.summary.startswith("[FALLBACK")


def test_a_failing_fallback_is_not_hidden():
    agent = FallbackAgent(DownAgent(), DownAgent(RuntimeError("also down")))
    with pytest.raises(RuntimeError):
        agent.diagnose(make_context())


def test_each_failure_is_counted_and_the_primary_is_retried_every_time():
    primary = DownAgent()
    agent = FallbackAgent(primary, MockDiagnosisAgent())
    agent.diagnose(make_context())
    agent.diagnose(make_context())
    assert primary.calls == 2
    assert agent.fallback_count == 2


def test_a_cap_outside_zero_to_one_is_refused():
    with pytest.raises(ValueError):
        FallbackAgent(DownAgent(), MockDiagnosisAgent(), confidence_cap=1.5)
    with pytest.raises(ValueError):
        FallbackAgent(DownAgent(), MockDiagnosisAgent(), confidence_cap=-0.1)


def build(agent):
    config = HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.6,
            "policies": {"restart_component": {"verdict": "allow"}},
        }
    )
    registry = ApplicationRegistry()
    registry.register("demo-app", Adapter())
    recorder = RecordingNotifier()
    audit = MemoryAuditLog()
    orchestrator = Orchestrator(
        registry=registry,
        agent=agent,
        policy=PolicyEngine(config, history=ActionHistory(), clock=Clock()),
        approvals=ApprovalQueue(600, clock=Clock()),
        notifier=SafeNotifier([recorder]),
        audit=audit,
    )
    return orchestrator, recorder, audit


def test_with_the_llm_down_a_person_is_asked_instead_of_acting_alone():
    orchestrator, _, _ = build(FallbackAgent(DownAgent(), MockDiagnosisAgent()))
    result = orchestrator.handle(make_incident())
    assert result.status is HandleStatus.PARKED
    assert result.decision.verdict is PolicyVerdict.NEEDS_APPROVAL


def test_the_alert_and_the_audit_trail_both_show_it_was_a_fallback():
    orchestrator, recorder, audit = build(FallbackAgent(DownAgent(), MockDiagnosisAgent()))
    orchestrator.handle(make_incident())
    alert_text = " ".join(recorder.alerts[0].details)
    assert "[FALLBACK: primary agent unavailable (ConnectionError)]" in alert_text
    assert "sk-secret-123" not in alert_text
    diagnosed = [e for e in audit.events if e.stage is AuditStage.DIAGNOSED]
    assert len(diagnosed) == 1
    assert "[FALLBACK" in str(diagnosed[0].payload)


def test_an_escalation_alert_also_carries_the_diagnosis():
    class SilentFallback:
        def diagnose(self, context):
            from healops.agents.base import AgentResult

            inner = MockDiagnosisAgent().diagnose(context)
            return AgentResult(inner.diagnosis, None)

    orchestrator, recorder, _ = build(FallbackAgent(DownAgent(), SilentFallback()))
    result = orchestrator.handle(make_incident())
    assert result.status is HandleStatus.ESCALATED
    assert any(
        line.startswith("diagnosis: [FALLBACK") for line in recorder.alerts[0].details
    )