import json
from datetime import UTC, datetime

import pytest

from healops import audit
from healops.audit import (
    AuditEvent,
    AuditLog,
    AuditStage,
    JsonlAuditLog,
    MemoryAuditLog,
    build_event,
    read_events,
)
from healops.events import Observation
from healops.exceptions import AuditError, AuditReadError, AuditWriteError, HealOpsError
from healops.types import ObservationKind


def event(**overrides):
    values = {
        "stage": AuditStage.DIAGNOSED,
        "incident_id": "inc-1",
        "app_name": "demo-app",
        "record_type": "Diagnosis",
        "payload": {"summary": "slow"},
    }
    values.update(overrides)
    return AuditEvent(**values)


def observation(**details):
    return Observation(
        app_name="demo-app",
        kind=ObservationKind.METRIC,
        name="latency_ms",
        value=950.0,
        details=details,
    )


def test_an_event_gets_an_id_and_a_utc_time():
    first, second = event(), event()
    assert first.event_id != second.event_id
    assert first.created_at.tzinfo is not None


@pytest.mark.parametrize("field", ["incident_id", "app_name", "record_type"])
def test_event_text_fields_must_not_be_blank(field):
    with pytest.raises(ValueError):
        event(**{field: " "})


def test_stage_must_be_an_audit_stage():
    with pytest.raises(TypeError):
        event(stage="diagnosed")


def test_a_naive_time_is_rejected():
    with pytest.raises(ValueError):
        event(created_at=datetime(2026, 10, 16, 12, 0))


def test_an_event_survives_a_dict_round_trip():
    original = event(note="why", created_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC))
    assert AuditEvent.from_dict(original.to_dict()) == original


def test_building_an_event_from_a_record_names_the_type_and_hides_secrets():
    built = build_event(
        AuditStage.INCIDENT_OPENED,
        "inc-1",
        "demo-app",
        record=observation(source="probe", api_key="sk-supersecretvalue1234"),
    )
    assert built.record_type == "Observation"
    assert built.payload["details"] == {"source": "probe", "api_key": "[REDACTED]"}
    assert built.payload["value"] == 950.0


def test_building_an_event_from_plain_data_hides_secrets_too():
    built = build_event(
        AuditStage.APPROVAL_RESOLVED,
        "inc-1",
        "demo-app",
        data={"approver": "ali", "token": "abc123XYZ"},
        note="approved; password=hunter2",
    )
    assert built.record_type == "data"
    assert built.payload["token"] == "[REDACTED]"
    assert "hunter2" not in built.note


def test_memory_log_keeps_events_in_order():
    log = MemoryAuditLog()
    first, second = event(), event(stage=AuditStage.PROPOSED)
    log.append(first)
    log.append(second)
    assert log.events == [first, second]


def test_logs_follow_the_audit_log_protocol():
    logs: list[AuditLog] = [MemoryAuditLog(), JsonlAuditLog("unused.jsonl")]
    assert len(logs) == 2


def test_each_event_is_one_line_of_valid_json(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(event())
    log.append(event(stage=AuditStage.PROPOSED))
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["stage"] == "proposed"


def test_the_log_is_append_only_across_instances(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(event())
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    JsonlAuditLog(path).append(event(stage=AuditStage.ESCALATED))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == first_line
    assert len(lines) == 2


def test_missing_folders_are_created(tmp_path):
    path = tmp_path / "deep" / "er" / "audit.jsonl"
    JsonlAuditLog(path).append(event())
    assert path.exists()


def test_events_can_be_read_back_exactly(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(path)
    written = [event(), event(stage=AuditStage.VERIFIED, note="ok")]
    for item in written:
        log.append(item)
    assert read_events(path) == written


def test_a_newline_in_a_note_does_not_break_the_one_line_rule(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(event(note="line one\nline two"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert read_events(path)[0].note == "line one\nline two"


def test_non_english_text_is_written_and_read_correctly(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(event(note="masla hal ho gaya \u2713"))
    assert read_events(path)[0].note == "masla hal ho gaya \u2713"


def test_secrets_are_not_on_disk_even_if_a_payload_was_built_by_hand(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(event(payload={"password": "hunter2", "note": "token=abc123XYZ"}))
    raw = path.read_text(encoding="utf-8")
    assert "hunter2" not in raw
    assert "abc123XYZ" not in raw


def test_every_append_is_forced_to_disk(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(audit.os, "fsync", lambda fileno: calls.append(fileno))
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(event())
    log.append(event())
    assert len(calls) == 2


def test_a_write_failure_is_an_audit_write_error(tmp_path):
    log = JsonlAuditLog(tmp_path)  # a folder cannot be opened as a file
    with pytest.raises(AuditWriteError):
        log.append(event())


def test_a_damaged_line_is_reported_with_its_number(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(event())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json\n")
    with pytest.raises(AuditReadError, match="line 2"):
        read_events(path)


def test_a_missing_file_is_an_audit_read_error(tmp_path):
    with pytest.raises(AuditReadError):
        read_events(tmp_path / "nope.jsonl")


def test_blank_lines_are_ignored_when_reading(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(event())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")
    assert len(read_events(path)) == 1


def test_audit_errors_share_one_base_class():
    assert issubclass(AuditWriteError, AuditError)
    assert issubclass(AuditReadError, AuditError)
    assert issubclass(AuditError, HealOpsError)