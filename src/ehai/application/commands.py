"""Immutable application Command inputs for the I3 vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from ehai import ID, JsonValue, json_dumps, normalize_id


@dataclass(frozen=True, slots=True)
class CreateProject:
    """Request creation of a long-lived Project."""

    idempotency_key: str
    name: str

    def __post_init__(self) -> None:
        _require_idempotency_key(self.idempotency_key, type(self).__name__)
        object.__setattr__(self, "name", _non_empty_text(self.name, "name", type(self).__name__))

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(type(self).__name__, {"name": self.name})


@dataclass(frozen=True, slots=True)
class CreateGoal:
    """Request creation of a Goal in an existing Project."""

    idempotency_key: str
    project_id: ID
    objective: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "project_id", _normalized_id(self.project_id, "project_id", owner))
        object.__setattr__(self, "objective", _non_empty_text(self.objective, "objective", owner))

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(
            type(self).__name__,
            {"objective": self.objective, "project_id": self.project_id},
        )


@dataclass(frozen=True, slots=True)
class ProposePlan:
    """Request a draft PlanRevision and CompletionContract proposal."""

    idempotency_key: str
    goal_id: ID
    criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "goal_id", _normalized_id(self.goal_id, "goal_id", owner))
        criteria = tuple(_non_empty_text(item, "criterion", owner) for item in self.criteria)
        if not criteria:
            raise ValueError(f"{owner} criteria must not be empty")
        object.__setattr__(self, "criteria", criteria)

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(
            type(self).__name__,
            {"criteria": list(self.criteria), "goal_id": self.goal_id},
        )


@dataclass(frozen=True, slots=True)
class ApprovePlan:
    """Request approval of an exact PlanRevision and CompletionContract pair."""

    idempotency_key: str
    plan_revision_id: ID
    completion_contract_id: ID

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(
            self,
            "plan_revision_id",
            _normalized_id(self.plan_revision_id, "plan_revision_id", owner),
        )
        object.__setattr__(
            self,
            "completion_contract_id",
            _normalized_id(self.completion_contract_id, "completion_contract_id", owner),
        )

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(
            type(self).__name__,
            {
                "completion_contract_id": self.completion_contract_id,
                "plan_revision_id": self.plan_revision_id,
            },
        )


@dataclass(frozen=True, slots=True)
class StartRun:
    """Request execution of an already approved PlanRevision."""

    idempotency_key: str
    plan_revision_id: ID

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(
            self,
            "plan_revision_id",
            _normalized_id(self.plan_revision_id, "plan_revision_id", owner),
        )

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(type(self).__name__, {"plan_revision_id": self.plan_revision_id})


@dataclass(frozen=True, slots=True)
class PauseRun:
    """Request pausing a running Run."""

    idempotency_key: str
    run_id: ID

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "run_id", _normalized_id(self.run_id, "run_id", owner))

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(type(self).__name__, {"run_id": self.run_id})


@dataclass(frozen=True, slots=True)
class ResumeRun:
    """Request resuming a paused Run."""

    idempotency_key: str
    run_id: ID

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "run_id", _normalized_id(self.run_id, "run_id", owner))

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(type(self).__name__, {"run_id": self.run_id})


@dataclass(frozen=True, slots=True)
class CancelRun:
    """Request cancellation of a non-terminal Run."""

    idempotency_key: str
    run_id: ID
    reason: str | None = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "run_id", _normalized_id(self.run_id, "run_id", owner))
        if self.reason is not None:
            object.__setattr__(self, "reason", _non_empty_text(self.reason, "reason", owner))

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(type(self).__name__, {"reason": self.reason, "run_id": self.run_id})


def _require_idempotency_key(value: str, owner: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} idempotency_key must not be empty")


def _non_empty_text(value: str, field_name: str, owner: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value.strip()


def _normalized_id(value: ID, field_name: str, owner: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{owner} has invalid {field_name}: {error}") from error


def _fingerprint(command_name: str, payload: dict[str, JsonValue]) -> str:
    envelope: JsonValue = {"command": command_name, "payload": payload}
    encoded = json_dumps(envelope).encode("utf-8")
    return sha256(encoded).hexdigest()
