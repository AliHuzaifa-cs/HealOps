"""A record of recovery actions that were really executed (used for retry limits and cooldowns)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

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

    A long-running service must not grow without limit, so old records go: those older
    than `retention` (measured from the newest record) and, beyond `max_records`, the
    oldest. Pick `retention` at least as long as your longest cooldown and approval
    window, or a cooldown could be forgotten early. Without limits it keeps everything.
    """

    def __init__(self, retention: timedelta | None = None, max_records: int | None = None) -> None:
        if retention is not None and retention <= timedelta(0):
            raise ValueError("retention must be positive")
        if max_records is not None and max_records < 1:
            raise ValueError("max_records must be at least 1")
        self._retention = retention
        self._max_records = max_records
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
        self._prune(executed_at)
        return entry

    def _prune(self, now: datetime) -> None:
        if self._retention is not None:
            cutoff = now - self._retention
            self._records = [r for r in self._records if r.executed_at >= cutoff]
        if self._max_records is not None and len(self._records) > self._max_records:
            self._records = self._records[-self._max_records :]

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