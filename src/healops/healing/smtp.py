"""Email alerts through SMTP (standard library only).

Email is a notification, not a control: it tells a person what to run
(`healops approve ...`) and never carries a link that does anything. Too many
emails get ignored, so this notifier holds back repeats:

- one email per incident and alert kind,
- one email per application and incident type within `cooldown_minutes`,
- at most `max_emails_per_hour`,
- no email for dry-run alerts unless asked.

Held-back alerts are not lost (the state store keeps every alert); the next email
that does go out says how many were held back. The password is read from an
environment variable at send time and never appears in config, logs or errors.
"""

import contextlib
import os
import smtplib
import ssl
import threading
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol

from healops.config import EmailConfig
from healops.events import _now
from healops.healing.notify import Alert, AlertKind

COMMAND_ID_LENGTH = 8


class SmtpConnection(Protocol):
    def ehlo(self) -> Any: ...

    def starttls(self, *, context: ssl.SSLContext) -> Any: ...

    def login(self, user: str, password: str) -> Any: ...

    def send_message(self, message: EmailMessage) -> Any: ...

    def quit(self) -> Any: ...


SmtpFactory = Callable[[EmailConfig], SmtpConnection]


def default_smtp_factory(config: EmailConfig) -> SmtpConnection:
    """Open a real connection with the security the config asks for."""
    if config.security == "ssl":
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
            context=ssl.create_default_context(),
        )
    return smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)


def _one_line(text: str) -> str:
    """Remove line breaks and other control characters (header injection)."""
    return "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text).strip()


class SmtpNotifier:
    """Sends alerts by email, within the limits above."""

    def __init__(
        self,
        config: EmailConfig,
        environ: Mapping[str, str] | None = None,
        smtp_factory: SmtpFactory = default_smtp_factory,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._config = config
        self._environ = environ if environ is not None else os.environ
        self._factory = smtp_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._last_by_group: dict[tuple[str, str], datetime] = {}
        self._sent_times: deque[datetime] = deque()
        self._held_back = 0
        self.sent = 0
        self.held_back_total = 0

    def notify(self, alert: Alert) -> None:
        """Send `alert`, or hold it back. Raises if sending was attempted and failed."""
        config = self._config
        if alert.dry_run and not config.email_in_dry_run:
            return  # dry-run alerts are for the console and the dashboard
        now = self._clock()
        with self._lock:
            if not self._allowed(alert, now):
                self._held_back += 1
                self.held_back_total += 1
                return
            held = self._held_back
        message = self._message(alert, held)
        self._send(message)
        with self._lock:
            self._seen.add((alert.incident_id, alert.kind.value))
            self._last_by_group[(alert.app_name, alert.incident_type or "")] = now
            self._sent_times.append(now)
            self._held_back = max(0, self._held_back - held)
            self.sent += 1

    def _allowed(self, alert: Alert, now: datetime) -> bool:
        config = self._config
        if (alert.incident_id, alert.kind.value) in self._seen:
            return False
        last = self._last_by_group.get((alert.app_name, alert.incident_type or ""))
        if last is not None and now - last < timedelta(minutes=config.cooldown_minutes):
            return False
        window_start = now - timedelta(hours=1)
        while self._sent_times and self._sent_times[0] <= window_start:
            self._sent_times.popleft()
        return len(self._sent_times) < config.max_emails_per_hour

    def _message(self, alert: Alert, held_back: int) -> EmailMessage:
        config = self._config
        label = "APPROVAL NEEDED" if alert.kind is AlertKind.APPROVAL_NEEDED else "NEEDS A PERSON"
        message = EmailMessage()
        message["Subject"] = _one_line(f"{config.subject_prefix} {label}: {alert.title}")
        message["From"] = config.sender
        message["To"] = ", ".join(config.recipients)
        short = alert.incident_id[:COMMAND_ID_LENGTH]
        lines = [
            alert.title,
            "",
            f"Application: {alert.app_name}",
            f"Incident:    {alert.incident_id}",
            *[f"  - {line}" for line in alert.details],
            "",
        ]
        if alert.kind is AlertKind.APPROVAL_NEEDED:
            lines += [
                "To approve:  healops approve " + short + " --by <your-name>",
                "To reject:   healops reject  " + short + ' --by <your-name> --reason "..."',
            ]
        else:
            lines += ["To see what is open:  healops list"]
        if held_back:
            note = f"{held_back} more alert(s) were not emailed (repeat or rate limit)."
            lines += ["", note + " Run `healops list` to see everything."]
        lines += ["", "This email only informs you. Nothing happens unless you answer."]
        message.set_content("\n".join(lines))
        return message

    def _send(self, message: EmailMessage) -> None:
        config = self._config
        password = None
        if config.password_env is not None:
            password = self._environ.get(config.password_env)
            if not password:
                raise RuntimeError(
                    f"the environment variable {config.password_env} is not set"
                )
        connection = self._factory(config)
        try:
            connection.ehlo()
            if config.security == "starttls":
                connection.starttls(context=ssl.create_default_context())
                connection.ehlo()
            if config.username is not None and password is not None:
                connection.login(config.username, password)
            connection.send_message(message)
        finally:
            with contextlib.suppress(Exception):  # closing is best effort
                connection.quit()