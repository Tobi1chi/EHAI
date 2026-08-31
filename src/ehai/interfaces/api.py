"""Minimal FastAPI Command and Query surface for the P1 execution plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, cast

from fastapi import APIRouter, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ehai import ID, JsonValue, format_utc_datetime, normalize_id
from ehai.application.commands import (
    ApprovePlan,
    CancelRun,
    CreateGoal,
    CreateProject,
    PauseRun,
    ProposePlan,
    ResumeRun,
    StartRun,
)
from ehai.application.orchestrator import OrchestrationError
from ehai.application.queries import QueryNotFoundError, QueryService
from ehai.application.run_control import RunControlConflictError, RunControlError
from ehai.application.service import (
    ApplicationError,
    EntityNotFoundError,
    ExecutionService,
    IdempotencyConflictError,
)
from ehai.domain.checking import InvalidCheckRunTransition
from ehai.domain.events import Event
from ehai.domain.execution import InvalidAttemptTransition, InvalidRunTransition
from ehai.domain.goal import GoalInvariantError
from ehai.domain.planning import PlanInvariantError, PlanTransitionError
from ehai.interfaces.http_models import (
    ApprovePlanRequest,
    CancelRunRequest,
    CreateGoalRequest,
    CreateProjectRequest,
    DataResponse,
    ErrorDetail,
    ErrorResponse,
    ProposePlanRequest,
    RunActionRequest,
    StartRunRequest,
    UuidInput,
)
from ehai.interfaces.sse import create_event_stream_endpoint


def _response_contract(schema_ref: str, description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": schema_ref}}},
    }


_CREATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}
_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    **_CREATE_RESPONSES,
}
_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}
_SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Durable Events followed by heartbeat comments",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    },
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}
_PROJECT_RESPONSES = {
    201: _response_contract(
        "commands.schema.json#/$defs/ProjectResponse",
        "Created Project",
    ),
    **_CREATE_RESPONSES,
}
_GOAL_RESPONSES = {
    201: _response_contract("commands.schema.json#/$defs/GoalResponse", "Created Goal"),
    **_WRITE_RESPONSES,
}
_PLAN_CREATED_RESPONSES = {
    201: _response_contract(
        "commands.schema.json#/$defs/PlanGraphResponse",
        "Draft PlanRevision",
    ),
    **_WRITE_RESPONSES,
}
_PLAN_RESPONSES = {
    200: _response_contract(
        "commands.schema.json#/$defs/PlanGraphResponse",
        "PlanRevision",
    ),
    **_WRITE_RESPONSES,
}
_RUN_CREATED_RESPONSES = {
    201: _response_contract("commands.schema.json#/$defs/RunResponse", "Created Run"),
    **_WRITE_RESPONSES,
}
_RUN_WRITE_RESPONSES = {
    200: _response_contract("commands.schema.json#/$defs/RunResponse", "Run state"),
    **_WRITE_RESPONSES,
}


def _read_responses(schema_definition: str, description: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: _response_contract(
            f"queries.schema.json#/$defs/{schema_definition}",
            description,
        ),
        **_READ_RESPONSES,
    }


def create_app(
    execution_service: ExecutionService,
    query_service: QueryService,
) -> FastAPI:
    """Create the versioned P1 HTTP app around already-constructed services."""
    app = FastAPI(title="EHAI Execution Plane", version="1")
    router = APIRouter(prefix="/api/v1")
    event_stream_endpoint = create_event_stream_endpoint(
        lambda *, after_event_id, limit: (
            query_service.list_events(
                after_event_id=after_event_id,
                limit=limit,
            ).events
        )
    )

    @router.post(
        "/projects",
        response_model=DataResponse,
        responses=_PROJECT_RESPONSES,
        status_code=201,
    )
    def create_project(request: CreateProjectRequest) -> DataResponse:
        return _response(
            execution_service.create_project(CreateProject(request.idempotency_key, request.name))
        )

    @router.post(
        "/goals",
        response_model=DataResponse,
        responses=_GOAL_RESPONSES,
        status_code=201,
    )
    def create_goal(request: CreateGoalRequest) -> DataResponse:
        return _response(
            execution_service.create_goal(
                CreateGoal(
                    request.idempotency_key,
                    _id(str(request.project_id)),
                    request.objective,
                )
            )
        )

    @router.post(
        "/plans/propose",
        response_model=DataResponse,
        responses=_PLAN_CREATED_RESPONSES,
        status_code=201,
    )
    def propose_plan(request: ProposePlanRequest) -> DataResponse:
        return _response(
            execution_service.propose_plan(
                ProposePlan(
                    request.idempotency_key,
                    _id(str(request.goal_id)),
                    tuple(request.criteria),
                )
            )
        )

    @router.post(
        "/plans/approve",
        response_model=DataResponse,
        responses=_PLAN_RESPONSES,
    )
    def approve_plan(request: ApprovePlanRequest) -> DataResponse:
        return _response(
            execution_service.approve_plan(
                ApprovePlan(
                    request.idempotency_key,
                    _id(str(request.plan_revision_id)),
                    _id(str(request.completion_contract_id)),
                )
            )
        )

    @router.post(
        "/runs/start",
        response_model=DataResponse,
        responses=_RUN_CREATED_RESPONSES,
        status_code=201,
    )
    def start_run(request: StartRunRequest) -> DataResponse:
        return _response(
            execution_service.start_run(
                StartRun(request.idempotency_key, _id(str(request.plan_revision_id)))
            )
        )

    @router.post(
        "/runs/{run_id}/pause",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    def pause_run(run_id: UuidInput, request: RunActionRequest) -> DataResponse:
        return _response(
            execution_service.pause_run(PauseRun(request.idempotency_key, _id(str(run_id))))
        )

    @router.post(
        "/runs/{run_id}/resume",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    def resume_run(run_id: UuidInput, request: RunActionRequest) -> DataResponse:
        return _response(
            execution_service.resume_run(ResumeRun(request.idempotency_key, _id(str(run_id))))
        )

    @router.post(
        "/runs/{run_id}/cancel",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    def cancel_run(run_id: UuidInput, request: CancelRunRequest) -> DataResponse:
        return _response(
            execution_service.cancel_run(
                CancelRun(request.idempotency_key, _id(str(run_id)), request.reason)
            )
        )

    @router.get(
        "/runs/{run_id}",
        response_model=DataResponse,
        responses=_read_responses("RunResponse", "Current Run state"),
    )
    def get_run(run_id: UuidInput) -> DataResponse:
        return _response(query_service.get_run(_id(str(run_id))))

    @router.get(
        "/plans/{plan_revision_id}",
        response_model=DataResponse,
        responses=_read_responses("PlanGraphResponse", "Versioned PlanGraph"),
    )
    def get_plan_graph(plan_revision_id: UuidInput) -> DataResponse:
        return _response(query_service.get_plan_graph(_id(str(plan_revision_id))))

    @router.get(
        "/runs/{run_id}/trace",
        response_model=DataResponse,
        responses=_read_responses("ExecutionTraceResponse", "ExecutionTrace"),
    )
    def get_execution_trace(run_id: UuidInput) -> DataResponse:
        return _response(query_service.get_execution_trace(_id(str(run_id))))

    @router.get(
        "/plans/{plan_revision_id}/checks",
        response_model=DataResponse,
        responses=_read_responses("CheckSpecListResponse", "Plan CheckSpecs"),
    )
    def list_check_specs(plan_revision_id: UuidInput) -> DataResponse:
        return _response(query_service.list_check_specs(_id(str(plan_revision_id))))

    @router.get(
        "/runs/{run_id}/checks",
        response_model=DataResponse,
        responses=_read_responses("CheckRunListResponse", "Run CheckRuns"),
    )
    def list_checks(run_id: UuidInput) -> DataResponse:
        return _response(query_service.list_check_runs(_id(str(run_id))))

    @router.get(
        "/runs/{run_id}/checkpoints",
        response_model=DataResponse,
        responses=_read_responses("CheckpointListResponse", "Run Checkpoints"),
    )
    def list_checkpoints(run_id: UuidInput) -> DataResponse:
        return _response(query_service.list_checkpoints(_id(str(run_id))))

    @router.get(
        "/runs/{run_id}/artifacts",
        response_model=DataResponse,
        responses=_read_responses("ArtifactListResponse", "Run Artifacts"),
    )
    def list_artifacts(run_id: UuidInput) -> DataResponse:
        return _response(query_service.list_artifacts(_id(str(run_id))))

    @router.get(
        "/artifacts/{artifact_id}",
        response_model=DataResponse,
        responses=_read_responses("ArtifactResponse", "Artifact metadata"),
    )
    def get_artifact(artifact_id: UuidInput) -> DataResponse:
        return _response(query_service.get_artifact(_id(str(artifact_id))))

    @router.get(
        "/events",
        response_model=DataResponse,
        responses=_read_responses("EventPageResponse", "Durable Event page"),
    )
    def list_events(
        after_event_id: UuidInput | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> DataResponse:
        cursor = None if after_event_id is None else _id(str(after_event_id))
        return _response(query_service.list_events(after_event_id=cursor, limit=limit))

    @router.get(
        "/events/stream",
        name="event-stream",
        response_class=StreamingResponse,
        response_model=None,
        responses=_SSE_RESPONSES,
    )
    async def event_stream(
        request: Request,
        after_event_id: Annotated[UuidInput | None, Query()] = None,
        last_event_id: Annotated[UuidInput | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        del after_event_id, last_event_id
        return await event_stream_endpoint(request)

    app.include_router(router)
    _install_error_handlers(app)
    return app


def _response(value: object) -> DataResponse:
    return DataResponse(data=_json_value(value))


def _json_value(value: object) -> JsonValue:
    """Encode public DTOs with the repository's canonical cross-plane conventions."""
    if isinstance(value, Event):
        return value.to_dict()
    if isinstance(value, datetime):
        return format_utc_datetime(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return cast(JsonValue, value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
            if not item.name.startswith("_")
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("public JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported public response value: {type(value).__name__}")


def _id(value: str) -> ID:
    return normalize_id(value)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del request
        return _error_response(422, "invalid_request", str(error))

    @app.exception_handler(EntityNotFoundError)
    @app.exception_handler(QueryNotFoundError)
    async def entity_not_found(request: Request, error: Exception) -> JSONResponse:
        del request
        code = (
            "unknown_event_cursor"
            if isinstance(error, QueryNotFoundError) and error.entity_name == "Event"
            else "not_found"
        )
        return _error_response(404, code, str(error))

    @app.exception_handler(IdempotencyConflictError)
    @app.exception_handler(RunControlConflictError)
    async def conflict(request: Request, error: Exception) -> JSONResponse:
        del request
        return _error_response(409, "conflict", str(error))

    @app.exception_handler(ApplicationError)
    @app.exception_handler(RunControlError)
    @app.exception_handler(OrchestrationError)
    @app.exception_handler(GoalInvariantError)
    @app.exception_handler(PlanInvariantError)
    @app.exception_handler(PlanTransitionError)
    @app.exception_handler(InvalidRunTransition)
    @app.exception_handler(InvalidAttemptTransition)
    @app.exception_handler(InvalidCheckRunTransition)
    async def state_error(request: Request, error: Exception) -> JSONResponse:
        del request
        return _error_response(409, "state_conflict", str(error))

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, error: ValueError) -> JSONResponse:
        del request
        return _error_response(422, "invalid_request", str(error))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        del request, error
        return _error_response(500, "internal_error", "internal server error")


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
