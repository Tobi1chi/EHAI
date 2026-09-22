"""HTTP transport for versioned project configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ehai import JsonValue, normalize_id
from ehai.application.project_configuration import (
    ProjectConfigurationConflict,
    ProjectConfigurationError,
    ProjectConfigurationNotFound,
    ProjectConfigurationService,
)
from ehai.interfaces.http_models import NonBlank, UuidInput


class UpdateProjectConfigurationRequest(BaseModel):
    """A complete replacement: empty static_rules clears rules in the new version only."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: NonBlank
    expected_version: int = Field(ge=0, strict=True)
    workspace: NonBlank
    execution_config_fingerprint: NonBlank | None
    role_configuration_ref: NonBlank
    static_rules: list[NonBlank] = Field(max_length=100)


def create_project_configuration_router(service: ProjectConfigurationService) -> APIRouter:
    """Create routes relative to the existing /api/v1 prefix."""
    router = APIRouter()

    @router.get(
        "/project-configuration-host",
        operation_id="getConfigurationHost",
        response_model=None,
        responses=_responses("HostConfigurationResponse"),
    )
    def host_configuration() -> dict[str, JsonValue] | JSONResponse:
        try:
            return {"data": service.host()}
        except ProjectConfigurationError as error:
            return _http_error(error)

    @router.get(
        "/projects/{project_id}/configuration",
        operation_id="getProjectConfiguration",
        response_model=None,
        responses=_responses("ProjectConfigurationResponse"),
    )
    def get_configuration(project_id: UuidInput) -> dict[str, JsonValue] | JSONResponse:
        try:
            return {"data": service.get(normalize_id(project_id))}
        except ProjectConfigurationError as error:
            return _http_error(error)

    @router.get(
        "/projects/{project_id}/configuration/versions",
        operation_id="getProjectConfigurationVersions",
        response_model=None,
        responses=_responses("ProjectConfigurationVersionsResponse"),
    )
    def configuration_versions(project_id: UuidInput) -> dict[str, JsonValue] | JSONResponse:
        try:
            return {"data": service.versions(normalize_id(project_id))}
        except ProjectConfigurationError as error:
            return _http_error(error)

    @router.post(
        "/projects/{project_id}/configuration",
        operation_id="configureProject",
        response_model=None,
        responses=_responses("UpdateProjectConfigurationResponse"),
    )
    def update_configuration(
        project_id: UuidInput, request: UpdateProjectConfigurationRequest
    ) -> dict[str, JsonValue] | JSONResponse:
        try:
            return {"data": service.update(normalize_id(project_id), **request.model_dump())}
        except ProjectConfigurationError as error:
            return _http_error(error)

    @router.get(
        "/runs/{run_id}/configuration",
        operation_id="getRunConfiguration",
        response_model=None,
        responses=_responses("RunConfigurationResponse"),
    )
    def run_configuration(run_id: UuidInput) -> dict[str, JsonValue] | JSONResponse:
        try:
            return {"data": service.get_run(normalize_id(run_id))}
        except ProjectConfigurationError as error:
            return _http_error(error)

    return router


def _http_error(error: ProjectConfigurationError) -> JSONResponse:
    status = 422
    if isinstance(error, ProjectConfigurationNotFound):
        status = 404
    elif isinstance(error, ProjectConfigurationConflict):
        status = 409
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": {404: "not_found", 409: "conflict", 422: "invalid_request"}[status],
                "message": str(error),
            }
        },
    )


def _responses(name: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {
            "description": "Versioned configuration",
            "content": {
                "application/json": {
                    "schema": {"$ref": f"project-configuration.schema.json#/$defs/{name}"}
                }
            },
        }
    }
