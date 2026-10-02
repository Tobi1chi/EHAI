"""Hub protocol v1 messages shared by the core client and the Hub service.

Every message is plain JSON so the Hub can run in another process or on another machine.
The Hub never owns EHAI facts: tool calls are forwarded to the core, and session references
travel back to the core in responses and events.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ehai import JsonValue

PROTOCOL_VERSION: Literal[1] = 1
TOKEN_ENVIRONMENT = "EHAI_HUB_TOKEN"
URL_ENVIRONMENT = "EHAI_HUB_URL"

Text = Annotated[str, Field(min_length=1)]


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessSelection(_Message):
    kind: Text
    settings: dict[str, JsonValue]


class ToolSpec(_Message):
    name: Text
    description: Text
    parameters: dict[str, JsonValue]


class StartSession(_Message):
    """Launch a harness session and verify it; the prompt is sent separately."""

    protocol_version: Literal[1] = PROTOCOL_VERSION
    invocation_id: Text
    state_key: Text
    native_session_id: Text
    harness: HarnessSelection
    fresh: bool
    system_prompt: Text
    tools: list[ToolSpec] = Field(min_length=1)
    model: Text
    reasoning_effort: str | None = None
    workspace: Text


class SessionStarted(_Message):
    session_id: Text
    native_session: JsonValue = None


class SteerMessages(_Message):
    messages: list[Text] = Field(min_length=1)


class PromptMessage(_Message):
    message: Text


class ToolResult(_Message):
    call_id: Text
    result: JsonValue
    is_error: bool
    finish: bool


class InputPrepared(_Message):
    """The harness is about to send these user inputs (SHA-256 of each text) to the model."""

    type: Literal["input_prepared"] = "input_prepared"
    seq: int
    input_hashes: list[str]


class AssistantMessage(_Message):
    type: Literal["assistant_message"] = "assistant_message"
    seq: int
    text: str
    stop_reason: str | None = None
    usage: dict[str, JsonValue] | None = None


class ToolCallEvent(_Message):
    type: Literal["tool_call"] = "tool_call"
    seq: int
    call_id: Text
    name: Text
    arguments: dict[str, JsonValue]
    batch_call_ids: list[str]


class Lifecycle(_Message):
    type: Literal["lifecycle"] = "lifecycle"
    seq: int
    name: Text


class Settled(_Message):
    """The harness reports that the turn has ended; completion is still decided by the core."""

    type: Literal["settled"] = "settled"
    seq: int


class Failed(_Message):
    """The control channel failed; the outcome of in-flight work is unknown."""

    type: Literal["failed"] = "failed"
    seq: int
    reason: Text


HubEvent = Annotated[
    InputPrepared | AssistantMessage | ToolCallEvent | Lifecycle | Settled | Failed,
    Field(discriminator="type"),
]


class EventBatch(_Message):
    events: list[HubEvent]
    state: Literal["running", "failed", "closed"]


class SessionState(_Message):
    session_id: Text
    harness: Text
    state: Literal["running", "failed", "closed"]
    last_seq: int


class HarnessInfo(_Message):
    kind: Text
    version: Text


class Health(_Message):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    harnesses: list[HarnessInfo]


class ErrorBody(_Message):
    """``invalid_request`` means nothing was launched; ``harness_failed`` means unknown."""

    code: Literal["invalid_request", "harness_failed", "not_found", "unauthorized"]
    message: str
