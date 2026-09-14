"""Immutable application Command inputs for the I3 vertical slice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.sanitization import redact_sensitive_text

_REQUEST_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_HUMAN_CHECK_TEXT_LENGTH = 8_000


@dataclass(frozen=True, slots=True)
class ApplyProcess:
    """Apply the exact candidate covered by a retained independent review."""

    idempotency_key: str
    review_id: ID

    def __post_init__(self) -> None:
        _require_idempotency_key(self.idempotency_key, type(self).__name__)
        object.__setattr__(self, "review_id", normalize_id(self.review_id))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(type(self).__name__, {"review_id": self.review_id})


@dataclass(frozen=True, slots=True)
class ReviewProcess:
    """Request an independent review of a generated process draft."""

    idempotency_key: str
    draft_id: ID

    def __post_init__(self) -> None:
        _require_idempotency_key(self.idempotency_key, type(self).__name__)
        object.__setattr__(self, "draft_id", normalize_id(self.draft_id))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(type(self).__name__, {"draft_id": self.draft_id})


@dataclass(frozen=True, slots=True)
class ProposeProcess:
    """Generate a retained process draft, without reviewing or applying it."""

    idempotency_key: str
    run_id: ID
    reason: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "run_id", _normalized_id(self.run_id, "run_id", owner))
        reason = redact_sensitive_text(_non_empty_text(self.reason, "reason", owner))
        if len(reason.encode("utf-8")) > 16_000:
            raise ValueError("Process adjustment reason exceeds 16000 UTF-8 bytes")
        object.__setattr__(self, "reason", reason)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(type(self).__name__, {"run_id": self.run_id, "reason": self.reason})


@dataclass(frozen=True, slots=True)
class ReplyIntervention:
    """Confirm that a version-bound blocker is resolved within existing approval."""

    idempotency_key: str
    intervention_id: ID
    request_token: str
    actor: str
    message: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "intervention_id", normalize_id(self.intervention_id))
        if not isinstance(self.request_token, str) or not _REQUEST_TOKEN_PATTERN.fullmatch(
            self.request_token
        ):
            raise ValueError("request_token must be a SHA-256 digest")
        object.__setattr__(self, "request_token", self.request_token.lower())
        for name in ("actor", "message"):
            value = _non_empty_text(getattr(self, name), name, owner)
            if len(value) > 8000:
                raise ValueError(f"{name} exceeds 8000 characters")
            object.__setattr__(self, name, redact_sensitive_text(value))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            type(self).__name__,
            {
                "intervention_id": self.intervention_id,
                "request_token": self.request_token,
                "actor": self.actor,
                "message": self.message,
            },
        )


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
class DiscussPlan:
    """Request a planning discussion; an empty criteria tuple means unspecified."""

    idempotency_key: str
    goal_id: ID
    message: str
    criteria: tuple[str, ...] = ()
    conversation_id: ID | None = None
    source_run_id: ID | None = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(self, "goal_id", normalize_id(self.goal_id))
        message = _non_empty_text(self.message, "message", owner)
        if len(message) > 8_000:
            raise ValueError("Planning message exceeds 8000 characters")
        object.__setattr__(self, "message", redact_sensitive_text(message))
        object.__setattr__(
            self,
            "criteria",
            tuple(_non_empty_text(item, "criterion", owner) for item in self.criteria),
        )
        if self.conversation_id is not None:
            object.__setattr__(self, "conversation_id", normalize_id(self.conversation_id))
        if self.source_run_id is not None:
            object.__setattr__(self, "source_run_id", normalize_id(self.source_run_id))

    @property
    def fingerprint(self) -> str:
        payload: dict[str, JsonValue] = {
            "goal_id": self.goal_id,
            "message": self.message,
            "criteria": list(self.criteria),
            "conversation_id": self.conversation_id,
        }
        if self.source_run_id is not None:
            payload["source_run_id"] = self.source_run_id
        return _fingerprint(type(self).__name__, payload)


@dataclass(frozen=True, slots=True)
class ReplanPlan:
    """Request a new draft PlanRevision after an approved base revision."""

    idempotency_key: str
    base_plan_revision_id: ID
    criteria: tuple[str, ...]
    source_run_id: ID | None = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(
            self,
            "base_plan_revision_id",
            _normalized_id(self.base_plan_revision_id, "base_plan_revision_id", owner),
        )
        criteria = tuple(_non_empty_text(item, "criterion", owner) for item in self.criteria)
        if not criteria:
            raise ValueError(f"{owner} criteria must not be empty")
        object.__setattr__(self, "criteria", criteria)
        if self.source_run_id is not None:
            object.__setattr__(
                self,
                "source_run_id",
                _normalized_id(self.source_run_id, "source_run_id", owner),
            )

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(
            type(self).__name__,
            {
                "base_plan_revision_id": self.base_plan_revision_id,
                "criteria": list(self.criteria),
                "source_run_id": self.source_run_id,
            },
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
    authorized_execution_config_json: str | None = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(
            self,
            "plan_revision_id",
            _normalized_id(self.plan_revision_id, "plan_revision_id", owner),
        )
        if self.authorized_execution_config_json is not None:
            if not isinstance(self.authorized_execution_config_json, str):
                raise ValueError(
                    f"{owner} authorized_execution_config_json must be a JSON object string"
                )
            document = json_loads(self.authorized_execution_config_json)
            if not isinstance(document, dict):
                raise ValueError(
                    f"{owner} authorized_execution_config_json must contain a JSON object"
                )
            object.__setattr__(
                self,
                "authorized_execution_config_json",
                json_dumps(document),
            )

    @property
    def authorized_execution_config(self) -> dict[str, JsonValue] | None:
        """Return a detached normalized authorization snapshot, when supplied."""
        if self.authorized_execution_config_json is None:
            return None
        document = json_loads(self.authorized_execution_config_json)
        if not isinstance(document, dict):  # pragma: no cover - guarded by construction
            raise RuntimeError("stored execution authorization is not a JSON object")
        return document

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        if self.authorized_execution_config_json is None:
            # Preserve the historical StartRun fingerprint for callers that do
            # not carry an explicit execution authorization.
            return _fingerprint(type(self).__name__, {"plan_revision_id": self.plan_revision_id})
        return _fingerprint(
            type(self).__name__,
            {
                "authorized_execution_config": self.authorized_execution_config,
                "plan_revision_id": self.plan_revision_id,
            },
        )


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


@dataclass(frozen=True, slots=True)
class DecideHumanCheck:
    """Request an explicit human verdict for one open CheckRun."""

    idempotency_key: str
    check_run_id: ID
    request_token: str
    passed: bool
    actor: str
    comment: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_idempotency_key(self.idempotency_key, owner)
        object.__setattr__(
            self, "check_run_id", _normalized_id(self.check_run_id, "check_run_id", owner)
        )
        if (
            not isinstance(self.request_token, str)
            or _REQUEST_TOKEN_PATTERN.fullmatch(self.request_token) is None
        ):
            raise ValueError(f"{owner} request_token must be 64 hexadecimal characters")
        object.__setattr__(self, "request_token", self.request_token.lower())
        if type(self.passed) is not bool:
            raise ValueError(f"{owner} passed must be a boolean")
        actor = _bounded_redacted_text(self.actor, "actor", owner)
        comment = _bounded_redacted_text(self.comment, "comment", owner)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "comment", comment)

    @property
    def fingerprint(self) -> str:
        """Return a deterministic fingerprint excluding the idempotency key."""
        return _fingerprint(
            type(self).__name__,
            {
                "actor": self.actor,
                "check_run_id": self.check_run_id,
                "comment": self.comment,
                "passed": self.passed,
                "request_token": self.request_token,
            },
        )


def _require_idempotency_key(value: str, owner: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} idempotency_key must not be empty")


def _non_empty_text(value: str, field_name: str, owner: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value.strip()


def _bounded_redacted_text(value: str, field_name: str, owner: str) -> str:
    text = _non_empty_text(value, field_name, owner)
    if len(text) > _MAX_HUMAN_CHECK_TEXT_LENGTH:
        raise ValueError(f"{owner} {field_name} exceeds {_MAX_HUMAN_CHECK_TEXT_LENGTH} characters")
    return redact_sensitive_text(text)


def _normalized_id(value: ID, field_name: str, owner: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{owner} has invalid {field_name}: {error}") from error


def _fingerprint(command_name: str, payload: dict[str, JsonValue]) -> str:
    envelope: JsonValue = {"command": command_name, "payload": payload}
    encoded = json_dumps(envelope).encode("utf-8")
    return sha256(encoded).hexdigest()
