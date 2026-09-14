"""Durable, host-owned discussion for members of one approved Plan Phase.

The public Event Log is deliberately used as the first Phase Session journal.
This keeps the implementation inside the existing Unit of Work boundary while
leaving physical Agent Sessions, Workspaces, Attempts, and domain transitions
under their existing owners.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ehai import ID, JsonValue, new_id, normalize_id, utc_now
from ehai.application.ports import StoredEvent, UnitOfWork
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    Branch,
    PlanNode,
    PlanNodeStatus,
    PlanPhase,
    PlanRevisionStatus,
)

UnitOfWorkFactory = Callable[[], UnitOfWork]

_MAX_KEY_CHARACTERS = 200
_MAX_CONTENT_BYTES = 16_000
_MAX_PAGE_SIZE = 32


@dataclass(frozen=True, slots=True)
class _PhaseIdentity:
    """The immutable identity reconstructed from a PhaseSessionOpened fact."""

    phase_session_id: ID
    run_id: ID
    phase_id: ID
    plan_revision_id: ID
    plan_revision_version: int
    opened_offset: int


@dataclass(frozen=True, slots=True)
class _Member:
    """One Attempt's durable membership and physical-session binding."""

    attempt_id: ID
    plan_node_id: ID
    branch_id: ID | None
    agent_session_ref_id: ID


@dataclass(frozen=True, slots=True)
class _AttemptContext:
    """Validated current state needed by every Phase Session operation."""

    attempt: Attempt
    run: Run
    plan_revision_id: ID
    plan_revision_version: int
    node: PlanNode
    phase: PlanPhase
    branch_id: ID | None
    agent_session_ref_id: ID


class PhaseSessions:
    """Expose a small durable Phase Session API without owning domain state.

    A caller supplies only an existing Attempt.  Run, approved PlanRevision,
    Phase, member scope, branch, and physical Agent Session identity are all
    derived from the transaction's current state.  The caller therefore cannot
    select a different Run or Phase by passing a forged context document.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def join(self, attempt_id: ID) -> dict[str, JsonValue] | None:
        """Create or replay membership for an active Attempt's explicit Phase.

        Plans without explicit Phases intentionally return ``None`` so the
        caller can omit Phase tools for the legacy single-node path.
        """
        normalized_attempt_id = _normalize_boundary_id(attempt_id, "attempt_id")
        with self._uow_factory() as uow:
            context = self._attempt_context(uow, normalized_attempt_id, require_active=True)
            if context is None:
                return None
            identity = self._find_or_open(uow, context)
            members = self._members(uow, identity)
            existing = members.get(context.attempt.attempt_id)
            current = self._member_from_context(context)
            if existing is not None:
                if existing != current:
                    raise ValueError(
                        f"Attempt {context.attempt.attempt_id} has conflicting Phase membership"
                    )
                uow.commit()
                return _membership_document(identity, context)

            uow.events.append(
                Event(
                    type=EventType.PHASE_SESSION_JOINED,
                    run_id=context.run.run_id,
                    correlation_id=context.attempt.attempt_id,
                    payload={
                        **_identity_payload(identity),
                        "attempt_id": context.attempt.attempt_id,
                        "plan_node_id": context.node.plan_node_id,
                        "branch_id": context.branch_id,
                        "agent_session_ref_id": context.agent_session_ref_id,
                    },
                    occurred_at=utc_now(),
                )
            )
            uow.commit()
            return _membership_document(identity, context)

    def read(
        self,
        attempt_id: ID,
        *,
        after_offset: int = 0,
        limit: int = 8,
    ) -> dict[str, JsonValue]:
        """Read ordered discussion entries after a durable Event offset.

        The offset is the existing Event Log offset, not a second unpersisted
        acknowledgement table.  A consumer records the returned
        ``next_offset`` in its own durable model input before asking for the
        next page; a failed input append safely causes replay rather than loss.
        """
        normalized_attempt_id = _normalize_boundary_id(attempt_id, "attempt_id")
        _validate_offset(after_offset)
        _validate_limit(limit)
        with self._uow_factory() as uow:
            context = self._attempt_context(uow, normalized_attempt_id, require_active=False)
            if context is None:
                raise ValueError(
                    f"Attempt {normalized_attempt_id} does not belong to an explicit Phase"
                )
            identity = self._find_existing(uow, context)
            if identity is None:
                raise ValueError(f"Phase Session for Phase {context.phase.phase_id} is not open")
            members = self._members(uow, identity)
            current = members.get(context.attempt.attempt_id)
            if current is None:
                raise ValueError(f"Attempt {context.attempt.attempt_id} is not a Phase member")
            if current != self._member_from_context(context):
                raise ValueError(
                    f"Attempt {context.attempt.attempt_id} has conflicting Phase membership"
                )

            published = self._published(uow, identity, members, after_offset)
            page = published[:limit]
            next_offset = after_offset if not page else page[-1].offset
            return {
                "phase_session_id": identity.phase_session_id,
                "run_id": identity.run_id,
                "phase_id": identity.phase_id,
                "plan_revision_id": identity.plan_revision_id,
                "plan_revision_version": identity.plan_revision_version,
                "entries": [_published_document(item) for item in page],
                "next_offset": next_offset,
                "has_more": len(published) > limit,
            }

    def publish(
        self,
        attempt_id: ID,
        *,
        key: str,
        content: str,
    ) -> dict[str, JsonValue]:
        """Append one discussion observation, idempotent by Attempt and key."""
        normalized_attempt_id = _normalize_boundary_id(attempt_id, "attempt_id")
        normalized_key = _validate_key(key)
        normalized_content = _validate_content(content)
        with self._uow_factory() as uow:
            context = self._attempt_context(uow, normalized_attempt_id, require_active=True)
            if context is None:
                raise ValueError(
                    f"Attempt {normalized_attempt_id} does not belong to an explicit Phase"
                )
            identity = self._find_existing(uow, context)
            if identity is None:
                raise ValueError(f"Phase Session for Phase {context.phase.phase_id} is not open")
            members = self._members(uow, identity)
            current = members.get(context.attempt.attempt_id)
            if current is None:
                raise ValueError(f"Attempt {context.attempt.attempt_id} is not a Phase member")
            if current != self._member_from_context(context):
                raise ValueError(
                    f"Attempt {context.attempt.attempt_id} has conflicting Phase membership"
                )

            existing = self._published_for_key(
                uow,
                identity,
                context.attempt.attempt_id,
                normalized_key,
            )
            if existing is not None:
                existing_content = _required_payload_text(
                    existing.event.payload,
                    "content",
                    EventType.PHASE_CONTEXT_PUBLISHED,
                )
                if existing_content != normalized_content:
                    raise ValueError(
                        f"Phase context key {normalized_key!r} was already published with "
                        "different content"
                    )
                return _published_document(existing)

            stored = uow.events.append(
                Event(
                    type=EventType.PHASE_CONTEXT_PUBLISHED,
                    run_id=context.run.run_id,
                    correlation_id=context.attempt.attempt_id,
                    payload={
                        **_identity_payload(identity),
                        "attempt_id": context.attempt.attempt_id,
                        "plan_node_id": context.node.plan_node_id,
                        "branch_id": context.branch_id,
                        "agent_session_ref_id": context.agent_session_ref_id,
                        "key": normalized_key,
                        "content": normalized_content,
                    },
                    occurred_at=utc_now(),
                )
            )
            uow.commit()
            return _published_document(stored)

    @staticmethod
    def _attempt_context(
        uow: UnitOfWork,
        attempt_id: ID,
        *,
        require_active: bool,
    ) -> _AttemptContext | None:
        attempt = uow.states.get_attempt(attempt_id)
        if attempt is None:
            raise ValueError(f"Attempt {attempt_id} is not persisted")
        run = uow.states.get_run(attempt.run_id)
        if run is None:
            raise ValueError(f"Run {attempt.run_id} is not persisted")
        if require_active:
            if run.status is not RunStatus.RUNNING:
                raise ValueError(
                    f"Phase Session requires a running Run; Run {run.run_id} is {run.status.value}"
                )
            if attempt.status is not AttemptStatus.RUNNING:
                raise ValueError(
                    f"Phase Session requires a running Attempt; Attempt {attempt.attempt_id} "
                    f"is {attempt.status.value}"
                )
        plan = uow.states.get_execution_plan(run.run_id)
        if plan is None:
            raise ValueError(f"PlanRevision {run.plan_revision_id} is not persisted")
        if plan.status is not PlanRevisionStatus.APPROVED:
            raise ValueError(
                f"Phase Session requires an approved PlanRevision; "
                f"PlanRevision {plan.plan_revision_id} is {plan.status.value}"
            )
        node = next(
            (
                candidate
                for candidate in plan.nodes
                if candidate.plan_node_id == attempt.plan_node_id
            ),
            None,
        )
        if node is None:
            raise ValueError(
                f"Attempt {attempt.attempt_id} references missing PlanNode {attempt.plan_node_id}"
            )
        phase = next(
            (candidate for candidate in plan.phases if node.plan_node_id in candidate.node_ids),
            None,
        )
        if phase is None:
            return None
        if require_active and node.status is not PlanNodeStatus.RUNNING:
            raise ValueError(
                f"Phase Session requires a running PlanNode; PlanNode {node.plan_node_id} "
                f"is {node.status.value}"
            )
        branch_id = _branch_for_node(plan.branches, node.plan_node_id)
        session_ref_id = attempt.agent_session_ref_id
        if session_ref_id is None:
            raise ValueError(f"Attempt {attempt.attempt_id} has no bound Agent Session")
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
        return _AttemptContext(
            attempt=attempt,
            run=run,
            plan_revision_id=plan.plan_revision_id,
            plan_revision_version=plan.version,
            node=node,
            phase=phase,
            branch_id=branch_id,
            agent_session_ref_id=session_ref_id,
        )

    @classmethod
    def _find_or_open(cls, uow: UnitOfWork, context: _AttemptContext) -> _PhaseIdentity:
        existing = cls._find_existing(uow, context)
        if existing is not None:
            return existing
        session_id = new_id()
        stored = uow.events.append(
            Event(
                type=EventType.PHASE_SESSION_OPENED,
                run_id=context.run.run_id,
                correlation_id=session_id,
                payload={
                    "phase_session_id": session_id,
                    "run_id": context.run.run_id,
                    "phase_id": context.phase.phase_id,
                    "plan_revision_id": context.plan_revision_id,
                    "plan_revision_version": context.plan_revision_version,
                },
                occurred_at=utc_now(),
            )
        )
        return _PhaseIdentity(
            phase_session_id=session_id,
            run_id=context.run.run_id,
            phase_id=context.phase.phase_id,
            plan_revision_id=context.plan_revision_id,
            plan_revision_version=context.plan_revision_version,
            opened_offset=stored.offset,
        )

    @staticmethod
    def _find_existing(uow: UnitOfWork, context: _AttemptContext) -> _PhaseIdentity | None:
        matches: list[_PhaseIdentity] = []
        for stored in uow.events.list_events():
            event = stored.event
            if event.type is not EventType.PHASE_SESSION_OPENED:
                continue
            if event.run_id != context.run.run_id:
                continue
            payload = event.payload
            phase_id = _required_payload_id(payload, "phase_id", event.type)
            if phase_id != context.phase.phase_id:
                continue
            session_id = _required_payload_id(payload, "phase_session_id", event.type)
            run_id = _required_payload_id(payload, "run_id", event.type)
            plan_id = _required_payload_id(payload, "plan_revision_id", event.type)
            version = _required_payload_int(payload, "plan_revision_version", event.type)
            if run_id != context.run.run_id:
                raise ValueError(f"Phase Session Opened fact {event.id} has a mismatched Run")
            if plan_id != context.plan_revision_id or version != context.plan_revision_version:
                raise ValueError(
                    f"Phase Session for Phase {context.phase.phase_id} has a mismatched "
                    "approved PlanRevision"
                )
            matches.append(
                _PhaseIdentity(
                    phase_session_id=session_id,
                    run_id=run_id,
                    phase_id=phase_id,
                    plan_revision_id=plan_id,
                    plan_revision_version=version,
                    opened_offset=stored.offset,
                )
            )
        if len(matches) > 1:
            raise ValueError(
                f"Run {context.run.run_id} and Phase {context.phase.phase_id} have "
                "multiple Phase Sessions"
            )
        return matches[0] if matches else None

    @staticmethod
    def _members(uow: UnitOfWork, identity: _PhaseIdentity) -> dict[ID, _Member]:
        members: dict[ID, _Member] = {}
        for stored in uow.events.list_events():
            event = stored.event
            if event.type is not EventType.PHASE_SESSION_JOINED:
                continue
            if event.run_id != identity.run_id:
                continue
            payload = event.payload
            if (
                _required_payload_id(payload, "phase_session_id", event.type)
                != identity.phase_session_id
            ):
                continue
            member = _Member(
                attempt_id=_required_payload_id(payload, "attempt_id", event.type),
                plan_node_id=_required_payload_id(payload, "plan_node_id", event.type),
                branch_id=_optional_payload_id(payload, "branch_id", event.type),
                agent_session_ref_id=_required_payload_id(
                    payload, "agent_session_ref_id", event.type
                ),
            )
            existing = members.get(member.attempt_id)
            if existing is not None and existing != member:
                raise ValueError(
                    f"Phase Session has conflicting membership for {member.attempt_id}"
                )
            members[member.attempt_id] = member
        return members

    @staticmethod
    def _published(
        uow: UnitOfWork,
        identity: _PhaseIdentity,
        members: Mapping[ID, _Member],
        after_offset: int,
    ) -> list[StoredEvent]:
        entries: list[StoredEvent] = []
        for stored in uow.events.list_events():
            if stored.offset <= after_offset:
                continue
            event = stored.event
            if event.type not in {
                EventType.PHASE_CONTEXT_PUBLISHED,
                EventType.ATTEMPT_HANDOFF_CONFIRMED,
            }:
                continue
            if event.run_id != identity.run_id:
                continue
            payload = event.payload
            if payload.get("phase_session_id") != identity.phase_session_id:
                continue
            attempt_id = _required_payload_id(payload, "attempt_id", event.type)
            member = members.get(attempt_id)
            if member is None:
                raise ValueError(f"Phase Context fact {event.id} is authored by a non-member")
            if _required_payload_id(payload, "plan_node_id", event.type) != member.plan_node_id:
                raise ValueError(f"Phase Context fact {event.id} has a mismatched PlanNode")
            if _optional_payload_id(payload, "branch_id", event.type) != member.branch_id:
                raise ValueError(f"Phase Context fact {event.id} has a mismatched Branch")
            if (
                _required_payload_id(payload, "agent_session_ref_id", event.type)
                != member.agent_session_ref_id
            ):
                raise ValueError(f"Phase Context fact {event.id} has a mismatched Agent Session")
            if event.type is EventType.PHASE_CONTEXT_PUBLISHED:
                _required_payload_text(payload, "key", event.type)
                _required_payload_text(payload, "content", event.type)
            entries.append(stored)
        return entries

    @staticmethod
    def _published_for_key(
        uow: UnitOfWork,
        identity: _PhaseIdentity,
        attempt_id: ID,
        key: str,
    ) -> StoredEvent | None:
        members = PhaseSessions._members(uow, identity)
        for stored in PhaseSessions._published(uow, identity, members, 0):
            if stored.event.type is not EventType.PHASE_CONTEXT_PUBLISHED:
                continue
            payload = stored.event.payload
            if (
                _required_payload_id(payload, "attempt_id", stored.event.type) == attempt_id
                and _required_payload_text(payload, "key", stored.event.type) == key
            ):
                return stored
        return None

    @staticmethod
    def _member_from_context(context: _AttemptContext) -> _Member:
        return _Member(
            attempt_id=context.attempt.attempt_id,
            plan_node_id=context.node.plan_node_id,
            branch_id=context.branch_id,
            agent_session_ref_id=context.agent_session_ref_id,
        )


def _identity_payload(identity: _PhaseIdentity) -> dict[str, JsonValue]:
    return {
        "phase_session_id": identity.phase_session_id,
        "run_id": identity.run_id,
        "phase_id": identity.phase_id,
        "plan_revision_id": identity.plan_revision_id,
        "plan_revision_version": identity.plan_revision_version,
    }


def _membership_document(
    identity: _PhaseIdentity,
    context: _AttemptContext,
) -> dict[str, JsonValue]:
    return {
        **_identity_payload(identity),
        "attempt_id": context.attempt.attempt_id,
        "plan_node_id": context.node.plan_node_id,
        "branch_id": context.branch_id,
        "agent_session_ref_id": context.agent_session_ref_id,
    }


def _published_document(stored: StoredEvent) -> dict[str, JsonValue]:
    payload = stored.event.payload
    if stored.event.type is EventType.ATTEMPT_HANDOFF_CONFIRMED:
        return {
            "offset": stored.offset,
            "event_id": stored.event.id,
            "kind": "host_confirmed_handoff",
            **payload,
            "occurred_at": stored.event.occurred_at.isoformat(),
        }
    return {
        "offset": stored.offset,
        "event_id": stored.event.id,
        "phase_session_id": _required_payload_id(payload, "phase_session_id", stored.event.type),
        "run_id": stored.event.run_id,
        "attempt_id": _required_payload_id(payload, "attempt_id", stored.event.type),
        "plan_node_id": _required_payload_id(payload, "plan_node_id", stored.event.type),
        "branch_id": _optional_payload_id(payload, "branch_id", stored.event.type),
        "agent_session_ref_id": _required_payload_id(
            payload, "agent_session_ref_id", stored.event.type
        ),
        "key": _required_payload_text(payload, "key", stored.event.type),
        "content": _required_payload_text(payload, "content", stored.event.type),
        "occurred_at": stored.event.occurred_at.isoformat(),
    }


def _branch_for_node(branches: tuple[Branch, ...], plan_node_id: ID) -> ID | None:
    branch = next((item for item in branches if plan_node_id in item.node_ids), None)
    return None if branch is None else branch.branch_id


def _normalize_boundary_id(value: ID, field_name: str) -> ID:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UUID string")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"invalid {field_name}: {error}") from error


def _validate_offset(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("after_offset must be a non-negative integer")


def _validate_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be an integer between 1 and {_MAX_PAGE_SIZE}")


def _validate_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("key must be non-blank text")
    normalized = value.strip()
    if len(normalized) > _MAX_KEY_CHARACTERS:
        raise ValueError(f"key exceeds {_MAX_KEY_CHARACTERS} characters")
    return normalized


def _validate_content(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content must be non-blank text")
    if len(value.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise ValueError(f"content exceeds {_MAX_CONTENT_BYTES} UTF-8 bytes")
    return value


def _required_payload_id(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> ID:
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


def _required_payload_int(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise ValueError(f"{event_type.value} payload field {key!r} must be an integer")
    return value


def _required_payload_text(
    payload: Mapping[str, JsonValue], key: str, event_type: EventType
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{event_type.value} payload field {key!r} must be non-blank text")
    return value


__all__ = ["PhaseSessions"]
