"""Sanitized public Event projection shared by REST and SSE."""

from ehai import JsonValue, json_dumps
from ehai.domain.events import Event

_PRIVATE_PAYLOAD_KEYS = frozenset(
    {"path", "cwd", "transcript", "thinking", "secret", "provider_request_id"}
)


def public_event_document(event: Event) -> dict[str, JsonValue]:
    document = event.to_dict()
    payload = document["payload"]
    if isinstance(payload, dict):
        document["payload"] = {
            key: item for key, item in payload.items() if key not in _PRIVATE_PAYLOAD_KEYS
        }
    return document


def public_event_json(event: Event) -> str:
    return json_dumps(public_event_document(event))
