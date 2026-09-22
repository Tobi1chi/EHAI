"""Durable Worker-blocker interventions and explicit user replies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from ehai import ID, JsonValue, json_dumps, normalize_id, utc_now
from ehai.application.ports import EventReader, ReadSession, StoredEvent, UnitOfWork
from ehai.application.sanitization import redact_sensitive_text
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, RunStatus
from ehai.domain.planning import (
    Branch,
    PlanNode,
    PlanNodeStatus,
    PlanPhase,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.process_drafts import ProcessDraft

_MAX_TEXT_BYTES = 16_000
_MAX_TOKEN_LENGTH = 64
_INTERVENTION_NAMESPACE_PREFIX = "ehai:attempt-intervention:"
_CONTINUATION_MEANING = (
    "User confirmed continuation within the existing approved requirements, interfaces, "
    "Gates, and permissions; no new boundary is approved."
)


@dataclass(frozen=True, slots=True)
class WorkerBlocker:
    """A bounded, redacted Worker explanation that requires host intervention."""

    reason: str
    evidence: str
    needed: str
    kind: str = "worker_blocked"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _bounded_text(self.reason, "reason"))
        object.__setattr__(self, "evidence", _bounded_text(self.evidence, "evidence"))
        object.__setattr__(self, "needed", _bounded_text(self.needed, "needed"))
        if self.kind not in {"worker_blocked", "external_effects"}:
            raise ValueError("WorkerBlocker kind must be 'worker_blocked' or 'external_effects'")

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the immutable blocker as a detached JSON object."""
        return {
            "reason": self.reason,
            "evidence": self.evidence,
            "needed": self.needed,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    attempt_id: ID
    run_id: ID
    plan_revision_id: ID
    plan_revision_version: int
    plan_node_id: ID
    phase_id: ID | None
    branch_id: ID | None
    agent_session_ref_id: ID | None
    phase_session_id: ID | None
    handoff_ids: tuple[ID, ...]
    artifact_ids: tuple[ID, ...]


def intervention_id_for(attempt_id: ID) -> ID:
    """Return the one stable intervention identity allocated to an Attempt."""
    normalized = _normalize_id(attempt_id, "attempt_id")
    return ID(
        str(
            uuid5(
                NAMESPACE_URL,
                f"{_INTERVENTION_NAMESPACE_PREFIX}{normalized}",
            )
        )
    )


def open_intervention(
    uow: UnitOfWork,
    attempt_id: ID,
    blocker: WorkerBlocker,
) -> dict[str, JsonValue]:
    """Append or replay one durable intervention request in the caller's UoW."""
    normalized_attempt_id = _normalize_id(attempt_id, "attempt_id")
    if not isinstance(blocker, WorkerBlocker):
        raise ValueError("blocker must be a WorkerBlocker")
    intervention_id = intervention_id_for(normalized_attempt_id)
    existing = _find_intervention(uow.events, intervention_id)
    if existing is not None:
        _ensure_same_blocker(existing, blocker)
        return existing

    context = _open_context(uow, normalized_attempt_id)
    if context is None:  # pragma: no cover - an Attempt always has a PlanNode
        raise ValueError(f"Attempt {normalized_attempt_id} has no execution context")
    baseline: dict[str, JsonValue] = {
        "run_id": context.run_id,
        "plan_revision_id": context.plan_revision_id,
        "plan_revision_version": context.plan_revision_version,
        "plan_node_id": context.plan_node_id,
        "phase_id": context.phase_id,
        "branch_id": context.branch_id,
        "agent_session_ref_id": context.agent_session_ref_id,
        "phase_session_id": context.phase_session_id,
        "handoff_ids": list(context.handoff_ids),
        "artifact_ids": list(context.artifact_ids),
    }
    request: dict[str, JsonValue] = {
        "intervention_id": intervention_id,
        "run_id": context.run_id,
        "plan_revision_id": context.plan_revision_id,
        "plan_revision_version": context.plan_revision_version,
        "plan_node_id": context.plan_node_id,
        "attempt_id": context.attempt_id,
        "phase_id": context.phase_id,
        "branch_id": context.branch_id,
        "agent_session_ref_id": context.agent_session_ref_id,
        "phase_session_id": context.phase_session_id,
        "handoff_ids": list(context.handoff_ids),
        "artifact_ids": list(context.artifact_ids),
        "baseline": baseline,
        **blocker.to_dict(),
    }
    payload = {**request, "request_token": _request_token(request)}
    stored = uow.events.append(
        Event(
            type=EventType.INTERVENTION_OPENED,
            run_id=context.run_id,
            correlation_id=intervention_id,
            payload=payload,
            occurred_at=utc_now(),
        )
    )
    return _intervention_document(stored)


def list_interventions(
    events: EventReader,
    run_id: ID,
    *,
    through_offset: int | None = None,
) -> tuple[dict[str, JsonValue], ...]:
    """Return requests in open order, optionally at an immutable Event-log boundary."""
    normalized_run_id = _normalize_id(run_id, "run_id")
    if through_offset is not None and (type(through_offset) is not int or through_offset < 0):
        raise ValueError("through_offset must be a non-negative Event offset")
    opens: list[dict[str, JsonValue]] = []
    replies: dict[ID, dict[str, JsonValue]] = {}
    for stored in events.list_events():
        if through_offset is not None and stored.offset > through_offset:
            continue
        event = stored.event
        if event.type is EventType.INTERVENTION_OPENED and event.run_id == normalized_run_id:
            document = _intervention_document(stored)
            intervention_id = _document_id(document, "intervention_id")
            if any(
                _document_id(existing, "intervention_id") == intervention_id for existing in opens
            ):
                raise ValueError(f"Intervention {intervention_id} has duplicate open facts")
            opens.append(document)
        elif event.type is EventType.INTERVENTION_REPLIED and event.run_id == normalized_run_id:
            reply_document = _reply_document(stored)
            intervention_id = _document_id(reply_document, "intervention_id")
            previous = replies.get(intervention_id)
            if previous is not None and not _same_reply(previous, reply_document):
                raise ValueError(f"Intervention {intervention_id} has conflicting replies")
            replies[intervention_id] = reply_document

    result: list[dict[str, JsonValue]] = []
    opened_ids = {_document_id(opened, "intervention_id") for opened in opens}
    orphan_reply_ids = set(replies).difference(opened_ids)
    if orphan_reply_ids:
        raise ValueError(
            "Intervention reply has no matching open fact: "
            + ", ".join(str(item) for item in sorted(orphan_reply_ids))
        )
    for opened in opens:
        intervention_id = _document_id(opened, "intervention_id")
        reply = replies.get(intervention_id)
        if reply is not None:
            _ensure_reply_scope(opened, reply)
        result.append(_with_status(opened, reply))
    return tuple(result)


def process_intervention_context(
    uow: UnitOfWork, draft: ProcessDraft, *, require_current: bool = False
) -> tuple[dict[str, JsonValue], ...]:
    """Recover precisely the question/reply history visible when this draft was requested.

    The existing DraftStarted Event provides the snapshot boundary. Later user
    replies are never silently substituted into an already generated draft.
    Source process IDs identify the originating Attempt, not the active version.
    """
    started = tuple(
        stored
        for stored in uow.events.list_events()
        if stored.event.type is EventType.PROCESS_DRAFT_STARTED
        and stored.event.run_id == draft.run_id
        and stored.event.correlation_id == draft.draft_id
    )
    if len(started) != 1 or (
        started[0].event.payload.get("draft_id") != draft.draft_id
        or started[0].event.payload.get("parent_process_revision_id")
        != draft.parent_process_revision_id
    ):
        raise ValueError("Process draft has no unambiguous retained request boundary")
    notices = list_interventions(uow.events, draft.run_id, through_offset=started[0].offset)
    if require_current and notices != list_interventions(uow.events, draft.run_id):
        raise ValueError(
            "Intervention questions or replies changed after this process draft request"
        )
    result: list[dict[str, JsonValue]] = []
    for notice in notices:
        attempt = uow.states.get_attempt(_document_id(notice, "attempt_id"))
        if (
            attempt is None
            or attempt.run_id != draft.run_id
            or attempt.plan_node_id != _document_id(notice, "plan_node_id")
        ):
            raise ValueError("Intervention source Attempt is missing or has inconsistent ownership")
        result.append({**notice, "source_process_revision_id": attempt.process_revision_id})
    return tuple(result)


def attempt_process_interventions(
    uow: UnitOfWork, attempt: Attempt
) -> tuple[dict[str, JsonValue], ...]:
    """Carry the applied draft's pinned facts into its Workers, even after node replacement."""
    if attempt.process_revision_id is None:
        return ()
    process = uow.states.get_process_revision(attempt.process_revision_id)
    if process is None or process.run_id != attempt.run_id:
        raise ValueError("Attempt process is missing or belongs to another Run")
    if process.version == 1:
        return ()
    applied = tuple(
        stored.event
        for stored in uow.events.list_events()
        if stored.event.type is EventType.PROCESS_REVISION_APPLIED
        and stored.event.run_id == attempt.run_id
        and stored.event.correlation_id == attempt.process_revision_id
    )
    if len(applied) != 1:
        raise ValueError("Attempt process has no unambiguous application fact")
    event = applied[0]
    if (
        event.payload.get("process_revision_id") != process.process_revision_id
        or event.payload.get("parent_process_revision_id") != process.parent_process_revision_id
    ):
        raise ValueError("Process application fact has inconsistent version ownership")
    draft = uow.states.get_process_draft(_payload_id(event.payload, "draft_id", event.type))
    if (
        draft is None
        or draft.run_id != attempt.run_id
        or draft.candidate != process
        or draft.parent_process_revision_id != process.parent_process_revision_id
    ):
        raise ValueError("Process application references an inconsistent draft")
    return process_intervention_context(uow, draft)


def reply_intervention(
    uow: UnitOfWork,
    intervention_id: ID,
    request_token: str,
    actor: str,
    message: str,
) -> dict[str, JsonValue]:
    """Append an explicit continuation reply without changing domain state."""
    normalized_id = _normalize_id(intervention_id, "intervention_id")
    token = _token(request_token)
    normalized_actor = _bounded_text(actor, "actor")
    normalized_message = _bounded_text(message, "message")
    opened = _find_intervention(uow.events, normalized_id)
    if opened is None:
        raise ValueError(f"Intervention {normalized_id} is not persisted")
    if opened.get("request_token") != token:
        raise ValueError("intervention request token does not match the persisted request")

    existing = _find_reply(uow.events, normalized_id)
    if existing is not None:
        if (
            existing.get("actor") != normalized_actor
            or existing.get("message") != normalized_message
        ):
            raise ValueError("intervention already has a different reply")
        return _with_status(opened, existing)

    validate_intervention_reply_context(uow, opened)
    run_id = _document_id(opened, "run_id")
    payload: dict[str, JsonValue] = {
        "intervention_id": normalized_id,
        "run_id": run_id,
        "plan_revision_id": _document_id(opened, "plan_revision_id"),
        "plan_node_id": _document_id(opened, "plan_node_id"),
        "attempt_id": _document_id(opened, "attempt_id"),
        "request_token": token,
        "actor": normalized_actor,
        "message": normalized_message,
        "meaning": _CONTINUATION_MEANING,
    }
    stored = uow.events.append(
        Event(
            type=EventType.INTERVENTION_REPLIED,
            run_id=run_id,
            correlation_id=normalized_id,
            payload=payload,
            occurred_at=utc_now(),
        )
    )
    return _with_status(opened, _reply_document(stored))


def _open_context(uow: UnitOfWork, attempt_id: ID) -> _ExecutionContext | None:
    attempt = uow.states.get_attempt(attempt_id)
    if attempt is None:
        raise ValueError(f"Attempt {attempt_id} is not persisted")
    if attempt.status is not AttemptStatus.RUNNING:
        raise ValueError(
            f"Intervention requires a running Attempt; Attempt {attempt_id} is "
            f"{attempt.status.value}"
        )
    run = uow.states.get_run(attempt.run_id)
    if run is None:
        raise ValueError(f"Run {attempt.run_id} is not persisted")
    if run.status is not RunStatus.RUNNING:
        raise ValueError(
            f"Intervention requires a running Run; Run {run.run_id} is {run.status.value}"
        )
    plan = uow.states.get_execution_plan(run.run_id)
    if plan is None:
        raise ValueError(f"PlanRevision {run.plan_revision_id} is not persisted")
    if plan.status is not PlanRevisionStatus.APPROVED:
        raise ValueError(
            f"Intervention requires an approved PlanRevision; PlanRevision "
            f"{plan.plan_revision_id} is {plan.status.value}"
        )
    node = _node(plan, attempt.plan_node_id)
    if node.status is not PlanNodeStatus.RUNNING:
        raise ValueError(
            f"Intervention requires a running PlanNode; PlanNode {node.plan_node_id} is "
            f"{node.status.value}"
        )
    session_ref_id = attempt.agent_session_ref_id
    # Startup failures can exhaust recovery before a Session/handle was bound.
    session = None if session_ref_id is None else uow.states.get_agent_session_ref(session_ref_id)
    if session_ref_id is not None and session is None:
        raise ValueError(f"Agent Session {session_ref_id} is not persisted")
    if session is not None and session.run_id != run.run_id:
        raise ValueError(f"Agent Session {session_ref_id} belongs to another Run")
    if (
        session is not None
        and attempt.worker_profile_id is not None
        and session.worker_profile_id != attempt.worker_profile_id
    ):
        raise ValueError(f"Agent Session {session_ref_id} does not match the Attempt profile")
    if (
        session is not None
        and attempt.worker_endpoint_id is not None
        and session.worker_endpoint_id != attempt.worker_endpoint_id
    ):
        raise ValueError(f"Agent Session {session_ref_id} does not match the Attempt endpoint")
    if (
        attempt.execution_handle is not None
        and attempt.execution_handle.agent_session_ref_id != session_ref_id
    ):
        raise ValueError(f"Agent Session {session_ref_id} does not match the execution handle")
    phase = _phase_for_node(plan, node.plan_node_id)
    return _ExecutionContext(
        attempt_id=attempt.attempt_id,
        run_id=run.run_id,
        plan_revision_id=plan.plan_revision_id,
        plan_revision_version=plan.version,
        plan_node_id=node.plan_node_id,
        phase_id=None if phase is None else phase.phase_id,
        branch_id=_branch_for_node(plan.branches, node.plan_node_id),
        agent_session_ref_id=session_ref_id,
        phase_session_id=_phase_session_for_attempt(uow, run.run_id, attempt_id),
        handoff_ids=_handoff_ids(uow, run.run_id, attempt_id),
        artifact_ids=tuple(attempt.artifact_ids),
    )


def validate_intervention_reply_context(
    uow: UnitOfWork | ReadSession, opened: Mapping[str, JsonValue]
) -> None:
    """Share current-context checks with the read-only human inbox."""
    run_id = _document_id(opened, "run_id")
    plan_revision_id = _document_id(opened, "plan_revision_id")
    plan_node_id = _document_id(opened, "plan_node_id")
    run = uow.states.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} is not persisted")
    if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ValueError(f"Run {run_id} is terminal and cannot accept an intervention reply")
    if run.plan_revision_id != plan_revision_id:
        raise ValueError("current Run PlanRevision differs from the intervention request")
    plan = uow.states.get_execution_plan(run.run_id)
    if plan is None:
        raise ValueError(f"PlanRevision {run.plan_revision_id} is not persisted")
    if plan.status is not PlanRevisionStatus.APPROVED:
        raise ValueError("intervention reply requires the approved requested PlanRevision")
    goal = uow.states.get_goal(run.goal_id)
    contract = None if goal is None else goal.completion_contract
    if contract is None or contract.completion_contract_id != plan.completion_contract_id:
        raise ValueError(
            "The intervention's approval baseline is no longer the Goal's current contract"
        )
    node = _node(plan, plan_node_id)
    if node.status is not PlanNodeStatus.SUSPENDED:
        raise ValueError(f"PlanNode {plan_node_id} is not suspended by the intervention request")
    if any(
        attempt.plan_node_id == plan_node_id and attempt.status is AttemptStatus.RUNNING
        for attempt in uow.states.list_attempts(run_id)
    ):
        raise ValueError(f"PlanNode {plan_node_id} still has a running Attempt")


def _find_intervention(events: EventReader, intervention_id: ID) -> dict[str, JsonValue] | None:
    found: dict[str, JsonValue] | None = None
    for stored in events.list_events():
        event = stored.event
        if event.type is not EventType.INTERVENTION_OPENED:
            continue
        payload = event.payload
        if _payload_id(payload, "intervention_id", event.type) != intervention_id:
            continue
        document = _intervention_document(stored)
        if found is not None and not _same_open_request(found, document):
            raise ValueError(f"Intervention {intervention_id} has conflicting open facts")
        found = document
    return found


def _find_reply(events: EventReader, intervention_id: ID) -> dict[str, JsonValue] | None:
    found: dict[str, JsonValue] | None = None
    for stored in events.list_events():
        event = stored.event
        if event.type is not EventType.INTERVENTION_REPLIED:
            continue
        payload = event.payload
        if _payload_id(payload, "intervention_id", event.type) != intervention_id:
            continue
        document = _reply_document(stored)
        if found is not None and not _same_reply(found, document):
            raise ValueError(f"Intervention {intervention_id} has conflicting replies")
        found = document
    return found


def _intervention_document(stored: StoredEvent) -> dict[str, JsonValue]:
    event = stored.event
    if event.type is not EventType.INTERVENTION_OPENED:
        raise ValueError("stored event is not an InterventionOpened fact")
    payload = event.payload
    intervention_id = _payload_id(payload, "intervention_id", event.type)
    if event.correlation_id != intervention_id:
        raise ValueError(f"Intervention fact {event.id} has a mismatched correlation ID")
    run_id = _payload_id(payload, "run_id", event.type)
    if event.run_id != run_id:
        raise ValueError(f"Intervention fact {event.id} has a mismatched Run")
    attempt_id = _payload_id(payload, "attempt_id", event.type)
    if intervention_id != intervention_id_for(attempt_id):
        raise ValueError(f"Intervention fact {event.id} has an invalid identity")
    plan_revision_id = _payload_id(payload, "plan_revision_id", event.type)
    plan_node_id = _payload_id(payload, "plan_node_id", event.type)
    plan_revision_version = _payload_int(payload, "plan_revision_version", event.type)
    blocker = WorkerBlocker(
        _payload_text(payload, "reason", event.type),
        _payload_text(payload, "evidence", event.type),
        _payload_text(payload, "needed", event.type),
        _payload_text(payload, "kind", event.type),
    )
    handoff_ids = _payload_ids(payload, "handoff_ids", event.type)
    artifact_ids = _payload_ids(payload, "artifact_ids", event.type)
    phase_id = _optional_payload_id(payload, "phase_id", event.type)
    branch_id = _optional_payload_id(payload, "branch_id", event.type)
    session_id = _optional_payload_id(payload, "agent_session_ref_id", event.type)
    phase_session_id = _optional_payload_id(payload, "phase_session_id", event.type)
    baseline = payload.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError(f"Intervention fact {event.id} has an invalid baseline")
    expected_baseline: dict[str, JsonValue] = {
        "run_id": run_id,
        "plan_revision_id": plan_revision_id,
        "plan_revision_version": plan_revision_version,
        "plan_node_id": plan_node_id,
        "phase_id": phase_id,
        "branch_id": branch_id,
        "agent_session_ref_id": session_id,
        "phase_session_id": phase_session_id,
        "handoff_ids": list(handoff_ids),
        "artifact_ids": list(artifact_ids),
    }
    if baseline != expected_baseline:
        raise ValueError(f"Intervention fact {event.id} has a mismatched baseline")
    request: dict[str, JsonValue] = {
        "intervention_id": intervention_id,
        "run_id": run_id,
        "plan_revision_id": plan_revision_id,
        "plan_revision_version": plan_revision_version,
        "plan_node_id": plan_node_id,
        "attempt_id": attempt_id,
        "phase_id": phase_id,
        "branch_id": branch_id,
        "agent_session_ref_id": session_id,
        "phase_session_id": phase_session_id,
        "handoff_ids": list(handoff_ids),
        "artifact_ids": list(artifact_ids),
        "baseline": baseline,
        **blocker.to_dict(),
    }
    request_token = _token(_payload_text(payload, "request_token", event.type))
    if request_token != _request_token(request):
        raise ValueError(f"Intervention fact {event.id} has a mismatched request token")
    return {
        "offset": stored.offset,
        "event_id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        **request,
        "request_token": request_token,
        "status": "open",
        "reply": None,
    }


def _reply_document(stored: StoredEvent) -> dict[str, JsonValue]:
    event = stored.event
    if event.type is not EventType.INTERVENTION_REPLIED:
        raise ValueError("stored event is not an InterventionReplied fact")
    payload = event.payload
    intervention_id = _payload_id(payload, "intervention_id", event.type)
    run_id = _payload_id(payload, "run_id", event.type)
    if event.correlation_id != intervention_id:
        raise ValueError(f"Intervention reply {event.id} has a mismatched correlation ID")
    if event.run_id != run_id:
        raise ValueError(f"Intervention reply {event.id} has a mismatched Run")
    meaning = _payload_text(payload, "meaning", event.type)
    if meaning != _CONTINUATION_MEANING:
        raise ValueError(f"Intervention reply {event.id} has an invalid meaning")
    return {
        "offset": stored.offset,
        "event_id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        "intervention_id": intervention_id,
        "run_id": run_id,
        "plan_revision_id": _payload_id(payload, "plan_revision_id", event.type),
        "plan_node_id": _payload_id(payload, "plan_node_id", event.type),
        "attempt_id": _payload_id(payload, "attempt_id", event.type),
        "request_token": _token(_payload_text(payload, "request_token", event.type)),
        "actor": _payload_text(payload, "actor", event.type),
        "message": _payload_text(payload, "message", event.type),
        "meaning": meaning,
    }


def _with_status(
    opened: Mapping[str, JsonValue],
    reply: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    result = dict(opened)
    result["status"] = "open" if reply is None else "replied"
    result["reply"] = None if reply is None else dict(reply)
    return result


def _same_open_request(first: Mapping[str, JsonValue], second: Mapping[str, JsonValue]) -> bool:
    keys = (
        "intervention_id",
        "run_id",
        "plan_revision_id",
        "plan_revision_version",
        "plan_node_id",
        "attempt_id",
        "phase_id",
        "branch_id",
        "agent_session_ref_id",
        "phase_session_id",
        "handoff_ids",
        "artifact_ids",
        "baseline",
        "reason",
        "evidence",
        "needed",
        "kind",
        "request_token",
    )
    return all(first.get(key) == second.get(key) for key in keys)


def _same_reply(first: Mapping[str, JsonValue], second: Mapping[str, JsonValue]) -> bool:
    return all(
        first.get(key) == second.get(key)
        for key in (
            "intervention_id",
            "run_id",
            "plan_revision_id",
            "plan_node_id",
            "attempt_id",
            "request_token",
            "actor",
            "message",
            "meaning",
        )
    )


def _ensure_reply_scope(opened: Mapping[str, JsonValue], reply: Mapping[str, JsonValue]) -> None:
    for key in (
        "intervention_id",
        "run_id",
        "plan_revision_id",
        "plan_node_id",
        "attempt_id",
        "request_token",
    ):
        if opened.get(key) != reply.get(key):
            raise ValueError(
                f"Intervention {opened.get('intervention_id')} reply scope does not match request"
            )


def _ensure_same_blocker(existing: Mapping[str, JsonValue], blocker: WorkerBlocker) -> None:
    for key, value in blocker.to_dict().items():
        if existing.get(key) != value:
            raise ValueError("Attempt already has an intervention with different content")


def _request_token(request: Mapping[str, JsonValue]) -> str:
    return sha256(json_dumps(dict(request)).encode("utf-8")).hexdigest()


def _bounded_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"WorkerBlocker {field_name} must be non-empty text")
    redacted = redact_sensitive_text(value.strip())
    if len(redacted.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"WorkerBlocker {field_name} exceeds {_MAX_TEXT_BYTES} UTF-8 bytes")
    return redacted


def _token(value: str) -> str:
    if not isinstance(value, str) or len(value) != _MAX_TOKEN_LENGTH:
        raise ValueError("intervention request_token must be a 64-character SHA-256 string")
    if any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("intervention request_token must be a 64-character SHA-256 string")
    return value.lower()


def _normalize_id(value: str, field_name: str) -> ID:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UUID string")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"invalid {field_name}: {error}") from error


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
    found: ID | None = None
    for stored in uow.events.list_events():
        event = stored.event
        if event.type is not EventType.PHASE_SESSION_JOINED or event.run_id != run_id:
            continue
        payload = event.payload
        if _payload_id(payload, "attempt_id", event.type) != attempt_id:
            continue
        session_id = _payload_id(payload, "phase_session_id", event.type)
        if found is not None and found != session_id:
            raise ValueError(f"Attempt {attempt_id} has conflicting Phase Session membership")
        found = session_id
    return found


def _handoff_ids(uow: UnitOfWork, run_id: ID, attempt_id: ID) -> tuple[ID, ...]:
    ids: list[ID] = []
    for stored in uow.events.list_events():
        event = stored.event
        if event.type is not EventType.ATTEMPT_HANDOFF_CONFIRMED or event.run_id != run_id:
            continue
        payload = event.payload
        if _payload_id(payload, "attempt_id", event.type) != attempt_id:
            continue
        handoff_id = _payload_id(payload, "handoff_id", event.type)
        if handoff_id not in ids:
            ids.append(handoff_id)
    return tuple(ids)


def _payload_id(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> ID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{event_type.value} payload field {key!r} must be an ID")
    return _normalize_id(value, f"{event_type.value}.{key}")


def _optional_payload_id(
    payload: Mapping[str, JsonValue], key: str, event_type: EventType
) -> ID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{event_type.value} payload field {key!r} must be an ID or null")
    return _normalize_id(value, f"{event_type.value}.{key}")


def _payload_ids(
    payload: Mapping[str, JsonValue], key: str, event_type: EventType
) -> tuple[ID, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{event_type.value} payload field {key!r} must be an ID list")
    result: list[ID] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{event_type.value} payload field {key!r}[{index}] must be an ID")
        result.append(_normalize_id(item, f"{event_type.value}.{key}[{index}]"))
    return tuple(result)


def _payload_int(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise ValueError(f"{event_type.value} payload field {key!r} must be an integer")
    return value


def _payload_text(payload: Mapping[str, JsonValue], key: str, event_type: EventType) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{event_type.value} payload field {key!r} must be non-empty text")
    return value


def _document_id(document: Mapping[str, JsonValue], key: str) -> ID:
    value = document.get(key)
    if not isinstance(value, str):
        raise ValueError(f"intervention document field {key!r} must be an ID")
    return _normalize_id(value, key)


__all__ = [
    "WorkerBlocker",
    "attempt_process_interventions",
    "intervention_id_for",
    "list_interventions",
    "open_intervention",
    "process_intervention_context",
    "reply_intervention",
]
