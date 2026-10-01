"""Independent workflow execution facts, without life-task-specific fields."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from ehai import JsonValue
from ehai.application.workflow_models import WorkflowModel


class WorkflowContractRef(WorkflowModel):
    kind: Literal["workflow", "step", "action"]
    name: str
    version: str
    digest: str | None = None


class WorkflowExecutionRecord(WorkflowModel):
    workflow_run_id: str
    project_id: str
    definition: WorkflowContractRef
    definition_snapshot: dict[str, JsonValue] | None
    status: str
    version: int = Field(ge=1)
    inputs: dict[str, JsonValue]
    result: dict[str, JsonValue]
    trigger: dict[str, JsonValue]
    recording_origin: Literal["native", "legacy_snapshot"]
    created_at: datetime
    completed_at: datetime | None


class WorkflowStepExecution(WorkflowModel):
    step_execution_id: str
    workflow_run_id: str
    node_id: str
    execution_index: int = Field(ge=1)
    contract: WorkflowContractRef
    status: Literal["completed", "waiting", "skipped"]
    inputs: dict[str, JsonValue]
    output: dict[str, JsonValue] | None
    recorded_at: datetime
    completed_at: datetime | None


class WorkflowInvocationRecord(WorkflowModel):
    invocation_id: str
    workflow_run_id: str
    step_execution_id: str
    kind: Literal["local", "connector"]
    action: WorkflowContractRef
    status: Literal["prepared", "completed", "failed", "unknown"]
    idempotency_key: str
    inputs: dict[str, JsonValue]
    output: dict[str, JsonValue] | None
    external_operation_ref: str | None
    error: str | None
    recorded_at: datetime
    completed_at: datetime | None


class WorkflowWaitRecord(WorkflowModel):
    wait_id: str
    workflow_run_id: str
    step_execution_id: str
    kind: Literal["human", "time", "event"]
    status: Literal["waiting", "resolved"]
    condition: dict[str, JsonValue]
    resolution: dict[str, JsonValue] | None
    recorded_at: datetime
    resolved_at: datetime | None


class WorkflowCompatibilityStatus(WorkflowModel):
    enabled: bool
    registered_adapters: int


class WorkflowExecutionView(WorkflowModel):
    run: WorkflowExecutionRecord
    steps: list[WorkflowStepExecution]
    invocations: list[WorkflowInvocationRecord]
    waits: list[WorkflowWaitRecord]
    compatibility: WorkflowCompatibilityStatus
