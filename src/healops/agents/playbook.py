"""Keeps a model agent inside the playbook, and points restarts at the right component.

``PlaybookGuard`` refuses a proposal that is not one of the playbook steps for the incident type
(an escalation is always allowed). Put it around a language model: a small model once answered an
API outage with "retrain the model". The fallback agent (rules) then answers instead.

``TargetRouter`` rewrites the component of a restart for the incident types named in the
configuration (``restart_targets``), whichever agent proposed it. One application can have
several things that can be restarted, and which one fits depends on what broke.
"""

from collections.abc import Mapping
from dataclasses import replace

from healops.agents.base import AgentResult, DiagnosisAgent, DiagnosisContext
from healops.exceptions import HealOpsError
from healops.types import ActionType, IncidentType


class OutsidePlaybook(HealOpsError):
    """The model proposed an action that the playbook does not allow for this incident type."""


class PlaybookGuard:
    def __init__(
        self, agent: DiagnosisAgent, playbook: Mapping[IncidentType, tuple[ActionType, ...]]
    ) -> None:
        self._agent = agent
        self._playbook = dict(playbook)

    def diagnose(self, context: DiagnosisContext) -> AgentResult:
        result = self._agent.diagnose(context)
        proposal = result.proposal
        if proposal is not None:
            allowed = set(self._playbook.get(context.incident.incident_type, ()))
            if proposal.action_type is not ActionType.ESCALATE and proposal.action_type not in allowed:
                raise OutsidePlaybook(proposal.action_type.value)
        return result


class TargetRouter:
    def __init__(self, agent: DiagnosisAgent, targets: Mapping[IncidentType, str]) -> None:
        self._agent = agent
        self._targets = dict(targets)

    def diagnose(self, context: DiagnosisContext) -> AgentResult:
        result = self._agent.diagnose(context)
        proposal = result.proposal
        target = self._targets.get(context.incident.incident_type)
        if proposal is not None and target and proposal.action_type is ActionType.RESTART_COMPONENT:
            proposal = replace(proposal, params={**proposal.params, "component": target})
            result = replace(result, proposal=proposal)
        return result
