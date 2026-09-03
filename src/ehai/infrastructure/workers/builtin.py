"""Standalone Built-in Agent RuntimeConnector with no Codex dependency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path

from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, json_dumps, new_id, normalize_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.builtin_agent import (
    DEFAULT_AGENT_BUDGET,
    AgentBudget,
    BuiltinAgent,
    BuiltinAgentLoop,
    BuiltinSession,
    BuiltinSessionEventType,
    BuiltinSessionStore,
    CancellationToken,
    ExecutionScope,
    ModelClient,
    PromptBuilder,
)
from ehai.application.execution_policy import RetrySafety
from ehai.application.orchestrator import Orchestrator
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.workers import CandidateArtifact, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.workers import (
    AttemptActivity,
    BuiltinExecutionRef,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.builtin_tools import BuiltinToolRuntime
from ehai.infrastructure.openai_responses import OpenAIResponsesModelClient

UnitOfWorkFactory = Callable[[], UnitOfWork]
ModelClientFactory = Callable[[WorkerProfile, WorkerRequest], ModelClient]
WorkspaceResolver = Callable[[ID], Path | None]

_DEFAULT_SYSTEM_PROMPT = """You are the EHAI Built-in Agent. Work only inside the assigned
Workspace and use only the provided tools. Complete the requested PlanNode, validate the
result with an allowed command when appropriate, and finish only by calling submit_candidate.
Do not claim that a Run or PlanNode is complete; EHAI Check and Gate own completion."""


class BuiltinAgentConnector:
    """Run one independent EHAI Agent scope per Attempt and expose Runtime events."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        orchestrator: Orchestrator,
        session_store: BuiltinSessionStore,
        artifact_store: ArtifactStore,
        profile: WorkerProfile,
        default_workspace: Path,
        allowed_commands: tuple[tuple[str, ...], ...] = (),
        reasoning_effort: ReasoningEffort = None,
        model_client_factory: ModelClientFactory | None = None,
        workspace_resolver: WorkspaceResolver | None = None,
        budget: AgentBudget = DEFAULT_AGENT_BUDGET,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
        command_timeout_seconds: float = 120.0,
        id_factory: Callable[[], ID] = new_id,
    ) -> None:
        if profile.kind is not WorkerKind.BUILTIN:
            raise ValueError("BuiltinAgentConnector requires a Built-in WorkerProfile")
        self._uow_factory = uow_factory
        self._orchestrator = orchestrator
        self._session_store = session_store
        self._artifact_store = artifact_store
        self._profile = profile
        self._default_workspace = default_workspace.resolve(strict=True)
        self._allowed_commands = tuple(allowed_commands)
        self._reasoning_effort = reasoning_effort
        self._model_client_factory = model_client_factory or self._create_model_client
        self._workspace_resolver = workspace_resolver
        self._budget = budget
        self._system_prompt = system_prompt
        self._command_timeout_seconds = command_timeout_seconds
        self._id_factory = id_factory
        self._requests: dict[ID, WorkerRequest] = {}
        self._workspaces: dict[ID, Path] = {}
        self._executions: dict[ID, ConnectorExecution] = {}
        self._tasks: dict[ID, asyncio.Task[WorkerResult]] = {}
        self._terminal_outcomes: dict[ID, tuple[str | None, str]] = {}
        self._cancellations: dict[ID, CancellationToken] = {}

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        attempt_id = request.attempt_id
        existing = self._executions.get(attempt_id)
        if existing is not None:
            return existing
        workspace = self._workspace_path(request.workspace)
        execution = ConnectorExecution(
            attempt_id,
            str(self._id_factory()),
            str(self._id_factory()),
            True,
        )
        self._requests[attempt_id] = request.request
        self._workspaces[attempt_id] = workspace
        self._executions[attempt_id] = execution
        return execution

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        self._require_execution(execution)
        if execution.attempt_id in self._terminal_outcomes:
            failure, cursor = self._terminal_outcomes[execution.attempt_id]
            if failure is not None:
                yield _failed_event(execution.attempt_id, failure, cursor=cursor)
                return
            result = self._completed_result(execution)
            yield _candidate_event(execution.attempt_id, result)
            yield _completed_event(execution.attempt_id)
            return
        task = self._tasks.get(execution.attempt_id)
        if task is None:
            task = asyncio.create_task(
                self._run_execution(execution),
                name=f"ehai-builtin-{execution.attempt_id}",
            )
            self._tasks[execution.attempt_id] = task
            task.add_done_callback(partial(self._record_terminal_outcome, execution.attempt_id))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            yield _failed_event(
                execution.attempt_id,
                "Built-in Agent execution was cancelled",
                cursor="cancelled",
            )
            return
        except Exception as error:
            yield _failed_event(execution.attempt_id, _failure_reason(error))
            return
        yield _candidate_event(execution.attempt_id, result)
        yield _completed_event(execution.attempt_id)

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        self._require_execution(execution)
        if execution.attempt_id in self._terminal_outcomes:
            return AttemptActivity.STALLED
        task = self._tasks.get(execution.attempt_id)
        return (
            AttemptActivity.RUNNING if task is None or not task.done() else AttemptActivity.STALLED
        )

    async def cancel(self, execution: ConnectorExecution) -> None:
        self._require_execution(execution)
        cancellation = self._cancellations.get(execution.attempt_id)
        if cancellation is not None:
            cancellation.cancel()
        task = self._tasks.get(execution.attempt_id)
        if task is not None and not task.done():
            task.cancel()

    async def recover(
        self,
        request: ConnectorRecoveryRequest,
    ) -> ConnectorExecution | None:
        attempt_id = normalize_id(request.attempt_id)
        with self._uow_factory() as uow:
            attempt = uow.states.get_attempt(attempt_id)
            if attempt is None or attempt.execution_handle is None:
                return None
            handle = attempt.execution_handle
            reference = handle.builtin
            if reference is None:
                return None
            session = uow.states.get_agent_session_ref(reference.agent_session_ref_id)
        if session is None:
            return None
        if (
            session.provider_session_id != request.provider_session_id
            or str(reference.builtin_execution_id) != request.provider_execution_id
        ):
            return None
        execution = ConnectorExecution(
            attempt_id,
            request.provider_session_id,
            request.provider_execution_id,
            True,
        )
        self._executions[attempt_id] = execution
        resolved = (
            None if self._workspace_resolver is None else self._workspace_resolver(attempt_id)
        )
        self._workspaces[attempt_id] = (
            self._default_workspace if resolved is None else resolved.resolve(strict=True)
        )
        return execution

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for cancellation in self._cancellations.values():
            cancellation.cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_execution(self, execution: ConnectorExecution) -> WorkerResult:
        request = self._requests.get(execution.attempt_id)
        if request is None:
            request = self._orchestrator.worker_request_for_attempt(execution.attempt_id)
        workspace = self._workspaces.get(execution.attempt_id, self._default_workspace)
        reference, session = self._execution_context(execution)
        cancellation = CancellationToken()
        self._cancellations[execution.attempt_id] = cancellation
        tools = BuiltinToolRuntime(
            artifact_store=self._artifact_store,
            workspace=workspace,
            allowed_commands=self._allowed_commands,
            command_timeout_seconds=self._command_timeout_seconds,
        )
        loop = BuiltinAgentLoop(
            model_client=self._model_client_factory(self._profile, request),
            prompt_builder=PromptBuilder(self._system_prompt),
            tool_set=tools.tool_set,
            session_store=self._session_store,
            budget=self._budget,
        )
        agent = BuiltinAgent(loop)
        scope = ExecutionScope(
            agent=agent,
            session=session,
            tool_executor=tools.executor,
            cancellation=cancellation,
        )
        try:
            async with scope:
                await agent.run(
                    scope,
                    reference,
                    request.plan_node.instruction,
                    _builtin_context(request),
                )
                return _candidate_result(session, execution.attempt_id)
        finally:
            self._cancellations.pop(execution.attempt_id, None)

    def _execution_context(
        self,
        execution: ConnectorExecution,
    ) -> tuple[BuiltinExecutionRef, BuiltinSession]:
        with self._uow_factory() as uow:
            attempt = uow.states.get_attempt(execution.attempt_id)
            if attempt is None or attempt.execution_handle is None:
                raise RuntimeError(f"Attempt {execution.attempt_id} has no execution binding")
            reference = attempt.execution_handle.builtin
            if reference is None:
                raise RuntimeError(f"Attempt {execution.attempt_id} is not a Built-in execution")
        return reference, self._session_store.load(reference.agent_session_ref_id)

    def _completed_result(self, execution: ConnectorExecution) -> WorkerResult:
        _, session = self._execution_context(execution)
        return _candidate_result(session, execution.attempt_id)

    def _record_terminal_outcome(
        self,
        attempt_id: ID,
        task: asyncio.Task[WorkerResult],
    ) -> None:
        if self._tasks.get(attempt_id) is task:
            self._tasks.pop(attempt_id, None)
        self._requests.pop(attempt_id, None)
        self._workspaces.pop(attempt_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            self._terminal_outcomes[attempt_id] = (
                "Built-in Agent execution was cancelled",
                "cancelled",
            )
        except Exception as error:
            self._terminal_outcomes[attempt_id] = (_failure_reason(error), "failed")
        else:
            self._terminal_outcomes[attempt_id] = (None, "completed")

    def _create_model_client(
        self,
        profile: WorkerProfile,
        request: WorkerRequest,
    ) -> ModelClient:
        del request
        return OpenAIResponsesModelClient(
            profile,
            reasoning_effort=self._reasoning_effort,
        )

    def _workspace_path(self, value: str | None) -> Path:
        workspace = self._default_workspace if value is None else Path(value).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Built-in Agent Workspace must be a directory")
        return workspace

    def _require_execution(self, execution: ConnectorExecution) -> None:
        existing = self._executions.get(execution.attempt_id)
        if existing != execution:
            raise RuntimeError(f"Built-in execution {execution.attempt_id} is not registered")


def _failed_event(attempt_id: ID, reason: str, *, cursor: str = "failed") -> WorkerEvent:
    return WorkerEvent(
        f"builtin:{attempt_id}:{cursor}",
        attempt_id,
        WorkerEventType.FAILED,
        cursor,
        reason=reason,
        retry_safety=RetrySafety.UNKNOWN,
    )


def _candidate_event(attempt_id: ID, result: WorkerResult) -> WorkerEvent:
    return WorkerEvent(
        f"builtin:{attempt_id}:candidate",
        attempt_id,
        WorkerEventType.CANDIDATE,
        "candidate",
        result=result,
    )


def _completed_event(attempt_id: ID) -> WorkerEvent:
    return WorkerEvent(
        f"builtin:{attempt_id}:completed",
        attempt_id,
        WorkerEventType.COMPLETED,
        "completed",
    )


def _candidate_result(session: BuiltinSession, attempt_id: ID) -> WorkerResult:
    if not session.is_turn_complete(attempt_id):
        raise RuntimeError(f"Built-in Attempt {attempt_id} has no completed Turn")
    call_id: str | None = None
    for event in reversed(session.events):
        if event.attempt_id != attempt_id or event.type is not BuiltinSessionEventType.TOOL_CALLED:
            continue
        if event.payload.get("name") == "submit_candidate":
            value = event.payload.get("call_id")
            call_id = value if isinstance(value, str) else None
            break
    if call_id is None:
        raise RuntimeError("Built-in Agent must finish with submit_candidate")
    submitted: dict[str, JsonValue] | None = None
    for event in session.events:
        if event.attempt_id != attempt_id or event.type is not BuiltinSessionEventType.TOOL_RESULT:
            continue
        if event.payload.get("call_id") == call_id:
            value = event.payload.get("result")
            submitted = value if isinstance(value, dict) else None
    if submitted is None:
        raise RuntimeError("submit_candidate has no durable Tool result")
    name = _required_text(submitted, "name")
    media_type = _required_text(submitted, "media_type")
    content = _required_text(submitted, "content", allow_empty=True)
    raw_output = json_dumps(submitted).encode("utf-8")
    return WorkerResult(
        (
            CandidateArtifact(
                ArtifactKind.CANDIDATE,
                name,
                media_type,
                content.encode("utf-8"),
            ),
        ),
        f"Built-in Agent submitted {name}",
        raw_output=raw_output,
    )


def _builtin_context(request: WorkerRequest) -> dict[str, JsonValue]:
    context = request.context
    context["confirmed_completion_contract"] = {
        "completion_contract_id": request.completion_contract.completion_contract_id,
        "criteria": list(request.completion_contract.criteria),
        "required_check_ids": list(request.completion_contract.required_check_ids),
        "version": request.completion_contract.version,
    }
    context["required_checks"] = [
        {
            "check_id": check.check_id,
            "name": check.name,
            "kind": check.kind.value,
            "description": check.description,
            "required": check.required,
        }
        for check in request.required_check_specs
    ]
    return context


def _required_text(
    document: dict[str, JsonValue],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = document.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RuntimeError(f"submit_candidate {key} must be text")
    return value


def _failure_reason(error: Exception) -> str:
    text = f"{type(error).__name__}: {error}"
    return text if len(text) <= 2_000 else text[:1_997] + "..."
