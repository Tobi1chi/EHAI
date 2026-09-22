"""Local workspace management and explicitly scoped forwarding to existing cores."""

from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal, cast

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, WithJsonSchema

from ehai import JsonValue, utc_now
from ehai.infrastructure.workspace_supervisor import SupervisorError, WorkspaceSupervisor
from ehai.interfaces.http_models import DataResponse
from ehai.interfaces.runtime import create_local_app
from ehai.interfaces.workspace_models import WorkspaceDescriptor, WorkspaceId, WorkspaceRegistration


class WorkspaceActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceResponse(BaseModel):
    data: WorkspaceDescriptor


class WorkspaceListData(BaseModel):
    workspaces: list[WorkspaceDescriptor]


class WorkspaceListResponse(BaseModel):
    data: WorkspaceListData


class WorkspaceExecutionConfigData(BaseModel):
    workspace_id: WorkspaceId
    execution_config: Annotated[
        dict[str, JsonValue] | None,
        WithJsonSchema(
            {
                "anyOf": [
                    {"$ref": "commands.schema.json#/$defs/ExecutionConfigRequest"},
                    {"type": "null"},
                ]
            }
        ),
    ]


class WorkspaceExecutionConfigResponse(BaseModel):
    data: WorkspaceExecutionConfigData


class WorkspaceQueryError(BaseModel):
    code: str
    message: str


class WorkspaceOverviewEntry(BaseModel):
    workspace: WorkspaceDescriptor
    api_path: str
    available: bool
    projects: (
        list[
            Annotated[
                dict[str, JsonValue],
                WithJsonSchema({"$ref": "queries.schema.json#/$defs/ProjectDetailView"}),
            ]
        ]
        | None
    )
    inbox: Annotated[
        dict[str, JsonValue] | None,
        WithJsonSchema(
            {"anyOf": [{"$ref": "queries.schema.json#/$defs/InboxListView"}, {"type": "null"}]}
        ),
    ]
    error: WorkspaceQueryError | None


class WorkspaceOverviewData(BaseModel):
    observed_at: str
    workspaces: list[WorkspaceOverviewEntry]
    consistent_across_workspaces: Literal[False] = False


class WorkspaceOverviewResponse(BaseModel):
    data: WorkspaceOverviewData


def _core_openapi() -> dict[str, Any]:
    # Schema assembly starts no lifespan, worker or model process. Keep the normal
    # production API contract even when no registered workspace is online.
    with TemporaryDirectory(prefix="ehai-manager-schema-") as temporary:
        root = Path(temporary)
        core = create_local_app(
            root / "schema.sqlite",
            root / "artifacts",
            worker_workspace=root,
            runtime_autostart=False,
        )
        return core.openapi()


def create_workspace_app(supervisor: WorkspaceSupervisor) -> FastAPI:
    """Compose one local manager; child cores retain all business-state ownership."""
    core_schema = _core_openapi()
    allowed_routes = [
        (method.upper(), re.compile("^" + re.sub(r"\{\w+\}", "[^/]+", path) + "$"))
        for path, item in core_schema["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    ]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await supervisor.open()
        async with httpx.AsyncClient(
            trust_env=False, timeout=httpx.Timeout(None, connect=10), follow_redirects=False
        ) as client:
            app.state.workspace_http = client
            try:
                yield
            finally:
                await supervisor.close()

    app = FastAPI(title="EHAI Workspace Manager", version="1", lifespan=lifespan)
    app.state.workspace_supervisor = supervisor
    original_openapi = app.openapi

    def manager_openapi() -> dict[str, Any]:
        document = original_openapi()
        document["x-ehai-workspace-manager"] = True
        return document

    app.openapi = manager_openapi  # type: ignore[method-assign]

    @app.exception_handler(SupervisorError)
    async def supervisor_error(request: Request, error: SupervisorError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": error.code, "message": str(error)}},
            status_code=error.status_code,
        )

    @app.get("/core-openapi.json", include_in_schema=False)
    def core_openapi() -> dict[str, Any]:
        return core_schema

    @app.get(
        "/api/v1/workspaces", operation_id="listWorkspaces", response_model=WorkspaceListResponse
    )
    async def list_workspaces() -> DataResponse:
        return DataResponse(
            data={"workspaces": [w.model_dump(mode="json") for w in await supervisor.list()]}
        )

    @app.post(
        "/api/v1/workspaces", operation_id="registerWorkspace", response_model=WorkspaceResponse
    )
    async def register_workspace(body: WorkspaceRegistration) -> DataResponse:
        return DataResponse(data=(await supervisor.register(body)).model_dump(mode="json"))

    @app.get(
        "/api/v1/workspaces/{workspace_id}",
        operation_id="getWorkspace",
        response_model=WorkspaceResponse,
    )
    async def get_workspace(workspace_id: str) -> DataResponse:
        return DataResponse(data=(await supervisor.get(workspace_id)).model_dump(mode="json"))

    @app.post(
        "/api/v1/workspaces/{workspace_id}/start",
        operation_id="startWorkspace",
        response_model=WorkspaceResponse,
    )
    async def start_workspace(workspace_id: str, body: WorkspaceActionRequest) -> DataResponse:
        return DataResponse(data=(await supervisor.start(workspace_id)).model_dump(mode="json"))

    @app.post(
        "/api/v1/workspaces/{workspace_id}/stop",
        operation_id="stopWorkspace",
        response_model=WorkspaceResponse,
    )
    async def stop_workspace(workspace_id: str, body: WorkspaceActionRequest) -> DataResponse:
        return DataResponse(data=(await supervisor.stop(workspace_id)).model_dump(mode="json"))

    @app.get(
        "/api/v1/workspaces/{workspace_id}/execution-config",
        operation_id="getWorkspaceExecutionConfig",
        response_model=WorkspaceExecutionConfigResponse,
    )
    async def get_execution_config(workspace_id: str) -> DataResponse:
        return DataResponse(
            data={
                "workspace_id": workspace_id,
                "execution_config": await supervisor.execution_config(workspace_id),
            }
        )

    async def read_core(client: httpx.AsyncClient, endpoint: str, path: str) -> Any:
        response = await client.get(endpoint + path, timeout=10)
        response.raise_for_status()
        return response.json()["data"]

    @app.get(
        "/api/v1/workspace-overview",
        operation_id="getWorkspaceOverview",
        response_model=WorkspaceOverviewResponse,
    )
    async def overview(request: Request) -> DataResponse:
        client = request.app.state.workspace_http
        slots = asyncio.Semaphore(4)

        async def summarize(workspace_id: str) -> dict[str, Any]:
            async with slots:
                descriptor = await supervisor.get(workspace_id)
                entry: dict[str, Any] = {
                    "workspace": descriptor.model_dump(mode="json"),
                    "api_path": f"/workspaces/{workspace_id}",
                    "available": False,
                    "projects": None,
                    "inbox": None,
                    "error": None,
                }
                try:
                    endpoint = await supervisor.endpoint(workspace_id)
                    projects = await read_core(client, endpoint, "/api/v1/projects")
                    details = []
                    for project in projects["projects"]:
                        details.append(
                            await read_core(
                                client, endpoint, "/api/v1/projects/" + project["project_id"]
                            )
                        )
                    entry["projects"] = details
                    entry["inbox"] = await read_core(client, endpoint, "/api/v1/inbox")
                    entry["available"] = True
                except SupervisorError as error:
                    entry["error"] = {"code": error.code, "message": str(error)}
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    entry["error"] = {
                        "code": "workspace_query_failed",
                        "message": "Workspace query unavailable; no request was retried",
                    }
                return entry

        workspaces = await supervisor.list()
        entries = await asyncio.gather(*(summarize(w.workspace_id) for w in workspaces))
        return DataResponse(
            data={
                "observed_at": utc_now().isoformat(),
                "workspaces": cast(list[JsonValue], entries),
                "consistent_across_workspaces": False,
            }
        )

    @app.api_route(
        "/workspaces/{workspace_id}/{core_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def forward(workspace_id: str, core_path: str, request: Request) -> Any:
        path = "/" + core_path
        if path == "/openapi.json" and request.method == "GET":
            await supervisor.get(workspace_id)
            return JSONResponse(core_schema)
        if any(segment in {".", ".."} for segment in core_path.split("/")) or "\\" in path:
            return JSONResponse(
                {"error": {"code": "invalid_core_path", "message": "Invalid scoped core path"}},
                status_code=400,
            )
        if not any(
            method == request.method and pattern.fullmatch(path)
            for method, pattern in allowed_routes
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "unknown_core_route",
                        "message": "Only registered core API routes can be forwarded",
                    }
                },
                status_code=404,
            )
        endpoint = await supervisor.endpoint(workspace_id)
        client: httpx.AsyncClient = request.app.state.workspace_http
        headers = {
            name: request.headers[name]
            for name in ("accept", "content-type", "last-event-id")
            if name in request.headers
        }
        upstream_request = client.build_request(
            request.method,
            endpoint + path + ("?" + request.url.query if request.url.query else ""),
            headers=headers,
            content=await request.body(),
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            return JSONResponse(
                {
                    "error": {
                        "code": "workspace_response_unknown",
                        "message": (
                            "Core response was not confirmed. No retry or fallback was attempted; "
                            "query this workspace before repeating a write."
                        ),
                    }
                },
                status_code=502,
            )

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response_headers = {
            name: upstream.headers[name]
            for name in ("content-type", "content-encoding", "cache-control", "x-accel-buffering")
            if name in upstream.headers
        }
        response_headers["x-ehai-workspace"] = workspace_id
        return StreamingResponse(
            stream(), status_code=upstream.status_code, headers=response_headers
        )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage isolated EHAI workspace cores on this machine"
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-worker-capacity", type=int, default=8)
    parser.add_argument("--max-planner-capacity", type=int, default=8)
    args = parser.parse_args()
    supervisor = WorkspaceSupervisor(
        args.data_dir,
        max_worker_capacity=args.max_worker_capacity,
        max_planner_capacity=args.max_planner_capacity,
    )
    uvicorn.run(
        create_workspace_app(supervisor),
        host="127.0.0.1",
        port=args.port,
        timeout_graceful_shutdown=15,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
