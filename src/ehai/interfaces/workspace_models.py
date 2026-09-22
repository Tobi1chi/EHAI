"""Closed registration and public lifecycle contracts for local workspace hosts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

WORKSPACE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
WorkspaceId = Annotated[str, StringConstraints(pattern=WORKSPACE_ID_PATTERN)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
GitPermission = Literal["git.read", "git.local_write", "git.remote_write", "git.dangerous"]


class RuntimeSettings(BaseModel):
    """Fixed executable assembly; this profile never grants a Run execution authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    worker_kind: Literal["pi", "fake"] = "fake"
    planner_kind: Literal["pi", "single"] = "single"
    worker_model: Text | None = None
    planner_model: Text | None = None
    worker_reasoning_effort: Text | None = None
    planner_reasoning_effort: Text | None = None
    worker_capacity: int = Field(default=1, ge=1, le=64, strict=True)
    planner_capacity: int = Field(default=1, ge=1, le=64, strict=True)
    worker_timeout_seconds: float = Field(default=300, gt=0, le=86400, allow_inf_nan=False)
    planner_timeout_seconds: float = Field(default=120, gt=0, le=86400, allow_inf_nan=False)
    command_timeout_seconds: float = Field(default=30, gt=0, le=3600, allow_inf_nan=False)
    pi_config_path: Text | None = None
    allowed_commands: list[list[Text]] = Field(default_factory=list, max_length=100)
    available_shells: list[Text] = Field(default_factory=list, max_length=32)
    git_permissions: list[GitPermission] = Field(default_factory=list, max_length=4)
    command_check_argv: list[Text] | None = None
    semantic_required_terms: list[Text] = Field(default_factory=list, max_length=100)
    attempt_deadline_seconds: float | None = Field(
        default=None, gt=0, le=86400, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        if self.worker_kind == "pi" and self.worker_model is None:
            raise ValueError("Pi Worker requires worker_model")
        if self.planner_kind == "pi" and self.planner_model is None:
            raise ValueError("Pi Planner requires planner_model")
        if "pi" in (self.worker_kind, self.planner_kind) and self.pi_config_path is None:
            raise ValueError("Pi roles require an explicit pi_config_path")
        if "pi" not in (self.worker_kind, self.planner_kind) and self.pi_config_path is not None:
            raise ValueError("pi_config_path requires a Pi role")
        if any(not command for command in self.allowed_commands):
            raise ValueError("allowed_commands entries must be nonempty argv arrays")
        if len({tuple(command) for command in self.allowed_commands}) != len(self.allowed_commands):
            raise ValueError("allowed_commands must not contain duplicates")
        if len(set(self.available_shells)) != len(self.available_shells):
            raise ValueError("available_shells must not contain duplicates")
        if len(set(self.git_permissions)) != len(self.git_permissions):
            raise ValueError("git_permissions must not contain duplicates")
        if self.command_check_argv == []:
            raise ValueError("command_check_argv must be nonempty or null")
        return self


class WorkspaceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: WorkspaceId
    path: Text
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)

    @field_validator("workspace_id")
    @classmethod
    def reject_reserved_names(cls, value: str) -> str:
        if value in {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }:
            raise ValueError("workspace_id is a reserved Windows name")
        return value


class PublicRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    worker_kind: Literal["pi", "fake"]
    planner_kind: Literal["pi", "single"]
    worker_model: str | None
    planner_model: str | None
    worker_reasoning_effort: str | None
    planner_reasoning_effort: str | None
    worker_capacity: int
    planner_capacity: int
    worker_timeout_seconds: float
    planner_timeout_seconds: float
    command_timeout_seconds: float
    allowed_commands: list[list[str]]
    available_shells: list[str]
    git_permissions: list[GitPermission]
    command_check_argv: list[str] | None
    semantic_required_terms: list[str]
    attempt_deadline_seconds: float | None


class WorkspaceDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: WorkspaceId
    path: str
    runtime: PublicRuntimeSettings
    status: Literal["registered", "starting", "ready", "stopping", "stopped", "failed"]
    failure_category: (
        Literal[
            "startup_timeout",
            "child_exited",
            "identity_mismatch",
            "shutdown_timeout",
            "launch_failed",
        ]
        | None
    )
