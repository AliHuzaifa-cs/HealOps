"""A record of recovery actions that were really executed (used for retry limits and cooldowns)."""

from dataclasses import dataclass
from datetime import datetime

from healops.events import _require_aware, _require_text
from healops.types import ActionType


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One executed action: which application, which incident, which action, when."""

    app_name: str
    incident_id: str
    action_type: ActionType
    executed_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.app_name, "app_name")
        _require_text(self.incident_id, "incident_id")
        _require_aware(self.executed_at, "executed_at")
        if not isinstance(self.action_type, ActionType):
            raise TypeError(f"action_type must be an ActionType, got {self.action_type!r}")


class ActionHistory:
    """In-memory history. The orchestrator records an action when it starts running it.

    It keeps every record for now (no pruning), which is fine for a short-lived demo
    but would need a size or age limit for a long-running service.
    """

    def __init__(self) -> None:
        self._records: list[ActionRecord] = []

    def record(
        self,
        app_name: str,
        incident_id: str,
        action_type: ActionType,
        executed_at: datetime,
    ) -> ActionRecord:
        entry = ActionRecord(app_name, incident_id, action_type, executed_at)
        self._records.append(entry)
        return entry

    def attempts(self, incident_id: str, action_type: ActionType) -> int:
        """How many times `action_type` was run for this incident."""
        return sum(
            1
            for entry in self._records
            if entry.incident_id == incident_id and entry.action_type is action_type
        )

    def last_executed(self, app_name: str, action_type: ActionType) -> datetime | None:
        """When `action_type` last ran on this application, in any incident."""
        times = [
            entry.executed_at
            for entry in self._records
            if entry.app_name == app_name and entry.action_type is action_type
        ]
        return max(times) if times else None

    def __len__(self) -> int:
        return len(self._records)