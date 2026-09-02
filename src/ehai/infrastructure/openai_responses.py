"""Official OpenAI Python SDK adapter for the P2 Responses ModelClient."""

from __future__ import annotations

from typing import cast

from openai import AsyncOpenAI, omit
from openai.types.responses import (
    FunctionToolParam,
    ResponseInputParam,
)

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


class OpenAIResponsesProtocolError(BuiltinAgentError):
    """Raised when a Responses stream cannot become one valid ModelResponse."""


class OpenAIResponsesModelClient(ModelClient):
    """Stream one Response Step using the official OpenAI Python SDK."""

    def __init__(
        self,
        profile: WorkerProfile,
        *,
        client: AsyncOpenAI | None = None,
        max_output_tokens: int = 4096,
        timeout_seconds: float = 120.0,
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
        self.profile = profile
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = float(timeout_seconds)
        self._client = AsyncOpenAI() if client is None else client
        self._owns_client = client is None
        self._closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._closed:
            raise RuntimeError("OpenAIResponsesModelClient is closed")
        stream = await self._client.responses.create(
            model=self.profile.model,
            instructions=_instructions(request.messages),
            input=_response_input(request.input_messages),
            tools=_function_tools(request),
            previous_response_id=(
                omit if request.previous_response_id is None else request.previous_response_id
            ),
            parallel_tool_calls=False,
            store=True,
            stream=True,
            max_output_tokens=self.max_output_tokens,
            timeout=self.timeout_seconds,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        completed = None
        try:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    text_parts.append(event.delta)
                elif event.type == "response.output_item.done":
                    item = event.item
                    if item.type == "function_call":
                        arguments = json_loads(item.arguments)
                        if not isinstance(arguments, dict):
                            raise OpenAIResponsesProtocolError(
                                f"function call {item.call_id} arguments are not an object"
                            )
                        tool_calls.append(ToolCall(item.call_id, item.name, arguments))
                elif event.type == "response.completed":
                    completed = event
                elif event.type in {"response.failed", "response.incomplete"}:
                    raise OpenAIResponsesProtocolError(f"Responses stream ended with {event.type}")
        finally:
            await stream.close()
        if completed is None:
            raise OpenAIResponsesProtocolError("Responses stream has no response.completed event")
        response = completed.response
        if response.status != "completed":
            raise OpenAIResponsesProtocolError(
                f"Response {response.id} has non-completed status {response.status}"
            )
        content = "".join(text_parts) or response.output_text
        usage = (
            None if response.usage is None else _json_object(response.usage.model_dump(mode="json"))
        )
        output_items = tuple(
            _json_object(item.model_dump(mode="json"))
            for item in response.output
            if item.type != "reasoning"
        )
        if tool_calls:
            return ModelResponse(
                content,
                tuple(tool_calls),
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

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            await self._client.close()
        self._closed = True


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
            raise OpenAIResponsesProtocolError(
                "assistant history requires previous_response_id and must not be replayed as input"
            )
    if not items:
        raise OpenAIResponsesProtocolError("Responses input is empty")
    return items


def _function_tools(request: ModelRequest) -> list[FunctionToolParam]:
    return [
        {
            "type": "function",
            "name": definition.name,
            "description": definition.description,
            "parameters": cast(dict[str, object], definition.input_schema),
            "strict": True,
        }
        for definition in request.tools
    ]


def _json_object(value: object) -> dict[str, JsonValue]:
    decoded = json_loads(json_dumps(cast(JsonValue, value)))
    if not isinstance(decoded, dict):
        raise OpenAIResponsesProtocolError("OpenAI response metadata is not a JSON object")
    return decoded
