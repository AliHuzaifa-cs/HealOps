"""The contract between HealOps and a diagnosis agent (mock, rule-based or LLM)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from healops.capabilities import FRAMEWORK_ACTIONS
from healops.events import Incident, Observation
from healops.records import ActionProposal, Diagnosis
from healops.types import ActionType, VerificationOutcome

MAX_RECENT_OBSERVATIONS = 50

# Which component to restart: one name for every application, or a name per application.
ComponentSpec = str | Mapping[str, str]
DEFAULT_COMPONENT = "application"


def check_component_spec(spec: ComponentSpec) -> None:
    if not isinstance(spec, str | Mapping):
        raise TypeError("component must be a string or a mapping of application to component")
    names = [spec] if isinstance(spec, str) else list(spec.values())
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("component names must be non-empty strings")


def resolve_component(spec: ComponentSpec, app_name: str) -> str:
    """The component to restart for `app_name`."""
    if isinstance(spec, str):
        return spec
    return spec.get(app_name, DEFAULT_COMPONENT)


# Information a diagnosis could use but this version of HealOps never supplies.
NOT_SUPPLIED: tuple[str, ...] = ("raw error logs", "model and data version information")


@dataclass(frozen=True, slots=True)
class PreviousAttempt:
    """An earlier recovery attempt for the same incident and how it ended."""

    action_type: ActionType
    outcome: VerificationOutcome
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, ActionType):
            raise TypeError(f"action_type must be an ActionType, got {self.action_type!r}")
        if not isinstance(self.outcome, VerificationOutcome):
            raise TypeError(f"outcome must be a VerificationOutcome, got {self.outcome!r}")
        if not self.summary.strip():
            raise ValueError("summary must be a non-empty string")


@dataclass(frozen=True, slots=True)
class DiagnosisContext:
    """Everything an agent is allowed to see for one diagnosis, and nothing more."""

    incident: Incident
    available_actions: frozenset[ActionType]
    previous_attempts: tuple[PreviousAttempt, ...] = ()
    recent_observations: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        if not self.available_actions:
            raise ValueError("available_actions must not be empty")
        if len(self.recent_observations) > MAX_RECENT_OBSERVATIONS:
            raise ValueError(
                f"at most {MAX_RECENT_OBSERVATIONS} recent observations may be supplied"
            )
        for observation in self.recent_observations:
            if observation.app_name != self.incident.app_name:
                raise ValueError("recent observations must belong to the incident's application")

    @property
    def supplied(self) -> tuple[str, ...]:
        """What the agent was actually given. Derived, so it cannot overstate."""
        items = ["incident and its evidence", "available actions"]
        if self.previous_attempts:
            items.append(f"previous attempts ({len(self.previous_attempts)})")
        if self.recent_observations:
            items.append(f"recent observations ({len(self.recent_observations)})")
        return tuple(items)

    @property
    def not_supplied(self) -> tuple[str, ...]:
        """Information the agent has no access to and must not claim to have seen."""
        return NOT_SUPPLIED


@dataclass(frozen=True, slots=True)
class AgentResult:
    """A diagnosis and, optionally, one proposed action."""

    diagnosis: Diagnosis
    proposal: ActionProposal | None = None

    def __post_init__(self) -> None:
        if self.proposal is None:
            return
        if self.proposal.diagnosis_id != self.diagnosis.diagnosis_id:
            raise ValueError("the proposal must refer to this diagnosis")
        if self.proposal.incident_id != self.diagnosis.incident_id:
            raise ValueError("the proposal must refer to the same incident as the diagnosis")
        no_cause = self.diagnosis.probable_cause is None
        if no_cause and self.proposal.action_type not in FRAMEWORK_ACTIONS:
            raise ValueError(
                "without a probable cause only run_diagnostic, escalate or no_action may be proposed"
            )


class DiagnosisAgent(Protocol):
    """Anything that can turn a DiagnosisContext into an AgentResult."""

    def diagnose(self, context: DiagnosisContext) -> AgentResult: ...