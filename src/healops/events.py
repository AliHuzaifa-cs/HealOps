"""Records for things that are observed or detected: Observation and Incident."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from healops.types import IncidentState, IncidentType, ObservationKind, Severity


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid4().hex


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Observation:
    """One measurement reported by an application (health, metric or drift)."""

    app_name: str
    kind: ObservationKind
    name: str
    value: float | bool
    timestamp: datetime = field(default_factory=_now)
    details: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.app_name, "app_name")
        _require_text(self.name, "name")
        _require_aware(self.timestamp, "timestamp")


@dataclass(frozen=True, slots=True)
class Incident:
    """A detected problem, backed by the observations that triggered it."""

    app_name: str
    incident_type: IncidentType
    severity: Severity
    summary: str
    evidence: tuple[Observation, ...]
    incident_id: str = field(default_factory=_new_id)
    detected_at: datetime = field(default_factory=_now)
    state: IncidentState = IncidentState.OPEN

    def __post_init__(self) -> None:
        _require_text(self.app_name, "app_name")
        _require_text(self.summary, "summary")
        _require_aware(self.detected_at, "detected_at")
        if not self.evidence:
            raise ValueError("an incident needs at least one piece of evidence")