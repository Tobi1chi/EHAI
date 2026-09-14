"""Bounded Web search/open/find tools for the shared Built-in Agent Runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from ehai import JsonValue, json_dumps, json_loads
from ehai.application.agent_contracts import (
    CancellationToken,
    RecoverableToolError,
    ToolDefinition,
    ToolHandler,
)

_MAX_WEB_RESULT_BYTES = 1024 * 1024


class WebProvider(Protocol):
    async def search(self, query: str) -> JsonValue: ...

    async def open(self, url: str) -> JsonValue: ...

    async def find(self, url: str, pattern: str) -> JsonValue: ...


class WebToolProvider:
    """Normalize one Web Provider into three fixed Agent tools."""

    def __init__(self, provider: WebProvider) -> None:
        self._provider = provider
        self.definitions = (
            _definition("web_search", "Search the configured Web provider", "query"),
            _definition("web_open", "Open one Web result URL", "url"),
            _definition("web_find", "Find text within one opened Web URL", "url", "pattern"),
        )
        self.handlers: Mapping[str, ToolHandler] = {
            "web_search": self._search,
            "web_open": self._open,
            "web_find": self._find,
        }

    async def _search(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        return _bounded(
            "search", await self._invoke(self._provider.search, _text(arguments, "query"))
        )

    async def _open(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        return _bounded("open", await self._invoke(self._provider.open, _text(arguments, "url")))

    async def _find(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        return _bounded(
            "find",
            await self._invoke(
                self._provider.find,
                _text(arguments, "url"),
                _text(arguments, "pattern"),
            ),
        )

    @staticmethod
    async def _invoke(operation: Callable[..., Awaitable[JsonValue]], *arguments: str) -> JsonValue:
        try:
            result = await operation(*arguments)
        except RecoverableToolError:
            raise
        except Exception as error:
            raise RecoverableToolError("web_error", "Web provider request failed") from error
        return result


def _definition(name: str, description: str, *fields: str) -> ToolDefinition:
    return ToolDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": {
                field: {"type": "string", "minLength": 1, "maxLength": 2048} for field in fields
            },
            "required": list(fields),
            "additionalProperties": False,
        },
    )


def _text(arguments: Mapping[str, JsonValue], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RecoverableToolError("invalid_arguments", f"{name} must be non-blank text")
    return value.strip()


def _bounded(operation: str, value: JsonValue) -> JsonValue:
    serialized = json_dumps(value)
    if len(serialized.encode("utf-8")) > _MAX_WEB_RESULT_BYTES:
        raise RecoverableToolError("output_limit", "Web result exceeds the Tool output limit")
    return {"operation": operation, "result": json_loads(serialized)}
