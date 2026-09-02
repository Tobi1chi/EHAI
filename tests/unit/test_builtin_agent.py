from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ehai import JsonValue, new_id
from ehai.application.builtin_agent import (
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
    ModelRequest,
    ModelResponse,
    PromptBuilder,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolSet,
    UnknownToolError,
)
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
) -> str:
    async def invoke() -> str:
        loop = BuiltinAgentLoop(
            model_client=client,
            prompt_builder=PromptBuilder("You are the EHAI Built-in Agent."),
            tool_set=tool_set,
            session_store=store,
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
