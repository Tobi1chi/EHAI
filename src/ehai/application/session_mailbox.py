"""Persistent Session Mailbox use cases and Agent tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import monotonic
from typing import Protocol

from ehai import ID, JsonValue, normalize_id
from ehai.application.builtin_agent import (
    CancellationToken,
    ModelMessage,
    ModelRole,
    RecoverableToolError,
    ToolDefinition,
    ToolHandler,
)
from ehai.domain.session_messages import SessionMessage


class SessionMailboxRepository(Protocol):
    def list_session_ids(self) -> tuple[ID, ...]: ...

    def add(self, message: SessionMessage) -> None: ...

    def list_for_target(
        self,
        target_session_id: ID,
        *,
        correlation_id: str | None = None,
        include_read: bool = True,
    ) -> tuple[SessionMessage, ...]: ...

    def save(self, message: SessionMessage) -> None: ...


class SessionMailbox:
    """Coordinate messages without owning Attempts, Artifacts, or domain state."""

    def __init__(self, repository: SessionMailboxRepository) -> None:
        self._repository = repository

    def sessions(self, current_session_id: ID) -> tuple[ID, ...]:
        current = normalize_id(current_session_id)
        return tuple(item for item in self._repository.list_session_ids() if item != current)

    def send(
        self,
        source_session_id: ID,
        target_session_id: ID,
        correlation_id: str,
        content: str,
    ) -> SessionMessage:
        source = normalize_id(source_session_id)
        target = normalize_id(target_session_id)
        sessions = frozenset(self._repository.list_session_ids())
        if source not in sessions or target not in sessions:
            raise RecoverableToolError(
                "session_not_found", "Messages may target only existing Sessions"
            )
        if source == target:
            raise RecoverableToolError("invalid_target", "Messages must target a different Session")
        message = SessionMessage(source, target, correlation_id, content)
        self._repository.add(message)
        return message

    def read(
        self,
        target_session_id: ID,
        *,
        correlation_id: str | None = None,
        mark_read: bool = True,
        include_read: bool = True,
    ) -> tuple[SessionMessage, ...]:
        messages = self._repository.list_for_target(
            normalize_id(target_session_id),
            correlation_id=correlation_id,
            include_read=include_read,
        )
        if mark_read:
            for message in messages:
                self._repository.save(message.mark_read())
        return messages

    def inject(self, target_session_id: ID) -> tuple[ModelMessage, ...]:
        messages = tuple(
            message
            for message in self._repository.list_for_target(
                normalize_id(target_session_id), include_read=False
            )
            if message.status.value == "pending"
        )
        for message in messages:
            self._repository.save(message.deliver())
        return tuple(
            ModelMessage(
                ModelRole.USER,
                (
                    "SESSION_MESSAGE "
                    f"source={message.source_session_id} correlation={message.correlation_id} "
                    f"message_id={message.message_id}: {message.content}"
                ),
            )
            for message in messages
        )

    async def wait(
        self,
        target_session_id: ID,
        *,
        correlation_id: str | None,
        timeout_seconds: float,
        cancellation: CancellationToken,
    ) -> tuple[SessionMessage, ...]:
        deadline = monotonic() + min(max(timeout_seconds, 0.0), 30.0)
        while True:
            cancellation.raise_if_cancelled()
            messages = self.read(
                target_session_id,
                correlation_id=correlation_id,
                mark_read=True,
                include_read=False,
            )
            if messages or monotonic() >= deadline:
                return messages
            await asyncio.sleep(0.1)


class SessionMailboxToolProvider:
    """Bind Mailbox tools to one existing Session identity."""

    def __init__(self, mailbox: SessionMailbox, current_session_id: ID) -> None:
        self._mailbox = mailbox
        self._current_session_id = normalize_id(current_session_id)
        self.definitions = (
            _definition("session_list", "List existing Sessions available for messaging", {}),
            _definition(
                "session_send",
                "Send one durable message to an existing Session",
                {
                    "target_session_id": {"type": "string", "minLength": 1},
                    "correlation_id": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "minLength": 1, "maxLength": 16_000},
                },
            ),
            _definition(
                "session_read",
                "Read durable messages for the current Session",
                {"correlation_id": {"type": ["string", "null"]}},
            ),
            _definition(
                "session_wait",
                "Wait up to 30 seconds for a durable Session message",
                {
                    "correlation_id": {"type": ["string", "null"]},
                    "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                },
            ),
        )
        self.handlers: Mapping[str, ToolHandler] = {
            "session_list": self._list,
            "session_send": self._send,
            "session_read": self._read,
            "session_wait": self._wait,
        }

    async def _list(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        del arguments
        cancellation.raise_if_cancelled()
        return {"session_ids": list(self._mailbox.sessions(self._current_session_id))}

    async def _send(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        message = self._mailbox.send(
            self._current_session_id,
            ID(_text(arguments, "target_session_id")),
            _text(arguments, "correlation_id"),
            _text(arguments, "content"),
        )
        return _document(message)

    async def _read(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        messages = self._mailbox.read(
            self._current_session_id,
            correlation_id=_optional_text(arguments, "correlation_id"),
        )
        return {"messages": [_document(message) for message in messages]}

    async def _wait(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        timeout = arguments.get("timeout_seconds")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise RecoverableToolError("invalid_arguments", "timeout_seconds must be a number")
        messages = await self._mailbox.wait(
            self._current_session_id,
            correlation_id=_optional_text(arguments, "correlation_id"),
            timeout_seconds=float(timeout),
            cancellation=cancellation,
        )
        return {"messages": [_document(message) for message in messages]}


def _definition(
    name: str,
    description: str,
    properties: Mapping[str, JsonValue],
) -> ToolDefinition:
    return ToolDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": dict(properties),
            "required": list(properties),
            "additionalProperties": False,
        },
    )


def _document(message: SessionMessage) -> dict[str, JsonValue]:
    return {
        "message_id": message.message_id,
        "source_session_id": message.source_session_id,
        "target_session_id": message.target_session_id,
        "correlation_id": message.correlation_id,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "status": message.status.value,
    }


def _text(arguments: Mapping[str, JsonValue], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RecoverableToolError("invalid_arguments", f"{name} must be non-blank text")
    return value.strip()


def _optional_text(arguments: Mapping[str, JsonValue], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    return _text(arguments, name)
