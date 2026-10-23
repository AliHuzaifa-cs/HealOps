"""Email alerts: what is sent, what is held back, and what must never leak."""

from datetime import UTC, datetime, timedelta

import pytest

from healops import HealOps
from healops.config import EmailConfig, HealOpsConfig
from healops.healing.notify import Alert, AlertKind, RecordingNotifier, SafeNotifier
from healops.healing.smtp import SmtpNotifier
from healops.testing.harness import DEFAULT_CONFIG
from healops.testing.toy_app import COMPONENT, Fault, ToyApp

T0 = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
PASSWORD = "s3cret-app-password"


class FakeSmtp:
    def __init__(self, fail_at=None):
        self.calls = []
        self.messages = []
        self.fail_at = fail_at

    def _do(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise OSError(f"{name} failed")

    def ehlo(self):
        self._do("ehlo")

    def starttls(self, *, context):
        self._do("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}:{password}")
        if self.fail_at == "login":
            raise OSError("login failed")

    def send_message(self, message):
        self._do("send")
        self.messages.append(message)

    def quit(self):
        self.calls.append("quit")


class Server:
    """Hands out FakeSmtp connections and remembers them."""

    def __init__(self, fail_at=None):
        self.connections = []
        self.fail_at = fail_at

    def __call__(self, config):
        connection = FakeSmtp(self.fail_at)
        self.connections.append(connection)
        return connection

    @property
    def messages(self):
        return [m for c in self.connections for m in c.messages]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def email_config(**overrides):
    data = {
        "host": "smtp.example.com",
        "sender": "healops@example.com",
        "recipients": ["me@example.com"],
        "username": "healops@example.com",
        "password_env": "HEALOPS_SMTP_PASSWORD",
        "cooldown_minutes": 0,
        **overrides,
    }
    return EmailConfig.model_validate(data)


def alert(incident="inc-0001-aaaa", kind=AlertKind.APPROVAL_NEEDED, app="toy", **kwargs):
    return Alert(
        kind=kind,
        incident_id=incident,
        app_name=app,
        title=kwargs.pop("title", "restart_component needs your approval"),
        details=kwargs.pop("details", ("incident: health check failed", "proposal_id: p1")),
        created_at=T0,
        incident_type=kwargs.pop("incident_type", "health_failure"),
        **kwargs,
    )


def notifier(server=None, clock=None, **overrides):
    server = server or Server()
    clock = clock or Clock()
    return (
        SmtpNotifier(
            email_config(**overrides),
            environ={"HEALOPS_SMTP_PASSWORD": PASSWORD},
            smtp_factory=server,
            clock=clock,
        ),
        server,
        clock,
    )


def test_an_approval_email_tells_the_person_exactly_what_to_run():
    sender, server, _ = notifier()
    sender.notify(alert())
    [message] = server.messages
    assert message["Subject"] == "[HealOps] APPROVAL NEEDED: restart_component needs your approval"
    assert message["From"] == "healops@example.com"
    assert message["To"] == "me@example.com"
    body = message.get_content()
    assert "healops approve inc-0001 --by <your-name>" in body
    assert "healops reject  inc-0001 --by <your-name>" in body
    assert "proposal_id: p1" in body
    assert "http" not in body  # no links that could do anything


def test_an_escalation_email_points_to_the_list_command_not_approval():
    sender, server, _ = notifier()
    sender.notify(alert(kind=AlertKind.ESCALATED, title="health_failure on toy needs a person"))
    body = server.messages[0].get_content()
    assert "NEEDS A PERSON" in server.messages[0]["Subject"]
    assert "healops list" in body
    assert "healops approve" not in body


def test_the_password_is_used_to_log_in_but_never_written_into_the_email():
    sender, server, _ = notifier()
    sender.notify(alert())
    assert f"login:healops@example.com:{PASSWORD}" in server.connections[0].calls
    assert PASSWORD not in server.messages[0].as_string()


def test_starttls_ssl_and_none_use_the_right_steps():
    sender, server, _ = notifier()
    sender.notify(alert())
    assert server.connections[0].calls[:3] == ["ehlo", "starttls", "ehlo"]
    assert server.connections[0].calls[-1] == "quit"

    sender, server, _ = notifier(security="ssl", port=465)
    sender.notify(alert())
    assert "starttls" not in server.connections[0].calls

    sender, server, _ = notifier(
        security="none", host="localhost", port=25, username=None, password_env=None
    )
    sender.notify(alert())
    assert server.connections[0].calls == ["ehlo", "send", "quit"]


def test_a_missing_password_variable_stops_before_connecting_and_names_only_the_variable():
    sender = SmtpNotifier(email_config(), environ={}, smtp_factory=(server := Server()))
    with pytest.raises(RuntimeError) as caught:
        sender.notify(alert())
    assert "HEALOPS_SMTP_PASSWORD" in str(caught.value)
    assert server.connections == []


def test_a_failed_send_raises_and_still_closes_the_connection():
    sender, server, _ = notifier(server=Server(fail_at="send"))
    with pytest.raises(OSError):
        sender.notify(alert())
    assert server.connections[0].calls[-1] == "quit"
    assert sender.sent == 0


def test_a_failed_email_does_not_break_the_other_channels():
    sender, _, _ = notifier(server=Server(fail_at="login"))
    recording = RecordingNotifier()
    results = SafeNotifier([sender, recording]).send(alert())
    assert [r.delivered for r in results] == [False, True]
    assert len(recording.alerts) == 1


def test_the_same_incident_and_kind_is_emailed_only_once():
    sender, server, clock = notifier()
    sender.notify(alert())
    clock.now += timedelta(hours=2)
    sender.notify(alert())
    assert len(server.messages) == 1
    sender.notify(alert(kind=AlertKind.ESCALATED))  # a different kind is new information
    assert len(server.messages) == 2


def test_alerts_for_the_same_app_and_type_wait_for_the_cooldown():
    sender, server, clock = notifier(cooldown_minutes=15)
    sender.notify(alert("inc-1111"))
    clock.now += timedelta(minutes=5)
    sender.notify(alert("inc-2222"))
    assert len(server.messages) == 1
    sender.notify(alert("inc-3333", app="other-app"))
    sender.notify(alert("inc-4444", incident_type="high_latency"))
    assert len(server.messages) == 3
    clock.now += timedelta(minutes=11)
    sender.notify(alert("inc-5555"))
    assert len(server.messages) == 4


def test_there_is_an_hourly_limit():
    sender, server, clock = notifier(max_emails_per_hour=2)
    for n in range(4):
        sender.notify(alert(f"inc-{n}000"))
    assert len(server.messages) == 2
    clock.now += timedelta(minutes=61)
    sender.notify(alert("inc-9000"))
    assert len(server.messages) == 3


def test_the_next_email_says_how_many_were_held_back_and_then_forgets():
    sender, server, clock = notifier(max_emails_per_hour=1)
    sender.notify(alert("inc-1000"))
    sender.notify(alert("inc-2000"))
    sender.notify(alert("inc-3000"))
    assert sender.held_back_total == 2
    clock.now += timedelta(hours=2)
    sender.notify(alert("inc-4000"))
    assert "2 more alert(s) were not emailed" in server.messages[1].get_content()
    clock.now += timedelta(hours=2)
    sender.notify(alert("inc-5000"))
    assert "were not emailed" not in server.messages[2].get_content()


def test_dry_run_alerts_are_not_emailed_unless_asked():
    sender, server, _ = notifier()
    sender.notify(alert(dry_run=True))
    assert server.messages == []
    sender, server, _ = notifier(email_in_dry_run=True)
    sender.notify(alert(dry_run=True))
    assert len(server.messages) == 1


def test_a_title_cannot_add_email_headers():
    sender, server, _ = notifier()
    sender.notify(alert(title="x\r\nBcc: attacker@example.com\r\nSubject: spoof"))
    message = server.messages[0]
    assert message["Bcc"] is None
    assert "\n" not in message["Subject"] and "\r" not in message["Subject"]


# ----- configuration -----


@pytest.mark.parametrize(
    "bad",
    [
        {"sender": "not-an-address"},
        {"recipients": []},
        {"recipients": ["a@b.com", "bad address@b.com"]},
        {"recipients": ["a@b.com\nBcc: x@y.com"]},
        {"security": "none"},  # not allowed away from localhost
        {"username": "me", "password_env": None},  # a user name needs a password variable
        {"password_env": "HEALOPS_SMTP_PASSWORD", "username": None},
        {"password_env": "hunter2 is my password"},
        {"password_env": "pa$$word"},
        {"port": 0},
        {"max_emails_per_hour": 0},
        {"cooldown_minutes": -1},
        {"subject_prefix": "a\nb"},
        {"host": "smtp example.com"},
        {"surprise": 1},
    ],
)
def test_bad_email_settings_are_refused(bad):
    data = {
        "host": "smtp.example.com",
        "sender": "a@example.com",
        "recipients": ["b@example.com"],
        "username": "a@example.com",
        "password_env": "PW",
    }
    data.update(bad)
    with pytest.raises(ValueError):
        EmailConfig.model_validate(data)


def test_plain_security_is_fine_for_localhost_and_email_is_optional():
    EmailConfig(host="localhost", security="none", sender="a@localhost", recipients=["b@localhost"])
    assert HealOpsConfig().email is None


# ----- through HealOps -----


def system_with_email(tmp_path=None, **config):
    server = Server()
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": None,
        "policies": {
            **DEFAULT_CONFIG["policies"],
            "restart_component": {"verdict": "needs_approval", "cooldown_seconds": 0},
        },
        "email": {
            "host": "smtp.example.com",
            "sender": "healops@example.com",
            "recipients": ["me@example.com"],
            "username": "healops@example.com",
            "password_env": "HEALOPS_SMTP_PASSWORD",
        },
        **config,
    }
    system = HealOps(
        data,
        environ={"HEALOPS_SMTP_PASSWORD": PASSWORD},
        smtp_factory=server,
        sleep=lambda _s: None,
    )
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    return system, app, server


def test_a_waiting_approval_sends_one_email_with_the_command(capsys):
    system, app, server = system_with_email()
    app.inject(Fault.CRASH)
    for _ in range(5):
        system.tick()
    [message] = server.messages
    incident_id = system.pending_approvals()[0].incident.incident_id
    assert f"healops approve {incident_id[:8]}" in message.get_content()
    assert PASSWORD not in message.as_string()


def test_every_alert_is_also_kept_in_the_state_store():
    system, app, _ = system_with_email()
    app.inject(Fault.CRASH)
    for _ in range(5):
        system.tick()
    [stored] = system.store.alerts()
    assert stored.kind is AlertKind.APPROVAL_NEEDED
    assert stored.incident_type == "health_failure"
    assert stored.dry_run is False


def test_in_dry_run_nothing_is_emailed_but_the_alert_is_still_stored():
    system, app, server = system_with_email(dry_run=True)
    app.inject(Fault.CRASH)
    for _ in range(5):
        system.tick()
    assert server.messages == []
    assert any(a.dry_run for a in system.store.alerts())


def test_passing_your_own_notifiers_replaces_console_and_email_but_alerts_are_still_stored():
    recording = RecordingNotifier()
    server = Server()
    data = {
        **DEFAULT_CONFIG,
        "dry_run": False,
        "audit_log_path": None,
        "state_db_path": None,
        "policies": {
            **DEFAULT_CONFIG["policies"],
            "restart_component": {"verdict": "needs_approval", "cooldown_seconds": 0},
        },
        "email": {
            "host": "smtp.example.com",
            "sender": "a@example.com",
            "recipients": ["b@example.com"],
        },
    }
    system = HealOps(data, notifiers=[recording], smtp_factory=server, sleep=lambda _s: None)
    app = ToyApp()
    system.register("toy", app, component=COMPONENT)
    app.inject(Fault.CRASH)
    for _ in range(5):
        system.tick()
    assert len(recording.alerts) == 1
    assert server.messages == []
    assert len(system.store.alerts()) == 1


def test_the_commented_email_block_in_the_example_config_is_valid_when_uncommented():
    from pathlib import Path

    import yaml

    text = (Path(__file__).parent.parent / "examples" / "selfhealing.example.yaml").read_text(
        encoding="utf-8"
    )
    inside = text.split("# --- email (optional) ---")[1].split("# --- end email ---")[0]
    lines = [ln[2:] for ln in inside.splitlines() if ln.startswith(("# email:", "#   "))]
    active = [ln for ln in lines if ln.startswith(("email:", "  ")) and "$env" not in ln]
    config = HealOpsConfig.model_validate(yaml.safe_load("\n".join(active)))
    assert config.email.host == "smtp.gmail.com"
    assert config.email.password_env == "HEALOPS_SMTP_PASSWORD"