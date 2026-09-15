"""Pydantic request and response envelopes for the P1 HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    WithJsonSchema,
    field_serializer,
    field_validator,
)

from ehai import JsonValue, format_utc_datetime, normalize_id
from ehai.application.plan_imports import plan_import_schema

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
HumanCompletionCriterion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=8000,
        pattern=r"^human:\s*\S.*$",
    ),
]
P1CompletionCriterion = (
    Literal[
        "artifact:non-empty",
        "command:exit-zero",
        "semantic:required-terms",
    ]
    | HumanCompletionCriterion
)
UuidInput = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
        )
    ),
    AfterValidator(normalize_id),
]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(_StrictRequest):
    idempotency_key: NonBlank
    name: NonBlank


class CreateGoalRequest(_StrictRequest):
    idempotency_key: NonBlank
    project_id: UuidInput
    objective: NonBlank


class ProposePlanRequest(_StrictRequest):
    idempotency_key: NonBlank
    goal_id: UuidInput
    criteria: list[P1CompletionCriterion] = Field(min_length=1, max_length=3)


class ImportPlanRequest(_StrictRequest):
    idempotency_key: NonBlank
    goal_id: UuidInput
    plan: Annotated[dict[str, JsonValue], WithJsonSchema(plan_import_schema())]


class ReplanPlanRequest(_StrictRequest):
    idempotency_key: NonBlank
    base_plan_revision_id: UuidInput
    criteria: list[P1CompletionCriterion] = Field(min_length=1, max_length=3)
    source_run_id: UuidInput | None = None


class ProposeProcessRequest(_StrictRequest):
    idempotency_key: NonBlank
    run_id: UuidInput
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class ReviewProcessRequest(_StrictRequest):
    idempotency_key: NonBlank
    draft_id: UuidInput


class ApplyProcessRequest(_StrictRequest):
    idempotency_key: NonBlank
    review_id: UuidInput


class DiscussPlanRequest(ProposePlanRequest):
    criteria: list[P1CompletionCriterion] = Field(default_factory=list, max_length=3)
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    conversation_id: UuidInput | None = None
    source_run_id: UuidInput | None = None


class ApprovePlanRequest(_StrictRequest):
    idempotency_key: NonBlank
    plan_revision_id: UuidInput
    completion_contract_id: UuidInput


class ExecutionEndpointCapabilitiesRequest(_StrictRequest):
    supports_background: StrictBool
    supports_unique_items: StrictBool
    supports_idempotent_create: StrictBool | None
    supports_previous_response_id: StrictBool
    supports_response_retrieval: StrictBool


ExecutionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\x00]*[^\s\x00][^\x00]*$"),
]


class ExecutionCodexServerRequest(_StrictRequest):
    executable: list[ExecutionText] = Field(min_length=1)
    approval_policy: Literal["untrusted", "on-request", "never"]
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"]


class ProcessAdjustmentPolicyRequest(_StrictRequest):
    max_per_goal: Annotated[int, Field(strict=True, ge=1)]
    model: ExecutionText
    reasoning_effort: ExecutionText | None


class TrajectoryReviewPolicyRequest(_StrictRequest):
    interval_seconds: Annotated[int, Field(strict=True, ge=1)] = 600
    step_count: Annotated[int, Field(strict=True, ge=1)] = 30
    model: ExecutionText = "gpt-5.6-luna"


class GoalWorkerBudgetRequest(_StrictRequest):
    max_worker_attempts: Annotated[int, Field(strict=True, ge=1)]


class PiBackendConfigRequest(_StrictRequest):
    node: ExecutionText
    cli: ExecutionText
    agent_dir: ExecutionText
    provider: ExecutionText
    environment_names: list[ExecutionText]
    configuration_hash: ExecutionText | None = None


class ExecutionConfigRequest(_StrictRequest):
    """Explicit confirmation of the host's credential-free execution settings."""

    config_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    worker_kind: Literal["pi", "codex-server"]
    model: ExecutionText
    reasoning_effort: ExecutionText | None
    capacity: Annotated[int, Field(strict=True, ge=1)]
    workspace: ExecutionText
    allowed_commands: list[Annotated[list[ExecutionText], Field(min_length=1)]] = Field(
        json_schema_extra={"uniqueItems": True},
        description="Distinct argv arrays; duplicates are checked after text normalization.",
    )
    available_shells: list[ExecutionText] = Field(
        json_schema_extra={"uniqueItems": True},
        description="Distinct shell names; duplicates are checked after text normalization.",
    )
    git_permissions: list[
        Literal["git.read", "git.local_write", "git.remote_write", "git.dangerous"]
    ]
    endpoint_capabilities: ExecutionEndpointCapabilitiesRequest
    command_timeout_seconds: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    codex_server: ExecutionCodexServerRequest | None = None
    process_adjustment: ProcessAdjustmentPolicyRequest | None = None
    goal_worker_budget: GoalWorkerBudgetRequest | None = None
    trajectory_review: TrajectoryReviewPolicyRequest | None = None
    pi: PiBackendConfigRequest | None = None


class StartRunRequest(_StrictRequest):
    idempotency_key: NonBlank
    plan_revision_id: UuidInput
    execution_config: ExecutionConfigRequest | None = None


class RunActionRequest(_StrictRequest):
    idempotency_key: NonBlank


class SuspendAttemptRequest(RunActionRequest):
    review_id: UuidInput
    through_sequence: Annotated[int, Field(strict=True, ge=1)]
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class CancelRunRequest(RunActionRequest):
    reason: NonBlank | None = None


class DecideHumanCheckRequest(_StrictRequest):
    idempotency_key: NonBlank
    request_token: Annotated[
        str,
        StringConstraints(
            min_length=64,
            max_length=64,
            pattern=r"^[0-9a-fA-F]{64}$",
        ),
    ]
    passed: StrictBool
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    comment: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=8000),
    ]


class ReplyInterventionRequest(_StrictRequest):
    idempotency_key: NonBlank
    request_token: Annotated[
        str,
        StringConstraints(
            min_length=64,
            max_length=64,
            pattern=r"^[0-9a-fA-F]{64}$",
        ),
    ]
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=8000),
    ]


class ExtendAttemptDeadlineRequest(_StrictRequest):
    idempotency_key: NonBlank
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must include a UTC offset")
        return value

    @field_serializer("deadline_at")
    def serialize_deadline(self, value: datetime) -> str:
        return format_utc_datetime(value)


class ResolveWorkerRequestRequest(_StrictRequest):
    idempotency_key: NonBlank
    resolution: dict[str, JsonValue]


class DataResponse(BaseModel):
    data: JsonValue


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
