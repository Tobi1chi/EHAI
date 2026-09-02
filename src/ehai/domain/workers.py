"""P2 Worker routing, Session, and execution-reference value models."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ehai import ID, new_id, normalize_id, utc_now

_CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


class WorkerKind(StrEnum):
    """Worker implementations supported during P2."""

    BUILTIN = "builtin"
    CODEX_CLI = "codex_cli"
    CODEX_APP_SERVER = "codex_app_server"


class WorkerEndpointType(StrEnum):
    """How the configured Worker endpoint is reached."""

    IN_PROCESS = "in_process"
    COMMAND = "command"
    ADDRESS = "address"


class WorkerEndpointStatus(StrEnum):
    """Operator-controlled availability of a Worker endpoint."""

    ENABLED = "enabled"
    DRAINING = "draining"
    DISABLED = "disabled"


class AttemptExecutionKind(StrEnum):
    """The two execution forms to which one Attempt may bind."""

    BUILTIN_TURN = "builtin_turn"
    EXTERNAL_EXECUTION = "external_execution"


class SessionPolicy(StrEnum):
    """How a Dispatcher obtains the Agent Session for an Attempt."""

    NEW = "new"
    REUSE = "reuse"
    FORK = "fork"


class AttemptActivity(StrEnum):
    """Live observation of non-terminal work, separate from Attempt lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    STALLED = "stalled"


@dataclass(frozen=True, slots=True, order=True)
class WorkerCapability:
    """An open, provider-neutral capability name used for exact matching."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _CAPABILITY_NAME.fullmatch(self.name) is None:
            raise ValueError(
                "capabilities must use lower-case dotted, dashed, or underscored names"
            )

    def __str__(self) -> str:
        return self.name


def normalize_capabilities(capabilities: Iterable[str]) -> frozenset[str]:
    """Validate opaque capability names without teaching core code provider details."""
    return frozenset(WorkerCapability(capability).name for capability in capabilities)


def supports_capabilities(*, offered: Iterable[str], required: Iterable[str]) -> bool:
    """Return whether every required capability is explicitly offered."""
    return normalize_capabilities(required).issubset(normalize_capabilities(offered))


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    """A schedulable Worker configuration without credentials or provider state."""

    name: str
    kind: WorkerKind
    model: str
    capabilities: frozenset[WorkerCapability] = field(default_factory=frozenset)
    session_policy: SessionPolicy = SessionPolicy.NEW
    budget_ref: str | None = None
    credential_ref: str | None = None
    priority: int = 0
    worker_profile_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_profile_id", normalize_id(self.worker_profile_id))
        object.__setattr__(self, "kind", WorkerKind(self.kind))
        object.__setattr__(self, "session_policy", SessionPolicy(self.session_policy))
        object.__setattr__(self, "name", _text(self.name, "WorkerProfile name"))
        object.__setattr__(self, "model", _text(self.model, "WorkerProfile model"))
        capabilities = frozenset(self.capabilities)
        if not all(isinstance(item, WorkerCapability) for item in capabilities):
            raise ValueError("WorkerProfile capabilities must contain WorkerCapability values")
        object.__setattr__(self, "capabilities", capabilities)
        if self.budget_ref is not None:
            object.__setattr__(self, "budget_ref", _text(self.budget_ref, "budget_ref"))
        if self.credential_ref is not None:
            object.__setattr__(
                self,
                "credential_ref",
                _text(self.credential_ref, "credential_ref"),
            )
        if type(self.priority) is not int:
            raise ValueError("WorkerProfile priority must be an integer")


@dataclass(frozen=True, slots=True)
class WorkerEndpoint:
    """One configured endpoint able to host a supported Worker kind."""

    name: str
    worker_kind: WorkerKind
    endpoint_type: WorkerEndpointType
    endpoint_ref: str
    capacity: int
    status: WorkerEndpointStatus = WorkerEndpointStatus.ENABLED
    worker_endpoint_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_endpoint_id", normalize_id(self.worker_endpoint_id))
        object.__setattr__(self, "worker_kind", WorkerKind(self.worker_kind))
        object.__setattr__(self, "endpoint_type", WorkerEndpointType(self.endpoint_type))
        object.__setattr__(self, "status", WorkerEndpointStatus(self.status))
        object.__setattr__(self, "name", _text(self.name, "WorkerEndpoint name"))
        object.__setattr__(self, "endpoint_ref", _text(self.endpoint_ref, "endpoint_ref"))
        if type(self.capacity) is not int or self.capacity < 1:
            raise ValueError("WorkerEndpoint capacity must be a positive integer")


@dataclass(frozen=True, slots=True)
class AgentSessionRef:
    """A durable reference to one Worker Session scoped to a Run."""

    run_id: ID
    worker_profile_id: ID
    worker_endpoint_id: ID
    provider_session_id: str
    recoverable: bool
    agent_session_ref_id: ID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_session_ref_id", normalize_id(self.agent_session_ref_id))
        object.__setattr__(self, "run_id", normalize_id(self.run_id))
        object.__setattr__(self, "worker_profile_id", normalize_id(self.worker_profile_id))
        object.__setattr__(self, "worker_endpoint_id", normalize_id(self.worker_endpoint_id))
        object.__setattr__(
            self,
            "provider_session_id",
            _text(self.provider_session_id, "provider_session_id"),
        )
        if not isinstance(self.recoverable, bool):
            raise ValueError("AgentSessionRef recoverable must be a boolean")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class ExternalExecutionRef:
    """A provider execution, such as one Codex turn, bound to an Attempt."""

    attempt_id: ID
    agent_session_ref_id: ID
    provider_execution_id: str
    external_execution_ref_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "external_execution_ref_id",
            normalize_id(self.external_execution_ref_id),
        )
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))
        object.__setattr__(self, "agent_session_ref_id", normalize_id(self.agent_session_ref_id))
        object.__setattr__(
            self,
            "provider_execution_id",
            _text(self.provider_execution_id, "provider_execution_id"),
        )


@dataclass(frozen=True, slots=True)
class BuiltinExecutionRef:
    """A local Built-in Agent Turn bound to an Attempt and Session."""

    attempt_id: ID
    agent_session_ref_id: ID
    builtin_execution_id: ID = field(default_factory=new_id)
    builtin_execution_ref_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "builtin_execution_ref_id",
            normalize_id(self.builtin_execution_ref_id),
        )
        object.__setattr__(self, "builtin_execution_id", normalize_id(self.builtin_execution_id))
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))
        object.__setattr__(self, "agent_session_ref_id", normalize_id(self.agent_session_ref_id))


@dataclass(frozen=True, slots=True)
class ExecutionHandle:
    """Exactly one Built-in or external execution reference for an Attempt."""

    builtin: BuiltinExecutionRef | None = None
    external: ExternalExecutionRef | None = None

    def __post_init__(self) -> None:
        if (self.builtin is None) == (self.external is None):
            raise ValueError("ExecutionHandle requires exactly one execution reference")

    @property
    def kind(self) -> AttemptExecutionKind:
        if self.builtin is not None:
            return AttemptExecutionKind.BUILTIN_TURN
        return AttemptExecutionKind.EXTERNAL_EXECUTION

    @property
    def attempt_id(self) -> ID:
        reference = self.builtin if self.builtin is not None else self.external
        assert reference is not None
        return reference.attempt_id

    @property
    def agent_session_ref_id(self) -> ID:
        reference = self.builtin if self.builtin is not None else self.external
        assert reference is not None
        return reference.agent_session_ref_id

    @property
    def provider_execution_id(self) -> str:
        if self.external is not None:
            return self.external.provider_execution_id
        assert self.builtin is not None
        return str(self.builtin.builtin_execution_id)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
