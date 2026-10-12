import pytest

from healops.agents.base import DiagnosisAgent, DiagnosisContext, PreviousAttempt
from healops.agents.mock import DEFAULT_PLAYBOOK, MOCK_CONFIDENCE, MockDiagnosisAgent
from healops.events import Incident, Observation
from healops.types import (
    ActionType,
    IncidentType,
    ObservationKind,
    Severity,
    VerificationOutcome,
)

ALL_ACTIONS = frozenset(ActionType)


def incident(incident_type=IncidentType.HEALTH_FAILURE):
    obs = Observation(
        app_name="demo-app", kind=ObservationKind.METRIC, name="latency_ms", value=950.0
    )
    return Incident(
        app_name="demo-app",
        incident_type=incident_type,
        severity=Severity.MEDIUM,
        summary="something is wrong",
        evidence=(obs,),
    )


def context(incident_type=IncidentType.HEALTH_FAILURE, actions=ALL_ACTIONS, attempts=()):
    return DiagnosisContext(
        incident=incident(incident_type),
        available_actions=frozenset(actions),
        previous_attempts=tuple(attempts),
    )


def attempt(action, outcome=VerificationOutcome.FAILED):
    return PreviousAttempt(action, outcome, f"{action.value} did not fix it")


def test_mock_agent_follows_the_agent_protocol():
    agent: DiagnosisAgent = MockDiagnosisAgent()
    assert agent.diagnose(context()).proposal is not None


def test_every_incident_type_has_a_playbook_ending_in_escalation():
    assert set(DEFAULT_PLAYBOOK) == set(IncidentType)
    for steps in DEFAULT_PLAYBOOK.values():
        assert steps[-1] is ActionType.ESCALATE


def test_playbooks_never_suggest_redeployment():
    for steps in DEFAULT_PLAYBOOK.values():
        assert ActionType.REQUEST_REDEPLOYMENT not in steps


def test_health_failure_starts_with_a_restart_of_the_named_component():
    result = MockDiagnosisAgent(component="api").diagnose(context())
    assert result.proposal.action_type is ActionType.RESTART_COMPONENT
    assert result.proposal.params == {"component": "api"}


@pytest.mark.parametrize(
    "incident_type",
    [IncidentType.DATA_DRIFT, IncidentType.DATA_QUALITY, IncidentType.PREDICTION_ANOMALY],
)
def test_data_problems_only_ask_for_more_diagnostics_never_retraining(incident_type):
    result = MockDiagnosisAgent().diagnose(context(incident_type))
    assert result.proposal.action_type is ActionType.RUN_DIAGNOSTIC


def test_performance_degradation_tries_rollback_before_retraining():
    agent = MockDiagnosisAgent()
    first = agent.diagnose(context(IncidentType.PERFORMANCE_DEGRADATION))
    assert first.proposal.action_type is ActionType.ROLLBACK_MODEL
    after = agent.diagnose(
        context(
            IncidentType.PERFORMANCE_DEGRADATION,
            attempts=[attempt(ActionType.ROLLBACK_MODEL)],
        )
    )
    assert after.proposal.action_type is ActionType.REQUEST_RETRAINING


def test_unavailable_actions_are_skipped():
    actions = {ActionType.RELOAD_MODEL, ActionType.ESCALATE}
    result = MockDiagnosisAgent().diagnose(context(actions=actions))
    assert result.proposal.action_type is ActionType.RELOAD_MODEL


def test_an_action_that_already_failed_is_not_repeated():
    result = MockDiagnosisAgent().diagnose(
        context(attempts=[attempt(ActionType.RESTART_COMPONENT)])
    )
    assert result.proposal.action_type is ActionType.RELOAD_MODEL


def test_after_the_safe_fixes_fail_the_playbook_ends_by_escalating():
    tried = [attempt(ActionType.RESTART_COMPONENT), attempt(ActionType.RELOAD_MODEL)]
    result = MockDiagnosisAgent().diagnose(context(attempts=tried))
    assert result.proposal.action_type is ActionType.ESCALATE


def test_when_the_playbook_is_used_up_it_abstains_and_escalates():
    playbook = {IncidentType.HEALTH_FAILURE: (ActionType.RESTART_COMPONENT,)}
    result = MockDiagnosisAgent(playbook=playbook).diagnose(
        context(attempts=[attempt(ActionType.RESTART_COMPONENT)])
    )
    assert result.proposal.action_type is ActionType.ESCALATE
    assert result.diagnosis.probable_cause is None
    assert "no untried safe action remains" in result.diagnosis.missing_information


def test_diagnosis_is_a_low_confidence_hypothesis_and_says_it_is_rule_based():
    diagnosis = MockDiagnosisAgent().diagnose(context()).diagnosis
    assert diagnosis.confidence == MOCK_CONFIDENCE
    assert diagnosis.confidence <= 0.5
    assert "rule-based" in diagnosis.summary
    assert diagnosis.alternatives


def test_evidence_cited_comes_only_from_the_incident():
    diagnosis = MockDiagnosisAgent().diagnose(context()).diagnosis
    assert diagnosis.evidence_used == ("latency_ms = 950.0",)


def test_it_admits_what_it_could_not_see():
    diagnosis = MockDiagnosisAgent().diagnose(context()).diagnosis
    assert "raw error logs" in diagnosis.missing_information


@pytest.mark.parametrize("incident_type", list(IncidentType))
def test_it_never_proposes_an_action_the_application_cannot_do(incident_type):
    actions = {ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE, ActionType.NO_ACTION}
    proposal = MockDiagnosisAgent().diagnose(context(incident_type, actions)).proposal
    assert proposal.action_type in actions


def test_same_input_gives_the_same_decision():
    agent = MockDiagnosisAgent()
    ctx = context()
    first, second = agent.diagnose(ctx), agent.diagnose(ctx)
    assert first.proposal.action_type == second.proposal.action_type
    assert first.diagnosis.probable_cause == second.diagnosis.probable_cause


def test_proposal_refers_to_its_diagnosis_and_incident():
    ctx = context()
    result = MockDiagnosisAgent().diagnose(ctx)
    assert result.proposal.diagnosis_id == result.diagnosis.diagnosis_id
    assert result.proposal.incident_id == ctx.incident.incident_id


def test_a_custom_playbook_can_be_supplied():
    playbook = {IncidentType.HEALTH_FAILURE: (ActionType.ROLLBACK_MODEL, ActionType.ESCALATE)}
    result = MockDiagnosisAgent(playbook=playbook).diagnose(context())
    assert result.proposal.action_type is ActionType.ROLLBACK_MODEL


def test_an_incident_type_missing_from_the_playbook_escalates():
    result = MockDiagnosisAgent(playbook={}).diagnose(context())
    assert result.proposal.action_type is ActionType.ESCALATE