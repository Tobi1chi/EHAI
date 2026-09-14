"""Host role audit records, including read compatibility with retired Built-in logs.

These records never derive model context. Native Pi sessions own that history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id, utc_now
from ehai.application.agent_contracts import ToolCall


class AgentTraceStateError(RuntimeError):
    """Invalid retained host audit data."""


class AgentTraceEventType(StrEnum):
    """Append-only facts for Built-in Agent Turn and Step replay."""

    TURN_STARTED = "turn/start"
    MESSAGE_RECEIVED = "message/received"
    STEP_STARTED = "step/start"
    MODEL_MESSAGE = "model/message"
    MODEL_TRANSPORT = "model/transport"
    TOOL_CALLED = "tool/call"
    TOOL_RESULT = "tool/result"
    TOOL_ERROR = "tool/error"
    STEP_ENDED = "step/end"
    FINAL = "final"
    TURN_ENDED = "turn/end"
    BACKEND_EVENT = "backend/event"
    BACKEND_ERROR = "backend/error"


@dataclass(frozen=True, slots=True, init=False)
class AgentTraceEvent:
    """One immutable Session fact with canonical JSON payload."""

    agent_session_ref_id: ID
    attempt_id: ID
    sequence: int
    type: AgentTraceEventType
    occurred_at: datetime
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        agent_session_ref_id: ID,
        attempt_id: ID,
        sequence: int,
        event_type: AgentTraceEventType,
        payload: Mapping[str, JsonValue],
        occurred_at: datetime | None = None,
    ) -> None:
        object.__setattr__(self, "agent_session_ref_id", normalize_id(agent_session_ref_id))
        object.__setattr__(self, "attempt_id", normalize_id(attempt_id))
        if type(sequence) is not int or sequence < 1:
            raise ValueError("AgentTraceEvent sequence must be positive")
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "type", AgentTraceEventType(event_type))
        timestamp = utc_now() if occurred_at is None else occurred_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("AgentTraceEvent occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", timestamp.astimezone(UTC))
        if not isinstance(payload, Mapping):
            raise ValueError("AgentTraceEvent payload must be a JSON object")
        object.__setattr__(self, "_payload_json", json_dumps(dict(payload)))

    @property
    def payload(self) -> dict[str, JsonValue]:
        value = json_loads(self._payload_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded at construction
            raise RuntimeError("stored AgentTraceEvent payload is not an object")
        return value


class AgentTrace:
    """One append-only Built-in Session containing sequential Attempt Turns."""

    def __init__(
        self,
        agent_session_ref_id: ID,
        events: tuple[AgentTraceEvent, ...] = (),
    ) -> None:
        self.agent_session_ref_id = normalize_id(agent_session_ref_id)
        self.events: tuple[AgentTraceEvent, ...] = ()
        self._closed = False
        for event in events:
            self._append_existing(event)

    def append(
        self,
        attempt_id: ID,
        event_type: AgentTraceEventType,
        payload: Mapping[str, JsonValue],
        *,
        occurred_at: datetime | None = None,
    ) -> AgentTraceEvent:
        if self._closed:
            raise RuntimeError("AgentTrace is closed")
        event = AgentTraceEvent(
            agent_session_ref_id=self.agent_session_ref_id,
            attempt_id=attempt_id,
            sequence=len(self.events) + 1,
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at,
        )
        self._append_existing(event)
        return event

    def has_turn(self, attempt_id: ID) -> bool:
        normalized = normalize_id(attempt_id)
        return any(
            event.attempt_id == normalized and event.type is AgentTraceEventType.TURN_STARTED
            for event in self.events
        )

    def is_turn_complete(self, attempt_id: ID) -> bool:
        normalized = normalize_id(attempt_id)
        return any(
            event.attempt_id == normalized and event.type is AgentTraceEventType.TURN_ENDED
            for event in self.events
        )

    def final_text(self, attempt_id: ID) -> str | None:
        normalized = normalize_id(attempt_id)
        for event in reversed(self.events):
            if event.attempt_id == normalized and event.type is AgentTraceEventType.FINAL:
                value = event.payload.get("text")
                return value if isinstance(value, str) else None
        return None

    def incomplete_write_call(self, attempt_id: ID) -> str | None:
        pending = self.incomplete_tool_call(attempt_id)
        return None if pending is None or not pending[1] else pending[0].call_id

    def incomplete_tool_call(self, attempt_id: ID) -> tuple[ToolCall, bool] | None:
        """Return the latest durable Tool call without a result or error."""
        normalized = normalize_id(attempt_id)
        pending: dict[str, tuple[ToolCall, bool]] = {}
        for event in self.events:
            if event.attempt_id != normalized:
                continue
            call_id = event.payload.get("call_id")
            if event.type is AgentTraceEventType.TOOL_CALLED and isinstance(call_id, str):
                arguments = event.payload.get("arguments")
                name = event.payload.get("name")
                if isinstance(arguments, dict) and isinstance(name, str):
                    pending[call_id] = (
                        ToolCall(call_id, name, arguments),
                        event.payload.get("writes_workspace") is True,
                    )
            elif event.type in {
                AgentTraceEventType.TOOL_RESULT,
                AgentTraceEventType.TOOL_ERROR,
            } and isinstance(call_id, str):
                pending.pop(call_id, None)
        return next(reversed(pending.values()), None) if pending else None

    def close(self) -> None:
        self._closed = True

    def _append_existing(self, event: AgentTraceEvent) -> None:
        if event.agent_session_ref_id != self.agent_session_ref_id:
            raise AgentTraceStateError("SessionEvent belongs to another AgentSessionRef")
        if event.sequence != len(self.events) + 1:
            raise AgentTraceStateError("SessionEvent sequence is not contiguous")
        self.events = (*self.events, event)


@runtime_checkable
class AgentTraceStore(Protocol):
    """Append-only durable storage for AgentTraceEvent batches."""

    def load(self, agent_session_ref_id: ID) -> AgentTrace:
        """Load the durable Session history, or an empty existing Session."""
        ...

    def append(
        self,
        agent_session_ref_id: ID,
        expected_sequence: int,
        events: tuple[AgentTraceEvent, ...],
    ) -> None:
        """Atomically append a contiguous event batch with optimistic sequence check."""
        ...
