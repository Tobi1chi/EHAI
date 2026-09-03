"""Durable P2 Built-in Agent spine driven through a ModelClient port."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id, utc_now
from ehai.domain.workers import BuiltinExecutionRef


class BuiltinAgentError(RuntimeError):
    """Base error for one Built-in Agent Turn."""


class RecoverableToolError(BuiltinAgentError):
    """A bounded Tool failure that is safe for the model to correct and retry."""

    def __init__(self, code: str, message: str) -> None:
        self.code = _text(code, "RecoverableToolError code")
        self.safe_message = _text(message, "RecoverableToolError message")
        super().__init__(self.safe_message)


class AgentCancelledError(BuiltinAgentError):
    """Raised at a cooperative cancellation boundary."""


class UnknownToolError(RecoverableToolError):
    """Raised when a model requests a Tool outside the fixed ToolSet."""

    def __init__(self) -> None:
        super().__init__("unknown_tool", "Tool is not in this Session ToolSet")


class IncompleteWriteToolError(BuiltinAgentError):
    """Raised when recovery finds a write Tool with an unknown outcome."""


class BuiltinSessionStateError(BuiltinAgentError):
    """Raised when append-only Session events violate Turn/Step ordering."""


class AgentBudgetExceededError(BuiltinAgentError):
    """Raised before a Built-in Agent exceeds its configured execution budget."""


@dataclass(frozen=True, slots=True)
class AgentBudget:
    """Finite Step, Tool, wall-clock, and output limits for one Turn."""

    max_steps: int
    max_tool_calls: int
    wall_clock_seconds: float
    max_output_bytes: int

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_tool_calls", "max_output_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"AgentBudget {name} must be a positive integer")
        if (
            not isinstance(self.wall_clock_seconds, (int, float))
            or isinstance(self.wall_clock_seconds, bool)
            or self.wall_clock_seconds <= 0
        ):
            raise ValueError("AgentBudget wall_clock_seconds must be positive")


DEFAULT_AGENT_BUDGET = AgentBudget(32, 64, 600.0, 8 * 1024 * 1024)


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
    """One model-visible message reconstructed from durable events."""

    role: ModelRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", ModelRole(self.role))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not isinstance(self.content, str):
            raise ValueError("ModelMessage content must be text")
        if self.role is ModelRole.ASSISTANT:
            if self.call_id is not None:
                raise ValueError("assistant ModelMessage cannot carry call_id")
        elif self.tool_calls:
            raise ValueError("only assistant ModelMessage can carry ToolCalls")
        if self.role is ModelRole.TOOL:
            if self.call_id is None:
                raise ValueError("tool ModelMessage requires call_id")
            object.__setattr__(self, "call_id", _text(self.call_id, "tool call_id"))
        elif self.call_id is not None:
            raise ValueError("only tool ModelMessage can carry call_id")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One Step request containing only replayable EHAI inputs."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    input_messages: tuple[ModelMessage, ...] = ()
    previous_response_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "input_messages", tuple(self.input_messages))
        if not self.messages:
            raise ValueError("ModelRequest requires model-visible history")
        if not self.input_messages:
            object.__setattr__(self, "input_messages", self.messages)
        if self.previous_response_id is not None:
            object.__setattr__(
                self,
                "previous_response_id",
                _text(self.previous_response_id, "previous_response_id"),
            )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One scripted or provider response for a Step."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    final_text: str | None = None
    provider_response_id: str | None = None
    status: str = "completed"
    usage: Mapping[str, JsonValue] | None = None
    output_items: tuple[Mapping[str, JsonValue], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("ModelResponse content must be text")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if bool(self.tool_calls) == (self.final_text is not None):
            raise ValueError("ModelResponse requires ToolCalls or final_text, but not both")
        if self.final_text is not None:
            object.__setattr__(self, "final_text", _text(self.final_text, "final_text"))
        if self.provider_response_id is not None:
            object.__setattr__(
                self,
                "provider_response_id",
                _text(self.provider_response_id, "provider_response_id"),
            )
        object.__setattr__(self, "status", _text(self.status, "response status"))
        if self.usage is not None:
            usage = json_loads(json_dumps(dict(self.usage)))
            if not isinstance(usage, dict):  # pragma: no cover - construction guarantees object
                raise RuntimeError("stored response usage is not an object")
            object.__setattr__(self, "usage", usage)
        output_items: list[Mapping[str, JsonValue]] = []
        for item in self.output_items:
            decoded = json_loads(json_dumps(dict(item)))
            if not isinstance(decoded, dict):  # pragma: no cover - construction guarantees object
                raise RuntimeError("stored response output item is not an object")
            output_items.append(decoded)
        object.__setattr__(self, "output_items", tuple(output_items))


@runtime_checkable
class ModelClient(Protocol):
    """Provider seam for one complete model Step."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one response without owning Session or Tool state."""
        ...

    async def aclose(self) -> None:
        """Release provider resources owned by this client."""
        ...


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


class BuiltinSessionEventType(StrEnum):
    """Append-only facts for Built-in Agent Turn and Step replay."""

    TURN_STARTED = "turn/start"
    STEP_STARTED = "step/start"
    MODEL_MESSAGE = "model/message"
    TOOL_CALLED = "tool/call"
    TOOL_RESULT = "tool/result"
    TOOL_ERROR = "tool/error"
    STEP_ENDED = "step/end"
    FINAL = "final"
    TURN_ENDED = "turn/end"


@dataclass(frozen=True, slots=True, init=False)
class BuiltinSessionEvent:
    """One immutable Session fact with canonical JSON payload."""

    agent_session_ref_id: ID
    attempt_id: ID
    sequence: int
    type: BuiltinSessionEventType
    occurred_at: datetime
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        agent_session_ref_id: ID,
        attempt_id: ID,
        sequence: int,
        event_type: BuiltinSessionEventType,
        payload: Mapping[str, JsonValue],
        occurred_at: datetime | None = None,
    ) -> None:
        object.__setattr__(self, "agent_session_ref_id", normalize_id(agent_session_ref_id))
        object.__setattr__(self, "attempt_id", normalize_id(attempt_id))
        if type(sequence) is not int or sequence < 1:
            raise ValueError("BuiltinSessionEvent sequence must be positive")
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "type", BuiltinSessionEventType(event_type))
        timestamp = utc_now() if occurred_at is None else occurred_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("BuiltinSessionEvent occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", timestamp.astimezone(UTC))
        if not isinstance(payload, Mapping):
            raise ValueError("BuiltinSessionEvent payload must be a JSON object")
        object.__setattr__(self, "_payload_json", json_dumps(dict(payload)))

    @property
    def payload(self) -> dict[str, JsonValue]:
        value = json_loads(self._payload_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded at construction
            raise RuntimeError("stored BuiltinSessionEvent payload is not an object")
        return value


class BuiltinSession:
    """One append-only Built-in Session containing sequential Attempt Turns."""

    def __init__(
        self,
        agent_session_ref_id: ID,
        events: tuple[BuiltinSessionEvent, ...] = (),
    ) -> None:
        self.agent_session_ref_id = normalize_id(agent_session_ref_id)
        self.events: tuple[BuiltinSessionEvent, ...] = ()
        self._closed = False
        for event in events:
            self._append_existing(event)

    def append(
        self,
        attempt_id: ID,
        event_type: BuiltinSessionEventType,
        payload: Mapping[str, JsonValue],
        *,
        occurred_at: datetime | None = None,
    ) -> BuiltinSessionEvent:
        if self._closed:
            raise RuntimeError("BuiltinSession is closed")
        event = BuiltinSessionEvent(
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
            event.attempt_id == normalized and event.type is BuiltinSessionEventType.TURN_STARTED
            for event in self.events
        )

    def is_turn_complete(self, attempt_id: ID) -> bool:
        normalized = normalize_id(attempt_id)
        return any(
            event.attempt_id == normalized and event.type is BuiltinSessionEventType.TURN_ENDED
            for event in self.events
        )

    def final_text(self, attempt_id: ID) -> str | None:
        normalized = normalize_id(attempt_id)
        for event in reversed(self.events):
            if event.attempt_id == normalized and event.type is BuiltinSessionEventType.FINAL:
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
            if event.type is BuiltinSessionEventType.TOOL_CALLED and isinstance(call_id, str):
                arguments = event.payload.get("arguments")
                name = event.payload.get("name")
                if isinstance(arguments, dict) and isinstance(name, str):
                    pending[call_id] = (
                        ToolCall(call_id, name, arguments),
                        event.payload.get("writes_workspace") is True,
                    )
            elif event.type in {
                BuiltinSessionEventType.TOOL_RESULT,
                BuiltinSessionEventType.TOOL_ERROR,
            } and isinstance(call_id, str):
                pending.pop(call_id, None)
        return next(reversed(pending.values()), None) if pending else None

    def model_messages(self) -> tuple[ModelMessage, ...]:
        history, _, _ = _replayed_messages(self.events)
        return history

    def last_provider_response_id(self) -> str | None:
        """Return the latest durable provider continuation handle."""
        _, response_id, _ = _replayed_messages(self.events)
        return response_id

    def continuation_messages(self) -> tuple[ModelMessage, ...]:
        """Return model inputs after the latest provider Response handle."""
        history, response_id, start = _replayed_messages(self.events)
        return history if response_id is None else history[start:]

    def close(self) -> None:
        self._closed = True

    def _append_existing(self, event: BuiltinSessionEvent) -> None:
        if event.agent_session_ref_id != self.agent_session_ref_id:
            raise BuiltinSessionStateError("SessionEvent belongs to another AgentSessionRef")
        if event.sequence != len(self.events) + 1:
            raise BuiltinSessionStateError("SessionEvent sequence is not contiguous")
        _validate_next_event(self.events, event)
        self.events = (*self.events, event)


@runtime_checkable
class BuiltinSessionStore(Protocol):
    """Append-only durable storage for BuiltinSessionEvent batches."""

    def load(self, agent_session_ref_id: ID) -> BuiltinSession:
        """Load the durable Session history, or an empty existing Session."""
        ...

    def append(
        self,
        agent_session_ref_id: ID,
        expected_sequence: int,
        events: tuple[BuiltinSessionEvent, ...],
    ) -> None:
        """Atomically append a contiguous event batch with optimistic sequence check."""
        ...


class PromptBuilder:
    """Build deterministic System/User inputs from EHAI-owned task context."""

    def __init__(self, system_prompt: str) -> None:
        self.system_prompt = _text(system_prompt, "system_prompt")

    def build(
        self,
        instruction: str,
        context: Mapping[str, JsonValue],
    ) -> tuple[ModelMessage, ModelMessage]:
        if not isinstance(context, Mapping):
            raise ValueError("PromptBuilder context must be a JSON object")
        user = json_dumps(
            {"instruction": _text(instruction, "instruction"), "context": dict(context)}
        )
        return (
            ModelMessage(ModelRole.SYSTEM, self.system_prompt),
            ModelMessage(ModelRole.USER, user),
        )


class ExecutionScope:
    """Own and close Agent, Session, Tool runtime, and cancellation in order."""

    def __init__(
        self,
        *,
        agent: BuiltinAgent,
        session: BuiltinSession,
        tool_executor: ToolExecutor,
        cancellation: CancellationToken,
    ) -> None:
        self.agent = agent
        self.session = session
        self.tool_executor = tool_executor
        self.cancellation = cancellation
        self._closed = False

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("ExecutionScope is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.agent.aclose()
        self.session.close()
        await self.tool_executor.aclose()
        self.cancellation.close()
        self._closed = True


class BuiltinAgentLoop:
    """Drive sequential Model Steps and Tools with durable recovery boundaries."""

    def __init__(
        self,
        *,
        model_client: ModelClient,
        prompt_builder: PromptBuilder,
        tool_set: ToolSet,
        session_store: BuiltinSessionStore,
        budget: AgentBudget = DEFAULT_AGENT_BUDGET,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.model_client = model_client
        self.prompt_builder = prompt_builder
        self.tool_set = tool_set
        self.session_store = session_store
        self.budget = budget
        self._monotonic_clock = monotonic_clock

    async def run(
        self,
        scope: ExecutionScope,
        execution: BuiltinExecutionRef,
        instruction: str,
        context: Mapping[str, JsonValue],
    ) -> str:
        session = scope.session
        attempt_id = execution.attempt_id
        if execution.agent_session_ref_id != session.agent_session_ref_id:
            raise BuiltinAgentError("BuiltinExecutionRef belongs to another Session")
        if session.is_turn_complete(attempt_id):
            final = session.final_text(attempt_id)
            if final is None:
                raise BuiltinSessionStateError("completed Turn has no final text")
            return final
        incomplete_call = session.incomplete_write_call(attempt_id)
        if incomplete_call is not None:
            raise IncompleteWriteToolError(
                f"write Tool call {incomplete_call} has an unknown outcome and cannot be replayed"
            )
        persisted_sequence = len(session.events)
        started_at = self._monotonic_clock()
        step_count, tool_call_count, output_bytes = _turn_usage(
            session.events,
            attempt_id,
        )
        if not session.has_turn(attempt_id):
            system_message, user_message = self.prompt_builder.build(instruction, context)
            session.append(
                attempt_id,
                BuiltinSessionEventType.TURN_STARTED,
                {"messages": [_message_document(system_message), _message_document(user_message)]},
            )
            persisted_sequence = self._persist(session, persisted_sequence)

        pending_call = session.incomplete_tool_call(attempt_id)
        if pending_call is not None:
            call, writes_workspace = pending_call
            if writes_workspace:  # pragma: no cover - guarded by incomplete_write_call above
                raise IncompleteWriteToolError(
                    f"write Tool call {call.call_id} has an unknown outcome and cannot be replayed"
                )
            event_type, payload, visible_result = await self._execute_tool(scope, call)
            output_bytes += len(json_dumps(visible_result).encode("utf-8"))
            self._check_output_budget(output_bytes)
            session.append(attempt_id, event_type, payload)
            session.append(
                attempt_id,
                BuiltinSessionEventType.STEP_ENDED,
                {"started_sequence": _latest_step_start(session.events, attempt_id)},
            )
            persisted_sequence = self._persist(session, persisted_sequence)

        while True:
            scope.cancellation.raise_if_cancelled()
            self._check_wall_clock(started_at)
            if step_count >= self.budget.max_steps:
                raise AgentBudgetExceededError("Built-in Agent Step budget exhausted")
            step_start = len(session.events)
            session.append(attempt_id, BuiltinSessionEventType.STEP_STARTED, {})
            request = ModelRequest(
                session.model_messages(),
                self.tool_set.definitions,
                input_messages=session.continuation_messages(),
                previous_response_id=session.last_provider_response_id(),
            )
            response = await self.model_client.complete(request)
            step_count += 1
            output_bytes += len(response.content.encode("utf-8"))
            if response.final_text is not None:
                output_bytes += len(response.final_text.encode("utf-8"))
            if output_bytes > self.budget.max_output_bytes:
                raise AgentBudgetExceededError("Built-in Agent output byte budget exhausted")
            assistant = ModelMessage(
                ModelRole.ASSISTANT,
                response.content,
                tool_calls=response.tool_calls,
            )
            session.append(
                attempt_id,
                BuiltinSessionEventType.MODEL_MESSAGE,
                _message_document(
                    assistant,
                    response.provider_response_id,
                    status=response.status,
                    usage=response.usage,
                    output_items=response.output_items,
                ),
            )
            if response.tool_calls:
                if tool_call_count + len(response.tool_calls) > self.budget.max_tool_calls:
                    raise AgentBudgetExceededError("Built-in Agent Tool Call budget exhausted")
                for call in response.tool_calls:
                    self._check_wall_clock(started_at)
                    try:
                        definition = self.tool_set.require(call.name)
                    except UnknownToolError:
                        definition = None
                    session.append(
                        attempt_id,
                        BuiltinSessionEventType.TOOL_CALLED,
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "writes_workspace": (
                                False if definition is None else definition.writes_workspace
                            ),
                        },
                    )
                    persisted_sequence = self._persist(session, persisted_sequence)
                    event_type, payload, visible_result = await self._execute_tool(scope, call)
                    tool_call_count += 1
                    output_bytes += len(json_dumps(visible_result).encode("utf-8"))
                    self._check_output_budget(output_bytes)
                    session.append(
                        attempt_id,
                        event_type,
                        payload,
                    )
                    if event_type is BuiltinSessionEventType.TOOL_ERROR:
                        continue
                    if definition is not None and definition.ends_turn:
                        if len(response.tool_calls) != 1:
                            raise BuiltinAgentError(
                                "a final Tool must be the only ToolCall in its Step"
                            )
                        final_text = json_dumps(visible_result)
                        session.append(
                            attempt_id,
                            BuiltinSessionEventType.FINAL,
                            {"text": final_text},
                        )
                        session.append(
                            attempt_id,
                            BuiltinSessionEventType.STEP_ENDED,
                            {"started_sequence": step_start + 1},
                        )
                        session.append(
                            attempt_id,
                            BuiltinSessionEventType.TURN_ENDED,
                            {},
                        )
                        self._persist(session, persisted_sequence)
                        return final_text
                session.append(
                    attempt_id,
                    BuiltinSessionEventType.STEP_ENDED,
                    {"started_sequence": step_start + 1},
                )
                persisted_sequence = self._persist(session, persisted_sequence)
                continue

            assert response.final_text is not None
            session.append(
                attempt_id,
                BuiltinSessionEventType.FINAL,
                {"text": response.final_text},
            )
            session.append(
                attempt_id,
                BuiltinSessionEventType.STEP_ENDED,
                {"started_sequence": step_start + 1},
            )
            session.append(attempt_id, BuiltinSessionEventType.TURN_ENDED, {})
            self._persist(session, persisted_sequence)
            return response.final_text

    def _check_wall_clock(self, started_at: float) -> None:
        if self._monotonic_clock() - started_at > self.budget.wall_clock_seconds:
            raise AgentBudgetExceededError("Built-in Agent wall-clock budget exhausted")

    def _check_output_budget(self, output_bytes: int) -> None:
        if output_bytes > self.budget.max_output_bytes:
            raise AgentBudgetExceededError("Built-in Agent output byte budget exhausted")

    @staticmethod
    async def _execute_tool(
        scope: ExecutionScope,
        call: ToolCall,
    ) -> tuple[BuiltinSessionEventType, dict[str, JsonValue], JsonValue]:
        try:
            result = await scope.tool_executor.execute(call, scope.cancellation)
        except RecoverableToolError as error:
            document: dict[str, JsonValue] = {
                "code": error.code[:100],
                "message": error.safe_message[:1_000],
                "recoverable": True,
            }
            return (
                BuiltinSessionEventType.TOOL_ERROR,
                {"call_id": call.call_id, "error": document},
                {"error": document},
            )
        return (
            BuiltinSessionEventType.TOOL_RESULT,
            {"call_id": call.call_id, "result": result},
            result,
        )

    def _persist(self, session: BuiltinSession, persisted_sequence: int) -> int:
        events = session.events[persisted_sequence:]
        if events:
            self.session_store.append(
                session.agent_session_ref_id,
                persisted_sequence,
                events,
            )
        return len(session.events)


class BuiltinAgent:
    """Facade for one Built-in Agent Loop owned by an ExecutionScope."""

    def __init__(self, loop: BuiltinAgentLoop) -> None:
        self.loop = loop
        self._closed = False

    async def run(
        self,
        scope: ExecutionScope,
        execution: BuiltinExecutionRef,
        instruction: str,
        context: Mapping[str, JsonValue],
    ) -> str:
        if self._closed:
            raise RuntimeError("BuiltinAgent is closed")
        return await self.loop.run(scope, execution, instruction, context)

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.loop.model_client.aclose()
        self._closed = True


def _validate_next_event(
    events: tuple[BuiltinSessionEvent, ...],
    event: BuiltinSessionEvent,
) -> None:
    attempt_events = tuple(item for item in events if item.attempt_id == event.attempt_id)
    previous = None if not attempt_events else attempt_events[-1].type
    if event.type is BuiltinSessionEventType.TURN_STARTED:
        if attempt_events:
            raise BuiltinSessionStateError("Attempt Turn can start only once")
        active_attempts: set[ID] = set()
        for existing in events:
            if existing.type is BuiltinSessionEventType.TURN_STARTED:
                active_attempts.add(existing.attempt_id)
            elif existing.type is BuiltinSessionEventType.TURN_ENDED:
                active_attempts.discard(existing.attempt_id)
        if active_attempts:
            raise BuiltinSessionStateError("BuiltinSession already has an active Turn")
        return
    if previous is None:
        raise BuiltinSessionStateError("Attempt event requires turn/start")
    allowed: dict[BuiltinSessionEventType, frozenset[BuiltinSessionEventType]] = {
        BuiltinSessionEventType.STEP_STARTED: frozenset(
            {BuiltinSessionEventType.TURN_STARTED, BuiltinSessionEventType.STEP_ENDED}
        ),
        BuiltinSessionEventType.MODEL_MESSAGE: frozenset({BuiltinSessionEventType.STEP_STARTED}),
        BuiltinSessionEventType.TOOL_CALLED: frozenset(
            {
                BuiltinSessionEventType.MODEL_MESSAGE,
                BuiltinSessionEventType.TOOL_RESULT,
                BuiltinSessionEventType.TOOL_ERROR,
            }
        ),
        BuiltinSessionEventType.TOOL_RESULT: frozenset({BuiltinSessionEventType.TOOL_CALLED}),
        BuiltinSessionEventType.TOOL_ERROR: frozenset({BuiltinSessionEventType.TOOL_CALLED}),
        BuiltinSessionEventType.FINAL: frozenset(
            {BuiltinSessionEventType.MODEL_MESSAGE, BuiltinSessionEventType.TOOL_RESULT}
        ),
        BuiltinSessionEventType.STEP_ENDED: frozenset(
            {
                BuiltinSessionEventType.MODEL_MESSAGE,
                BuiltinSessionEventType.TOOL_RESULT,
                BuiltinSessionEventType.TOOL_ERROR,
                BuiltinSessionEventType.FINAL,
            }
        ),
        BuiltinSessionEventType.TURN_ENDED: frozenset({BuiltinSessionEventType.STEP_ENDED}),
    }
    if previous not in allowed[event.type]:
        raise BuiltinSessionStateError(f"cannot append {event.type.value} after {previous.value}")
    if event.type is BuiltinSessionEventType.TURN_ENDED:
        latest_step_start = max(
            index
            for index, existing in enumerate(attempt_events)
            if existing.type is BuiltinSessionEventType.STEP_STARTED
        )
        if not any(
            existing.type is BuiltinSessionEventType.FINAL
            for existing in attempt_events[latest_step_start:]
        ):
            raise BuiltinSessionStateError("turn/end requires a final Step")


def _message_document(
    message: ModelMessage,
    provider_response_id: str | None = None,
    *,
    status: str | None = None,
    usage: Mapping[str, JsonValue] | None = None,
    output_items: tuple[Mapping[str, JsonValue], ...] = (),
) -> dict[str, JsonValue]:
    calls: list[JsonValue] = [
        {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
        for call in message.tool_calls
    ]
    return {
        "role": message.role.value,
        "content": message.content,
        "tool_calls": calls,
        "call_id": message.call_id,
        "provider_response_id": provider_response_id,
        "status": status,
        "usage": None if usage is None else dict(usage),
        "output_items": [dict(item) for item in output_items],
    }


def _message_from_document(document: Mapping[str, JsonValue]) -> ModelMessage:
    calls_value = document.get("tool_calls")
    if not isinstance(calls_value, list):
        raise BuiltinSessionStateError("model/message tool_calls must be an array")
    calls: list[ToolCall] = []
    for value in calls_value:
        if not isinstance(value, dict):
            raise BuiltinSessionStateError("model/message ToolCall must be an object")
        arguments = value.get("arguments")
        if not isinstance(arguments, dict):
            raise BuiltinSessionStateError("model/message ToolCall arguments must be an object")
        calls.append(
            ToolCall(
                _required_string(value, "call_id"),
                _required_string(value, "name"),
                arguments,
            )
        )
    return ModelMessage(
        ModelRole(_required_string(document, "role")),
        _required_string(document, "content", allow_empty=True),
        tuple(calls),
        _optional_string(document, "call_id"),
    )


def _turn_messages(document: Mapping[str, JsonValue]) -> tuple[ModelMessage, ...]:
    values = document.get("messages")
    if not isinstance(values, list):
        raise BuiltinSessionStateError("turn/start messages must be an array")
    return tuple(_message_from_document(value) for value in values if isinstance(value, dict))


def _replayed_messages(
    events: tuple[BuiltinSessionEvent, ...],
) -> tuple[tuple[ModelMessage, ...], str | None, int]:
    history: list[ModelMessage] = []
    pending_step: list[ModelMessage] = []
    response_id: str | None = None
    pending_continuation_start: int | None = None
    continuation_start = 0
    for event in events:
        if event.type is BuiltinSessionEventType.TURN_STARTED:
            history.extend(_turn_messages(event.payload))
        elif event.type is BuiltinSessionEventType.STEP_STARTED:
            pending_step = []
            pending_continuation_start = None
        elif event.type is BuiltinSessionEventType.MODEL_MESSAGE:
            pending_step.append(_message_from_document(event.payload))
            provider_id = event.payload.get("provider_response_id")
            if isinstance(provider_id, str) and provider_id:
                response_id = provider_id
                pending_continuation_start = len(history) + len(pending_step)
        elif event.type is BuiltinSessionEventType.TOOL_RESULT:
            pending_step.append(_tool_result_message(event.payload))
        elif event.type is BuiltinSessionEventType.TOOL_ERROR:
            pending_step.append(_tool_error_message(event.payload))
        elif event.type is BuiltinSessionEventType.STEP_ENDED:
            history.extend(pending_step)
            if pending_continuation_start is not None:
                continuation_start = pending_continuation_start
            pending_step = []
            pending_continuation_start = None
    return tuple(history), response_id, continuation_start


def _turn_usage(
    events: tuple[BuiltinSessionEvent, ...],
    attempt_id: ID,
) -> tuple[int, int, int]:
    step_count = 0
    tool_call_count = 0
    output_bytes = 0
    for event in events:
        if event.attempt_id != attempt_id:
            continue
        if event.type is BuiltinSessionEventType.STEP_STARTED:
            step_count += 1
        elif event.type is BuiltinSessionEventType.TOOL_CALLED:
            tool_call_count += 1
        elif event.type is BuiltinSessionEventType.MODEL_MESSAGE:
            content = event.payload.get("content")
            if isinstance(content, str):
                output_bytes += len(content.encode("utf-8"))
        elif event.type is BuiltinSessionEventType.TOOL_RESULT:
            output_bytes += len(json_dumps(event.payload.get("result")).encode("utf-8"))
        elif event.type is BuiltinSessionEventType.TOOL_ERROR:
            output_bytes += len(json_dumps(event.payload.get("error")).encode("utf-8"))
        elif event.type is BuiltinSessionEventType.FINAL:
            text = event.payload.get("text")
            if isinstance(text, str):
                output_bytes += len(text.encode("utf-8"))
    return step_count, tool_call_count, output_bytes


def _tool_result_message(document: Mapping[str, JsonValue]) -> ModelMessage:
    return ModelMessage(
        ModelRole.TOOL,
        json_dumps(document.get("result")),
        call_id=_required_string(document, "call_id"),
    )


def _tool_error_message(document: Mapping[str, JsonValue]) -> ModelMessage:
    return ModelMessage(
        ModelRole.TOOL,
        json_dumps({"error": document.get("error")}),
        call_id=_required_string(document, "call_id"),
    )


def _latest_step_start(events: tuple[BuiltinSessionEvent, ...], attempt_id: ID) -> int:
    return max(
        event.sequence
        for event in events
        if event.attempt_id == attempt_id and event.type is BuiltinSessionEventType.STEP_STARTED
    )


def _required_string(
    document: Mapping[str, JsonValue],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = document.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise BuiltinSessionStateError(f"{key} must be text")
    return value


def _optional_string(document: Mapping[str, JsonValue], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    return _required_string(document, key)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()
