"""Provider-neutral sanitization for bounded public diagnostic payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping

from ehai import JsonValue, json_dumps, json_loads

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_QUOTED_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|secret|authorization)"
    r"[\"']?\s*[:=]\s*)([\"']).*?\2"
)
_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|secret|authorization)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+"
)
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]+")


def redact_sensitive_text(value: str) -> str:
    """Redact recognizable credential forms without requiring the secret value."""
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _QUOTED_SECRET_PATTERN.sub(r"\1\2[REDACTED]\2", redacted)
    redacted = _SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    return _KEY_PATTERN.sub("[REDACTED]", redacted)


def bounded_redacted_text(value: str | None, *, max_bytes: int = 500) -> str | None:
    """Redact a diagnostic string and bound its UTF-8 representation."""
    if value is None:
        return None
    if type(max_bytes) is not int or max_bytes < 32:
        raise ValueError("max_bytes must be an integer of at least 32")
    return _bounded_utf8(redact_sensitive_text(value), max_bytes)


def sanitize_json_object(
    value: Mapping[str, JsonValue],
    *,
    max_bytes: int,
) -> tuple[dict[str, JsonValue], bool]:
    """Return a redacted JSON object whose encoded representation is explicitly bounded."""
    if type(max_bytes) is not int or max_bytes < 256:
        raise ValueError("max_bytes must be an integer of at least 256")
    redacted = redact_sensitive_text(json_dumps(dict(value)))
    if len(redacted.encode("utf-8")) <= max_bytes:
        parsed = json_loads(redacted)
        if isinstance(parsed, dict):
            return parsed, False
    preview = _bounded_utf8(redacted, max_bytes // 2)
    return {"preview": preview, "truncated": True}, True


def _bounded_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "...[truncated]"
    available = max(0, limit - len(marker.encode("utf-8")))
    return encoded[:available].decode("utf-8", errors="ignore") + marker
