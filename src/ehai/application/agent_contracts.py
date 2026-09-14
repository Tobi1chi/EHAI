"""Backend-neutral host tool contracts; no model loop or provider client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ehai import JsonValue, json_dumps, json_loads


class AgentExecutionError(RuntimeError):
    """Base error for one Built-in Agent Turn."""


class RecoverableToolError(AgentExecutionError):
    """A bounded Tool failure that is safe for the model to correct and retry."""

    def __init__(self, code: str, message: str) -> None:
        self.code = _text(code, "RecoverableToolError code")
        self.safe_message = _text(message, "RecoverableToolError message")
        super().__init__(self.safe_message)


class AgentCancelledError(AgentExecutionError):
    """Raised at a cooperative cancellation boundary."""


class UnknownToolError(RecoverableToolError):
    """Raised when a model requests a Tool outside the fixed ToolSet."""

    def __init__(self) -> None:
        super().__init__("unknown_tool", "Tool is not in this Session ToolSet")


class ModelRole(StrEnum):
    """Provider-neutral roles reconstructed from durable Session events."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True, init=False)
class ToolCall:
    """One strict custom Tool request emitted by a model."""

    call_id: str
    name: str
    _arguments_json: str = field(repr=False)

    def __init__(self, call_id: str, name: str, arguments: Mapping[str, JsonValue]) -> None:
        object.__setattr__(self, "call_id", _text(call_id, "ToolCall call_id"))
        object.__setattr__(self, "name", _text(name, "ToolCall name"))
        if not isinstance(arguments, Mapping):
            raise ValueError("ToolCall arguments must be a JSON object")
        object.__setattr__(self, "_arguments_json", json_dumps(dict(arguments)))

    @property
    def arguments(self) -> dict[str, JsonValue]:
        value = json_loads(self._arguments_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded at construction
            raise RuntimeError("stored ToolCall arguments are not an object")
        return value


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """An explicit host context injection, not stored model history."""

    role: ModelRole
    content: str
    acknowledge: Callable[[], None] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True, init=False)
class ToolDefinition:
    """One Tool fixed into a Session ToolSet at construction."""

    name: str
    description: str
    writes_workspace: bool
    ends_turn: bool
    _input_schema_json: str = field(repr=False)

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, JsonValue],
        *,
        writes_workspace: bool = False,
        ends_turn: bool = False,
    ) -> None:
        object.__setattr__(self, "name", _text(name, "ToolDefinition name"))
        object.__setattr__(self, "description", _text(description, "ToolDefinition description"))
        if not isinstance(input_schema, Mapping):
            raise ValueError("ToolDefinition input_schema must be a JSON object")
        object.__setattr__(self, "_input_schema_json", json_dumps(dict(input_schema)))
        if not isinstance(writes_workspace, bool):
            raise ValueError("ToolDefinition writes_workspace must be a boolean")
        object.__setattr__(self, "writes_workspace", writes_workspace)
        if not isinstance(ends_turn, bool):
            raise ValueError("ToolDefinition ends_turn must be a boolean")
        object.__setattr__(self, "ends_turn", ends_turn)

    @property
    def input_schema(self) -> dict[str, JsonValue]:
        value = json_loads(self._input_schema_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded at construction
            raise RuntimeError("stored ToolDefinition schema is not an object")
        return value


@dataclass(frozen=True, slots=True)
class ToolSet:
    """An immutable set of Tool definitions fixed for a Session."""

    definitions: tuple[ToolDefinition, ...]

    def __post_init__(self) -> None:
        definitions = tuple(self.definitions)
        if not definitions:
            raise ValueError("ToolSet requires at least one ToolDefinition")
        if len({item.name for item in definitions}) != len(definitions):
            raise ValueError("ToolSet contains duplicate Tool names")
        object.__setattr__(self, "definitions", definitions)

    def require(self, name: str) -> ToolDefinition:
        for definition in self.definitions:
            if definition.name == name:
                return definition
        raise UnknownToolError


class CancellationToken:
    """A cooperative token owned and closed by one ExecutionScope."""

    def __init__(self) -> None:
        self._cancelled = False
        self._closed = False
        self._cancelled_event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        if not self._closed:
            self._cancelled = True
            self._cancelled_event.set()

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise AgentCancelledError("Built-in Agent execution was cancelled")

    async def wait_cancelled(self) -> None:
        """Wait until cancellation is requested for this execution scope."""
        await self._cancelled_event.wait()

    def close(self) -> None:
        self._cancelled = True
        self._closed = True
        self._cancelled_event.set()


ToolHandler = Callable[[dict[str, JsonValue], CancellationToken], Awaitable[JsonValue]]


class ToolExecutor:
    """Execute only handlers matching the Session's fixed ToolSet."""

    def __init__(self, tool_set: ToolSet, handlers: Mapping[str, ToolHandler]) -> None:
        if set(handlers) != {item.name for item in tool_set.definitions}:
            raise ValueError("ToolExecutor handlers must exactly match the fixed ToolSet")
        self.tool_set = tool_set
        self._handlers = dict(handlers)
        self._closed = False

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> JsonValue:
        if self._closed:
            raise RuntimeError("ToolExecutor is closed")
        self.tool_set.require(call.name)
        cancellation.raise_if_cancelled()
        result = await self._handlers[call.name](call.arguments, cancellation)
        cancellation.raise_if_cancelled()
        return json_loads(json_dumps(result))

    async def aclose(self) -> None:
        self._closed = True


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value
