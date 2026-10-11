"""Hide well-known secrets before records are written to logs.

This is a best-effort safety net, not a guarantee. It catches:
  * dictionary keys that look like secrets (password, token, api_key, ...), and
  * a few common secret shapes inside text (key=value pairs, Bearer tokens,
    sk-... keys, AWS access key ids, user:password@ in URLs).
It does NOT catch secrets written as plain sentences or unknown formats.
The best protection is still not putting raw input data into records.
"""

import json
import re
from typing import Any

from healops.serialization import record_to_dict

REDACTED = "[REDACTED]"

_SENSITIVE_WORDS = frozenset(
    {"password", "passwd", "pwd", "secret", "token", "authorization", "credential", "credentials"}
)
_SENSITIVE_COMPOUNDS = ("api_key", "apikey", "private_key", "access_key", "secret_key")

_KEY_VALUE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization)"
    r"(\s*[=:]\s*)(?:Bearer\s+)?([^\s,;&\"']+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_AWS_KEY_ID = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")


def is_sensitive_key(key: str) -> bool:
    """Return True if a dictionary key name looks like it holds a secret."""
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if set(normalized.split("_")) & _SENSITIVE_WORDS:
        return True
    return any(compound in normalized for compound in _SENSITIVE_COMPOUNDS)


def redact_text(text: str) -> str:
    """Replace well-known secret shapes inside `text`."""
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _SK_KEY.sub(REDACTED, text)
    text = _AWS_KEY_ID.sub(REDACTED, text)
    return _URL_CREDENTIALS.sub(f"{REDACTED}@", text)


def redact_value(value: Any) -> Any:
    """Return a redacted copy of JSON-like data; the input is never modified."""
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redacted_dict(record: object) -> dict[str, Any]:
    """JSON-safe dict of a record with secrets hidden."""
    return redact_value(record_to_dict(record))


def redacted_json(record: object) -> str:
    """One compact JSON line of a record with secrets hidden (for the audit log)."""
    return json.dumps(redacted_dict(record), separators=(",", ":"), sort_keys=True)