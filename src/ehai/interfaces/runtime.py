"""Local FastAPI/Uvicorn composition root for the P1 Execution Plane."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import cast

import uvicorn
from fastapi import FastAPI
from openai.types.shared import ReasoningEffort

from ehai import ID, json_loads
from ehai.application.async_runtime import RuntimeConnector, SingleSlotRuntime
from ehai.application.builtin_agent import ModelClient
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.execution_policy import ExecutionPolicy
from ehai.application.queries import QueryService
from ehai.application.runtime_control import RuntimeControlService
from ehai.application.scheduler import CapacityPolicy, ConcurrentRuntime, Dispatcher
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
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import BuiltinAgentConnector, WorkerAdapterConnector
from ehai.infrastructure.workspaces import WorkspaceManager
from ehai.interfaces.api import create_app
from ehai.interfaces.cli import build_service

_MAX_RUNTIME_RESTARTS = 2
_RUNTIME_RESTART_BASE_SECONDS = 0.05


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
    worker_timeout_seconds: float = 300.0,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    builtin_model: str | None = None,
    builtin_reasoning_effort: str | None = None,
    builtin_allowed_commands: Sequence[Sequence[str]] = (),
    builtin_capacity: int = 1,
    builtin_model_client_factory: Callable[[WorkerProfile, WorkerRequest], ModelClient]
    | None = None,
    p2_runtime: bool = False,
) -> FastAPI:
    """Construct one long-lived Command service and short-lived read sessions."""
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
        worker_timeout_seconds=worker_timeout_seconds,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        background_start=p2_runtime,
    )
    query_database = SQLiteDatabase(database_path)
    query_service = QueryService(read_session_factory=query_database.read_session)
    if not p2_runtime:
        execution_service.recover_startup()
        return create_app(execution_service, query_service)

    connector: RuntimeConnector
    if worker_kind == "builtin":
        if builtin_model is None or not builtin_model.strip():
            raise ValueError("--builtin-model is required for the Built-in Worker")
        worker_kind_value = WorkerKind.BUILTIN
        capabilities = frozenset(
            {
                WorkerCapability("worker.builtin"),
                WorkerCapability("workspace.read"),
                WorkerCapability("workspace.write"),
            }
        )
        profile = WorkerProfile(
            "local-builtin",
            WorkerKind.BUILTIN,
            builtin_model,
            capabilities,
            credential_ref=OPENAI_CREDENTIAL_REF,
        )
    elif worker_kind == "fake":
        worker_kind_value = WorkerKind.BUILTIN
        profile = WorkerProfile("local-fake", WorkerKind.BUILTIN, "scripted")
    else:
        worker_kind_value = WorkerKind.CODEX_CLI
        profile = WorkerProfile(
            "local-codex",
            WorkerKind.CODEX_CLI,
            codex_model or "codex-cli",
        )
    endpoint_capacity = builtin_capacity if worker_kind == "builtin" else 1
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
    )
    workspace = worker_workspace or Path.cwd()
    workspace_manager: WorkspaceManager | None = None
    if worker_kind == "builtin":
        artifact_store = FilesystemArtifactStore(artifact_root)
        workspace_manager = WorkspaceManager(
            database=query_database,
            base_workspace=workspace,
            owned_root=artifact_root.parent / "worktrees",
        )

        def workspace_resolver(attempt_id: ID) -> Path | None:
            assert workspace_manager is not None
            allocation = workspace_manager.allocation_for_attempt(attempt_id)
            return None if allocation is None else Path(allocation.reference.path)

        connector = BuiltinAgentConnector(
            uow_factory=query_database.unit_of_work,
            orchestrator=execution_service.orchestrator,
            session_store=SQLiteBuiltinSessionStore(query_database),
            artifact_store=artifact_store,
            profile=profile,
            default_workspace=workspace,
            allowed_commands=_normalize_allowed_command_argv(builtin_allowed_commands),
            reasoning_effort=cast(ReasoningEffort, builtin_reasoning_effort),
            model_client_factory=builtin_model_client_factory,
            workspace_resolver=workspace_resolver,
        )
    else:
        connector = WorkerAdapterConnector(execution_service.orchestrator.worker)
    if worker_kind == "builtin":
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
                absolute_attempt_timeout=timedelta(seconds=worker_timeout_seconds),
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
    app = create_app(execution_service, query_service, runtime_control)
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
        if isinstance(connector, BuiltinAgentConnector):
            await connector.close()

    app.router.add_event_handler("startup", start_runtime)
    app.router.add_event_handler("shutdown", stop_runtime)
    return app


def create_parser() -> argparse.ArgumentParser:
    """Create the local API server command line."""
    parser = argparse.ArgumentParser(prog="ehai-api", description="EHAI P1 HTTP API")
    parser.add_argument("--database", type=Path, default=Path(".ehai/state.sqlite3"))
    parser.add_argument("--artifacts", type=Path, default=Path(".ehai/artifacts"))
    parser.add_argument("--worker", choices=("fake", "builtin", "codex"), default="fake")
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
    parser.add_argument("--codex-model")
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
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
        "--builtin-capacity",
        type=int,
        default=1,
        help="maximum concurrent Built-in Agent Sessions",
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
        codex_model=args.codex_model,
        codex_reasoning_effort=args.codex_reasoning_effort,
        builtin_model=args.builtin_model,
        builtin_reasoning_effort=args.builtin_reasoning_effort,
        builtin_allowed_commands=_parse_allowed_command_argv(args.builtin_allowed_command),
        builtin_capacity=args.builtin_capacity,
        p2_runtime=args.p2_runtime,
        command_check_argv=_parse_command_argv(args.command_check_argv),
        semantic_required_terms=tuple(args.semantic_required_term),
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
