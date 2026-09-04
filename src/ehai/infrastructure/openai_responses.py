"""Official OpenAI Python SDK adapter for the P2 Responses ModelClient."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    AsyncStream,
    BadRequestError,
    omit,
)
from openai.types.responses import (
    FunctionToolParam,
    Response,
    ResponseInputParam,
    ResponseOutputItem,
    ResponseStreamEvent,
)
from openai.types.shared import ReasoningEffort

from ehai import JsonValue, json_dumps, json_loads
from ehai.application.builtin_agent import (
    BuiltinAgentError,
    ModelClient,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ToolCall,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.domain.workers import WorkerKind, WorkerProfile

_ACTIVE_RESPONSE_STATUSES = frozenset({"queued", "in_progress"})
"""Provider statuses that mean the execution is still running and must be awaited."""


class OpenAIResponsesProtocolError(BuiltinAgentError):
    """Raised when a Responses stream cannot become one valid ModelResponse."""


class OpenAIResponsesUnknownOutcomeError(OpenAIResponsesProtocolError):
    """A create may have succeeded, but no recoverable Response ID was received."""


class _BackgroundUnsupported(Exception):
    """Internal signal: the endpoint rejected background mode before creating a Response."""


@dataclass(frozen=True, slots=True)
class ResponsesEndpointCapabilities:
    """Explicit Responses features guaranteed by one configured provider endpoint."""

    supports_background: bool = True
    supports_idempotent_create: bool | None = None
    supports_unique_items: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.supports_background, bool):
            raise ValueError("supports_background must be a boolean")
        if self.supports_idempotent_create is not None and not isinstance(
            self.supports_idempotent_create, bool
        ):
            raise ValueError("supports_idempotent_create must be a boolean")
        if not isinstance(self.supports_unique_items, bool):
            raise ValueError("supports_unique_items must be a boolean")


class OpenAIResponsesModelClient(ModelClient):
    """Run one Response Step using the official OpenAI Python SDK.

    ``timeout_seconds`` is a connection / stream-idle timeout, not a total task
    lifetime. Provider executions remain valid while they are queued or in
    progress, interrupted streams are recovered by retrieving the same Response
    (never by creating a duplicate one), and only explicit terminal statuses or
    exhausted recovery budgets end the execution.
    """

    def __init__(
        self,
        profile: WorkerProfile,
        *,
        client: AsyncOpenAI | None = None,
        max_output_tokens: int = 4096,
        timeout_seconds: float = 300.0,
        reasoning_effort: ReasoningEffort = None,
        background: bool = False,
        max_http_retries: int = 4,
        max_stream_reconnects: int = 5,
        retry_delay_seconds: float = 1.0,
        poll_interval_seconds: float = 2.0,
        retry_owner: Literal["ehai", "client"] | None = None,
        endpoint_capabilities: ResponsesEndpointCapabilities | None = None,
    ) -> None:
        if profile.kind is not WorkerKind.BUILTIN:
            raise ValueError("OpenAIResponsesModelClient requires a Built-in WorkerProfile")
        if profile.credential_ref != OPENAI_CREDENTIAL_REF:
            raise ValueError(f"OpenAI WorkerProfile credential_ref must be {OPENAI_CREDENTIAL_REF}")
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if type(max_http_retries) is not int or max_http_retries < 0:
            raise ValueError("max_http_retries must be a non-negative integer")
        if type(max_stream_reconnects) is not int or max_stream_reconnects < 0:
            raise ValueError("max_stream_reconnects must be a non-negative integer")
        if (
            not isinstance(retry_delay_seconds, (int, float))
            or isinstance(retry_delay_seconds, bool)
            or retry_delay_seconds < 0
        ):
            raise ValueError("retry_delay_seconds must be non-negative")
        if (
            not isinstance(poll_interval_seconds, (int, float))
            or isinstance(poll_interval_seconds, bool)
            or poll_interval_seconds < 0
        ):
            raise ValueError("poll_interval_seconds must be non-negative")
        if retry_owner not in {None, "ehai", "client"}:
            raise ValueError("retry_owner must be 'ehai' or 'client'")
        capabilities = endpoint_capabilities or ResponsesEndpointCapabilities()
        if not isinstance(capabilities, ResponsesEndpointCapabilities):
            raise TypeError("endpoint_capabilities must be ResponsesEndpointCapabilities")
        if client is not None and retry_owner is None:
            raise ValueError("injected OpenAI clients must declare retry_owner")
        resolved_retry_owner = retry_owner or "ehai"
        if client is None and resolved_retry_owner == "client":
            raise ValueError("internally created OpenAI clients use EHAI-owned retries")
        if resolved_retry_owner == "client" and max_http_retries != 0:
            raise ValueError("client-owned retries require max_http_retries=0")
        if client is not None and resolved_retry_owner == "ehai":
            client_max_retries = getattr(client, "max_retries", None)
            if client_max_retries != 0:
                raise ValueError("EHAI-owned retries require an injected client with max_retries=0")
        self.profile = profile
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = float(timeout_seconds)
        self.reasoning_effort = reasoning_effort
        self.background = background
        self.max_http_retries = max_http_retries
        self.max_stream_reconnects = max_stream_reconnects
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.retry_owner = resolved_retry_owner
        self.endpoint_capabilities = capabilities
        self._client = AsyncOpenAI(max_retries=0) if client is None else client
        self._owns_client = client is None
        self.supports_idempotent_create = (
            _is_official_openai_endpoint(self._client)
            if capabilities.supports_idempotent_create is None
            else capabilities.supports_idempotent_create
        )
        self._closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._closed:
            raise RuntimeError("OpenAIResponsesModelClient is closed")
        logical_request_id = f"ehai-{uuid4()}"
        if self.background and self.endpoint_capabilities.supports_background:
            try:
                return await self._complete_background(request, logical_request_id)
            except _BackgroundUnsupported:
                # The background create was rejected before any Response existed,
                # so the streaming fallback cannot duplicate a Response.
                pass
        return await self._complete_streaming(request, logical_request_id)

    async def _complete_background(
        self,
        request: ModelRequest,
        logical_request_id: str,
    ) -> ModelResponse:
        """Create a background Response and poll it through retrieve until terminal."""
        http_failures = 0
        while True:
            try:
                response = await self._start_background(request, logical_request_id)
            except Exception as error:
                if not _is_retriable(error):
                    raise
                self._require_safe_create_retry(error)
                http_failures += 1
                if http_failures > self.max_http_retries:
                    raise OpenAIResponsesProtocolError(
                        f"Responses background request failed after {self.max_http_retries} "
                        "HTTP retries"
                    ) from error
                await asyncio.sleep(self.retry_delay_seconds)
                continue
            return await self._await_terminal_response(response.id)

    async def _complete_streaming(
        self,
        request: ModelRequest,
        logical_request_id: str,
    ) -> ModelResponse:
        http_failures = 0
        reconnects = 0
        seen_response_id: str | None = None
        while True:
            try:
                stream = await self._start_stream(request, logical_request_id)
            except Exception as error:
                if not _is_retriable(error):
                    raise
                self._require_safe_create_retry(error)
                http_failures += 1
                if http_failures > self.max_http_retries:
                    raise OpenAIResponsesProtocolError(
                        f"Responses request failed after {self.max_http_retries} HTTP retries"
                    ) from error
                await asyncio.sleep(self.retry_delay_seconds)
                continue
            text_parts: list[str] = []
            streamed_output_items: list[ResponseOutputItem] = []
            try:
                async for event in stream:
                    response_id = _event_response_id(event)
                    if response_id is not None:
                        seen_response_id = response_id
                    if event.type == "response.output_text.delta":
                        text_parts.append(event.delta)
                    elif event.type == "response.output_item.done":
                        item = event.item
                        streamed_output_items.append(item)
                        if item.type == "function_call":
                            arguments = json_loads(item.arguments)
                            if not isinstance(arguments, dict):
                                raise OpenAIResponsesProtocolError(
                                    f"function call {item.call_id} arguments are not an object"
                                )
                    elif event.type == "response.completed":
                        return _model_response(
                            event.response,
                            text="".join(text_parts),
                            streamed_output_items=tuple(streamed_output_items),
                        )
                    elif event.type in {"response.failed", "response.incomplete"}:
                        raise OpenAIResponsesProtocolError(
                            f"Responses stream ended with {event.type}"
                        )
            except Exception as error:
                if not _is_retriable(error):
                    raise
            finally:
                await stream.close()
            reconnects += 1
            if reconnects > self.max_stream_reconnects:
                raise OpenAIResponsesProtocolError(
                    f"Responses stream could not be recovered after "
                    f"{self.max_stream_reconnects} reconnects"
                )
            if seen_response_id is not None:
                return await self._await_terminal_response(
                    seen_response_id,
                    reconnects_used=reconnects,
                )
            await asyncio.sleep(self.retry_delay_seconds)
            continue

    async def _await_terminal_response(
        self,
        response_id: str,
        *,
        reconnects_used: int = 0,
    ) -> ModelResponse:
        """Recover or poll the same stored Response until an explicit terminal status.

        queued/in_progress keep waiting; connection failures consume the remaining
        stream reconnect budget instead of failing the provider execution.
        """
        reconnects = reconnects_used
        while True:
            try:
                response = await self._client.responses.retrieve(
                    response_id,
                    timeout=self.timeout_seconds,
                )
            except Exception as error:
                if not _is_retriable(error):
                    raise
                reconnects += 1
                if reconnects > self.max_stream_reconnects:
                    raise OpenAIResponsesProtocolError(
                        f"Response {response_id} could not be recovered after "
                        f"{self.max_stream_reconnects} stream reconnects"
                    ) from error
                await asyncio.sleep(self.retry_delay_seconds)
                continue
            status = response.status
            if status in _ACTIVE_RESPONSE_STATUSES:
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            if status == "completed":
                return _model_response(response)
            raise OpenAIResponsesProtocolError(
                f"Response {response_id} ended with terminal status {status}"
            )

    async def _start_stream(
        self,
        request: ModelRequest,
        logical_request_id: str,
    ) -> AsyncStream[ResponseStreamEvent]:
        try:
            return await self._create_stream(
                request,
                input_messages=request.input_messages,
                previous_response_id=request.previous_response_id,
                logical_request_id=logical_request_id,
            )
        except BadRequestError as error:
            if request.previous_response_id is not None and _rejects_continuation(error):
                return await self._create_stream(
                    request,
                    input_messages=request.messages,
                    previous_response_id=None,
                    logical_request_id=logical_request_id,
                )
            raise

    async def _start_background(
        self,
        request: ModelRequest,
        logical_request_id: str,
    ) -> Response:
        try:
            return await self._create_background(
                request,
                input_messages=request.input_messages,
                previous_response_id=request.previous_response_id,
                logical_request_id=logical_request_id,
            )
        except BadRequestError as error:
            if _rejects_background(error):
                raise _BackgroundUnsupported from error
            if request.previous_response_id is not None and _rejects_continuation(error):
                return await self._create_background(
                    request,
                    input_messages=request.messages,
                    previous_response_id=None,
                    logical_request_id=logical_request_id,
                )
            raise

    async def _create_stream(
        self,
        request: ModelRequest,
        *,
        input_messages: tuple[ModelMessage, ...],
        previous_response_id: str | None,
        logical_request_id: str,
    ) -> AsyncStream[ResponseStreamEvent]:
        return await self._client.responses.create(
            model=self.profile.model,
            instructions=_instructions(request.messages),
            input=_response_input(input_messages),
            tools=_function_tools(
                request,
                supports_unique_items=self.endpoint_capabilities.supports_unique_items,
            ),
            previous_response_id=(omit if previous_response_id is None else previous_response_id),
            parallel_tool_calls=False,
            tool_choice=request.tool_choice,
            store=True,
            stream=True,
            max_output_tokens=self.max_output_tokens,
            reasoning=(
                omit if self.reasoning_effort is None else {"effort": self.reasoning_effort}
            ),
            timeout=self.timeout_seconds,
            extra_headers={"Idempotency-Key": logical_request_id},
        )

    async def _create_background(
        self,
        request: ModelRequest,
        *,
        input_messages: tuple[ModelMessage, ...],
        previous_response_id: str | None,
        logical_request_id: str,
    ) -> Response:
        return await self._client.responses.create(
            model=self.profile.model,
            instructions=_instructions(request.messages),
            input=_response_input(input_messages),
            tools=_function_tools(
                request,
                supports_unique_items=self.endpoint_capabilities.supports_unique_items,
            ),
            previous_response_id=(omit if previous_response_id is None else previous_response_id),
            parallel_tool_calls=False,
            tool_choice=request.tool_choice,
            store=True,
            background=True,
            max_output_tokens=self.max_output_tokens,
            reasoning=(
                omit if self.reasoning_effort is None else {"effort": self.reasoning_effort}
            ),
            timeout=self.timeout_seconds,
            extra_headers={"Idempotency-Key": logical_request_id},
        )

    def _require_safe_create_retry(self, error: BaseException) -> None:
        if self.supports_idempotent_create:
            return
        raise OpenAIResponsesUnknownOutcomeError(
            "Responses create outcome is unknown; the endpoint has not proven "
            "idempotent create support, so EHAI did not create a replacement Response"
        ) from error

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            await self._client.close()
        self._closed = True


def _model_response(
    response: Response,
    *,
    text: str | None = None,
    streamed_output_items: tuple[ResponseOutputItem, ...] = (),
) -> ModelResponse:
    if response.status != "completed":
        raise OpenAIResponsesProtocolError(
            f"Response {response.id} has non-completed status {response.status}"
        )
    content = text if text and text.strip() else response.output_text
    usage = None if response.usage is None else _json_object(response.usage.model_dump(mode="json"))
    response_items = tuple(response.output) or streamed_output_items
    output_items = tuple(
        _json_object(item.model_dump(mode="json"))
        for item in response_items
        if item.type != "reasoning"
    )
    tool_calls = tuple(
        ToolCall(item.call_id, item.name, _response_arguments(item.arguments))
        for item in response_items
        if item.type == "function_call"
    )
    if tool_calls:
        return ModelResponse(
            content,
            tool_calls,
            provider_response_id=response.id,
            status=response.status,
            usage=usage,
            output_items=output_items,
        )
    if not content.strip():
        raise OpenAIResponsesProtocolError(
            f"Response {response.id} has neither ToolCalls nor output text"
        )
    return ModelResponse(
        content,
        final_text=content,
        provider_response_id=response.id,
        status=response.status,
        usage=usage,
        output_items=output_items,
    )


def _response_arguments(raw: str) -> dict[str, JsonValue]:
    decoded = json_loads(raw)
    if not isinstance(decoded, dict):
        raise OpenAIResponsesProtocolError("function call arguments are not an object")
    return decoded


def _event_response_id(event: ResponseStreamEvent) -> str | None:
    response = getattr(event, "response", None)
    response_id = getattr(response, "id", None)
    return response_id if isinstance(response_id, str) and response_id else None


def _is_retriable(error: BaseException) -> bool:
    if isinstance(error, APIConnectionError):
        return True
    return isinstance(error, APIStatusError) and error.status_code >= 500


def _is_official_openai_endpoint(client: AsyncOpenAI) -> bool:
    base_url = getattr(client, "base_url", None)
    return getattr(base_url, "host", None) == "api.openai.com"


def _instructions(messages: tuple[ModelMessage, ...]) -> str:
    instructions = "\n\n".join(
        message.content for message in messages if message.role is ModelRole.SYSTEM
    )
    if not instructions:
        raise OpenAIResponsesProtocolError("ModelRequest has no System instructions")
    return instructions


def _response_input(messages: tuple[ModelMessage, ...]) -> ResponseInputParam:
    items: ResponseInputParam = []
    for message in messages:
        if message.role is ModelRole.SYSTEM:
            continue
        if message.role is ModelRole.USER:
            items.append({"role": "user", "content": message.content})
        elif message.role is ModelRole.TOOL:
            assert message.call_id is not None
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.call_id,
                    "output": message.content,
                }
            )
        else:
            if message.content:
                items.append({"role": "assistant", "content": message.content})
            items.extend(
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": json_dumps(call.arguments),
                }
                for call in message.tool_calls
            )
    if not items:
        raise OpenAIResponsesProtocolError("Responses input is empty")
    return items


def _function_tools(
    request: ModelRequest,
    *,
    supports_unique_items: bool = True,
) -> list[FunctionToolParam]:
    tools: list[FunctionToolParam] = []
    for definition in request.tools:
        parameters = definition.input_schema
        if not supports_unique_items:
            compatible = _without_unique_items(parameters)
            if not isinstance(compatible, dict):  # pragma: no cover - root schema is an object
                raise OpenAIResponsesProtocolError("Tool schema is not a JSON object")
            parameters = compatible
        tools.append(
            {
                "type": "function",
                "name": definition.name,
                "description": definition.description,
                "parameters": cast(dict[str, object], parameters),
                "strict": True,
            }
        )
    return tools


def _without_unique_items(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: _without_unique_items(item) for key, item in value.items() if key != "uniqueItems"
        }
    if isinstance(value, list):
        return [_without_unique_items(item) for item in value]
    return value


def _json_object(value: object) -> dict[str, JsonValue]:
    decoded = json_loads(json_dumps(value))  # type: ignore[arg-type]
    if not isinstance(decoded, dict):
        raise OpenAIResponsesProtocolError("OpenAI response metadata is not a JSON object")
    return decoded


def _rejects_continuation(error: BadRequestError) -> bool:
    return "previous_response_id" in str(error)


def _rejects_background(error: BadRequestError) -> bool:
    return "background" in str(error)
