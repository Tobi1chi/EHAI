"""Private child-process entry point for one managed execution workspace."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from ehai import json_loads
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.infrastructure.pi_config import PiBackendConfig
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workspace_supervisor import ProcessFileLock
from ehai.interfaces.runtime import create_local_app
from ehai.interfaces.workspace_models import WorkspaceRegistration


class _ShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class _ChildActivity(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        internal = request.url.path.startswith("/_ehai_internal/")
        writing = not internal and request.method not in {"GET", "HEAD", "OPTIONS"}
        if writing and request.app.state.manager_stopping:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {"code": "host_stopping", "message": "Workspace host is stopping"}
                },
            )
        if writing:
            request.app.state.manager_active_writes += 1
        try:
            return await call_next(request)
        finally:
            if writing:
                request.app.state.manager_active_writes -= 1


async def _serve(configuration: Path, port: int) -> None:
    token = os.environ.pop("EHAI_WORKSPACE_CHILD_TOKEN", "")
    if not token:
        raise ValueError("Workspace child requires its private supervisor token")
    value = json_loads(configuration.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid private child configuration")
    registration = WorkspaceRegistration.model_validate(value.get("registration"))
    settings = registration.runtime
    private_backend = value.get("pi_backend_path")
    backend = (
        PiBackendConfig.from_path(Path(private_backend))
        if isinstance(private_backend, str)
        else None
    )
    data_root = configuration.parent.resolve(strict=True)
    lock = ProcessFileLock(data_root / "host.lock")
    try:
        app = create_local_app(
            data_root / "state.sqlite",
            data_root / "artifacts",
            worker_kind=settings.worker_kind,
            worker_workspace=Path(registration.path),
            planner_kind=settings.planner_kind,
            planner_model=settings.planner_model,
            planner_reasoning_effort=settings.planner_reasoning_effort,
            planner_timeout_seconds=settings.planner_timeout_seconds,
            worker_timeout_seconds=settings.worker_timeout_seconds,
            command_check_timeout_seconds=settings.command_timeout_seconds,
            worker_capacity=settings.worker_capacity,
            planner_capacity=settings.planner_capacity,
            agent_model=settings.worker_model,
            agent_reasoning_effort=settings.worker_reasoning_effort,
            agent_allowed_commands=settings.allowed_commands,
            available_shells=settings.available_shells,
            git_permissions=settings.git_permissions,
            command_check_argv=settings.command_check_argv,
            semantic_required_terms=settings.semantic_required_terms,
            attempt_deadline_seconds=settings.attempt_deadline_seconds,
            pi_backend=backend,
            p2_runtime=True,
        )
        app.state.manager_active_writes = 0
        app.state.manager_stopping = False
        app.add_middleware(_ChildActivity)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=10,
            )
        )

        def authorized(request: Request) -> bool:
            return hmac.compare_digest(request.headers.get("X-EHAI-Child-Token", ""), token)

        def denied() -> JSONResponse:
            return JSONResponse(status_code=403, content={"error": "private supervisor control"})

        @app.get("/_ehai_internal/identity", include_in_schema=False)
        async def identity(request: Request) -> JSONResponse:
            if not authorized(request):
                return denied()
            health = app.state.runtime_control.runtime_health()
            if health.status.value != "healthy":
                return JSONResponse(status_code=503, content={"status": health.status.value})
            return JSONResponse(
                {
                    "workspace_id": registration.workspace_id,
                    "path": registration.path,
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                }
            )

        @app.get("/_ehai_internal/execution-config", include_in_schema=False)
        async def execution_config(request: Request) -> JSONResponse:
            if not authorized(request):
                return denied()
            config = app.state.runtime_composition.execution_config
            return JSONResponse(
                {"execution_config": None if config is None else config.to_document()}
            )

        @app.post("/_ehai_internal/shutdown", include_in_schema=False)
        async def shutdown(request: Request, body: _ShutdownRequest) -> JSONResponse:
            if not authorized(request):
                return denied()
            adjustments = app.state.runtime_composition.process_adjustments
            if not body.force and (
                app.state.manager_active_writes
                or app.state.execution_service.planner_capacity.snapshot()["in_use"]
                or _execution_active(app.state.database)
                or (adjustments is not None and adjustments.pending_run_ids())
            ):
                return JSONResponse(
                    status_code=409, content={"error": "workspace has active execution or planning"}
                )
            app.state.manager_stopping = True
            asyncio.get_running_loop().call_later(0.1, setattr, server, "should_exit", True)
            return JSONResponse({"stopping": True})

        async def watch_parent() -> None:
            await asyncio.to_thread(sys.stdin.buffer.read, 1)
            app.state.manager_stopping = True
            server.should_exit = True

        watcher = asyncio.create_task(watch_parent())
        try:
            await server.serve()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    finally:
        lock.close()


def _execution_active(database: SQLiteDatabase) -> bool:
    with database.read_session() as session:
        for project in session.states.list_projects():
            for goal in session.states.list_goals(project.project_id):
                for run in session.states.list_runs(goal.goal_id):
                    if run.status in {RunStatus.PENDING, RunStatus.RUNNING}:
                        return True
                    if any(
                        attempt.status is AttemptStatus.RUNNING
                        for attempt in session.states.list_attempts(run.run_id)
                    ):
                        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Private managed workspace child")
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port is outside the TCP port range")
    asyncio.run(_serve(args.configuration, args.port))


if __name__ == "__main__":
    main()
