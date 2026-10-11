"""Records produced by the diagnosis and planning stages: Diagnosis and ActionProposal."""

from dataclasses import dataclass, field
from datetime import datetime

from healops.events import _new_id, _now, _require_aware, _require_text
from healops.types import ActionType


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """The agent's structured reading of an incident.

    `probable_cause` is a hypothesis, not a proven fact. If the agent cannot
    name a cause, leave it as None and list what is missing instead.
    """

    incident_id: str
    summary: str
    confidence: float
    probable_cause: str | None = None
    evidence_used: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    diagnosis_id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_text(self.incident_id, "incident_id")
        _require_text(self.summary, "summary")
        _require_aware(self.created_at, "created_at")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.probable_cause is not None:
            _require_text(self.probable_cause, "probable_cause")
            if not self.evidence_used:
                raise ValueError("a probable cause must cite the evidence it is based on")
        elif not self.missing_information:
            raise ValueError(
                "without a probable cause, list the missing information that blocks one"
            )


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """An action the agent suggests. Only a proposal: the policy engine decides."""

    incident_id: str
    diagnosis_id: str
    action_type: ActionType
    rationale: str
    expected_outcome: str
    params: dict[str, str] = field(default_factory=dict)
    risks: tuple[str, ...] = ()
    proposal_id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_text(self.incident_id, "incident_id")
        _require_text(self.diagnosis_id, "diagnosis_id")
        _require_text(self.rationale, "rationale")
        _require_text(self.expected_outcome, "expected_outcome")
        _require_aware(self.created_at, "created_at")
        if not isinstance(self.action_type, ActionType):
            raise TypeError(
                f"action_type must be an ActionType, got {self.action_type!r}"
            )