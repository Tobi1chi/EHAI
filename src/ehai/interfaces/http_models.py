"""Pydantic request and response envelopes for the P1 HTTP API."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from ehai import JsonValue, normalize_id

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
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
    criteria: list[NonBlank] = Field(min_length=1)


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


class DataResponse(BaseModel):
    data: JsonValue


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
