"""Convert HealOps records to JSON-safe data and back.

Pydantic does the work, so data coming back in (for example an LLM reply or a
line from the audit log) is validated on the way in, and the record's own
checks (`__post_init__`) still run.
"""

import json
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from healops.events import Incident, Observation
from healops.exceptions import RecordError
from healops.outcomes import ActionResult, PolicyDecision, VerificationResult
from healops.records import ActionProposal, Diagnosis

T = TypeVar("T")

RECORD_TYPES: tuple[type, ...] = (
    Observation,
    Incident,
    Diagnosis,
    ActionProposal,
    PolicyDecision,
    ActionResult,
    VerificationResult,
)

_ADAPTERS: dict[type, TypeAdapter] = {cls: TypeAdapter(cls) for cls in RECORD_TYPES}


def _adapter_for(record_type: type) -> TypeAdapter:
    try:
        return _ADAPTERS[record_type]
    except KeyError:
        raise RecordError(f"{record_type.__name__} is not a HealOps record type") from None


def record_to_dict(record: object) -> dict[str, Any]:
    """Return a JSON-safe dict: enums become their values, datetimes become ISO text."""
    adapter = _adapter_for(type(record))
    return adapter.dump_python(record, mode="json")


def record_from_dict(record_type: type[T], data: dict[str, Any]) -> T:
    """Build and validate a record of `record_type` from a dict."""
    adapter = _adapter_for(record_type)
    try:
        return adapter.validate_python(data)
    except (ValidationError, ValueError, TypeError) as error:
        raise RecordError(f"invalid {record_type.__name__}: {error}") from error


def record_to_json(record: object) -> str:
    """Return one compact JSON line (suitable for a JSONL audit log)."""
    return json.dumps(record_to_dict(record), separators=(",", ":"), sort_keys=True)


def record_from_json(record_type: type[T], text: str) -> T:
    """Parse JSON text into a validated record of `record_type`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RecordError(f"invalid JSON for {record_type.__name__}: {error}") from error
    if not isinstance(data, dict):
        raise RecordError(f"{record_type.__name__} JSON must be an object")
    return record_from_dict(record_type, data)