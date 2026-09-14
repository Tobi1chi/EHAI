"""Minimal FastAPI Command and Query surface for the P1 execution plane."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ehai import ID, json_dumps, normalize_id
from ehai.application.commands import (
    ApplyProcess,
    ApprovePlan,
    CancelRun,
    CreateGoal,
    CreateProject,
    DecideHumanCheck,
    DiscussPlan,
    PauseRun,
    ProposePlan,
    ProposeProcess,
    ReplanPlan,
    ReplyIntervention,
    ResumeRun,
    ReviewProcess,
    StartRun,
)
from ehai.application.orchestrator import OrchestrationError
from ehai.application.ports import StateConflictError
from ehai.application.process_adjustments import ProcessAdjustments
from ehai.application.queries import QueryNotFoundError, QueryService
from ehai.application.run_control import RunControlConflictError, RunControlError
from ehai.application.runtime_control import RuntimeControlError, RuntimeControlService
from ehai.application.service import (
    ApplicationError,
    EntityNotFoundError,
    ExecutionService,
    IdempotencyConflictError,
)
from ehai.domain.checking import InvalidCheckRunTransition
from ehai.domain.execution import InvalidAttemptTransition, InvalidRunTransition, RunStatus
from ehai.domain.goal import GoalInvariantError
from ehai.domain.planning import PlanInvariantError, PlanTransitionError
from ehai.interfaces.http_models import (
    ApplyProcessRequest,
    ApprovePlanRequest,
    CancelRunRequest,
    CreateGoalRequest,
    CreateProjectRequest,
    DataResponse,
    DecideHumanCheckRequest,
    DiscussPlanRequest,
    ErrorDetail,
    ErrorResponse,
    ExtendAttemptDeadlineRequest,
    ProposePlanRequest,
    ProposeProcessRequest,
    ReplanPlanRequest,
    ReplyInterventionRequest,
    ResolveWorkerRequestRequest,
    ReviewProcessRequest,
    RunActionRequest,
    StartRunRequest,
    UuidInput,
)
from ehai.interfaces.public_documents import public_json_value
from ehai.interfaces.session_host import ExecutionConfig
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
    runtime_control: RuntimeControlService | None = None,
    *,
    process_adjustments: ProcessAdjustments | None = None,
    execution_config_validator: Callable[[ExecutionConfig], None] | None = None,
) -> FastAPI:
    """Create the additive P2 HTTP surface around already-constructed services."""
    app = FastAPI(title="EHAI Execution Plane", version="2")
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
        "/planning/discuss",
        response_model=DataResponse,
        responses={
            200: _response_contract(
                "queries.schema.json#/$defs/PlanningConversationResponse", "Planning discussion"
            ),
            **_WRITE_RESPONSES,
        },
    )
    def discuss_plan(request: DiscussPlanRequest) -> DataResponse:
        return _response(
            execution_service.discuss_plan(
                DiscussPlan(
                    request.idempotency_key,
                    _id(str(request.goal_id)),
                    request.message,
                    tuple(request.criteria),
                    None if request.conversation_id is None else _id(str(request.conversation_id)),
                    source_run_id=(
                        None if request.source_run_id is None else _id(str(request.source_run_id))
                    ),
                )
            )
        )

    @router.get(
        "/planning/{conversation_id}",
        response_model=DataResponse,
        responses=_read_responses("PlanningConversationResponse", "Planning discussion"),
    )
    def get_planning_conversation(conversation_id: UuidInput) -> DataResponse:
        return _response(query_service.get_planning_conversation(_id(str(conversation_id))))

    @router.post(
        "/plans/replan",
        response_model=DataResponse,
        responses=_PLAN_CREATED_RESPONSES,
        status_code=201,
    )
    def replan_plan(request: ReplanPlanRequest) -> DataResponse:
        return _response(
            execution_service.replan_plan(
                ReplanPlan(
                    request.idempotency_key,
                    _id(str(request.base_plan_revision_id)),
                    tuple(request.criteria),
                    (None if request.source_run_id is None else _id(str(request.source_run_id))),
                )
            )
        )

    @router.post(
        "/commands/propose-process",
        response_model=DataResponse,
        responses={
            200: _response_contract(
                "commands.schema.json#/$defs/ProcessDraftAcceptedResponse",
                "Process draft request",
            ),
            **_WRITE_RESPONSES,
        },
        operation_id="proposeProcess",
    )
    async def propose_process(request: ProposeProcessRequest) -> DataResponse:
        draft = await execution_service.propose_process_async(
            ProposeProcess(
                request.idempotency_key,
                _id(str(request.run_id)),
                request.reason,
            )
        )
        return _response({"draft_id": draft.draft_id, "status": draft.status.value})

    @router.post(
        "/commands/review-process",
        response_model=DataResponse,
        responses={
            200: _response_contract(
                "commands.schema.json#/$defs/ProcessReviewAcceptedResponse",
                "Process review",
            ),
            **_WRITE_RESPONSES,
        },
        operation_id="reviewProcess",
    )
    async def review_process(request: ReviewProcessRequest) -> DataResponse:
        review = await execution_service.review_process_async(
            ReviewProcess(
                request.idempotency_key,
                _id(str(request.draft_id)),
            )
        )
        return _response(
            {
                "review_id": review.review_id,
                "status": review.status.value,
                "preserves_boundary": review.preserves_boundary,
            }
        )

    @router.post(
        "/commands/apply-process",
        response_model=DataResponse,
        responses={
            201: _response_contract(
                "queries.schema.json#/$defs/ProcessRevisionResponse",
                "Applied ProcessRevision",
            ),
            **_WRITE_RESPONSES,
        },
        status_code=201,
        operation_id="applyProcess",
    )
    def apply_process(request: ApplyProcessRequest) -> DataResponse:
        process = execution_service.apply_process(
            ApplyProcess(
                request.idempotency_key,
                _id(str(request.review_id)),
            )
        )
        return _response(query_service.get_process_revision(process.process_revision_id))

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
        if request.execution_config is None:
            return _response(
                execution_service.start_run(
                    StartRun(request.idempotency_key, _id(str(request.plan_revision_id)))
                )
            )
        document = request.execution_config.model_dump(mode="json")
        if document.get("codex_server") is None:
            document.pop("codex_server", None)
        try:
            config = ExecutionConfig.from_document(document)
        except OSError as error:
            raise ValueError("execution workspace is unavailable") from error
        command = StartRun(
            request.idempotency_key,
            _id(str(request.plan_revision_id)),
            authorized_execution_config_json=json_dumps(config.to_document()),
        )
        replay = execution_service.get_start_run_replay(command)
        if replay is not None:
            return _response(replay)
        if execution_config_validator is None:
            raise StateConflictError("This host does not support explicit execution configuration")
        execution_config_validator(config)
        return _response(execution_service.start_run(command))

    @router.post(
        "/check-runs/{check_run_id}/decision",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
        operation_id="decideHumanCheck",
    )
    def decide_human_check(
        check_run_id: UuidInput,
        request: DecideHumanCheckRequest,
    ) -> DataResponse:
        run = execution_service.decide_human_check(
            DecideHumanCheck(
                request.idempotency_key,
                _id(str(check_run_id)),
                request.request_token,
                request.passed,
                request.actor,
                request.comment,
            )
        )
        if runtime_control is not None and run.status is RunStatus.RUNNING:
            runtime_control.resume_run_scheduling(run.run_id)
        return _response(run)

    @router.get(
        "/runs/{run_id}/interventions",
        response_model=DataResponse,
        responses=_read_responses("InterventionListResponse", "Run interventions"),
    )
    def get_run_interventions(run_id: UuidInput) -> DataResponse:
        return _response(query_service.list_interventions(_id(str(run_id))))

    @router.post(
        "/interventions/{intervention_id}/reply",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
        operation_id="replyIntervention",
    )
    def reply_intervention(
        intervention_id: UuidInput,
        request: ReplyInterventionRequest,
    ) -> DataResponse:
        run = execution_service.reply_intervention(
            ReplyIntervention(
                request.idempotency_key,
                _id(str(intervention_id)),
                request.request_token,
                request.actor,
                request.message,
            )
        )
        if runtime_control is not None and run.status is RunStatus.RUNNING:
            runtime_control.resume_run_scheduling(run.run_id)
        return _response(run)

    @router.post(
        "/runs/{run_id}/pause",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    async def pause_run(run_id: UuidInput, request: RunActionRequest) -> DataResponse:
        normalized_id = _id(str(run_id))
        command = PauseRun(request.idempotency_key, normalized_id)
        replay = execution_service.get_run_control_replay(command)
        if replay is not None:
            return _response(replay)
        if process_adjustments is not None:
            await process_adjustments.cancel(normalized_id)
        if runtime_control is not None:
            await runtime_control.quiesce_run(normalized_id)
        try:
            paused = execution_service.pause_run(command)
        except BaseException:
            if runtime_control is not None:
                runtime_control.resume_run_scheduling(normalized_id)
            raise
        if process_adjustments is not None:
            await process_adjustments.cancel(normalized_id)
        if runtime_control is not None:
            if paused.status is RunStatus.PAUSED:
                runtime_control.release_run_dispatch(normalized_id)
            else:
                runtime_control.resume_run_scheduling(normalized_id)
        return _response(paused)

    @router.post(
        "/runs/{run_id}/resume",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    async def resume_run(run_id: UuidInput, request: RunActionRequest) -> DataResponse:
        normalized_id = _id(str(run_id))
        command = ResumeRun(request.idempotency_key, normalized_id)
        replay = execution_service.get_run_control_replay(command)
        if replay is not None:
            return _response(replay)
        if process_adjustments is not None:
            await process_adjustments.cancel(normalized_id)
        resumed = execution_service.resume_run(command)
        if runtime_control is not None:
            runtime_control.resume_run_scheduling(normalized_id)
        return _response(resumed)

    @router.post(
        "/runs/{run_id}/cancel",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    async def cancel_run(run_id: UuidInput, request: CancelRunRequest) -> DataResponse:
        normalized_id = _id(str(run_id))
        command = CancelRun(request.idempotency_key, normalized_id, request.reason)
        replay = execution_service.get_run_control_replay(command)
        if replay is not None:
            return _response(replay)
        if process_adjustments is not None:
            await process_adjustments.cancel(normalized_id)
        if runtime_control is not None:
            await runtime_control.quiesce_run(normalized_id)
        try:
            cancelled = execution_service.cancel_run(command)
        except BaseException:
            if runtime_control is not None:
                runtime_control.resume_run_scheduling(normalized_id)
            raise
        if process_adjustments is not None:
            await process_adjustments.cancel(normalized_id)
        if runtime_control is not None:
            runtime_control.release_run_dispatch(normalized_id)
        return _response(cancelled)

    @router.get(
        "/runs/{run_id}",
        response_model=DataResponse,
        responses=_read_responses("RunResponse", "Current Run state"),
    )
    def get_run(run_id: UuidInput) -> DataResponse:
        return _response(query_service.get_run(_id(str(run_id))))

    @router.get(
        "/workers/profiles",
        response_model=DataResponse,
        responses=_read_responses("WorkerProfileListResponse", "Worker Profiles"),
    )
    def list_worker_profiles() -> DataResponse:
        return _response(query_service.list_worker_profiles())

    @router.get(
        "/runtime/health",
        response_model=DataResponse,
        responses=_read_responses("RuntimeHealthResponse", "Runtime scheduler health"),
    )
    def get_runtime_health() -> DataResponse:
        control = _required_runtime_control(runtime_control)
        return _response(control.runtime_health())

    @router.get(
        "/workers/endpoints",
        response_model=DataResponse,
        responses=_read_responses("WorkerEndpointListResponse", "Worker Endpoints"),
    )
    def list_worker_endpoints() -> DataResponse:
        return _response(query_service.list_worker_endpoints())

    @router.get(
        "/attempts/{attempt_id}/runtime",
        response_model=DataResponse,
        responses=_read_responses("AttemptRuntimeResponse", "Attempt Runtime diagnostics"),
    )
    def get_attempt_runtime(attempt_id: UuidInput) -> DataResponse:
        return _response(query_service.get_attempt_runtime(_id(str(attempt_id))))

    @router.get(
        "/attempts/{attempt_id}/worker-requests",
        response_model=DataResponse,
        responses=_read_responses("WorkerRequestListResponse", "Pending Worker requests"),
    )
    def list_worker_requests(attempt_id: UuidInput) -> DataResponse:
        control = _required_runtime_control(runtime_control)
        return _response(control.list_waiting_requests(_id(str(attempt_id))))

    @router.post(
        "/worker-requests/{worker_request_id}/resolve",
        response_model=DataResponse,
        responses=_read_responses("WorkerRequestResponse", "Resolved Worker request"),
    )
    async def resolve_worker_request(
        worker_request_id: UuidInput,
        request: ResolveWorkerRequestRequest,
    ) -> DataResponse:
        control = _required_runtime_control(runtime_control)
        return _response(
            await control.resolve_worker_request(
                _id(str(worker_request_id)),
                request.resolution,
                idempotency_key=request.idempotency_key,
            )
        )

    @router.post(
        "/worker-requests/{worker_request_id}/decline",
        response_model=DataResponse,
        responses=_read_responses("WorkerRequestResponse", "Declined Worker request"),
    )
    async def decline_worker_request(
        worker_request_id: UuidInput,
        request: RunActionRequest,
    ) -> DataResponse:
        control = _required_runtime_control(runtime_control)
        return _response(
            await control.decline_worker_request(
                _id(str(worker_request_id)),
                idempotency_key=request.idempotency_key,
            )
        )

    @router.post(
        "/attempts/{attempt_id}/deadline",
        response_model=DataResponse,
        responses=_read_responses("AttemptRuntimeResponse", "Extended Attempt deadline"),
    )
    def extend_attempt_deadline(
        attempt_id: UuidInput,
        request: ExtendAttemptDeadlineRequest,
    ) -> DataResponse:
        control = _required_runtime_control(runtime_control)
        normalized_id = _id(str(attempt_id))
        control.extend_deadline(
            normalized_id,
            request.deadline_at,
            idempotency_key=request.idempotency_key,
        )
        return _response(query_service.get_attempt_runtime(normalized_id))

    @router.post(
        "/attempts/{attempt_id}/cancel",
        response_model=DataResponse,
        responses=_RUN_WRITE_RESPONSES,
    )
    async def cancel_attempt(
        attempt_id: UuidInput,
        request: RunActionRequest,
    ) -> DataResponse:
        control = _required_runtime_control(runtime_control)
        return _response(
            await control.cancel_attempt(
                _id(str(attempt_id)),
                idempotency_key=request.idempotency_key,
            )
        )

    @router.get(
        "/plans/{plan_revision_id}",
        response_model=DataResponse,
        responses=_read_responses("PlanGraphResponse", "Versioned PlanGraph"),
    )
    def get_plan_graph(plan_revision_id: UuidInput) -> DataResponse:
        return _response(query_service.get_plan_graph(_id(str(plan_revision_id))))

    @router.get(
        "/runs/{run_id}/plan",
        response_model=DataResponse,
        responses=_read_responses("PlanGraphResponse", "Current Run execution PlanGraph"),
        operation_id="getRunPlan",
    )
    def get_run_plan(run_id: UuidInput) -> DataResponse:
        return _response(query_service.get_run_plan(_id(str(run_id))))

    @router.get(
        "/process-revisions/{process_revision_id}",
        response_model=DataResponse,
        responses=_read_responses("ProcessRevisionResponse", "ProcessRevision"),
        operation_id="getProcessRevision",
    )
    def get_process_revision(process_revision_id: UuidInput) -> DataResponse:
        return _response(query_service.get_process_revision(_id(str(process_revision_id))))

    @router.get(
        "/process-drafts/{draft_id}",
        response_model=DataResponse,
        responses=_read_responses("ProcessDraftResponse", "Process draft"),
        operation_id="getProcessDraft",
    )
    def get_process_draft(draft_id: UuidInput) -> DataResponse:
        return _response(query_service.get_process_draft(_id(str(draft_id))))

    @router.get(
        "/process-reviews/{review_id}",
        response_model=DataResponse,
        responses=_read_responses("ProcessReviewResponse", "Process review"),
        operation_id="getProcessReview",
    )
    def get_process_review(review_id: UuidInput) -> DataResponse:
        return _response(query_service.get_process_review(_id(str(review_id))))

    @router.get(
        "/process-drafts/{draft_id}/reviews",
        response_model=DataResponse,
        responses=_read_responses("ProcessDraftReviewsResponse", "Process draft reviews"),
        operation_id="getProcessDraftReviews",
    )
    def get_process_draft_reviews(draft_id: UuidInput) -> DataResponse:
        return _response(query_service.get_process_draft_reviews(_id(str(draft_id))))

    @router.get(
        "/runs/{run_id}/process-drafts",
        response_model=DataResponse,
        responses=_read_responses("RunProcessDraftsResponse", "Run process drafts"),
        operation_id="getRunProcessDrafts",
    )
    def get_run_process_drafts(run_id: UuidInput) -> DataResponse:
        return _response(query_service.get_run_process_drafts(_id(str(run_id))))

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
        "/runs/{run_id}/adoptions",
        response_model=DataResponse,
        responses=_read_responses("ResultAdoptionListResponse", "Run result adoptions"),
    )
    def list_result_adoptions(run_id: UuidInput) -> DataResponse:
        return _response(query_service.list_result_adoptions(_id(str(run_id))))

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
    return DataResponse(data=public_json_value(value))


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
    @app.exception_handler(StateConflictError)
    @app.exception_handler(RunControlError)
    @app.exception_handler(RuntimeControlError)
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


def _required_runtime_control(
    runtime_control: RuntimeControlService | None,
) -> RuntimeControlService:
    if runtime_control is None:
        raise RuntimeControlError("P2 Runtime Control is not configured for this process")
    return runtime_control
