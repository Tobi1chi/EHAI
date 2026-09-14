"""Host-confirmed, immutable handoff facts for Worker Attempts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id, utc_now
from ehai.application.ports import StoredEvent, UnitOfWork
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.planning import (
    Branch,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanPhase,
    PlanRevision,
    PlanRevisionStatus,
)

UnitOfWorkFactory = Callable[[], UnitOfWork]

_MAX_KEY_CHARACTERS = 200
_MAX_TEXT_BYTES = 16_000
_HANDOFF_NAMESPACE_PREFIX = "ehai:attempt-handoff:"


@dataclass(frozen=True, slots=True)
class HandoffSubmission:
    """Worker-authored handoff prose awaiting host confirmation."""

    key: str
    completed: str
    context: str = ""
    remaining: str = ""
    known_issues: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _text(self.key, "key", required=True, key=True))
        object.__setattr__(
            self,
            "completed",
            _text(self.completed, "completed", required=True),
        )
        for field_name in ("context", "remaining", "known_issues"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name, required=False),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, JsonValue]) -> HandoffSubmission:
        """Parse exactly one structured handoff submission from JSON data."""
        if not isinstance(value, Mapping):
            raise ValueError("handoff submission must be a JSON object")
        expected = {"key", "completed", "context", "remaining", "known_issues"}
        actual = set(value)
        if not all(isinstance(item, str) for item in actual):
            raise ValueError("handoff submission field names must be strings")
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(f"invalid handoff submission fields; missing={missing}, extra={extra}")
        fields: dict[str, str] = {}
        for field_name in expected:
            field_value = value[field_name]
            if not isinstance(field_value, str):
                raise ValueError(f"handoff submission field {field_name!r} must be text")
            fields[field_name] = field_value
        return cls(**fields)

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a detached, canonical JSON-compatible handoff document."""
        return {
            "key": self.key,
            "completed": self.completed,
            "context": self.context,
            "remaining": self.remaining,
            "known_issues": self.known_issues,
        }

    def fingerprint(self) -> str:
        """Hash the canonical structured submission, excluding host metadata."""
        return sha256(json_dumps(self.to_dict()).encode("utf-8")).hexdigest()


def handoff_id_for(attempt_id: ID, key: str) -> ID:
    """Return the deterministic handoff identity for one Attempt/key pair."""
    normalized_attempt_id = _normalize_id(attempt_id, "attempt_id")
    normalized_key = _text(key, "key", required=True, key=True)
    return ID(
        str(
            uuid5(
                NAMESPACE_URL,
                f"{_HANDOFF_NAMESPACE_PREFIX}{normalized_attempt_id}:{normalized_key}",
            )
        )
    )


@dataclass(frozen=True, slots=True)
class _AttemptContext:
    attempt_id: ID
    run_id: ID
    plan_revision_id: ID
    plan_revision_version: int
    plan_node_id: ID
    node: PlanNode
    phase_id: ID | None
    branch_id: ID | None
    agent_session_ref_id: ID
    phase_session_id: ID | None


class AttemptHandoffs:
    """Persist host-confirmed handoffs without mutating execution state."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def get(self, handoff_id: ID) -> dict[str, JsonValue] | None:
        """Return one confirmed handoff by its deterministic durable identity."""
        normalized_id = _normalize_id(handoff_id, "handoff_id")
        with self._uow_factory() as uow:
            return self._find_by_id(uow, normalized_id)

    def confirm(
        self,
        attempt_id: ID,
        submission: HandoffSubmission,
        code_snapshot: Mapping[str, JsonValue],
        dependency_commits: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        """Confirm and append one immutable handoff fact for an active Attempt."""
        normalized_attempt_id = _normalize_id(attempt_id, "attempt_id")
        if not isinstance(submission, HandoffSubmission):
            raise ValueError("submission must be a HandoffSubmission")
        snapshot = _snapshot_document(code_snapshot, normalized_attempt_id)
        dependencies = _dependency_commits(dependency_commits)
        handoff_id = handoff_id_for(normalized_attempt_id, submission.key)

        with self._uow_factory() as uow:
            existing = self._find_by_id(uow, handoff_id)
            if existing is not None:
                _ensure_same_handoff(
                    existing,
                    attempt_id=normalized_attempt_id,
                    submission=submission,
                    code_snapshot=snapshot,
                    dependency_commits=dependencies,
                )
                return existing

            context = self._active_context(uow, normalized_attempt_id)
            if context is None:  # pragma: no cover - active context always has a Node
                raise ValueError(f"Attempt {normalized_attempt_id} has no execution context")
            if snapshot.get("run_id") != context.run_id:
                raise ValueError("code_snapshot.run_id does not match the Run")
            payload: dict[str, JsonValue] = {
                "handoff_id": handoff_id,
                "run_id": context.run_id,
                "plan_revision_id": context.plan_revision_id,
                "plan_revision_version": context.plan_revision_version,
                "plan_node_id": context.plan_node_id,
                "attempt_id": context.attempt_id,
                "phase_id": context.phase_id,
                "branch_id": context.branch_id,
                "agent_session_ref_id": context.agent_session_ref_id,
                "phase_session_id": context.phase_session_id,
                "submission": submission.to_dict(),
                "submission_fingerprint": submission.fingerprint(),
                "code_snapshot": snapshot,
                "dependency_commits": list(dependencies),
            }
            stored = uow.events.append(
                Event(
                    type=EventType.ATTEMPT_HANDOFF_CONFIRMED,
                    run_id=context.run_id,
                    correlation_id=handoff_id,
                    payload=payload,
                    occurred_at=utc_now(),
                )
            )
            uow.commit()
            return _handoff_document(stored)

    def list_for_run(self, run_id: ID) -> tuple[dict[str, JsonValue], ...]:
        """Return confirmed handoffs for a Run in durable Event offset order."""
        normalized_run_id = _normalize_id(run_id, "run_id")
        with self._uow_factory() as uow:
            documents: list[dict[str, JsonValue]] = []
            seen: dict[ID, dict[str, JsonValue]] = {}
            for stored in uow.events.list_events():
                event = stored.event
                if (
                    event.type is not EventType.ATTEMPT_HANDOFF_CONFIRMED
                    or event.run_id != normalized_run_id
                ):
                    continue
                document = _handoff_document(stored)
                identity = _document_id(document, "handoff_id")
                previous = seen.get(identity)
                if previous is not None:
                    if previous != document:
                        raise ValueError(f"Handoff {identity} has conflicting persisted facts")
                    continue
                seen[identity] = document
                documents.append(document)
            return tuple(documents)

    @staticmethod
    def _active_context(uow: UnitOfWork, attempt_id: ID) -> _AttemptContext | None:
        attempt = uow.states.get_attempt(attempt_id)
        if attempt is None:
            raise ValueError(f"Attempt {attempt_id} is not persisted")
        if attempt.status is not AttemptStatus.RUNNING:
            raise ValueError(
                f"Handoff requires a running Attempt; Attempt {attempt_id} is "
                f"{attempt.status.value}"
            )
        run = uow.states.get_run(attempt.run_id)
        if run is None:
            raise ValueError(f"Run {attempt.run_id} is not persisted")
        if run.status is not RunStatus.RUNNING:
            raise ValueError(
                f"Handoff requires a running Run; Run {run.run_id} is {run.status.value}"
            )
        plan = uow.states.get_execution_plan(run.run_id)
        if plan is None:
            raise ValueError(f"PlanRevision {run.plan_revision_id} is not persisted")
        if plan.status is not PlanRevisionStatus.APPROVED:
            raise ValueError(
                f"Handoff requires an approved PlanRevision; PlanRevision "
                f"{plan.plan_revision_id} is {plan.status.value}"
            )
        node = _node(plan, attempt.plan_node_id)
        if node.status is not PlanNodeStatus.RUNNING:
            raise ValueError(
                f"Handoff requires a running PlanNode; PlanNode {node.plan_node_id} is "
                f"{node.status.value}"
            )
        if node.kind in {PlanNodeKind.REVIEWER, PlanNodeKind.EVALUATOR}:
            raise ValueError(f"Handoff is not allowed for {node.kind.value} PlanNode")
        session_ref_id = attempt.agent_session_ref_id
        if session_ref_id is None:
            raise ValueError(f"Attempt {attempt_id} has no bound Agent Session")
        session = uow.states.get_agent_session_ref(session_ref_id)
        if session is None:
            raise ValueError(f"Agent Session {session_ref_id} is not persisted")
        if session.run_id != run.run_id:
            raise ValueError(f"Agent Session {session_ref_id} belongs to another Run")
        if (
            attempt.worker_profile_id is not None
            and session.worker_profile_id != attempt.worker_profile_id
        ):
            raise ValueError(f"Agent Session {session_ref_id} does not match the Attempt profile")
        if (
            attempt.worker_endpoint_id is not None
            and session.worker_endpoint_id != attempt.worker_endpoint_id
        ):
            raise ValueError(f"Agent Session {session_ref_id} does not match the Attempt endpoint")
        if (
            attempt.execution_handle is not None
            and attempt.execution_handle.agent_session_ref_id != session_ref_id
        ):
            raise ValueError(f"Agent Session {session_ref_id} does not match the execution handle")
        phase = _phase_for_node(plan, node.plan_node_id)
        branch_id = _branch_for_node(plan.branches, node.plan_node_id)
        phase_session_id = _phase_session_for_attempt(uow, run.run_id, attempt_id)
        return _AttemptContext(
            attempt_id=attempt.attempt_id,
            run_id=run.run_id,
            plan_revision_id=plan.plan_revision_id,
            plan_revision_version=plan.version,
            plan_node_id=node.plan_node_id,
            node=node,
            phase_id=None if phase is None else phase.phase_id,
            branch_id=branch_id,
            agent_session_ref_id=session_ref_id,
            phase_session_id=phase_session_id,
        )

    @staticmethod
    def _find_by_id(uow: UnitOfWork, handoff_id: ID) -> dict[str, JsonValue] | None:
        found: dict[str, JsonValue] | None = None
        for stored in uow.events.list_events():
            event = stored.event
            if event.type is not EventType.ATTEMPT_HANDOFF_CONFIRMED:
                continue
            payload = event.payload
            event_handoff_id = _payload_id(payload, "handoff_id", event.type)
            if event_handoff_id != handoff_id:
                continue
            document = _handoff_document(stored)
            if found is not None and found != document:
                raise ValueError(f"Handoff {handoff_id} has conflicting persisted facts")
            found = document
        return found


def _text(value: str, field_name: str, *, required: bool, key: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"handoff {field_name} must be text")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"handoff {field_name} must not be empty")
    if key and len(normalized) > _MAX_KEY_CHARACTERS:
        raise ValueError(f"handoff key exceeds {_MAX_KEY_CHARACTERS} characters")
    if len(normalized.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"handoff {field_name} exceeds {_MAX_TEXT_BYTES} UTF-8 bytes")
    return normalized


def _normalize_id(value: str, field_name: str) -> ID:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UUID string")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"invalid {field_name}: {error}") from error


def _snapshot_document(value: Mapping[str, JsonValue], attempt_id: ID) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError("code_snapshot must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError("code_snapshot field names must be strings")
    document = dict(value)
    run_id = document.get("run_id")
    snapshot_attempt_id = document.get("attempt_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("code_snapshot.run_id must be non-blank")
    if not isinstance(snapshot_attempt_id, str) or not snapshot_attempt_id.strip():
        raise ValueError("code_snapshot.attempt_id must be non-blank")
    normalized_snapshot_attempt_id = _normalize_id(snapshot_attempt_id, "code_snapshot.attempt_id")
    if normalized_snapshot_attempt_id != attempt_id:
        raise ValueError("code_snapshot.attempt_id does not match the Attempt")
    # The Run identity cannot be checked until the Attempt is loaded.  Keep the
    # normalized UUID here so a later persisted fact has one canonical form.
    document["run_id"] = _normalize_id(run_id, "code_snapshot.run_id")
    document["attempt_id"] = normalized_snapshot_attempt_id
    try:
        encoded = json_dumps(document)
        decoded = json_loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("code_snapshot must contain only JSON values") from error
    if not isinstance(decoded, dict):  # pragma: no cover - guarded by json_dumps
        raise ValueError("code_snapshot must be a JSON object")
    return decoded


def _dependency_commits(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("dependency_commits must be a tuple of commit strings")
    normalized: list[str] = []
    for index, commit in enumerate(value):
        if not isinstance(commit, str) or not commit.strip():
            raise ValueError(f"dependency_commits[{index}] must be non-blank")
        normalized.append(commit.strip())
    return tuple(normalized)


def _node(plan: PlanRevision, plan_node_id: ID) -> PlanNode:
    node = next((item for item in plan.nodes if item.plan_node_id == plan_node_id), None)
    if node is None:
        raise ValueError(f"Attempt references missing PlanNode {plan_node_id}")
    return node


def _phase_for_node(plan: PlanRevision, plan_node_id: ID) -> PlanPhase | None:
    return next((phase for phase in plan.phases if plan_node_id in phase.node_ids), None)


def _branch_for_node(branches: tuple[Branch, ...], plan_node_id: ID) -> ID | None:
    branch = next((item for item in branches if plan_node_id in item.node_ids), None)
    return None if branch is None else branch.branch_id


def _phase_session_for_attempt(uow: UnitOfWork, run_id: ID, attempt_id: ID) -> ID | None:
    for stored in uow.events.list_events():
        event = stored.event
        if event.type is not EventType.PHASE_SESSION_JOINED or event.run_id != run_id:
            continue
        payload = event.payload
        if _payload_id(payload, "attempt_id", event.type) != attempt_id:
            continue
        return _payload_id(payload, "phase_session_id", event.type)
    return None


def _handoff_document(stored: StoredEvent) -> dict[str, JsonValue]:
    event = stored.event
    if event.type is not EventType.ATTEMPT_HANDOFF_CONFIRMED:
        raise ValueError("stored event is not an AttemptHandoffConfirmed fact")
    payload = event.payload
    run_id = _payload_id(payload, "run_id", event.type)
    if event.run_id != run_id:
        raise ValueError(f"Handoff fact {event.id} has a mismatched Run")
    submission_value = payload.get("submission")
    if not isinstance(submission_value, dict):
        raise ValueError(f"Handoff fact {event.id} has an invalid submission")
    submission = HandoffSubmission.from_mapping(cast(Mapping[str, JsonValue], submission_value))
    fingerprint = _payload_text(payload, "submission_fingerprint", event.type)
    if fingerprint != submission.fingerprint():
        raise ValueError(f"Handoff fact {event.id} has a mismatched submission fingerprint")
    snapshot_value = payload.get("code_snapshot")
    if not isinstance(snapshot_value, dict):
        raise ValueError(f"Handoff fact {event.id} has an invalid code snapshot")
    snapshot = _snapshot_document(
        cast(Mapping[str, JsonValue], snapshot_value),
        _payload_id(payload, "attempt_id", event.type),
    )
    if snapshot.get("run_id") != run_id:
        raise ValueError(f"Handoff fact {event.id} has a mismatched code snapshot Run")
    dependencies = payload.get("dependency_commits")
    if not isinstance(dependencies, list):
        raise ValueError(f"Handoff fact {event.id} has invalid dependency commits")
    dependency_values: list[str] = []
    for index, value in enumerate(dependencies):
        if not isinstance(value, str):
            raise ValueError(f"Handoff fact {event.id} has an invalid dependency at {index}")
        dependency_values.append(value)
    normalized_dependencies = _dependency_commits(tuple(dependency_values))
    handoff_id = _payload_id(payload, "handoff_id", event.type)
    attempt_id = _payload_id(payload, "attempt_id", event.type)
    if handoff_id != handoff_id_for(attempt_id, submission.key):
        raise ValueError(f"Handoff fact {event.id} has an invalid handoff identity")
    return {
        "offset": stored.offset,
        "event_id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        "handoff_id": handoff_id,
        "run_id": run_id,
        "plan_revision_id": _payload_id(payload, "plan_revision_id", event.type),
        "plan_revision_version": _payload_int(payload, "plan_revision_version", event.type),
        "plan_node_id": _payload_id(payload, "plan_node_id", event.type),
        "attempt_id": attempt_id,
        "phase_id": _optional_payload_id(payload, "phase_id", event.type),
        "branch_id": _optional_payload_id(payload, "branch_id", event.type),
        "agent_session_ref_id": _payload_id(payload, "agent_session_ref_id", event.type),
        "phase_session_id": _optional_payload_id(payload, "phase_session_id", event.type),
        "submission": submission.to_dict(),
        "submission_fingerprint": fingerprint,
        "code_snapshot": snapshot,
        "dependency_commits": list(normalized_dependencies),
        "key": submission.key,
        "completed": submission.completed,
        "context": submission.context,
        "remaining": submission.remaining,
        "known_issues": submission.known_issues,
    }


def _ensure_same_handoff(
    existing: Mapping[str, JsonValue],
    *,
    attempt_id: ID,
    submission: HandoffSubmission,
    code_snapshot: Mapping[str, JsonValue],
    dependency_commits: tuple[str, ...],
) -> None:
    if existing.get("attempt_id") != attempt_id:
        raise ValueError("handoff identity belongs to another Attempt")
    if existing.get("submission") != submission.to_dict():
        raise ValueError("handoff key was already confirmed with different content")
    if existing.get("code_snapshot") != dict(code_snapshot):
        raise ValueError("handoff was already confirmed with a different code snapshot")
    if existing.get("dependency_commits") != list(dependency_commits):
        raise ValueError("handoff was already confirmed with a different dependency baseline")


def _document_id(document: Mapping[str, JsonValue], key: str) -> ID:
    value = document.get(key)
    if not isinstance(value, str):
        raise ValueError(f"handoff document field {key!r} must be an ID")
    return _normalize_id(value, key)


def _payload_id(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> ID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{event_type.value} payload field {key!r} must be an ID")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{event_type.value} payload field {key!r} is not a valid ID") from error


def _optional_payload_id(
    payload: Mapping[str, JsonValue], key: str, event_type: EventType
) -> ID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{event_type.value} payload field {key!r} must be an ID or null")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{event_type.value} payload field {key!r} is not a valid ID") from error


def _payload_int(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise ValueError(f"{event_type.value} payload field {key!r} must be an integer")
    return value


def _payload_text(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{event_type.value} payload field {key!r} must be non-blank text")
    return value


__all__ = ["AttemptHandoffs", "HandoffSubmission", "handoff_id_for"]
