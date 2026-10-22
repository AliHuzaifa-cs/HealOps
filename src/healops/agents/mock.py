"""A rule-based stand-in for the LLM agent.

It follows a fixed playbook per incident type, so the whole recovery loop can be
tested without an LLM, and it can act as the no-LLM fallback. Its diagnosis is a
low-confidence hypothesis from rules, never a proven root cause.
"""

from collections.abc import Mapping

from healops.agents.base import (
    AgentResult,
    ComponentSpec,
    DiagnosisContext,
    check_component_spec,
    resolve_component,
)
from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType, IncidentType

MOCK_CONFIDENCE = 0.4

# Actions to try, in order. Drift and data problems never trigger retraining or
# redeployment; the last resort is always to escalate to a person.
DEFAULT_PLAYBOOK: dict[IncidentType, tuple[ActionType, ...]] = {
    IncidentType.HEALTH_FAILURE: (
        ActionType.RESTART_COMPONENT,
        ActionType.RELOAD_MODEL,
        ActionType.ESCALATE,
    ),
    IncidentType.HIGH_LATENCY: (ActionType.RESTART_COMPONENT, ActionType.ESCALATE),
    IncidentType.HIGH_ERROR_RATE: (
        ActionType.ROLLBACK_MODEL,
        ActionType.RESTART_COMPONENT,
        ActionType.ESCALATE,
    ),
    IncidentType.PERFORMANCE_DEGRADATION: (
        ActionType.ROLLBACK_MODEL,
        ActionType.REQUEST_RETRAINING,
        ActionType.ESCALATE,
    ),
    IncidentType.DATA_DRIFT: (ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE),
    IncidentType.DATA_QUALITY: (ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE),
    IncidentType.PREDICTION_ANOMALY: (ActionType.RUN_DIAGNOSTIC, ActionType.ESCALATE),
}

_HYPOTHESES: dict[IncidentType, tuple[str, tuple[str, ...]]] = {
    IncidentType.HEALTH_FAILURE: (
        "an application component is not responding",
        ("host or network problem", "a dependency is down"),
    ),
    IncidentType.HIGH_LATENCY: (
        "the service is overloaded or a component is degraded",
        ("a slow downstream dependency", "larger inputs than usual"),
    ),
    IncidentType.HIGH_ERROR_RATE: (
        "a recent change or a failing component is causing errors",
        ("bad input data", "a dependency is down"),
    ),
    IncidentType.PERFORMANCE_DEGRADATION: (
        "model quality has dropped, possibly after a change or a data shift",
        ("label delay or a measurement problem", "data drift"),
    ),
    IncidentType.DATA_DRIFT: (
        "the input data distribution has shifted",
        ("an expected seasonal change", "an upstream data pipeline change"),
    ),
    IncidentType.DATA_QUALITY: (
        "the input data has quality problems",
        ("an upstream pipeline fault",),
    ),
    IncidentType.PREDICTION_ANOMALY: (
        "the model outputs look abnormal",
        ("unusual but valid inputs",),
    ),
}

_EXPECTED_OUTCOME: dict[ActionType, str] = {
    ActionType.RESTART_COMPONENT: "the component responds again and the incident clears",
    ActionType.RELOAD_MODEL: "the model reloads cleanly and the incident clears",
    ActionType.ROLLBACK_MODEL: "the previous model version restores normal behaviour",
    ActionType.REQUEST_RETRAINING: "a retrained candidate model is produced for review",
    ActionType.REQUEST_REDEPLOYMENT: "the chosen model or service becomes live",
    ActionType.RUN_DIAGNOSTIC: "more evidence is collected before anything is changed",
    ActionType.ESCALATE: "a person reviews the incident",
    ActionType.NO_ACTION: "no change is made",
}

_RISKS: dict[ActionType, tuple[str, ...]] = {
    ActionType.RESTART_COMPONENT: ("brief interruption while the component restarts",),
    ActionType.RELOAD_MODEL: ("brief interruption while the model reloads",),
    ActionType.ROLLBACK_MODEL: ("the older model may miss recent improvements",),
    ActionType.REQUEST_RETRAINING: ("uses compute and does not change what is live",),
}


class MockDiagnosisAgent:
    """Picks the first playbook action that is available and not yet tried."""

    def __init__(
        self,
        component: ComponentSpec = "application",
        playbook: Mapping[IncidentType, tuple[ActionType, ...]] | None = None,
    ) -> None:
        check_component_spec(component)
        self._component = component
        self._playbook = dict(DEFAULT_PLAYBOOK if playbook is None else playbook)

    def diagnose(self, context: DiagnosisContext) -> AgentResult:
        incident = context.incident
        tried = {attempt.action_type for attempt in context.previous_attempts}
        candidates = self._playbook.get(incident.incident_type, (ActionType.ESCALATE,))
        chosen = next(
            (
                action
                for action in candidates
                if action in context.available_actions and action not in tried
            ),
            None,
        )

        if chosen is None:
            diagnosis = Diagnosis(
                incident_id=incident.incident_id,
                summary="rule-based agent: no untried safe action remains",
                confidence=MOCK_CONFIDENCE / 2,
                missing_information=("no untried safe action remains", *context.not_supplied),
            )
            return AgentResult(diagnosis, self._proposal(diagnosis, ActionType.ESCALATE, incident.app_name))

        cause, alternatives = _HYPOTHESES[incident.incident_type]
        diagnosis = Diagnosis(
            incident_id=incident.incident_id,
            summary=f"rule-based hypothesis for {incident.incident_type.value}",
            confidence=MOCK_CONFIDENCE,
            probable_cause=cause,
            evidence_used=tuple(f"{o.name} = {o.value}" for o in incident.evidence),
            alternatives=alternatives,
            missing_information=context.not_supplied,
        )
        return AgentResult(diagnosis, self._proposal(diagnosis, chosen, incident.app_name))

    def _proposal(self, diagnosis: Diagnosis, action: ActionType, app_name: str) -> ActionProposal:
        component = resolve_component(self._component, app_name)
        params = {"component": component} if action is ActionType.RESTART_COMPONENT else {}
        return ActionProposal(
            incident_id=diagnosis.incident_id,
            diagnosis_id=diagnosis.diagnosis_id,
            action_type=action,
            rationale=f"playbook step for this incident type: {action.value}",
            expected_outcome=_EXPECTED_OUTCOME[action],
            params=params,
            risks=_RISKS.get(action, ()),
        )