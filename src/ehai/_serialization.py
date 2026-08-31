"""UTC timestamp and deterministic JSON conventions."""

import json
import math
from datetime import UTC, datetime
from typing import Never, TypeGuard

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def format_utc_datetime(value: datetime) -> str:
    """Serialize an aware datetime as RFC 3339 UTC with fixed microseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")

    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc_datetime(value: str) -> datetime:
    """Parse an offset-aware RFC 3339 timestamp and normalize it to UTC."""
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"invalid RFC 3339 timestamp: {value!r}") from error

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")

    return parsed.astimezone(UTC)


def json_dumps(value: JsonValue) -> str:
    """Encode a JSON value as deterministic, compact UTF-8 text."""
    if not _is_json_value(value):
        raise ValueError("value contains data that JSON cannot represent")

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def json_loads(document: str) -> JsonValue:
    """Decode strict JSON text, rejecting non-standard numeric constants."""
    value: object = json.loads(document, parse_constant=_reject_json_constant)
    if not _is_json_value(value):
        raise ValueError("document does not contain a supported JSON value")
    return value


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False
