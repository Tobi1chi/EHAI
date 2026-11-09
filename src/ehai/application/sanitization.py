"""Provider-neutral sanitization for bounded public diagnostic payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping

from ehai import JsonValue, json_dumps

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
_TYPESAFE_KEY_PATTERN = re.compile(r"\bapikey_[A-Za-z0-9]+_[A-Za-z0-9]+")


def redact_sensitive_text(value: str) -> str:
    """Redact recognizable credential forms without requiring the secret value."""
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _QUOTED_SECRET_PATTERN.sub(r"\1\2[REDACTED]\2", redacted)
    redacted = _SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    return _TYPESAFE_KEY_PATTERN.sub("[REDACTED]", _KEY_PATTERN.sub("[REDACTED]", redacted))


def redact_secret_bytes(value: bytes, secret_values: tuple[str, ...] = ()) -> bytes:
    """Redact known secret values and recognizable credential forms from process output."""
    text = value.decode("utf-8", errors="replace")
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return redact_sensitive_text(text).encode("utf-8")


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

    def redact(item: JsonValue, key: str = "") -> JsonValue:
        if (
            isinstance(item, (str, int, float))
            and not isinstance(item, bool)
            and key.lower().endswith(
                (
                    "api_key",
                    "api-key",
                    "apikey",
                    "access_token",
                    "access-token",
                    "accesstoken",
                    "token",
                    "password",
                    "secret",
                    "authorization",
                )
            )
        ):
            return "[REDACTED]"
        if isinstance(item, str):
            return redact_sensitive_text(item)
        if isinstance(item, dict):
            return {name: redact(member, name) for name, member in item.items()}
        if isinstance(item, list):
            return [redact(member) for member in item]
        return item

    redacted = {key: redact(member, key) for key, member in value.items()}
    serialized = json_dumps(redacted)
    if len(serialized.encode("utf-8")) <= max_bytes:
        return redacted, False
    preview = _bounded_utf8(serialized, max_bytes // 2)
    return {"preview": preview, "truncated": True}, True


def _bounded_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "...[truncated]"
    available = max(0, limit - len(marker.encode("utf-8")))
    return encoded[:available].decode("utf-8", errors="ignore") + marker
