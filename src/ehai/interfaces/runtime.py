"""Local FastAPI/Uvicorn composition root for the P1 Execution Plane."""

from __future__ import annotations

import argparse
import asyncio
import inspect
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import uvicorn
from fastapi import FastAPI
from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, json_dumps, json_loads
from ehai.application.async_runtime import RuntimeConnector, SingleSlotRuntime
from ehai.application.builtin_agent import (
    LONG_RUNNING_AGENT_BUDGET,
    AgentBudget,
    ModelClient,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.execution_policy import ExecutionPolicy
from ehai.application.queries import QueryService
from ehai.application.runtime_control import RuntimeControlService
from ehai.application.scheduler import CapacityPolicy, ConcurrentRuntime, Dispatcher
from ehai.application.service import ExecutionService
from ehai.application.session_mailbox import SessionMailbox
from ehai.application.workers import WorkerRequest
from ehai.domain.workers import (
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.builtin_sessions import SQLiteBuiltinSessionStore
from ehai.infrastructure.openai_responses import ResponsesEndpointCapabilities
from ehai.infrastructure.session_mailbox import SQLiteSessionMailboxRepository
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import (
    BuiltinAgentConnector,
    CodexAppServerConnector,
    WorkerAdapterConnector,
)
from ehai.infrastructure.workers.code import CodeRuntimeConnector
from ehai.infrastructure.workspaces import WorkspaceManager
from ehai.interfaces.api import create_app
from ehai.interfaces.cli import add_responses_arguments, build_service, responses_capabilities

_MAX_RUNTIME_RESTARTS = 2
_RUNTIME_RESTART_BASE_SECONDS = 0.05


RuntimeInstance = SingleSlotRuntime | ConcurrentRuntime


@dataclass(frozen=True, slots=True)
class LocalRuntimeComposition:
    """Concrete local services and Runtime objects shared by API and CLI hosts."""

    database: SQLiteDatabase
    artifact_root: Path
    execution_service: ExecutionService
    query_service: QueryService
    runtime: RuntimeInstance
    runtime_control: RuntimeControlService
    connector: RuntimeConnector
    base_connector: RuntimeConnector
    workspace_manager: WorkspaceManager | None


def create_local_app(
    database_path: Path,
    artifact_root: Path,
    *,
    worker_kind: str = "fake",
    worker_workspace: Path | None = None,
    planner_kind: str = "single",
    planner_timeout_seconds: float = 120.0,
    builtin_planner_model: str | None = None,
    builtin_planner_reasoning_effort: str | None = None,
    command_check_argv: Sequence[str] | None = None,
    semantic_required_terms: Sequence[str] = (),
    command_check_timeout_seconds: float = 30.0,
    worker_timeout_seconds: float = 300.0,
    attempt_deadline_seconds: float | None = None,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    builtin_model: str | None = None,
    builtin_reasoning_effort: str | None = None,
    builtin_agent_budget: AgentBudget | None = None,
    builtin_allowed_commands: Sequence[Sequence[str]] = (),
    available_shells: Sequence[str] = (),
    git_permissions: Sequence[str] = (),
    builtin_capacity: int = 1,
    builtin_model_client_factory: Callable[[WorkerProfile, WorkerRequest], ModelClient]
    | None = None,
    codex_server_executable: str | Sequence[str] = "codex",
    codex_server_approval_policy: str = "on-request",
    codex_server_sandbox: str = "workspace-write",
    p2_runtime: bool = False,
    runtime_autostart: bool = True,
    endpoint_capabilities: ResponsesEndpointCapabilities | None = None,
) -> FastAPI:
    """Construct one Command service and, optionally, its local P2 Runtime."""
    execution_service = build_service(
        database_path,
        artifact_root,
        worker_kind=worker_kind,
        worker_workspace=worker_workspace,
        planner_kind=planner_kind,
        planner_timeout_seconds=planner_timeout_seconds,
        builtin_planner_model=(
            builtin_planner_model
            if builtin_planner_model is not None
            else (builtin_model if planner_kind == "builtin" else None)
        ),
        builtin_planner_reasoning_effort=(
            builtin_planner_reasoning_effort
            if builtin_planner_reasoning_effort is not None
            else (builtin_reasoning_effort if planner_kind == "builtin" else None)
        ),
        command_check_argv=command_check_argv,
        semantic_required_terms=semantic_required_terms,
        command_check_timeout_seconds=command_check_timeout_seconds,
        worker_timeout_seconds=worker_timeout_seconds,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        background_start=p2_runtime,
        endpoint_capabilities=endpoint_capabilities,
    )
    query_database = SQLiteDatabase(database_path)
    builtin_sessions = SQLiteBuiltinSessionStore(query_database)
    session_mailbox = SessionMailbox(SQLiteSessionMailboxRepository(query_database))
    query_service = QueryService(
        read_session_factory=query_database.read_session,
        builtin_session_reader=builtin_sessions,
    )
    if not p2_runtime:
        execution_service.recover_startup()
        app = create_app(execution_service, query_service, artifact_root=str(artifact_root))
        app.state.database = query_database
        app.state.artifact_root = artifact_root.resolve()
        app.state.execution_service = execution_service
        app.state.query_service = query_service
        return app

    connector: RuntimeConnector
    worker_kind = _canonical_runtime_worker_kind(worker_kind)
    if worker_kind == "builtin":
        if builtin_model is None or not builtin_model.strip():
            raise ValueError("--builtin-model is required for the Built-in Worker")
        worker_kind_value = WorkerKind.BUILTIN
        capabilities = {
            WorkerCapability("worker.builtin"),
            WorkerCapability("workspace.read"),
            WorkerCapability("workspace.write"),
            WorkerCapability("session.message"),
        }
        if available_shells:
            capabilities.add(WorkerCapability("shell.execute"))
        capabilities.update(WorkerCapability(permission) for permission in git_permissions)
        profile = WorkerProfile(
            "local-builtin",
            WorkerKind.BUILTIN,
            builtin_model,
            frozenset(capabilities),
            credential_ref=OPENAI_CREDENTIAL_REF,
            worker_profile_id=_stable_runtime_id(
                "profile",
                {
                    "kind": WorkerKind.BUILTIN.value,
                    "model": builtin_model,
                    "capabilities": cast(
                        JsonValue,
                        sorted(str(item) for item in capabilities),
                    ),
                    "workspace": str((worker_workspace or Path.cwd()).resolve()),
                },
            ),
        )
    elif worker_kind == "codex-server":
        if codex_model is None or not codex_model.strip():
            raise ValueError("--codex-model is required for the Codex App Server Worker")
        profile = WorkerProfile(
            "local-codex-server",
            WorkerKind.CODEX_APP_SERVER,
            codex_model,
            frozenset(
                {
                    WorkerCapability("worker.codex-app-server"),
                    WorkerCapability("workspace.read"),
                    WorkerCapability("workspace.write"),
                }
            ),
            worker_profile_id=_stable_runtime_id(
                "profile",
                {
                    "kind": WorkerKind.CODEX_APP_SERVER.value,
                    "model": codex_model,
                    "workspace": str((worker_workspace or Path.cwd()).resolve()),
                },
            ),
        )
    elif worker_kind == "fake":
        worker_kind_value = WorkerKind.BUILTIN
        profile = WorkerProfile(
            "local-fake",
            WorkerKind.BUILTIN,
            "scripted",
            worker_profile_id=_stable_runtime_id(
                "profile",
                {"kind": WorkerKind.BUILTIN.value, "model": "scripted"},
            ),
        )
    else:
        worker_kind_value = WorkerKind.CODEX_CLI
        profile = WorkerProfile(
            "local-codex",
            WorkerKind.CODEX_CLI,
            codex_model or "codex-cli",
            worker_profile_id=_stable_runtime_id(
                "profile",
                {
                    "kind": WorkerKind.CODEX_CLI.value,
                    "model": codex_model or "codex-cli",
                    "workspace": str((worker_workspace or Path.cwd()).resolve()),
                },
            ),
        )
    if worker_kind == "codex-server":
        worker_kind_value = WorkerKind.CODEX_APP_SERVER
    endpoint_capacity = builtin_capacity if worker_kind in {"builtin", "codex-server"} else 1
    workspace = (worker_workspace or Path.cwd()).resolve(strict=True)
    endpoint = WorkerEndpoint(
        f"local-{worker_kind}",
        worker_kind_value,
        (
            WorkerEndpointType.IN_PROCESS
            if worker_kind in {"fake", "builtin"}
            else WorkerEndpointType.COMMAND
        ),
        worker_kind,
        endpoint_capacity,
        worker_endpoint_id=_stable_runtime_id(
            "endpoint",
            {
                "kind": worker_kind_value.value,
                "type": (
                    WorkerEndpointType.IN_PROCESS.value
                    if worker_kind in {"fake", "builtin"}
                    else WorkerEndpointType.COMMAND.value
                ),
                "ref": worker_kind,
                "capacity": endpoint_capacity,
                "workspace": str(workspace),
            },
        ),
    )
    workspace_manager: WorkspaceManager | None = None
    base_connector: RuntimeConnector
    if worker_kind == "builtin":
        artifact_store = FilesystemArtifactStore(artifact_root)
        workspace_manager = WorkspaceManager(
            database=query_database,
            base_workspace=workspace,
            owned_root=artifact_root.parent / "worktrees",
            preserve_completed=True,
        )

        def workspace_resolver(attempt_id: ID) -> Path | None:
            assert workspace_manager is not None
            allocation = workspace_manager.allocation_for_attempt(attempt_id)
            return None if allocation is None else Path(allocation.reference.path)

        connector = BuiltinAgentConnector(
            uow_factory=query_database.unit_of_work,
            orchestrator=execution_service.orchestrator,
            session_store=builtin_sessions,
            artifact_store=artifact_store,
            profile=profile,
            default_workspace=workspace,
            allowed_commands=_normalize_allowed_command_argv(builtin_allowed_commands),
            command_timeout_seconds=command_check_timeout_seconds,
            available_shells=tuple(available_shells),
            git_permissions=frozenset(git_permissions),
            reasoning_effort=cast(ReasoningEffort, builtin_reasoning_effort),
            model_client_factory=builtin_model_client_factory,
            workspace_resolver=workspace_resolver,
            budget=(
                LONG_RUNNING_AGENT_BUDGET if builtin_agent_budget is None else builtin_agent_budget
            ),
            mailbox=session_mailbox,
            endpoint_capabilities=endpoint_capabilities,
        )
        base_connector = connector
    elif worker_kind == "codex-server":
        artifact_store = FilesystemArtifactStore(artifact_root)
        workspace_manager = WorkspaceManager(
            database=query_database,
            base_workspace=workspace,
            owned_root=artifact_root.parent / "worktrees",
            preserve_completed=True,
        )
        base_connector = CodexAppServerConnector(
            workspace=workspace,
            model=codex_model or "codex",
            executable=codex_server_executable,
            approval_policy=codex_server_approval_policy,
            sandbox=codex_server_sandbox,
            reasoning_effort=codex_reasoning_effort,
        )
        connector = base_connector
    else:
        base_connector = WorkerAdapterConnector(execution_service.orchestrator.worker)
        connector = base_connector
    if workspace_manager is not None:
        execution_service.orchestrator.enable_code_execution(
            lambda attempt_id: _workspace_for_attempt(workspace_manager, attempt_id)
        )
        connector = CodeRuntimeConnector(
            connector,
            database=query_database,
            workspace_manager=workspace_manager,
        )
    if worker_kind in {"builtin", "codex-server"}:
        capacity = CapacityPolicy(
            endpoint_capacity,
            endpoint_capacity,
            endpoint_capacity,
            {profile.worker_profile_id: endpoint_capacity},
            {endpoint.worker_endpoint_id: endpoint_capacity},
        )
        runtime: SingleSlotRuntime | ConcurrentRuntime = ConcurrentRuntime(
            uow_factory=query_database.unit_of_work,
            orchestrator=execution_service.orchestrator,
            dispatcher=Dispatcher(
                profiles=(profile,),
                endpoints=(endpoint,),
                capacity=capacity,
            ),
            connectors={endpoint.worker_endpoint_id: connector},
            policy=ExecutionPolicy(
                no_progress_timeout=None,
                absolute_attempt_timeout=(
                    None
                    if attempt_deadline_seconds is None
                    else timedelta(seconds=attempt_deadline_seconds)
                ),
                max_connector_calls=None,
                max_concurrency=endpoint_capacity,
            ),
            workspace_manager=workspace_manager,
        )
    else:
        runtime = SingleSlotRuntime(
            uow_factory=query_database.unit_of_work,
            orchestrator=execution_service.orchestrator,
            connector=connector,
            profile=profile,
            endpoint=endpoint,
            workspace=workspace,
        )
    runtime_control = RuntimeControlService(
        uow_factory=query_database.unit_of_work,
        orchestrator=execution_service.orchestrator,
        runtimes={endpoint.worker_endpoint_id: runtime},
    )
    composition = LocalRuntimeComposition(
        database=query_database,
        artifact_root=artifact_root.resolve(),
        execution_service=execution_service,
        query_service=query_service,
        runtime=runtime,
        runtime_control=runtime_control,
        connector=connector,
        base_connector=base_connector,
        workspace_manager=workspace_manager,
    )
    app = create_app(
        execution_service, query_service, runtime_control, artifact_root=str(artifact_root)
    )
    app.state.database = query_database
    app.state.artifact_root = artifact_root.resolve()
    app.state.execution_service = execution_service
    app.state.query_service = query_service
    app.state.runtime = runtime
    app.state.runtime_control = runtime_control
    app.state.connector = connector
    app.state.runtime_composition = composition
    task: asyncio.Task[None] | None = None

    async def runtime_loop() -> None:
        consecutive_failures = 0
        needs_recovery = True
        runtime_control.mark_runtime_starting()
        while True:
            try:
                if needs_recovery:
                    await runtime.recover_startup()
                    needs_recovery = False
                if isinstance(runtime, ConcurrentRuntime):
                    completed = await runtime.run_until_idle()
                else:
                    result = await runtime.run_once()
                    completed = () if result is None else (result,)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                terminal = consecutive_failures >= _MAX_RUNTIME_RESTARTS
                runtime_control.mark_runtime_failure(error, terminal=terminal)
                if terminal:
                    return
                delay = _RUNTIME_RESTART_BASE_SECONDS * (2**consecutive_failures)
                consecutive_failures += 1
                needs_recovery = True
                await asyncio.sleep(delay)
                continue
            consecutive_failures = 0
            runtime_control.mark_runtime_healthy()
            if not completed:
                await asyncio.sleep(0.05)

    async def start_runtime() -> None:
        nonlocal task
        task = asyncio.create_task(runtime_loop(), name="ehai-p2-runtime")

    async def stop_runtime() -> None:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        runtime_control.mark_runtime_stopped()
        await _close_runtime_connectors(composition)

    if runtime_autostart:
        app.router.add_event_handler("startup", start_runtime)
        app.router.add_event_handler("shutdown", stop_runtime)
    return app


def _canonical_runtime_worker_kind(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    if normalized == "codex-app-server":
        return "codex-server"
    return normalized


def _stable_runtime_id(kind: str, value: JsonValue) -> ID:
    return ID(str(uuid5(NAMESPACE_URL, f"ehai:{kind}:{json_dumps(value)}")))


def _workspace_for_attempt(manager: WorkspaceManager, attempt_id: ID) -> Path | None:
    allocation = manager.allocation_for_attempt(attempt_id)
    return None if allocation is None else Path(allocation.reference.path)


async def _close_runtime_connectors(composition: LocalRuntimeComposition) -> None:
    closed: set[int] = set()
    for connector in (composition.connector, composition.base_connector):
        identity = id(connector)
        if identity in closed:
            continue
        closed.add(identity)
        close = getattr(connector, "close", None)
        if not callable(close):
            continue
        result = close()
        if inspect.isawaitable(result):
            await result


def create_parser() -> argparse.ArgumentParser:
    """Create the local API server command line."""
    parser = argparse.ArgumentParser(prog="ehai-api", description="EHAI P1 HTTP API")
    add_responses_arguments(parser)
    parser.add_argument("--database", type=Path, default=Path(".ehai/state.sqlite3"))
    parser.add_argument("--artifacts", type=Path, default=Path(".ehai/artifacts"))
    parser.add_argument(
        "--worker",
        choices=("fake", "builtin", "codex", "codex-server"),
        default="fake",
    )
    parser.add_argument("--worker-workspace", type=Path)
    parser.add_argument(
        "--planner",
        choices=("single", "exploration", "codex", "builtin"),
        default="single",
    )
    parser.add_argument("--planner-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--builtin-planner-model")
    parser.add_argument(
        "--builtin-planner-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    )
    parser.add_argument("--worker-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--command-check-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--attempt-deadline-seconds",
        type=float,
        default=None,
        help=(
            "opt-in absolute Attempt deadline; by default no wall-clock deadline "
            "terminates a long-running high-reasoning model execution early"
        ),
    )
    parser.add_argument("--codex-model")
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument("--codex-server-executable", action="append")
    parser.add_argument(
        "--codex-server-approval-policy",
        choices=("untrusted", "on-request", "never"),
        default="on-request",
    )
    parser.add_argument(
        "--codex-server-sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="workspace-write",
    )
    parser.add_argument("--builtin-model")
    parser.add_argument(
        "--builtin-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    )
    parser.add_argument(
        "--builtin-allowed-command",
        action="append",
        default=[],
        metavar="JSON_ARGV",
        help="exact trusted argv JSON array exposed through the Built-in command Tool",
    )
    parser.add_argument(
        "--builtin-available-shell",
        action="append",
        default=[],
        help="trusted shell executable exposed through the Built-in shell Tool",
    )
    parser.add_argument(
        "--builtin-git-permission",
        action="append",
        choices=("git.read", "git.local_write", "git.remote_write", "git.dangerous"),
        default=[],
        help="Git permission exposed through the Built-in git Tool",
    )
    parser.add_argument(
        "--builtin-capacity",
        type=int,
        default=1,
        help="maximum concurrent Built-in Agent Sessions",
    )
    parser.add_argument(
        "--builtin-agent-max-steps",
        type=int,
        default=None,
        help="maximum model steps in one Built-in Agent Attempt",
    )
    parser.add_argument(
        "--builtin-agent-max-tool-calls",
        type=int,
        default=None,
        help="maximum Tool calls in one Built-in Agent Attempt",
    )
    parser.add_argument(
        "--builtin-agent-wall-clock-seconds",
        type=float,
        default=None,
        help="Built-in Agent loop deadline inside the Runtime Attempt deadline",
    )
    parser.add_argument(
        "--builtin-agent-max-output-bytes",
        type=int,
        default=None,
        help="maximum accumulated model and Tool output per Built-in Agent Attempt",
    )
    parser.add_argument(
        "--command-check-argv",
        help="trusted host Command Check argv as a JSON string array",
    )
    parser.add_argument("--semantic-required-term", action="append", default=[])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--p2-runtime",
        action="store_true",
        help="run StartRun through the local single-slot P2 background Runtime",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local P1 API without introducing a second application service."""
    args = create_parser().parse_args(argv)
    app = create_local_app(
        args.database,
        args.artifacts,
        worker_kind=args.worker,
        worker_workspace=args.worker_workspace,
        planner_kind=args.planner,
        planner_timeout_seconds=args.planner_timeout_seconds,
        builtin_planner_model=args.builtin_planner_model,
        builtin_planner_reasoning_effort=args.builtin_planner_reasoning_effort,
        worker_timeout_seconds=args.worker_timeout_seconds,
        attempt_deadline_seconds=args.attempt_deadline_seconds,
        codex_model=args.codex_model,
        codex_reasoning_effort=args.codex_reasoning_effort,
        builtin_model=args.builtin_model,
        builtin_reasoning_effort=args.builtin_reasoning_effort,
        builtin_agent_budget=_runtime_agent_budget(args),
        builtin_allowed_commands=_parse_allowed_command_argv(args.builtin_allowed_command),
        available_shells=tuple(args.builtin_available_shell),
        git_permissions=tuple(args.builtin_git_permission),
        builtin_capacity=args.builtin_capacity,
        codex_server_executable=(
            "codex" if args.codex_server_executable is None else tuple(args.codex_server_executable)
        ),
        codex_server_approval_policy=args.codex_server_approval_policy,
        codex_server_sandbox=args.codex_server_sandbox,
        p2_runtime=args.p2_runtime,
        command_check_argv=_parse_command_argv(args.command_check_argv),
        command_check_timeout_seconds=args.command_check_timeout_seconds,
        semantic_required_terms=tuple(args.semantic_required_term),
        endpoint_capabilities=responses_capabilities(args),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _runtime_agent_budget(args: argparse.Namespace) -> AgentBudget | None:
    values = (
        args.builtin_agent_max_steps,
        args.builtin_agent_max_tool_calls,
        args.builtin_agent_wall_clock_seconds,
        args.builtin_agent_max_output_bytes,
    )
    if all(value is None for value in values):
        return None
    return AgentBudget(*values)


def _parse_command_argv(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    decoded = json_loads(value)
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) or not item for item in decoded)
    ):
        raise ValueError("--command-check-argv must be a non-empty JSON string array")
    return tuple(item for item in decoded if isinstance(item, str))


def _parse_allowed_command_argv(values: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    policies: list[tuple[str, ...]] = []
    for value in values:
        decoded = _parse_command_argv(value)
        if decoded is None:  # pragma: no cover - argparse always supplies text values
            raise ValueError("--builtin-allowed-command requires an argv JSON array")
        policies.append(decoded)
    return tuple(policies)


def _normalize_allowed_command_argv(
    values: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    policies: list[tuple[str, ...]] = []
    for argv in values:
        if isinstance(argv, str) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("Built-in allowed commands must be non-empty argv sequences")
        policies.append(tuple(argv))
    return tuple(policies)


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
