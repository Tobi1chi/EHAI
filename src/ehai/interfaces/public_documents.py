"""Canonical public DTO encoding shared by CLI and HTTP."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import cast

from ehai import JsonValue, format_utc_datetime
from ehai.domain.events import Event
from ehai.domain.planning import PlanNode
from ehai.interfaces.public_events import public_event_document


def public_json_value(value: object) -> JsonValue:
    """Encode public DTOs without exposing private domain fields."""
    if isinstance(value, Event):
        return public_event_document(value)
    if isinstance(value, datetime):
        return format_utc_datetime(value)
    if isinstance(value, Enum):
        return public_json_value(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return cast(JsonValue, value)
    if is_dataclass(value) and not isinstance(value, type):
        hidden_fields = (
            {"required_capabilities", "session_policy"} if isinstance(value, PlanNode) else set()
        )
        return {
            item.name: public_json_value(getattr(value, item.name))
            for item in fields(value)
            if not item.name.startswith("_") and item.name not in hidden_fields
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("public JSON object keys must be strings")
        return {key: public_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_json_value(item) for item in value]
    raise TypeError(f"unsupported public response value: {type(value).__name__}")
