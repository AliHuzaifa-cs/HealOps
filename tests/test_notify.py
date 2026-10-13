from datetime import datetime

import pytest

from healops.healing.notify import (
    MAX_ERROR_LENGTH,
    Alert,
    AlertKind,
    ConsoleNotifier,
    NotificationResult,
    Notifier,
    RecordingNotifier,
    SafeNotifier,
    redact_alert,
)


def alert(**overrides):
    values = {
        "kind": AlertKind.APPROVAL_NEEDED,
        "incident_id": "inc-1",
        "app_name": "demo-app",
        "title": "rollback_model needs your approval",
        "details": ("confidence 0.40 is below 0.60",),
    }
    values.update(overrides)
    return Alert(**values)


class BrokenNotifier:
    def __init__(self, message="connection refused"):
        self._message = message

    def notify(self, alert):
        raise RuntimeError(self._message)


def test_alert_has_a_utc_time_by_default():
    assert alert().created_at.tzinfo is not None


@pytest.mark.parametrize("field", ["incident_id", "app_name", "title"])
def test_alert_text_fields_must_not_be_blank(field):
    with pytest.raises(ValueError):
        alert(**{field: "  "})


def test_alert_kind_must_be_an_alert_kind():
    with pytest.raises(TypeError):
        alert(kind="escalated")


def test_alert_rejects_a_naive_time():
    with pytest.raises(ValueError):
        alert(created_at=datetime(2026, 10, 12, 12, 0))


def test_a_failed_notification_must_explain_itself():
    with pytest.raises(ValueError):
        NotificationResult("EmailNotifier", delivered=False)
    assert NotificationResult("EmailNotifier", delivered=True).error is None


def test_any_object_with_notify_is_a_notifier():
    notifier: Notifier = RecordingNotifier()
    notifier.notify(alert())
    assert len(notifier.alerts) == 1


def test_console_notifier_writes_readable_lines():
    lines = []
    ConsoleNotifier(write=lines.append).notify(alert())
    assert lines == [
        "[ALERT approval_needed] demo-app / incident inc-1",
        "rollback_model needs your approval",
        "  - confidence 0.40 is below 0.60",
    ]


def test_safe_notifier_delivers_to_every_notifier():
    first, second = RecordingNotifier(), RecordingNotifier()
    results = SafeNotifier([first, second]).send(alert())
    assert [r.delivered for r in results] == [True, True]
    assert len(first.alerts) == len(second.alerts) == 1


def test_one_broken_channel_does_not_stop_the_others_or_raise():
    good = RecordingNotifier()
    results = SafeNotifier([BrokenNotifier(), good]).send(alert())
    assert [r.delivered for r in results] == [False, True]
    assert len(good.alerts) == 1


def test_a_failure_is_reported_with_the_notifier_name_and_reason():
    result = SafeNotifier([BrokenNotifier("connection refused")]).send(alert())[0]
    assert result.notifier == "BrokenNotifier"
    assert result.delivered is False
    assert "connection refused" in result.error


def test_secrets_are_hidden_before_any_notifier_sees_the_alert():
    recorder = RecordingNotifier()
    SafeNotifier([recorder]).send(
        alert(title="restart failed: password=hunter2", details=("token=abc123XYZ",))
    )
    sent = recorder.alerts[0]
    assert "hunter2" not in sent.title
    assert "abc123XYZ" not in sent.details[0]


def test_secrets_in_error_messages_are_hidden_too():
    result = SafeNotifier([BrokenNotifier("login failed password=hunter2")]).send(alert())[0]
    assert "hunter2" not in result.error


def test_very_long_errors_are_shortened():
    result = SafeNotifier([BrokenNotifier("x" * 1000)]).send(alert())[0]
    assert len(result.error) == MAX_ERROR_LENGTH


def test_no_notifiers_means_no_results():
    assert SafeNotifier([]).send(alert()) == ()


def test_redact_alert_returns_a_copy_and_keeps_the_original():
    original = alert(title="password=hunter2")
    cleaned = redact_alert(original)
    assert "hunter2" in original.title
    assert "hunter2" not in cleaned.title
    assert cleaned.incident_id == original.incident_id