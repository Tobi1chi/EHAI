"""Pydantic request and response envelopes for the P1 HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
)

from ehai import JsonValue, format_utc_datetime, normalize_id

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
P1CompletionCriterion = Literal[
    "artifact:non-empty",
    "command:exit-zero",
    "semantic:required-terms",
]
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
    criteria: list[P1CompletionCriterion] = Field(min_length=1, max_length=1)


class ReplanPlanRequest(_StrictRequest):
    idempotency_key: NonBlank
    base_plan_revision_id: UuidInput
    criteria: list[P1CompletionCriterion] = Field(min_length=1, max_length=1)


class ApprovePlanRequest(_StrictRequest):
    idempotency_key: NonBlank
    plan_revision_id: UuidInput
    completion_contract_id: UuidInput


class StartRunRequest(_StrictRequest):
    idempotency_key: NonBlank
    plan_revision_id: UuidInput


class RunActionRequest(_StrictRequest):
    idempotency_key: NonBlank


class CancelRunRequest(RunActionRequest):
    reason: NonBlank | None = None


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
