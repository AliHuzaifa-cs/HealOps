"""Alerts that tell a person something needs attention.

Two moments call for a person: an action waits for approval, or an incident is
escalated because it could not be resolved. Sending an alert must never break
the recovery loop and must never leak secrets, so all sending goes through
`SafeNotifier`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from healops.events import _now, _require_aware, _require_text
from healops.redaction import redact_text

MAX_ERROR_LENGTH = 200


class AlertKind(StrEnum):
    APPROVAL_NEEDED = "approval_needed"
    ESCALATED = "escalated"


@dataclass(frozen=True, slots=True)
class Alert:
    """What a person needs to know, in plain text."""

    kind: AlertKind
    incident_id: str
    app_name: str
    title: str
    details: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AlertKind):
            raise TypeError(f"kind must be an AlertKind, got {self.kind!r}")
        _require_text(self.incident_id, "incident_id")
        _require_text(self.app_name, "app_name")
        _require_text(self.title, "title")
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Whether one notifier delivered an alert, and why not if it failed."""

    notifier: str
    delivered: bool
    error: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.notifier, "notifier")
        if not self.delivered and not self.error:
            raise ValueError("a failed notification must say why it failed")


class Notifier(Protocol):
    """Anything that can deliver an alert. It should raise if delivery fails."""

    def notify(self, alert: Alert) -> None: ...


class RecordingNotifier:
    """Keeps alerts in memory. Useful for tests and for dry-run demos."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.alerts.append(alert)


class ConsoleNotifier:
    """Writes alerts as readable text, to the terminal by default."""

    def __init__(self, write: Callable[[str], None] = print) -> None:
        self._write = write

    def notify(self, alert: Alert) -> None:
        self._write(f"[ALERT {alert.kind.value}] {alert.app_name} / incident {alert.incident_id}")
        self._write(alert.title)
        for line in alert.details:
            self._write(f"  - {line}")


def redact_alert(alert: Alert) -> Alert:
    """Return a copy of `alert` with well-known secrets hidden in its text."""
    return replace(
        alert,
        title=redact_text(alert.title),
        details=tuple(redact_text(line) for line in alert.details),
    )


class SafeNotifier:
    """Sends an alert to every notifier without ever raising or leaking secrets.

    Each notifier is tried separately, so one broken channel (for example an
    email server that is down) does not stop the others, and the caller always
    learns exactly which ones delivered.
    """

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = tuple(notifiers)

    def send(self, alert: Alert) -> tuple[NotificationResult, ...]:
        clean = redact_alert(alert)
        results: list[NotificationResult] = []
        for notifier in self._notifiers:
            name = type(notifier).__name__
            try:
                notifier.notify(clean)
            except Exception as error:  # noqa: BLE001 - a broken channel must not stop recovery
                message = redact_text(f"{type(error).__name__}: {error}")
                results.append(NotificationResult(name, False, message[:MAX_ERROR_LENGTH]))
            else:
                results.append(NotificationResult(name, True))
        return tuple(results)