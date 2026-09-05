"""Immutable event envelopes for facts emitted by the execution plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ehai._ids import ID, new_id, normalize_id
from ehai._serialization import (
    JsonValue,
    format_utc_datetime,
    json_dumps,
    json_loads,
    parse_utc_datetime,
    utc_now,
)

EVENT_SCHEMA_VERSION = 1
"""The only event-envelope schema version supported during P1."""


class EventType(StrEnum):
    """Facts required to trace the P1 execution loop.

    Values deliberately use past-tense domain language. Adding a fact is an
    explicit schema change rather than accepting arbitrary event-name strings.
    """

    PROJECT_CREATED = "ProjectCreated"
    GOAL_CREATED = "GoalCreated"
    COMPLETION_CONTRACT_CONFIRMED = "CompletionContractConfirmed"
    PLAN_REVISION_PROPOSED = "PlanRevisionProposed"
    PLAN_REVISION_APPROVED = "PlanRevisionApproved"
    PLANNING_TURN_STARTED = "PlanningTurnStarted"
    PLANNING_TURN_COMPLETED = "PlanningTurnCompleted"
    PLANNING_TURN_FAILED = "PlanningTurnFailed"
    PLAN_NODE_READIED = "PlanNodeReadied"
    PLAN_NODE_STARTED = "PlanNodeStarted"
    PLAN_NODE_CANDIDATE_SUBMITTED = "PlanNodeCandidateSubmitted"
    PLAN_NODE_COMPLETED = "PlanNodeCompleted"
    PLAN_NODE_FAILED = "PlanNodeFailed"
    PLAN_NODE_PRUNED = "PlanNodePruned"
    BRANCH_SELECTED = "BranchSelected"
    BRANCH_PRUNED = "BranchPruned"
    RUN_STARTED = "RunStarted"
    RUN_PAUSED = "RunPaused"
    RUN_RESUMED = "RunResumed"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"
    RUN_CANCELLED = "RunCancelled"
    ATTEMPT_STARTED = "AttemptStarted"
    ATTEMPT_QUEUED = "AttemptQueued"
    ATTEMPT_DISPATCHED = "AttemptDispatched"
    ATTEMPT_BOUND = "AttemptBound"
    ATTEMPT_HEARTBEAT_OBSERVED = "AttemptHeartbeatObserved"
    ATTEMPT_WAITING = "AttemptWaiting"
    ATTEMPT_DEADLINE_EXTENDED = "AttemptDeadlineExtended"
    ATTEMPT_RETRY_SCHEDULED = "AttemptRetryScheduled"
    ATTEMPT_SUCCEEDED = "AttemptSucceeded"
    ATTEMPT_FAILED = "AttemptFailed"
    ATTEMPT_TIMED_OUT = "AttemptTimedOut"
    ATTEMPT_CANCELLED = "AttemptCancelled"
    ATTEMPT_INTERRUPTED = "AttemptInterrupted"
    DISPATCH_WORK_CLAIMED = "DispatchWorkClaimed"
    ENDPOINT_HEALTH_CHANGED = "EndpointHealthChanged"
    PROVIDER_USAGE_RECORDED = "ProviderUsageRecorded"
    ARTIFACT_CREATED = "ArtifactCreated"
    CHECK_STARTED = "CheckStarted"
    CHECK_PASSED = "CheckPassed"
    CHECK_FAILED = "CheckFailed"
    CHECK_INTERRUPTED = "CheckInterrupted"
    GATE_PASSED = "GatePassed"
    GATE_FAILED = "GateFailed"
    CHECKPOINT_CREATED = "CheckpointCreated"
    CHECKPOINT_RESTORED = "CheckpointRestored"
    WORKSPACE_PRESERVED = "WorkspacePreserved"


@dataclass(frozen=True, slots=True, init=False)
class Event:
    """An immutable, versioned description of a fact that already happened.

    ``run_id`` is explicitly nullable because Project, Goal, and planning facts
    happen before a Run exists. The payload is stored as canonical JSON text so
    callers cannot mutate the envelope through a retained or returned container.
    """

    id: ID
    type: EventType
    occurred_at: datetime
    schema_version: int
    run_id: ID | None
    correlation_id: ID
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        type: EventType | str,
        correlation_id: str,
        payload: Mapping[str, JsonValue],
        run_id: str | None = None,
        id: str | None = None,
        occurred_at: datetime | None = None,
        schema_version: int = EVENT_SCHEMA_VERSION,
    ) -> None:
        event_type = _parse_event_type(type)
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != EVENT_SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported event schema version: {schema_version!r}")

        timestamp = utc_now() if occurred_at is None else occurred_at
        if not isinstance(timestamp, datetime):
            raise ValueError("occurred_at must be a datetime")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")

        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be a JSON object")
        payload_json = json_dumps(dict(payload))

        event_id = new_id() if id is None else _parse_id(id, field_name="id")
        normalized_run_id = None if run_id is None else _parse_id(run_id, field_name="run_id")

        object.__setattr__(self, "id", event_id)
        object.__setattr__(self, "type", event_type)
        object.__setattr__(self, "occurred_at", timestamp.astimezone(UTC))
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "run_id", normalized_run_id)
        object.__setattr__(
            self,
            "correlation_id",
            _parse_id(correlation_id, field_name="correlation_id"),
        )
        object.__setattr__(self, "_payload_json", payload_json)

    @property
    def payload(self) -> dict[str, JsonValue]:
        """Return an isolated JSON-compatible copy of the event payload."""
        payload = json_loads(self._payload_json)
        if not isinstance(payload, dict):  # pragma: no cover - guarded by construction
            raise RuntimeError("stored event payload is not a JSON object")
        return payload

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the complete envelope as a language-neutral JSON object."""
        return {
            "id": self.id,
            "type": self.type.value,
            "occurred_at": format_utc_datetime(self.occurred_at),
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Serialize the envelope as deterministic, compact JSON text."""
        return json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> Event:
        """Validate and reconstruct an Event from a decoded JSON object."""
        if not all(isinstance(key, str) for key in document):
            raise ValueError("event envelope keys must be strings")
        expected_keys = {
            "id",
            "type",
            "occurred_at",
            "schema_version",
            "run_id",
            "correlation_id",
            "payload",
        }
        actual_keys = set(document)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ValueError(f"invalid event envelope keys; missing={missing}, extra={extra}")

        event_id = document["id"]
        event_type = document["type"]
        occurred_at = document["occurred_at"]
        schema_version = document["schema_version"]
        run_id = document["run_id"]
        correlation_id = document["correlation_id"]
        payload = document["payload"]

        if not isinstance(event_id, str):
            raise ValueError("event id must be a string")
        if not isinstance(event_type, str):
            raise ValueError("event type must be a string")
        if not isinstance(occurred_at, str):
            raise ValueError("event occurred_at must be a string")
        if type(schema_version) is not int:
            raise ValueError("event schema_version must be an integer")
        if run_id is not None and not isinstance(run_id, str):
            raise ValueError("event run_id must be a string or null")
        if not isinstance(correlation_id, str):
            raise ValueError("event correlation_id must be a string")
        if not isinstance(payload, dict):
            raise ValueError("event payload must be a JSON object")

        return cls(
            id=event_id,
            type=event_type,
            occurred_at=parse_utc_datetime(occurred_at),
            schema_version=schema_version,
            run_id=run_id,
            correlation_id=correlation_id,
            payload=payload,
        )

    @classmethod
    def from_json(cls, document: str) -> Event:
        """Validate and reconstruct an Event from strict JSON text."""
        decoded = json_loads(document)
        if not isinstance(decoded, dict):
            raise ValueError("event document must contain a JSON object")
        return cls.from_dict(decoded)


def _parse_event_type(value: EventType | str) -> EventType:
    try:
        return EventType(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"unknown or non-past-tense event type: {value!r}") from error


def _parse_id(value: str, *, field_name: str) -> ID:
    if not isinstance(value, str):
        raise ValueError(f"event {field_name} must be a string")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"invalid event {field_name}: {value!r}") from error
