import json
from datetime import UTC, datetime

import pytest

from healops.events import Observation
from healops.outcomes import ActionResult
from healops.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_text,
    redact_value,
    redacted_dict,
    redacted_json,
)
from healops.serialization import record_to_dict
from healops.types import ObservationKind

T1 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "key",
    ["password", "api_key", "API-Key", "X-Api-Key", "db_password", "Authorization",
     "auth_token", "client_secret", "private_key", "credentials"],
)
def test_secret_looking_keys_are_detected(key):
    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key", ["latency_ms", "max_tokens", "source", "incident_id", "model_version", "monkey"]
)
def test_ordinary_keys_are_not_flagged(key):
    assert not is_sensitive_key(key)


def test_values_under_secret_keys_are_hidden():
    data = {"source": "probe-1", "api_key": "sk-abc", "latency_ms": 950}
    assert redact_value(data) == {
        "source": "probe-1",
        "api_key": REDACTED,
        "latency_ms": 950,
    }


def test_a_secret_key_hides_everything_below_it():
    data = {"credentials": {"user": "bob", "pass": "x"}}
    assert redact_value(data) == {"credentials": REDACTED}


def test_nested_dicts_and_lists_are_walked():
    data = {"items": [{"token": "abc"}, {"name": "ok"}]}
    assert redact_value(data) == {"items": [{"token": REDACTED}, {"name": "ok"}]}


def test_the_original_data_is_not_changed():
    data = {"password": "hunter2"}
    redact_value(data)
    assert data == {"password": "hunter2"}


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("connect failed: password=hunter2", "hunter2"),
        ("db_password: hunter2 rejected", "hunter2"),
        ("token=abc123XYZ", "abc123XYZ"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("key is sk-abcdefghijklmnop1234", "sk-abcdefghijklmnop1234"),
        ("aws AKIAABCDEFGHIJKLMNOP used", "AKIAABCDEFGHIJKLMNOP"),
        ("postgres://admin:s3cret@db.local:5432/app", "s3cret"),
    ],
)
def test_common_secret_shapes_in_text_are_hidden(text, secret):
    cleaned = redact_text(text)
    assert secret not in cleaned
    assert REDACTED in cleaned


def test_text_without_secrets_is_left_alone():
    text = "latency rose from 120ms to 950ms after the model reload"
    assert redact_text(text) == text


def test_context_around_a_secret_is_kept():
    assert redact_text("login failed: password=hunter2 for user bob") == (
        f"login failed: password={REDACTED} for user bob"
    )


def test_known_gap_plain_sentences_are_not_detected():
    # Documented limit: no "=" or ":" and no known format, so it is not caught.
    assert "hunter2" in redact_text("the password is hunter2")


def test_record_details_are_hidden_but_structure_survives():
    obs = Observation(
        app_name="demo-app",
        kind=ObservationKind.METRIC,
        name="latency_ms",
        value=950.0,
        timestamp=T1,
        details={"source": "probe-1", "api_key": "sk-supersecretvalue1234"},
    )
    data = redacted_dict(obs)
    assert data["details"] == {"source": "probe-1", "api_key": REDACTED}
    assert data["value"] == 950.0
    assert "supersecret" not in redacted_json(obs)


def test_free_text_fields_are_cleaned_too():
    result = ActionResult(
        proposal_id="prop-1",
        dry_run=False,
        completed=False,
        message="restart failed: password=hunter2",
        started_at=T1,
        ended_at=T1,
    )
    assert "hunter2" not in redacted_json(result)


def test_records_without_secrets_serialize_unchanged():
    obs = Observation(
        app_name="demo-app",
        kind=ObservationKind.METRIC,
        name="latency_ms",
        value=950.0,
        timestamp=T1,
    )
    assert redacted_dict(obs) == record_to_dict(obs)


def test_redacted_json_is_one_valid_json_line():
    obs = Observation(
        app_name="demo-app",
        kind=ObservationKind.HEALTH,
        name="alive",
        value=True,
        timestamp=T1,
        details={"token": "abc"},
    )
    line = redacted_json(obs)
    assert "\n" not in line
    assert json.loads(line)["details"] == {"token": REDACTED}