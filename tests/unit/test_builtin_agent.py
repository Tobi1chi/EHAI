from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError, omit

from ehai import JsonValue, json_loads, new_id
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
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolSet,
    UnknownToolError,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.ports import ArtifactStore
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanRevision, PlanRevisionStatus
from ehai.domain.workers import (
    AgentSessionRef,
    BuiltinExecutionRef,
    ExecutionHandle,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.builtin_sessions import SQLiteBuiltinSessionStore
from ehai.infrastructure.builtin_tools import BuiltinToolRuntime
from ehai.infrastructure.openai_responses import OpenAIResponsesModelClient
from ehai.infrastructure.sqlite import SQLiteDatabase

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
ToolHandler = Callable[[dict[str, JsonValue], CancellationToken], Awaitable[JsonValue]]


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
    def __init__(self, streams: tuple[_FakeStream | Exception, ...]) -> None:
        self._streams = list(streams)
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> _FakeStream:
        self.requests.append(kwargs)
        result = self._streams.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeOpenAI:
    def __init__(self, streams: tuple[_FakeStream | Exception, ...]) -> None:
        self.responses = _FakeResponses(streams)


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
        (ModelResponse("Unknown.", (ToolCall("call-1", "missing", {}),)),)
    )
    with pytest.raises(UnknownToolError):
        _run_agent(
            store=store,
            session=session,
            execution=execution,
            client=client,
            tool_set=tool_set,
            handlers={"inspect": inspect},
        )

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
        allowed_commands=("git.exe",),
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
        "command",
        "submit_candidate",
    }
    assert definitions["submit_candidate"].ends_turn
    assert definitions["workspace_patch"].writes_workspace
    assert patched == {"path": "example.txt", "changed": True}
    assert candidate == {
        "name": "result.txt",
        "media_type": "text/plain",
        "content": "after",
    }
    assert target.read_text(encoding="utf-8") == "after"


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
        allowed_commands=("git",),
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
