"""Contracts for the two fixed, project-owned life workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

WorkflowName = Literal["life.capture", "life.review"]
LifeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
WorkflowKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LifeTaskInput(WorkflowModel):
    title: Annotated[LifeText, Field(max_length=300)]
    due_at: AwareDatetime | None = None


class StartWorkflowRequest(WorkflowModel):
    """Capture needs source_text and tasks; review reads this project and accepts neither.

    Confirmation applies only to capture. Omitting it means true; false explicitly creates
    the provided tasks immediately. Neither workflow executes the described life tasks.
    """

    idempotency_key: WorkflowKey
    workflow: WorkflowName
    source_text: LifeText | None = None
    tasks: list[LifeTaskInput] = Field(default_factory=list, max_length=100)
    require_confirmation: bool = True

    @model_validator(mode="after")
    def validate_input(self) -> StartWorkflowRequest:
        if self.workflow == "life.capture" and (not self.source_text or not self.tasks):
            raise ValueError("life.capture requires source_text and at least one structured task")
        if self.workflow == "life.review" and (self.source_text is not None or self.tasks):
            raise ValueError(
                "life.review takes no source_text or tasks and only reads this project"
            )
        return self


class DecideWorkflowRequest(WorkflowModel):
    idempotency_key: WorkflowKey
    expected_version: int = Field(ge=1, strict=True)
    decision: Literal["approve", "reject"]
    actor: Annotated[LifeText, Field(max_length=200)]
    reason: LifeText


class UpdateLifeTaskRequest(WorkflowModel):
    """Complete replacement of task-editable fields; null explicitly clears due_at."""

    idempotency_key: WorkflowKey
    expected_version: int = Field(ge=1, strict=True)
    title: Annotated[LifeText, Field(max_length=300)]
    due_at: AwareDatetime | None
    status: Literal["open", "done", "cancelled"]


class CreateRoutineRequest(WorkflowModel):
    """Schedule life.review v1 at a fixed interval, with no external or model side effects.

    next_due_at must include a timezone offset. A past time triggers a single coalesced review
    when the host next ticks. enabled=false persists the routine without scheduling it.
    """

    idempotency_key: WorkflowKey
    name: Annotated[LifeText, Field(max_length=300)]
    interval_seconds: int = Field(ge=60, le=31536000, strict=True)
    next_due_at: AwareDatetime
    enabled: bool = True


class UpdateRoutineRequest(CreateRoutineRequest):
    """Replace scheduling fields; completed runs retain their original schedule snapshot."""

    expected_version: int = Field(ge=1, strict=True)


class LifeTask(WorkflowModel):
    task_id: str
    project_id: str
    workflow_run_id: str
    title: str
    due_at: datetime | None
    status: Literal["open", "done", "cancelled"]
    version: int
    created_at: datetime
    updated_at: datetime


class LifeReview(WorkflowModel):
    as_of: datetime
    open_tasks: list[LifeTask]
    overdue_task_ids: list[str]
    done_count: int
    cancelled_count: int


class WorkflowDecision(WorkflowModel):
    decision: Literal["approve", "reject"]
    actor: str
    reason: str
    decided_at: datetime


class LifeRoutine(WorkflowModel):
    routine_id: str
    project_id: str
    name: str
    workflow: Literal["life.review"] = "life.review"
    workflow_version: Literal[1] = 1
    interval_seconds: int
    next_due_at: datetime
    enabled: bool
    version: int
    last_workflow_run_id: str | None
    created_at: datetime
    updated_at: datetime


class WorkflowRun(WorkflowModel):
    workflow_run_id: str
    project_id: str
    workflow: WorkflowName
    workflow_version: Literal[1] = 1
    status: Literal["awaiting_confirmation", "completed", "rejected"]
    version: int
    source_text: str | None
    proposed_tasks: list[LifeTaskInput]
    task_ids: list[str]
    review: LifeReview | None
    decision: WorkflowDecision | None
    routine_snapshot: LifeRoutine | None
    scheduled_for: datetime | None
    coalesced_occurrences: int
    created_at: datetime
    completed_at: datetime | None


class WorkflowDefinition(WorkflowModel):
    workflow: WorkflowName
    version: Literal[1] = 1
    description: str
    steps: list[str]


class RoutineSchedulerStatus(WorkflowModel):
    active: bool
    last_tick_at: datetime | None
    last_error: str | None
