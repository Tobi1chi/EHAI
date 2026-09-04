from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from openai import (
    APIConnectionError,
    AsyncOpenAI,
    BadRequestError,
    omit,
)

from ehai import JsonValue, json_dumps, json_loads, new_id
from ehai.application.builtin_agent import (
    AgentBudget,
    AgentBudgetExceededError,
    AgentCancelledError,
    BuiltinAgent,
    BuiltinAgentLoop,
    BuiltinSession,
    BuiltinSessionEventType,
    BuiltinSessionStateError,
    CancellationToken,
    ExecutionScope,
    IncompleteWriteToolError,
    ModelClient,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    PromptBuilder,
    RecoverableToolError,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolSet,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.ports import ArtifactStore
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanNodeKind, PlanRevision, PlanRevisionStatus
from ehai.domain.workers import (
    AgentSessionRef,
    BuiltinExecutionRef,
    ExecutionHandle,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure import builtin_tools
from ehai.infrastructure.builtin_sessions import SQLiteBuiltinSessionStore
from ehai.infrastructure.builtin_tools import BuiltinToolRuntime
from ehai.infrastructure.openai_responses import (
    OpenAIResponsesModelClient,
    OpenAIResponsesProtocolError,
    OpenAIResponsesUnknownOutcomeError,
)
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers.builtin import _builtin_role_protocol

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
ToolHandler = Callable[[dict[str, JsonValue], CancellationToken], Awaitable[JsonValue]]


def test_builtin_role_protocol_defines_evaluator_and_merge_boundaries() -> None:
    evaluator = _builtin_role_protocol(PlanNodeKind.EVALUATOR)
    merge = _builtin_role_protocol(PlanNodeKind.MERGE)

    assert evaluator is not None
    assert evaluator["content_required_keys"] == [
        "selected_branch_id",
        "pruned_branch_ids",
        "criterion",
        "explanation",
        "compared_artifact_ids",
        "selected_artifact_ids",
    ]
    assert merge is not None and merge["role"] == "merge"
    assert _builtin_role_protocol(PlanNodeKind.WORK) is None


class _ScriptedModelClient:
    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("ScriptedModelClient has no response")
        return self._responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _Dumpable(SimpleNamespace):
    def model_dump(self, *, mode: str) -> dict[str, JsonValue]:
        assert mode == "json"
        return cast(dict[str, JsonValue], self.document)


class _FakeStream:
    def __init__(self, events: tuple[SimpleNamespace, ...]) -> None:
        self.events = events
        self.closed = False
        self._index = 0

    def __aiter__(self) -> _FakeStream:
        self._index = 0
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self._index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self._index]
        self._index += 1
        return event

    async def close(self) -> None:
        self.closed = True


class _FakeResponses:
    def __init__(
        self,
        streams: tuple[_FakeStream | Exception, ...],
        retrievals: tuple[object | Exception, ...] = (),
    ) -> None:
        self._streams = list(streams)
        self._retrievals = list(retrievals)
        self.requests: list[dict[str, object]] = []
        self.retrieve_calls: list[str] = []

    async def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        result = self._streams.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def retrieve(self, response_id: str, **kwargs: object) -> object:
        del kwargs
        self.retrieve_calls.append(response_id)
        result = self._retrievals.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeOpenAI:
    def __init__(
        self,
        streams: tuple[_FakeStream | Exception, ...],
        retrievals: tuple[object | Exception, ...] = (),
    ) -> None:
        self.max_retries = 0
        self.responses = _FakeResponses(streams, retrievals)


def _execution_context(
    tmp_path: Path,
) -> tuple[SQLiteBuiltinSessionStore, BuiltinSession, BuiltinExecutionRef]:
    database = SQLiteDatabase(tmp_path / "builtin.sqlite3")
    project = Project.create("builtin", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "run agent", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("submit final",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = PlanNode(new_id(), "work", "run", required_check_ids=(check_id,))
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(node,),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(goal.goal_id, plan.plan_revision_id, created_at=NOW)
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=NOW)
    profile = WorkerProfile("builtin", WorkerKind.BUILTIN, "scripted")
    endpoint = WorkerEndpoint(
        "builtin",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "builtin",
        1,
    )
    session_ref = AgentSessionRef(
        run.run_id,
        profile.worker_profile_id,
        endpoint.worker_endpoint_id,
        "session",
        True,
        created_at=NOW,
    )
    execution = BuiltinExecutionRef(attempt.attempt_id, session_ref.agent_session_ref_id)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.worker_registry.put_worker_profile(profile)
        uow.worker_registry.put_worker_endpoint(endpoint)
        uow.states.put_run(run)
        uow.states.put_attempt(attempt)
        uow.states.put_agent_session_ref(session_ref)
        uow.states.put_builtin_execution_ref(execution)
        attempt = attempt.assign(
            profile=profile,
            endpoint=endpoint,
            session=session_ref,
        ).bind_execution(ExecutionHandle(builtin=execution))
        uow.states.put_attempt(attempt)
        uow.commit()
    store = SQLiteBuiltinSessionStore(database)
    return store, store.load(session_ref.agent_session_ref_id), execution


def _tool(name: str, *, writes_workspace: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name,
        f"{name} tool",
        {"type": "object", "additionalProperties": False},
        writes_workspace=writes_workspace,
    )


def _run_agent(
    *,
    store: SQLiteBuiltinSessionStore,
    session: BuiltinSession,
    execution: BuiltinExecutionRef,
    client: ModelClient,
    tool_set: ToolSet,
    handlers: dict[str, ToolHandler],
    budget: AgentBudget | None = None,
) -> str:
    async def invoke() -> str:
        arguments = {
            "model_client": client,
            "prompt_builder": PromptBuilder("You are the EHAI Built-in Agent."),
            "tool_set": tool_set,
            "session_store": store,
        }
        loop = (
            BuiltinAgentLoop(**arguments)
            if budget is None
            else BuiltinAgentLoop(**arguments, budget=budget)
        )
        agent = BuiltinAgent(loop)
        scope = ExecutionScope(
            agent=agent,
            session=session,
            tool_executor=ToolExecutor(tool_set, handlers),
            cancellation=CancellationToken(),
        )
        async with scope:
            return await agent.run(scope, execution, "do work", {"run": "test"})

    return asyncio.run(invoke())


def test_builtin_agent_runs_two_steps_with_one_tool_and_replayable_history(
    tmp_path: Path,
) -> None:
    store, session, execution = _execution_context(tmp_path)
    calls: list[dict[str, JsonValue]] = []

    async def inspect(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        calls.append(arguments)
        return {"value": "observed"}

    tool_set = ToolSet((_tool("inspect"),))
    client = _ScriptedModelClient(
        (
            ModelResponse(
                "I will inspect.",
                (ToolCall("call-1", "inspect", {}),),
                provider_response_id="response-1",
            ),
            ModelResponse("Done.", final_text="candidate", provider_response_id="response-2"),
        )
    )

    result = _run_agent(
        store=store,
        session=session,
        execution=execution,
        client=client,
        tool_set=tool_set,
        handlers={"inspect": inspect},
    )

    restored = store.load(session.agent_session_ref_id)
    assert result == "candidate"
    assert calls == [{}]
    assert len(client.requests) == 2
    assert any(message.role.value == "tool" for message in client.requests[1].messages)
    assert restored.is_turn_complete(execution.attempt_id)
    assert restored.final_text(execution.attempt_id) == "candidate"
    assert (
        tuple(event.type for event in restored.events).count(BuiltinSessionEventType.STEP_ENDED)
        == 2
    )


def test_completed_step_recovers_but_incomplete_write_tool_is_not_replayed(
    tmp_path: Path,
) -> None:
    store, session, execution = _execution_context(tmp_path)
    writes = 0

    async def inspect(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        del arguments
        cancellation.raise_if_cancelled()
        return {"ready": True}

    async def patch(arguments: dict[str, JsonValue], cancellation: CancellationToken) -> JsonValue:
        nonlocal writes
        del arguments
        writes += 1
        cancellation.cancel()
        return {"written": True}

    tool_set = ToolSet((_tool("inspect"), _tool("patch", writes_workspace=True)))
    first_client = _ScriptedModelClient(
        (
            ModelResponse("Inspect.", (ToolCall("read-1", "inspect", {}),)),
            ModelResponse("Patch.", (ToolCall("write-1", "patch", {}),)),
        )
    )
    with pytest.raises(AgentCancelledError):
        _run_agent(
            store=store,
            session=session,
            execution=execution,
            client=first_client,
            tool_set=tool_set,
            handlers={"inspect": inspect, "patch": patch},
        )

    restored = store.load(session.agent_session_ref_id)
    assert writes == 1
    assert any(message.role.value == "tool" for message in restored.model_messages())
    assert restored.incomplete_write_call(execution.attempt_id) == "write-1"
    recovery_client = _ScriptedModelClient((ModelResponse("Done", final_text="done"),))
    with pytest.raises(IncompleteWriteToolError, match="cannot be replayed"):
        _run_agent(
            store=store,
            session=restored,
            execution=execution,
            client=recovery_client,
            tool_set=tool_set,
            handlers={"inspect": inspect, "patch": patch},
        )
    assert writes == 1
    assert recovery_client.requests == []


def test_builtin_agent_rejects_unknown_tool_and_illegal_session_transition(
    tmp_path: Path,
) -> None:
    store, session, execution = _execution_context(tmp_path)

    async def inspect(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        del arguments, cancellation
        return None

    tool_set = ToolSet((_tool("inspect"),))
    client = _ScriptedModelClient(
        (
            ModelResponse("Unknown.", (ToolCall("call-1", "missing", {}),)),
            ModelResponse("Recovered.", final_text="candidate"),
        )
    )
    assert (
        _run_agent(
            store=store,
            session=session,
            execution=execution,
            client=client,
            tool_set=tool_set,
            handlers={"inspect": inspect},
        )
        == "candidate"
    )
    restored = store.load(session.agent_session_ref_id)
    assert BuiltinSessionEventType.TOOL_ERROR in tuple(event.type for event in restored.events)
    error_message = next(
        message for message in client.requests[1].messages if message.role is ModelRole.TOOL
    )
    assert json_loads(error_message.content) == {
        "error": {
            "code": "unknown_tool",
            "message": "Tool is not in this Session ToolSet",
            "recoverable": True,
        }
    }

    invalid = BuiltinSession(new_id())
    with pytest.raises(BuiltinSessionStateError, match="turn/start"):
        invalid.append(new_id(), BuiltinSessionEventType.STEP_STARTED, {})


def test_builtin_agent_enforces_turn_budget_before_another_model_step(tmp_path: Path) -> None:
    store, session, execution = _execution_context(tmp_path)

    async def inspect(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        del arguments
        cancellation.raise_if_cancelled()
        return {"ok": True}

    tool_set = ToolSet((_tool("inspect"),))
    client = _ScriptedModelClient(
        (
            ModelResponse("Inspect.", (ToolCall("call-1", "inspect", {}),)),
            ModelResponse("Done.", final_text="candidate"),
        )
    )

    with pytest.raises(AgentBudgetExceededError, match="Step budget"):
        _run_agent(
            store=store,
            session=session,
            execution=execution,
            client=client,
            tool_set=tool_set,
            handlers={"inspect": inspect},
            budget=AgentBudget(1, 1, 60.0, 1024),
        )
    assert len(client.requests) == 1


def test_openai_responses_protocol_streams_function_result_continuation_and_final() -> None:
    function_item = _Dumpable(
        type="function_call",
        call_id="call-1",
        name="inspect",
        arguments="{}",
        document={
            "type": "function_call",
            "call_id": "call-1",
            "name": "inspect",
            "arguments": "{}",
        },
    )
    reasoning_item = _Dumpable(
        type="reasoning",
        document={"type": "reasoning", "summary": []},
    )
    usage = _Dumpable(document={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    first_response = SimpleNamespace(
        id="resp-1",
        status="completed",
        output_text="",
        usage=usage,
        output=(function_item, reasoning_item),
    )
    final_item = _Dumpable(
        type="message",
        document={"type": "message", "role": "assistant", "content": []},
    )
    second_response = SimpleNamespace(
        id="resp-2",
        status="completed",
        output_text="candidate",
        usage=usage,
        output=(final_item,),
    )
    first_stream = _FakeStream(
        (
            SimpleNamespace(type="response.output_item.done", item=function_item),
            SimpleNamespace(type="response.completed", response=first_response),
        )
    )
    second_stream = _FakeStream(
        (
            SimpleNamespace(type="response.output_text.delta", delta="candidate"),
            SimpleNamespace(type="response.completed", response=second_response),
        )
    )
    fake = _FakeOpenAI((first_stream, second_stream))
    profile = WorkerProfile(
        "openai",
        WorkerKind.BUILTIN,
        "gpt-5.6-luna",
        credential_ref=OPENAI_CREDENTIAL_REF,
    )
    client = OpenAIResponsesModelClient(
        profile,
        client=cast(AsyncOpenAI, fake),
        reasoning_effort="high",
        retry_owner="ehai",
    )
    tool = _tool("inspect")
    system = ModelMessage(ModelRole.SYSTEM, "system")
    user = ModelMessage(ModelRole.USER, "user")

    async def invoke() -> tuple[ModelResponse, ModelResponse]:
        first = await client.complete(ModelRequest((system, user), (tool,)))
        assert first.tool_calls
        assistant = ModelMessage(
            ModelRole.ASSISTANT,
            first.content,
            tool_calls=first.tool_calls,
        )
        tool_result = ModelMessage(ModelRole.TOOL, '{"ok":true}', call_id="call-1")
        second = await client.complete(
            ModelRequest(
                (system, user, assistant, tool_result),
                (tool,),
                input_messages=(tool_result,),
                previous_response_id="resp-1",
            )
        )
        await client.aclose()
        return first, second

    first, second = asyncio.run(invoke())

    assert first.tool_calls == (ToolCall("call-1", "inspect", {}),)
    assert first.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert all(item["type"] != "reasoning" for item in first.output_items)
    assert second.final_text == "candidate"
    assert first_stream.closed and second_stream.closed
    assert fake.responses.requests[0]["stream"] is True
    assert fake.responses.requests[0]["store"] is True
    assert fake.responses.requests[0]["parallel_tool_calls"] is False
    assert fake.responses.requests[0]["reasoning"] == {"effort": "high"}
    assert fake.responses.requests[0]["previous_response_id"] is omit
    tools = cast(list[dict[str, object]], fake.responses.requests[0]["tools"])
    assert tools[0]["strict"] is True
    assert fake.responses.requests[1]["previous_response_id"] == "resp-1"
    response_input = cast(list[dict[str, object]], fake.responses.requests[1]["input"])
    assert response_input == [
        {"type": "function_call_output", "call_id": "call-1", "output": '{"ok":true}'}
    ]


def test_openai_responses_replays_durable_history_when_continuation_is_rejected() -> None:
    final_item = _Dumpable(
        type="message",
        document={"type": "message", "role": "assistant", "content": []},
    )
    response = SimpleNamespace(
        id="resp-replayed",
        status="completed",
        output_text="candidate",
        usage=None,
        output=(final_item,),
    )
    stream = _FakeStream(
        (
            SimpleNamespace(type="response.output_text.delta", delta="candidate"),
            SimpleNamespace(type="response.completed", response=response),
        )
    )
    rejection = BadRequestError(
        "previous_response_id requires an OpenAI API-key account for HTTP requests",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://example.invalid/responses"),
        ),
        body={"error": {"message": "previous_response_id rejected"}},
    )
    fake = _FakeOpenAI((rejection, stream))
    profile = WorkerProfile(
        "openai-replay",
        WorkerKind.BUILTIN,
        "gpt-5.6-luna",
        credential_ref=OPENAI_CREDENTIAL_REF,
    )
    client = OpenAIResponsesModelClient(
        profile,
        client=cast(AsyncOpenAI, fake),
        reasoning_effort="high",
        retry_owner="ehai",
    )
    system = ModelMessage(ModelRole.SYSTEM, "system")
    user = ModelMessage(ModelRole.USER, "user")
    assistant = ModelMessage(
        ModelRole.ASSISTANT,
        "",
        tool_calls=(ToolCall("call-1", "inspect", {}),),
    )
    tool_result = ModelMessage(ModelRole.TOOL, '{"ok":true}', call_id="call-1")

    result = asyncio.run(
        client.complete(
            ModelRequest(
                (system, user, assistant, tool_result),
                (_tool("inspect"),),
                input_messages=(tool_result,),
                previous_response_id="resp-1",
            )
        )
    )

    assert result.final_text == "candidate"
    assert len(fake.responses.requests) == 2
    assert fake.responses.requests[0]["previous_response_id"] == "resp-1"
    assert fake.responses.requests[1]["previous_response_id"] is omit
    replayed_input = cast(list[dict[str, object]], fake.responses.requests[1]["input"])
    assert replayed_input == [
        {"role": "user", "content": "user"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "inspect",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call-1", "output": '{"ok":true}'},
    ]


def _connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://example.invalid/responses"))


def _terminal_response(
    response_id: str,
    status: str,
    *,
    output_text: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=response_id,
        status=status,
        output_text=output_text,
        usage=None,
        output=(),
    )


def _client_with(
    fake: _FakeOpenAI,
    **options: object,
) -> OpenAIResponsesModelClient:
    profile = WorkerProfile(
        "openai-longrunning",
        WorkerKind.BUILTIN,
        "gpt-5.6-luna",
        credential_ref=OPENAI_CREDENTIAL_REF,
    )
    return OpenAIResponsesModelClient(
        profile,
        client=cast(AsyncOpenAI, fake),
        retry_delay_seconds=0.0,
        poll_interval_seconds=0.0,
        retry_owner="ehai",
        **options,
    )


def _single_tool_request() -> ModelRequest:
    return ModelRequest(
        (ModelMessage(ModelRole.SYSTEM, "system"), ModelMessage(ModelRole.USER, "user")),
        (_tool("inspect"),),
    )


def test_openai_responses_recovers_same_response_id_after_stream_drop() -> None:
    created = SimpleNamespace(
        type="response.created",
        response=SimpleNamespace(id="resp-drop", status="queued"),
    )
    dropped_stream = _FakeStream((created,))
    completed = _terminal_response("resp-drop", "completed", output_text="recovered")
    fake = _FakeOpenAI((dropped_stream,), (completed,))
    client = _client_with(fake)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "recovered"
    assert len(fake.responses.requests) == 1
    assert fake.responses.retrieve_calls == ["resp-drop"]
    assert dropped_stream.closed


def test_openai_responses_retries_http_failures_up_to_four_times() -> None:
    final_item = _Dumpable(
        type="message",
        document={"type": "message", "role": "assistant", "content": []},
    )
    response = SimpleNamespace(
        id="resp-retry",
        status="completed",
        output_text="after retries",
        usage=None,
        output=(final_item,),
    )
    good_stream = _FakeStream(
        (
            SimpleNamespace(type="response.output_text.delta", delta="after retries"),
            SimpleNamespace(type="response.completed", response=response),
        )
    )
    fake = _FakeOpenAI(
        (
            _connection_error(),
            _connection_error(),
            _connection_error(),
            _connection_error(),
            good_stream,
        )
    )
    client = _client_with(fake, supports_idempotent_create=True)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "after retries"
    assert len(fake.responses.requests) == 5

    exhausted = _FakeOpenAI(tuple(_connection_error() for _ in range(5)))
    exhausted_client = _client_with(exhausted, supports_idempotent_create=True)
    with pytest.raises(OpenAIResponsesProtocolError, match="HTTP retries"):
        asyncio.run(exhausted_client.complete(_single_tool_request()))
    assert len(exhausted.responses.requests) == 5


def test_openai_responses_reuses_one_idempotency_key_for_logical_create_retries() -> None:
    final_item = _Dumpable(
        type="message",
        document={"type": "message", "role": "assistant", "content": []},
    )
    response = SimpleNamespace(
        id="resp-idempotent",
        status="completed",
        output_text="done",
        usage=None,
        output=(final_item,),
    )
    stream = _FakeStream((SimpleNamespace(type="response.completed", response=response),))
    fake = _FakeOpenAI((_connection_error(), stream))
    client = _client_with(fake, supports_idempotent_create=True)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "done"
    keys = [
        cast(dict[str, str], request["extra_headers"])["Idempotency-Key"]
        for request in fake.responses.requests
    ]
    assert len(keys) == 2
    assert len(set(keys)) == 1
    assert keys[0].startswith("ehai-")


@pytest.mark.parametrize("background", [False, True])
def test_openai_responses_does_not_repeat_ambiguous_create_without_endpoint_support(
    background: bool,
) -> None:
    fake = _FakeOpenAI((_connection_error(),))
    client = _client_with(
        fake,
        background=background,
        supports_idempotent_create=False,
    )

    with pytest.raises(OpenAIResponsesUnknownOutcomeError, match="outcome is unknown"):
        asyncio.run(client.complete(_single_tool_request()))

    assert len(fake.responses.requests) == 1


def test_openai_responses_requires_explicit_retry_owner_for_injected_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOpenAI(())
    profile = WorkerProfile(
        "openai-retry-owner",
        WorkerKind.BUILTIN,
        "gpt-5.6-luna",
        credential_ref=OPENAI_CREDENTIAL_REF,
    )

    with pytest.raises(ValueError, match="declare retry_owner"):
        OpenAIResponsesModelClient(profile, client=cast(AsyncOpenAI, fake))

    fake.max_retries = 2
    with pytest.raises(ValueError, match="max_retries=0"):
        OpenAIResponsesModelClient(
            profile,
            client=cast(AsyncOpenAI, fake),
            retry_owner="ehai",
        )

    client_owned = OpenAIResponsesModelClient(
        profile,
        client=cast(AsyncOpenAI, fake),
        retry_owner="client",
        max_http_retries=0,
    )
    assert client_owned.retry_owner == "client"

    with pytest.raises(ValueError, match="internally created"):
        OpenAIResponsesModelClient(
            profile,
            retry_owner="client",
            max_http_retries=0,
        )

    created_with: list[dict[str, object]] = []

    def create_client(**kwargs: object) -> _FakeOpenAI:
        created_with.append(kwargs)
        created = _FakeOpenAI(())
        created.base_url = SimpleNamespace(host="api.openai.com")
        return created

    monkeypatch.setattr(
        "ehai.infrastructure.openai_responses.AsyncOpenAI",
        create_client,
    )
    internally_owned = OpenAIResponsesModelClient(profile)
    assert created_with == [{"max_retries": 0}]
    assert internally_owned.retry_owner == "ehai"
    assert internally_owned.supports_idempotent_create is True


def test_openai_responses_stream_reconnects_within_five_attempt_budget() -> None:
    created = SimpleNamespace(
        type="response.created",
        response=SimpleNamespace(id="resp-reconnect", status="in_progress"),
    )
    dropped_stream = _FakeStream((created,))
    completed = _terminal_response("resp-reconnect", "completed", output_text="reconnected")
    fake = _FakeOpenAI(
        (dropped_stream,),
        (
            _connection_error(),
            _connection_error(),
            _connection_error(),
            _connection_error(),
            completed,
        ),
    )
    client = _client_with(fake)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "reconnected"
    assert len(fake.responses.requests) == 1
    assert len(fake.responses.retrieve_calls) == 5

    failing = _FakeOpenAI((_FakeStream((created,)),))
    failing.responses._retrievals = [_connection_error() for _ in range(6)]
    failing_client = _client_with(failing)
    with pytest.raises(OpenAIResponsesProtocolError, match="reconnects"):
        asyncio.run(failing_client.complete(_single_tool_request()))
    assert len(failing.responses.retrieve_calls) == 5


def test_openai_responses_keeps_waiting_while_response_is_queued_or_in_progress() -> None:
    created = SimpleNamespace(
        type="response.created",
        response=SimpleNamespace(id="resp-slow", status="queued"),
    )
    dropped_stream = _FakeStream((created,))
    fake = _FakeOpenAI(
        (dropped_stream,),
        (
            _terminal_response("resp-slow", "in_progress"),
            _terminal_response("resp-slow", "in_progress"),
            _terminal_response("resp-slow", "completed", output_text="finally done"),
        ),
    )
    client = _client_with(fake)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "finally done"
    assert len(fake.responses.requests) == 1
    assert len(fake.responses.retrieve_calls) == 3


def test_openai_responses_background_mode_polls_retrieve_until_terminal() -> None:
    queued = _terminal_response("resp-bg", "queued")
    fake = _FakeOpenAI(
        (queued,),
        (
            _terminal_response("resp-bg", "in_progress"),
            _terminal_response("resp-bg", "completed", output_text="background result"),
        ),
    )
    client = _client_with(fake, background=True)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "background result"
    assert fake.responses.requests[0]["background"] is True
    assert "stream" not in fake.responses.requests[0]
    assert fake.responses.retrieve_calls == ["resp-bg", "resp-bg"]


def test_openai_responses_background_falls_back_to_streaming_when_rejected() -> None:
    final_item = _Dumpable(
        type="message",
        document={"type": "message", "role": "assistant", "content": []},
    )
    response = SimpleNamespace(
        id="resp-fallback",
        status="completed",
        output_text="fallback result",
        usage=None,
        output=(final_item,),
    )
    stream = _FakeStream(
        (
            SimpleNamespace(type="response.output_text.delta", delta="fallback result"),
            SimpleNamespace(type="response.completed", response=response),
        )
    )
    rejection = BadRequestError(
        "background mode is not supported by this endpoint",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://example.invalid/responses"),
        ),
        body={"error": {"message": "background is not supported"}},
    )
    fake = _FakeOpenAI((rejection, stream))
    client = _client_with(fake, background=True)

    result = asyncio.run(client.complete(_single_tool_request()))

    assert result.final_text == "fallback result"
    assert len(fake.responses.requests) == 2
    assert fake.responses.requests[0]["background"] is True
    assert fake.responses.requests[1]["stream"] is True
    background_headers = cast(dict[str, str], fake.responses.requests[0]["extra_headers"])
    stream_headers = cast(dict[str, str], fake.responses.requests[1]["extra_headers"])
    assert background_headers["Idempotency-Key"] == stream_headers["Idempotency-Key"]
    assert fake.responses.retrieve_calls == []


def test_builtin_tool_runtime_fixes_controlled_patch_and_final_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "example.txt"
    target.write_text("before", encoding="utf-8")
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
        allowed_commands=(),
    )
    definitions = {definition.name: definition for definition in runtime.tool_set.definitions}

    async def invoke() -> tuple[JsonValue, JsonValue]:
        token = CancellationToken()
        patched = await runtime.executor.execute(
            ToolCall(
                "patch-1",
                "workspace_patch",
                {"path": "example.txt", "expected": "before", "replacement": "after"},
            ),
            token,
        )
        candidate = await runtime.executor.execute(
            ToolCall(
                "final-1",
                "submit_candidate",
                {"name": "result.txt", "media_type": "text/plain", "content": "after"},
            ),
            token,
        )
        await runtime.executor.aclose()
        return patched, candidate

    patched, candidate = asyncio.run(invoke())

    assert set(definitions) == {
        "artifact_read",
        "workspace_list",
        "workspace_search",
        "workspace_read",
        "workspace_patch",
        "submit_candidate",
    }
    assert definitions["submit_candidate"].ends_turn
    assert definitions["workspace_patch"].writes_workspace
    path_schema = definitions["workspace_read"].input_schema["properties"]
    assert isinstance(path_schema, dict)
    assert path_schema["path"] == {"type": "string", "minLength": 1}
    patch_schema = definitions["workspace_patch"].input_schema["properties"]
    assert isinstance(patch_schema, dict)
    assert patch_schema["replacement"] == {"type": "string"}
    assert patched == {"path": "example.txt", "changed": True}
    assert candidate == {
        "name": "result.txt",
        "media_type": "text/plain",
        "content": "after",
    }
    assert target.read_text(encoding="utf-8") == "after"


def test_builtin_agent_returns_durable_read_error_to_model(tmp_path: Path) -> None:
    store, session, execution = _execution_context(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
    )
    client = _ScriptedModelClient(
        (
            ModelResponse(
                "Read missing file.",
                (ToolCall("read-missing", "workspace_read", {"path": "missing.txt"}),),
            ),
            ModelResponse(
                "Submit after correcting the lookup.",
                (
                    ToolCall(
                        "submit-after-error",
                        "submit_candidate",
                        {"name": "result.txt", "media_type": "text/plain", "content": "ok"},
                    ),
                ),
            ),
        )
    )

    async def invoke() -> str:
        loop = BuiltinAgentLoop(
            model_client=client,
            prompt_builder=PromptBuilder("Use the fixed Tools."),
            tool_set=runtime.tool_set,
            session_store=store,
        )
        agent = BuiltinAgent(loop)
        scope = ExecutionScope(
            agent=agent,
            session=session,
            tool_executor=runtime.executor,
            cancellation=CancellationToken(),
        )
        async with scope:
            return await agent.run(scope, execution, "read then submit", {})

    assert json_loads(asyncio.run(invoke()))["content"] == "ok"
    restored = store.load(session.agent_session_ref_id)
    types = tuple(event.type for event in restored.events)
    assert BuiltinSessionEventType.MODEL_MESSAGE in types
    assert BuiltinSessionEventType.TOOL_CALLED in types
    assert BuiltinSessionEventType.TOOL_ERROR in types
    error = next(
        event.payload["error"]
        for event in restored.events
        if event.type is BuiltinSessionEventType.TOOL_ERROR
    )
    assert error == {
        "code": "not_found",
        "message": "Workspace file does not exist",
        "recoverable": True,
    }


def test_workspace_read_rejects_oversized_file_before_full_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace-read-limit"
    workspace.mkdir()
    (workspace / "large.txt").write_bytes(b"x" * 9)
    monkeypatch.setattr(builtin_tools, "_MAX_TOOL_OUTPUT_BYTES", 8)

    def unexpected_read_bytes(_path: Path) -> bytes:
        raise AssertionError("workspace_read must not allocate through Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read_bytes)
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
    )

    with pytest.raises(RecoverableToolError, match="exceeds"):
        asyncio.run(
            runtime.executor.execute(
                ToolCall("read-large", "workspace_read", {"path": "large.txt"}),
                CancellationToken(),
            )
        )


def test_workspace_search_bounds_lines_and_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace-search-limit"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle" + ("x" * 100), encoding="utf-8")
    (workspace / "b.txt").write_text("other", encoding="utf-8")
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
    )
    monkeypatch.setattr(builtin_tools, "_MAX_SEARCH_LINE_BYTES", 8)

    bounded = asyncio.run(
        runtime.executor.execute(
            ToolCall("search-line", "workspace_search", {"path": ".", "query": "needle"}),
            CancellationToken(),
        )
    )
    assert isinstance(bounded, dict)
    matches = cast(list[dict[str, JsonValue]], bounded["matches"])
    assert matches[0]["text"] == "needlexx"
    assert matches[0]["text_truncated"] is True

    monkeypatch.setattr(builtin_tools, "_MAX_SEARCH_ENTRIES", 1)
    truncated = asyncio.run(
        runtime.executor.execute(
            ToolCall("search-tree", "workspace_search", {"path": ".", "query": "absent"}),
            CancellationToken(),
        )
    )
    assert isinstance(truncated, dict)
    assert truncated["truncated"] is True
    assert truncated["truncation_reason"] == "traversal_limit"


def test_workspace_search_yields_to_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace-search-cancel"
    workspace.mkdir()
    for index in range(100):
        (workspace / f"{index:03}.txt").write_text("searchable", encoding="utf-8")
    monkeypatch.setattr(builtin_tools, "_SEARCH_YIELD_INTERVAL", 1)
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
    )

    async def invoke() -> None:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            runtime.executor.execute(
                ToolCall("search-cancel", "workspace_search", {"path": ".", "query": "missing"}),
                cancellation,
            )
        )
        await asyncio.sleep(0)
        cancellation.cancel()
        with pytest.raises(AgentCancelledError):
            await task

    asyncio.run(invoke())


def test_incomplete_read_only_tool_is_replayed_after_recovery(tmp_path: Path) -> None:
    store, session, execution = _execution_context(tmp_path)
    reads = 0

    async def interrupted_read(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        nonlocal reads
        del arguments
        reads += 1
        cancellation.cancel()
        return {"read": True}

    tool_set = ToolSet((_tool("inspect"),))
    with pytest.raises(AgentCancelledError):
        _run_agent(
            store=store,
            session=session,
            execution=execution,
            client=_ScriptedModelClient(
                (ModelResponse("Inspect.", (ToolCall("read-1", "inspect", {}),)),)
            ),
            tool_set=tool_set,
            handlers={"inspect": interrupted_read},
        )

    restored = store.load(session.agent_session_ref_id)
    assert restored.incomplete_write_call(execution.attempt_id) is None
    assert restored.incomplete_tool_call(execution.attempt_id) is not None

    async def successful_read(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        nonlocal reads
        del arguments
        cancellation.raise_if_cancelled()
        reads += 1
        return {"read": True}

    final_client = _ScriptedModelClient((ModelResponse("Done.", final_text="candidate"),))
    assert (
        _run_agent(
            store=store,
            session=restored,
            execution=execution,
            client=final_client,
            tool_set=tool_set,
            handlers={"inspect": successful_read},
        )
        == "candidate"
    )
    assert reads == 2
    assert any(message.role is ModelRole.TOOL for message in final_client.requests[0].messages)


def test_builtin_command_uses_trusted_resolution_and_rejects_qualified_argv(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable_name = Path(sys.executable).name
    workspace_executable = workspace / executable_name
    shutil.copy2(sys.executable, workspace_executable)
    script = "import sys; print(sys.executable)"
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
        allowed_commands=((executable_name, "-c", script),),
    )
    command_definition = runtime.tool_set.require("command")
    properties = cast(
        dict[str, dict[str, JsonValue]], command_definition.input_schema["properties"]
    )
    assert "enum" not in properties["argv"]
    assert json_dumps([[executable_name, "-c", script]]) in command_definition.description

    async def invoke() -> JsonValue:
        token = CancellationToken()
        try:
            result = await runtime.executor.execute(
                ToolCall(
                    "allowed",
                    "command",
                    {
                        "argv": [
                            executable_name,
                            "-c",
                            script,
                        ]
                    },
                ),
                token,
            )
            for index, value in enumerate(
                (
                    f".\\{executable_name}",
                    f"subdir/{executable_name}",
                    str(workspace_executable.resolve()),
                )
            ):
                with pytest.raises(RecoverableToolError, match="unqualified"):
                    await runtime.executor.execute(
                        ToolCall(f"qualified-{index}", "command", {"argv": [value]}),
                        token,
                    )
            with pytest.raises(RecoverableToolError, match="not allowed"):
                await runtime.executor.execute(
                    ToolCall("denied", "command", {"argv": ["definitely-not-allowed"]}),
                    token,
                )
            with pytest.raises(RecoverableToolError, match="not allowed"):
                await runtime.executor.execute(
                    ToolCall(
                        "arguments-denied",
                        "command",
                        {"argv": [executable_name, "-c", "print('different')"]},
                    ),
                    token,
                )
            return result
        finally:
            await runtime.executor.aclose()

    result = asyncio.run(invoke())

    assert isinstance(result, dict)
    assert result["exit_code"] == 0
    stdout = result["stdout"]
    assert isinstance(stdout, str)
    assert Path(stdout.strip()).samefile(sys.executable)


def test_builtin_command_strips_credentials_and_redacts_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable_name = Path(sys.executable).name
    script = (
        "import os; "
        "print(os.getenv('OPENAI_API_KEY')); "
        "print(os.getenv('OPENAI_BASE_URL')); "
        "print('api_key=sk-visible-sentinel')"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-sentinel")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://sentinel.invalid")
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
        allowed_commands=((executable_name, "-c", script),),
    )

    result = asyncio.run(
        runtime.executor.execute(
            ToolCall("environment", "command", {"argv": [executable_name, "-c", script]}),
            CancellationToken(),
        )
    )

    assert isinstance(result, dict)
    assert result["exit_code"] == 0
    assert str(result["stdout"]).splitlines() == [
        "None",
        "None",
        "api_key=[REDACTED]",
    ]


def test_builtin_command_stops_process_tree_on_cancellation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_pid_file = workspace / "child.pid"
    executable_name = Path(sys.executable).name
    child_script = "import time; time.sleep(60)"
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
        allowed_commands=((executable_name, "-c", parent_script),),
    )

    async def invoke() -> int:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            runtime.executor.execute(
                ToolCall(
                    "cancel-tree",
                    "command",
                    {"argv": [executable_name, "-c", parent_script]},
                ),
                cancellation,
            )
        )
        for _ in range(200):
            if child_pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        cancellation.cancel()
        with pytest.raises(AgentCancelledError):
            await task
        return child_pid

    child_pid = asyncio.run(invoke())
    for _ in range(100):
        if not _process_is_running(child_pid):
            break
        time.sleep(0.02)
    assert not _process_is_running(child_pid)


def test_builtin_command_enforces_output_limit_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable_name = Path(sys.executable).name
    script = "import sys, time; sys.stdout.write('x'*65536); sys.stdout.flush(); time.sleep(60)"
    monkeypatch.setattr(builtin_tools, "_MAX_TOOL_OUTPUT_BYTES", 1024)
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
        allowed_commands=((executable_name, "-c", script),),
        command_timeout_seconds=10,
    )

    with pytest.raises(ValueError, match="output exceeds"):
        asyncio.run(
            runtime.executor.execute(
                ToolCall("bounded", "command", {"argv": [executable_name, "-c", script]}),
                CancellationToken(),
            )
        )


def test_scripted_builtin_agent_reads_modifies_runs_command_and_submits_candidate(
    tmp_path: Path,
) -> None:
    store, session, execution = _execution_context(tmp_path)
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    target = workspace / "task.txt"
    target.write_text("before", encoding="utf-8")
    runtime = BuiltinToolRuntime(
        artifact_store=cast(ArtifactStore, object()),
        workspace=workspace,
        allowed_commands=(("git", "--version"),),
    )
    client = _ScriptedModelClient(
        (
            ModelResponse(
                "Read.",
                (ToolCall("read-1", "workspace_read", {"path": "task.txt"}),),
            ),
            ModelResponse(
                "Patch.",
                (
                    ToolCall(
                        "patch-1",
                        "workspace_patch",
                        {
                            "path": "task.txt",
                            "expected": "before",
                            "replacement": "after",
                        },
                    ),
                ),
            ),
            ModelResponse(
                "Verify.",
                (ToolCall("command-1", "command", {"argv": ["git", "--version"]}),),
            ),
            ModelResponse(
                "Submit.",
                (
                    ToolCall(
                        "final-1",
                        "submit_candidate",
                        {
                            "name": "task.txt",
                            "media_type": "text/plain",
                            "content": "after",
                        },
                    ),
                ),
            ),
        )
    )

    async def invoke() -> str:
        loop = BuiltinAgentLoop(
            model_client=client,
            prompt_builder=PromptBuilder("Use only the fixed EHAI Tools."),
            tool_set=runtime.tool_set,
            session_store=store,
        )
        agent = BuiltinAgent(loop)
        scope = ExecutionScope(
            agent=agent,
            session=session,
            tool_executor=runtime.executor,
            cancellation=CancellationToken(),
        )
        async with scope:
            return await agent.run(scope, execution, "update task.txt", {})

    final = json_loads(asyncio.run(invoke()))

    assert final == {"name": "task.txt", "media_type": "text/plain", "content": "after"}
    assert target.read_text(encoding="utf-8") == "after"
    assert store.load(session.agent_session_ref_id).is_turn_complete(execution.attempt_id)


def _process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    synchronize = 0x00100000
    wait_timeout = 258
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)
