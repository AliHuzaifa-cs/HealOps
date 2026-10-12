import pytest

from healops.agents.base import (
    MAX_RECENT_OBSERVATIONS,
    NOT_SUPPLIED,
    AgentResult,
    DiagnosisAgent,
    DiagnosisContext,
    PreviousAttempt,
)
from healops.events import Incident, Observation
from healops.records import ActionProposal, Diagnosis
from healops.types import (
    ActionType,
    IncidentType,
    ObservationKind,
    Severity,
    VerificationOutcome,
)

ACTIONS = frozenset({ActionType.RELOAD_MODEL, ActionType.ESCALATE, ActionType.NO_ACTION})


def observation(app="demo-app", value=950.0):
    return Observation(
        app_name=app, kind=ObservationKind.METRIC, name="latency_ms", value=value
    )


def incident(app="demo-app"):
    return Incident(
        app_name=app,
        incident_type=IncidentType.HIGH_LATENCY,
        severity=Severity.MEDIUM,
        summary="latency_ms was above 500 for 3 checks in a row",
        evidence=(observation(app),),
    )


def context(**overrides):
    values = {"incident": incident(), "available_actions": ACTIONS}
    values.update(overrides)
    return DiagnosisContext(**values)


def confident_diagnosis(incident_id="inc-1"):
    return Diagnosis(
        incident_id=incident_id,
        summary="slow after reload",
        confidence=0.7,
        probable_cause="model is slower",
        evidence_used=("latency_ms 950",),
    )


def abstaining_diagnosis(incident_id="inc-1"):
    return Diagnosis(
        incident_id=incident_id,
        summary="not enough information",
        confidence=0.2,
        missing_information=("raw error logs",),
    )


def proposal(diagnosis, action=ActionType.RELOAD_MODEL):
    return ActionProposal(
        incident_id=diagnosis.incident_id,
        diagnosis_id=diagnosis.diagnosis_id,
        action_type=action,
        rationale="because",
        expected_outcome="latency recovers",
    )


def test_minimal_context_says_what_it_supplied():
    assert context().supplied == ("incident and its evidence", "available actions")


def test_supplied_grows_only_with_what_is_really_given():
    attempt = PreviousAttempt(
        ActionType.RELOAD_MODEL, VerificationOutcome.FAILED, "reload did not help"
    )
    ctx = context(previous_attempts=(attempt,), recent_observations=(observation(),) * 3)
    assert "previous attempts (1)" in ctx.supplied
    assert "recent observations (3)" in ctx.supplied


def test_context_states_what_the_agent_has_not_been_given():
    assert context().not_supplied == NOT_SUPPLIED
    assert "raw error logs" in NOT_SUPPLIED


def test_context_needs_at_least_one_available_action():
    with pytest.raises(ValueError):
        context(available_actions=frozenset())


def test_recent_observations_from_another_application_are_rejected():
    with pytest.raises(ValueError, match="application"):
        context(recent_observations=(observation(app="other-app"),))


def test_too_many_recent_observations_are_rejected():
    too_many = (observation(),) * (MAX_RECENT_OBSERVATIONS + 1)
    with pytest.raises(ValueError):
        context(recent_observations=too_many)


def test_previous_attempt_needs_known_action_and_outcome_and_a_summary():
    with pytest.raises(TypeError):
        PreviousAttempt("reload_model", VerificationOutcome.FAILED, "x")
    with pytest.raises(TypeError):
        PreviousAttempt(ActionType.RELOAD_MODEL, "failed", "x")
    with pytest.raises(ValueError):
        PreviousAttempt(ActionType.RELOAD_MODEL, VerificationOutcome.FAILED, " ")


def test_result_without_a_proposal_is_valid():
    assert AgentResult(diagnosis=confident_diagnosis()).proposal is None


def test_confident_diagnosis_may_propose_a_real_action():
    diagnosis = confident_diagnosis()
    result = AgentResult(diagnosis, proposal(diagnosis, ActionType.ROLLBACK_MODEL))
    assert result.proposal.action_type is ActionType.ROLLBACK_MODEL


def test_proposal_must_refer_to_the_same_diagnosis():
    diagnosis = confident_diagnosis()
    other = confident_diagnosis()
    with pytest.raises(ValueError, match="diagnosis"):
        AgentResult(diagnosis, proposal(other))


def test_proposal_must_refer_to_the_same_incident():
    diagnosis = confident_diagnosis("inc-1")
    wrong = ActionProposal(
        incident_id="inc-2",
        diagnosis_id=diagnosis.diagnosis_id,
        action_type=ActionType.RELOAD_MODEL,
        rationale="because",
        expected_outcome="latency recovers",
    )
    with pytest.raises(ValueError, match="incident"):
        AgentResult(diagnosis, wrong)


@pytest.mark.parametrize(
    "action", [ActionType.ROLLBACK_MODEL, ActionType.RELOAD_MODEL, ActionType.REQUEST_RETRAINING]
)
def test_an_abstaining_agent_cannot_propose_a_risky_action(action):
    diagnosis = abstaining_diagnosis()
    with pytest.raises(ValueError, match="probable cause"):
        AgentResult(diagnosis, proposal(diagnosis, action))


@pytest.mark.parametrize(
    "action", [ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE, ActionType.NO_ACTION]
)
def test_an_abstaining_agent_may_ask_for_diagnostics_escalate_or_do_nothing(action):
    diagnosis = abstaining_diagnosis()
    assert AgentResult(diagnosis, proposal(diagnosis, action)).proposal is not None


def test_any_object_with_diagnose_is_an_agent():
    class FixedAgent:
        def diagnose(self, context: DiagnosisContext) -> AgentResult:
            return AgentResult(diagnosis=confident_diagnosis(context.incident.incident_id))

    agent: DiagnosisAgent = FixedAgent()
    assert agent.diagnose(context()).diagnosis.probable_cause is not None