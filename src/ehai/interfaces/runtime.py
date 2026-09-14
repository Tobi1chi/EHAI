"""Local FastAPI/Uvicorn composition root for the P1 Execution Plane."""

from __future__ import annotations

import argparse
import asyncio
import inspect
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import uvicorn
from fastapi import FastAPI

from ehai import ID, JsonValue, json_dumps, json_loads
from ehai.application.async_runtime import RuntimeConnector, SingleSlotRuntime
from ehai.application.execution_policy import ExecutionPolicy
from ehai.application.legacy_config import ResponsesEndpointCapabilities
from ehai.application.ports import StateConflictError
from ehai.application.process_adjustments import ProcessAdjustments
from ehai.application.queries import QueryService
from ehai.application.runtime_control import RuntimeControlService
from ehai.application.scheduler import (
    CapacityPolicy,
    ConcurrentRuntime,
    Dispatcher,
    RuntimeQuiescenceError,
)
from ehai.application.service import ExecutionService
from ehai.application.session_mailbox import SessionMailbox
from ehai.domain.execution import RunStatus
from ehai.domain.workers import (
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.agent_traces import SQLiteAgentTraceStore
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.pi_config import PiBackendConfig
from ehai.infrastructure.session_mailbox import SQLiteSessionMailboxRepository
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import (
    CodexAppServerConnector,
    PiAgentConnector,
    WorkerAdapterConnector,
)
from ehai.infrastructure.workers.code import CodeRuntimeConnector
from ehai.infrastructure.workspaces import WorkspaceManager
from ehai.interfaces.agent_backends import resolve_planner_kind
from ehai.interfaces.api import create_app
from ehai.interfaces.cli import build_service
from ehai.interfaces.session_host import ExecutionConfig

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
    process_adjustments: ProcessAdjustments | None = None
    execution_config: ExecutionConfig | None = None


def create_local_app(
    database_path: Path,
    artifact_root: Path,
    *,
    worker_kind: str | None = None,
    worker_workspace: Path | None = None,
    planner_kind: str | None = None,
    planner_timeout_seconds: float = 120.0,
    planner_model: str | None = None,
    planner_reasoning_effort: str | None = None,
    command_check_argv: Sequence[str] | None = None,
    semantic_required_terms: Sequence[str] = (),
    command_check_timeout_seconds: float = 30.0,
    worker_timeout_seconds: float = 300.0,
    attempt_deadline_seconds: float | None = None,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    agent_model: str | None = None,
    agent_reasoning_effort: str | None = None,
    agent_allowed_commands: Sequence[Sequence[str]] = (),
    available_shells: Sequence[str] = (),
    git_permissions: Sequence[str] = (),
    worker_capacity: int = 1,
    codex_server_executable: str | Sequence[str] = "codex",
    codex_server_approval_policy: str = "on-request",
    codex_server_sandbox: str = "workspace-write",
    p2_runtime: bool = False,
    runtime_autostart: bool = True,
    endpoint_capabilities: ResponsesEndpointCapabilities | None = None,
    pi_backend: PiBackendConfig | None = None,
) -> FastAPI:
    """Construct one Command service and, optionally, its local P2 Runtime."""
    worker_kind = worker_kind or (
        "pi" if agent_model is not None or (pi_backend is not None and p2_runtime) else "fake"
    )
    planner_kind = resolve_planner_kind(
        planner_kind, pi_configured=pi_backend is not None, model=planner_model
    )
    if worker_kind == "builtin" or planner_kind == "builtin":
        raise ValueError("Built-in execution is retired; old authorizations cannot be used for Pi")
    if (worker_kind == "pi" or planner_kind == "pi") and pi_backend is None:
        raise ValueError("Pi execution requires an explicit --pi-config")
    host_execution_config = None
    configured_kind = _canonical_runtime_worker_kind(worker_kind)
    if p2_runtime and configured_kind in {"pi", "codex-server"}:
        host_execution_config = ExecutionConfig(
            worker_kind=configured_kind,
            model=(agent_model if configured_kind == "pi" else codex_model) or "",
            reasoning_effort=(
                agent_reasoning_effort if configured_kind == "pi" else codex_reasoning_effort
            ),
            capacity=worker_capacity,
            allowed_commands=_normalize_allowed_command_argv(agent_allowed_commands),
            available_shells=tuple(available_shells),
            git_permissions=frozenset(git_permissions),
            workspace=worker_workspace or Path.cwd(),
            endpoint_capabilities=endpoint_capabilities or ResponsesEndpointCapabilities(),
            command_timeout_seconds=command_check_timeout_seconds,
            pi_backend=pi_backend,
            codex_server_executable=(
                (codex_server_executable,)
                if isinstance(codex_server_executable, str)
                else tuple(codex_server_executable)
            ),
            codex_server_approval_policy=codex_server_approval_policy,
            codex_server_sandbox=codex_server_sandbox,
        )
        # Use the same canonical values for execution and later authorization.
        worker_kind = host_execution_config.worker_kind
        worker_workspace = host_execution_config.workspace
        if worker_kind == "pi":
            agent_model = host_execution_config.model
            agent_reasoning_effort = host_execution_config.reasoning_effort
        else:
            codex_model = host_execution_config.model
            codex_reasoning_effort = host_execution_config.reasoning_effort
        agent_allowed_commands = host_execution_config.allowed_commands
        available_shells = host_execution_config.available_shells
        git_permissions = tuple(sorted(host_execution_config.git_permissions))
        endpoint_capabilities = host_execution_config.endpoint_capabilities
        command_check_timeout_seconds = host_execution_config.command_timeout_seconds
        codex_server_executable = host_execution_config.codex_server_executable
        codex_server_approval_policy = host_execution_config.codex_server_approval_policy
        codex_server_sandbox = host_execution_config.codex_server_sandbox
    if planner_model is not None:
        planner_model = planner_model.strip()
    if planner_reasoning_effort is not None:
        planner_reasoning_effort = planner_reasoning_effort.strip()
    resolved_planner_model = (
        planner_model
        if planner_model is not None
        else (agent_model if planner_kind == "pi" else None)
    )
    resolved_planner_effort = (
        planner_reasoning_effort
        if planner_reasoning_effort is not None
        else (agent_reasoning_effort if planner_kind == "pi" and planner_model is None else None)
    )
    execution_service = build_service(
        database_path,
        artifact_root,
        worker_kind=worker_kind,
        worker_workspace=worker_workspace,
        planner_kind=planner_kind,
        planner_timeout_seconds=planner_timeout_seconds,
        planner_model=resolved_planner_model,
        planner_reasoning_effort=resolved_planner_effort,
        command_check_argv=command_check_argv,
        semantic_required_terms=semantic_required_terms,
        command_check_timeout_seconds=command_check_timeout_seconds,
        worker_timeout_seconds=worker_timeout_seconds,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        background_start=p2_runtime,
        endpoint_capabilities=endpoint_capabilities,
        pi_backend=pi_backend,
    )
    query_database = SQLiteDatabase(database_path)
    builtin_sessions = SQLiteAgentTraceStore(query_database)
    session_mailbox = SessionMailbox(SQLiteSessionMailboxRepository(query_database))
    query_service = QueryService(
        read_session_factory=query_database.read_session,
        builtin_session_reader=builtin_sessions,
    )
    if not p2_runtime:
        execution_service.recover_startup()
        app = create_app(execution_service, query_service)
        app.state.database = query_database
        app.state.artifact_root = artifact_root.resolve()
        app.state.execution_service = execution_service
        app.state.query_service = query_service
        return app

    connector: RuntimeConnector
    worker_kind = _canonical_runtime_worker_kind(worker_kind)
    if worker_kind == "pi":
        if agent_model is None or not agent_model.strip():
            raise ValueError("--agent-model is required for the Pi Worker")
        worker_kind_value = WorkerKind.PI
        capabilities = {
            WorkerCapability("worker.pi"),
            WorkerCapability("workspace.read"),
            WorkerCapability("workspace.write"),
            WorkerCapability("session.message"),
        }
        if available_shells:
            capabilities.add(WorkerCapability("shell.execute"))
        capabilities.update(WorkerCapability(permission) for permission in git_permissions)
        profile = WorkerProfile(
            "local-pi",
            WorkerKind.PI,
            agent_model,
            frozenset(capabilities),
            worker_profile_id=_stable_runtime_id(
                "profile",
                {
                    "kind": WorkerKind.PI.value,
                    "model": agent_model,
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
    endpoint_capacity = worker_capacity if worker_kind in {"pi", "codex-server"} else 1
    workspace = (worker_workspace or Path.cwd()).resolve(strict=True)
    endpoint = WorkerEndpoint(
        f"local-{worker_kind}",
        worker_kind_value,
        (WorkerEndpointType.IN_PROCESS if worker_kind == "fake" else WorkerEndpointType.COMMAND),
        worker_kind,
        endpoint_capacity,
        worker_endpoint_id=_stable_runtime_id(
            "endpoint",
            {
                "kind": worker_kind_value.value,
                "type": (
                    WorkerEndpointType.IN_PROCESS.value
                    if worker_kind == "fake"
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
    if worker_kind == "pi":
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

        assert pi_backend is not None
        connector = PiAgentConnector(
            backend=pi_backend,
            uow_factory=query_database.unit_of_work,
            orchestrator=execution_service.orchestrator,
            session_store=builtin_sessions,
            artifact_store=artifact_store,
            profile=profile,
            default_workspace=workspace,
            allowed_commands=_normalize_allowed_command_argv(agent_allowed_commands),
            command_timeout_seconds=command_check_timeout_seconds,
            available_shells=tuple(available_shells),
            git_permissions=frozenset(git_permissions),
            reasoning_effort=agent_reasoning_effort,
            workspace_resolver=workspace_resolver,
            mailbox=session_mailbox,
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
            artifact_store=artifact_store,
        )
        execution_service.orchestrator.enable_adoption_execution(
            connector.prepare_adoption_verification,
            connector.adoption_inputs_are_applicable,
        )
    if worker_kind in {"pi", "codex-server"}:
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
    process_adjustments = (
        ProcessAdjustments(
            uow_factory=query_database.unit_of_work,
            service=execution_service,
            planner_model=resolved_planner_model,
            planner_reasoning_effort=resolved_planner_effort,
        )
        if planner_kind == "pi" and resolved_planner_model is not None
        else None
    )

    def validate_execution_config(config: ExecutionConfig) -> None:
        if host_execution_config is None:
            raise StateConflictError("This Worker host has no full execution configuration")
        if replace(config, process_adjustment=None).to_document() != (
            host_execution_config.to_document()
        ):
            raise StateConflictError("Execution configuration does not match this host")
        adjustment_policy = config.process_adjustment
        if adjustment_policy is not None and (
            process_adjustments is None
            or adjustment_policy.model != resolved_planner_model
            or adjustment_policy.reasoning_effort != resolved_planner_effort
        ):
            raise StateConflictError("Process adjustment policy does not match this host's Planner")

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
        process_adjustments=process_adjustments,
        execution_config=host_execution_config,
    )
    app = create_app(
        execution_service,
        query_service,
        runtime_control,
        process_adjustments=process_adjustments,
        execution_config_validator=validate_execution_config,
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
    adjustment_tasks: dict[ID, asyncio.Task[None]] = {}

    async def adjust_run(run_id: ID) -> None:
        if not isinstance(runtime, ConcurrentRuntime) or process_adjustments is None:
            return
        if execution_service.get_run(run_id).status is not RunStatus.PAUSED:
            return
        await runtime.quiesce_run(run_id)
        if execution_service.get_run(run_id).status is RunStatus.RUNNING:
            runtime.resume_run_scheduling(run_id)
            return
        runtime.release_run_dispatch(run_id)
        adjustment = await process_adjustments.advance(run_id)
        if adjustment.resumed and execution_service.get_run(run_id).status is RunStatus.RUNNING:
            runtime.resume_run_scheduling(run_id)

    async def runtime_iterations() -> None:
        consecutive_failures = 0
        needs_recovery = True
        runtime_control.mark_runtime_starting()
        while True:
            try:
                for run_id, adjustment_task in tuple(adjustment_tasks.items()):
                    if adjustment_task.done():
                        del adjustment_tasks[run_id]
                        adjustment_task.result()
                if needs_recovery:
                    await runtime.recover_startup()
                    needs_recovery = False
                if isinstance(runtime, ConcurrentRuntime):
                    completed = await runtime.run_until_idle()
                else:
                    result = await runtime.run_once()
                    completed = () if result is None else (result,)
                if isinstance(runtime, ConcurrentRuntime) and process_adjustments is not None:
                    for run_id in process_adjustments.pending_run_ids():
                        if run_id in adjustment_tasks:
                            continue
                        current = execution_service.get_run(run_id)
                        if current.status is not RunStatus.PAUSED:
                            continue
                        adjustment_tasks[run_id] = asyncio.create_task(
                            adjust_run(run_id), name=f"ehai-process-host-{run_id}"
                        )
            except asyncio.CancelledError:
                raise
            except RuntimeQuiescenceError as error:
                runtime_control.mark_runtime_failure(error, terminal=True)
                return
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

    async def runtime_loop() -> None:
        try:
            await runtime_iterations()
        finally:
            # Drain host wrappers before closing connectors. Each wrapper's
            # advance call also cancels and drains its model child on shutdown.
            for adjustment_task in adjustment_tasks.values():
                adjustment_task.cancel()
            await asyncio.gather(*adjustment_tasks.values(), return_exceptions=True)
            adjustment_tasks.clear()

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
    parser.add_argument("--pi-config", type=Path)
    parser.add_argument("--database", type=Path, default=Path(".ehai/state.sqlite3"))
    parser.add_argument("--artifacts", type=Path, default=Path(".ehai/artifacts"))
    parser.add_argument(
        "--worker",
        choices=("fake", "pi", "codex", "codex-server"),
        default=None,
        help="Worker backend (Pi with --agent-model or --pi-config --p2-runtime)",
    )
    parser.add_argument("--worker-workspace", type=Path)
    parser.add_argument(
        "--planner",
        choices=("single", "exploration", "codex", "pi"),
        default=None,
        help="Planner backend (Pi when --pi-config/--planner-model is supplied)",
    )
    parser.add_argument("--planner-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--planner-model")
    parser.add_argument(
        "--planner-reasoning-effort",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
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
    parser.add_argument("--agent-model")
    parser.add_argument(
        "--agent-reasoning-effort",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
    )
    parser.add_argument(
        "--agent-allowed-command",
        action="append",
        default=[],
        metavar="JSON_ARGV",
        help="exact trusted argv JSON array exposed through the Pi command Tool",
    )
    parser.add_argument(
        "--agent-available-shell",
        action="append",
        default=[],
        help="trusted shell executable exposed through the Pi shell Tool",
    )
    parser.add_argument(
        "--agent-git-permission",
        action="append",
        choices=("git.read", "git.local_write", "git.remote_write", "git.dangerous"),
        default=[],
        help="Git permission exposed through the Pi git Tool",
    )
    parser.add_argument(
        "--worker-capacity",
        type=int,
        default=1,
        help="maximum concurrent Pi Agent Sessions",
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
        planner_model=args.planner_model,
        planner_reasoning_effort=args.planner_reasoning_effort,
        worker_timeout_seconds=args.worker_timeout_seconds,
        attempt_deadline_seconds=args.attempt_deadline_seconds,
        codex_model=args.codex_model,
        codex_reasoning_effort=args.codex_reasoning_effort,
        agent_model=args.agent_model,
        agent_reasoning_effort=args.agent_reasoning_effort,
        agent_allowed_commands=_parse_allowed_command_argv(args.agent_allowed_command),
        available_shells=tuple(args.agent_available_shell),
        git_permissions=tuple(args.agent_git_permission),
        worker_capacity=args.worker_capacity,
        codex_server_executable=(
            "codex" if args.codex_server_executable is None else tuple(args.codex_server_executable)
        ),
        codex_server_approval_policy=args.codex_server_approval_policy,
        codex_server_sandbox=args.codex_server_sandbox,
        p2_runtime=args.p2_runtime,
        command_check_argv=_parse_command_argv(args.command_check_argv),
        command_check_timeout_seconds=args.command_check_timeout_seconds,
        semantic_required_terms=tuple(args.semantic_required_term),
        pi_backend=None if args.pi_config is None else PiBackendConfig.from_path(args.pi_config),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


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
            raise ValueError("--agent-allowed-command requires an argv JSON array")
        policies.append(decoded)
    return tuple(policies)


def _normalize_allowed_command_argv(
    values: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    policies: list[tuple[str, ...]] = []
    for argv in values:
        if isinstance(argv, str) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("Pi allowed commands must be non-empty argv sequences")
        policies.append(tuple(argv))
    return tuple(policies)


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
