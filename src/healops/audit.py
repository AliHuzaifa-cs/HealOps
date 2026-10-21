"""The audit trail: an append-only record of what HealOps saw, decided and did.

Every event is one line of JSON. Secrets are hidden before an event is built and
again before it is written. Lines are only ever added, never changed.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from healops.events import _new_id, _now, _require_aware, _require_text
from healops.exceptions import AuditReadError, AuditWriteError
from healops.redaction import redact_text, redact_value, redacted_dict


class AuditStage(StrEnum):
    INCIDENT_OPENED = "incident_opened"
    DIAGNOSED = "diagnosed"
    PROPOSED = "proposed"
    POLICY_DECIDED = "policy_decided"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ACTION_STARTING = "action_starting"
    ACTION_FINISHED = "action_finished"
    VERIFIED = "verified"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One thing that happened, with the (already redacted) record it is about."""

    stage: AuditStage
    incident_id: str
    app_name: str
    record_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    event_id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not isinstance(self.stage, AuditStage):
            raise TypeError(f"stage must be an AuditStage, got {self.stage!r}")
        _require_text(self.incident_id, "incident_id")
        _require_text(self.app_name, "app_name")
        _require_text(self.record_type, "record_type")
        _require_aware(self.created_at, "created_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "created_at": self.created_at.isoformat(),
            "stage": self.stage.value,
            "incident_id": self.incident_id,
            "app_name": self.app_name,
            "record_type": self.record_type,
            "payload": self.payload,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEvent":
        return cls(
            stage=AuditStage(data["stage"]),
            incident_id=data["incident_id"],
            app_name=data["app_name"],
            record_type=data["record_type"],
            payload=dict(data.get("payload") or {}),
            note=data.get("note"),
            event_id=data["event_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )


def build_event(
    stage: AuditStage,
    incident_id: str,
    app_name: str,
    record: object | None = None,
    data: dict[str, Any] | None = None,
    note: str | None = None,
) -> AuditEvent:
    """Build an event from a HealOps record, or from plain data. Secrets are hidden."""
    if record is not None:
        payload = redacted_dict(record)
        record_type = type(record).__name__
    else:
        payload = redact_value(data or {})
        record_type = "data"
    return AuditEvent(
        stage=stage,
        incident_id=incident_id,
        app_name=app_name,
        record_type=record_type,
        payload=payload,
        note=redact_text(note) if note else None,
    )


class AuditLog(Protocol):
    """Anything that can store audit events. `append` must raise AuditWriteError on failure."""

    def append(self, event: AuditEvent) -> None: ...


class MemoryAuditLog:
    """Keeps events in memory. For tests and short demos."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonlAuditLog:
    """Appends one JSON line per event to a file and flushes it to disk."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: AuditEvent) -> None:
        # Hidden once more at write time, in case a payload was built by hand.
        line = json.dumps(
            redact_value(event.to_dict()), sort_keys=True, separators=(",", ":")
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise AuditWriteError(
                f"could not write to the audit log {self._path}: {type(error).__name__}"
            ) from error


def read_events(path: str | Path) -> list[AuditEvent]:
    """Read an audit file back. A damaged line is an error, never silently skipped."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as error:
        raise AuditReadError(f"cannot read the audit log {file_path}") from error

    events = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(AuditEvent.from_dict(json.loads(line)))
        except (ValueError, KeyError, TypeError) as error:
            raise AuditReadError(f"line {number} of {file_path} is damaged") from error
    return events