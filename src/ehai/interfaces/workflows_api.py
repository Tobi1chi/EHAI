"""Normal HTTP entry points for life workflows; MCP consumes the same OpenAPI."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict

from ehai import normalize_id
from ehai.application.workflow_execution_models import WorkflowExecutionView
from ehai.application.workflow_models import (
    CreateRoutineRequest,
    DecideWorkflowRequest,
    LifeRoutine,
    LifeTask,
    RoutineSchedulerStatus,
    StartWorkflowRequest,
    UpdateLifeTaskRequest,
    UpdateRoutineRequest,
    WorkflowDefinition,
    WorkflowRun,
)
from ehai.application.workflows import WorkflowService
from ehai.interfaces.http_models import UuidInput


class WorkflowResponse[T](BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: T


def build_workflows_router(service: WorkflowService) -> APIRouter:
    router = APIRouter()

    @router.get("/workflows", operation_id="listWorkflows")
    def definitions() -> WorkflowResponse[list[WorkflowDefinition]]:
        return WorkflowResponse(data=service.definitions())

    @router.get("/routines/scheduler", operation_id="getRoutineScheduler")
    def scheduler() -> WorkflowResponse[RoutineSchedulerStatus]:
        return WorkflowResponse(data=service.scheduler_status())

    @router.post("/projects/{project_id}/workflow-runs", operation_id="startWorkflow")
    def start(project_id: UuidInput, body: StartWorkflowRequest) -> WorkflowResponse[WorkflowRun]:
        return WorkflowResponse(data=service.start(normalize_id(project_id), body))

    @router.get("/projects/{project_id}/workflow-runs", operation_id="listWorkflowRuns")
    def runs(project_id: UuidInput) -> WorkflowResponse[list[WorkflowRun]]:
        return WorkflowResponse(
            data=service.list_records("run", normalize_id(project_id), WorkflowRun)
        )

    @router.get("/workflow-runs/{workflow_run_id}", operation_id="getWorkflowRun")
    def run(workflow_run_id: UuidInput) -> WorkflowResponse[WorkflowRun]:
        return WorkflowResponse(data=service.get("run", normalize_id(workflow_run_id), WorkflowRun))

    @router.post("/workflow-runs/{workflow_run_id}/decisions", operation_id="decideWorkflow")
    def decide(
        workflow_run_id: UuidInput,
        body: DecideWorkflowRequest,
    ) -> WorkflowResponse[WorkflowRun]:
        return WorkflowResponse(data=service.decide(normalize_id(workflow_run_id), body))

    @router.get("/workflow-runs/{workflow_run_id}/execution", operation_id="getWorkflowExecution")
    def execution(workflow_run_id: UuidInput) -> WorkflowResponse[WorkflowExecutionView]:
        """Read independent Run, step, local invocation and wait records from one snapshot."""
        return WorkflowResponse(data=service.execution(normalize_id(workflow_run_id)))

    @router.get("/projects/{project_id}/life-tasks", operation_id="listLifeTasks")
    def tasks(project_id: UuidInput) -> WorkflowResponse[list[LifeTask]]:
        return WorkflowResponse(
            data=service.list_records("task", normalize_id(project_id), LifeTask)
        )

    @router.get("/life-tasks/{task_id}", operation_id="getLifeTask")
    def task(task_id: UuidInput) -> WorkflowResponse[LifeTask]:
        return WorkflowResponse(data=service.get("task", normalize_id(task_id), LifeTask))

    @router.post("/life-tasks/{task_id}", operation_id="updateLifeTask")
    def update_task(task_id: UuidInput, body: UpdateLifeTaskRequest) -> WorkflowResponse[LifeTask]:
        return WorkflowResponse(data=service.update_task(normalize_id(task_id), body))

    @router.post("/projects/{project_id}/routines", operation_id="createRoutine")
    def create_routine(
        project_id: UuidInput,
        body: CreateRoutineRequest,
    ) -> WorkflowResponse[LifeRoutine]:
        return WorkflowResponse(data=service.create_routine(normalize_id(project_id), body))

    @router.get("/projects/{project_id}/routines", operation_id="listRoutines")
    def routines(project_id: UuidInput) -> WorkflowResponse[list[LifeRoutine]]:
        return WorkflowResponse(
            data=service.list_records("routine", normalize_id(project_id), LifeRoutine)
        )

    @router.get("/routines/{routine_id}", operation_id="getRoutine")
    def routine(routine_id: UuidInput) -> WorkflowResponse[LifeRoutine]:
        return WorkflowResponse(data=service.get("routine", normalize_id(routine_id), LifeRoutine))

    @router.post("/routines/{routine_id}", operation_id="updateRoutine")
    def update_routine(
        routine_id: UuidInput,
        body: UpdateRoutineRequest,
    ) -> WorkflowResponse[LifeRoutine]:
        return WorkflowResponse(data=service.update_routine(normalize_id(routine_id), body))

    return router


def install_routine_scheduler(app: FastAPI, service: WorkflowService) -> None:
    """Start in lightweight and Worker hosts; drain local ticks before shutdown."""
    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None

    async def loop() -> None:
        service.scheduler_active = True
        try:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(service.tick)
                except Exception:
                    service.last_error = (
                        "Routine tick failed; individual transactions remain atomic"
                    )
                    logging.getLogger(__name__).exception("Routine tick failed")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
        finally:
            service.scheduler_active = False

    async def start() -> None:
        nonlocal task
        stop.clear()
        task = asyncio.create_task(loop(), name="ehai-life-routines")

    async def shutdown() -> None:
        stop.set()
        if task is not None:
            await task

    app.router.add_event_handler("startup", start)
    app.router.add_event_handler("shutdown", shutdown)
