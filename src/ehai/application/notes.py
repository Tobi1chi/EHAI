"""Durable discussion and explicit decisions; source services retain execution authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from ehai import ID, JsonValue, format_utc_datetime, json_dumps, new_id, normalize_id, utc_now
from ehai.application.ports import CommandReceipt, ReadSession, UnitOfWork
from ehai.application.queries import QueryNotFoundError
from ehai.application.sanitization import redact_sensitive_text
from ehai.domain.events import Event, EventType

NoteDocument = dict[str, JsonValue]
DecisionExecutor = Callable[[NoteDocument, NoteDocument, str], Awaitable[NoteDocument]]
NOTE_ACTIONS = ("resolve", "continue", "propose_process", "revise_plan")


def _text(value: str, field: str, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must contain 1..{limit} characters")
    return redact_sensitive_text(value.strip())


def note_documents(session: ReadSession | UnitOfWork) -> list[NoteDocument]:
    notes: dict[str, NoteDocument] = {}
    for stored in session.events.list_events():
        event = stored.event
        if event.type is EventType.NOTE_CREATED:
            notes[str(event.correlation_id)] = {
                **event.payload,
                "messages": [],
                "decision": None,
                "status": "open",
                "created_at": format_utc_datetime(event.occurred_at),
            }
        elif event.type in {
            EventType.NOTE_MESSAGE_ADDED,
            EventType.NOTE_DECISION_STARTED,
            EventType.NOTE_DECISION_COMPLETED,
            EventType.NOTE_DECISION_FAILED,
        }:
            note = notes.get(str(event.correlation_id))
            if note is None:
                continue
            if event.type is EventType.NOTE_MESSAGE_ADDED:
                cast(list[JsonValue], note["messages"]).append(event.payload)
            else:
                note["decision"] = event.payload
                note["status"] = (
                    "resolved"
                    if event.type is EventType.NOTE_DECISION_COMPLETED
                    else "decision_pending"
                )
            note["request_token"] = str(event.id)
    return list(notes.values())


def _get(session: ReadSession | UnitOfWork, note_id: ID) -> NoteDocument:
    for note in note_documents(session):
        if note["note_id"] == normalize_id(note_id):
            return note
    raise QueryNotFoundError("Note", note_id)


def _source(
    session: ReadSession | UnitOfWork,
    goal_id: ID,
    run_id: ID | None,
    source_kind: str | None,
    source_id: ID | None,
    source_token: str | None,
) -> NoteDocument:
    goal = session.states.get_goal(goal_id)
    if goal is None:
        raise QueryNotFoundError("Goal", goal_id)
    plans = session.states.list_plan_revisions(goal_id)
    run = None if run_id is None else session.states.get_run(run_id)
    if run_id is not None and (run is None or run.goal_id != goal_id):
        raise ValueError("Note Run must exist and belong to its Goal")
    process = None if run_id is None else session.states.get_active_process_revision(run_id)
    source: NoteDocument = {
        "project_id": goal.project_id,
        "goal_id": goal_id,
        "run_id": run_id,
        "plan_revision_id": None if not plans else plans[-1].plan_revision_id,
        "completion_contract_id": None
        if goal.completion_contract is None
        else goal.completion_contract.completion_contract_id,
        "process_revision_id": None if process is None else process.process_revision_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_token": source_token,
    }
    if source_kind is not None:
        if (
            source_kind not in {"intervention", "human_check"}
            or source_id is None
            or source_token is None
            or run_id is None
        ):
            raise ValueError(
                "Bound notes require intervention/human_check, Run, source ID and token"
            )
        # Import here because Inbox also projects notes.
        from ehai.application.inbox import InboxQuery

        item = (
            InboxQuery(session, None, include_notes=False)
            .get(cast("InboxKind", source_kind), source_id)
            .item
        )
        if item.owner.run_id != run_id or item.request_token != source_token or not item.pending:
            raise ValueError("Note source request is stale or belongs to another Run")
    elif source_id is not None or source_token is not None:
        raise ValueError("Source ID/token require source_kind")
    return source


def note_stale_reason(session: ReadSession | UnitOfWork, note: NoteDocument) -> str | None:
    source = cast(NoteDocument, note["source"])
    try:
        current = _source(
            session,
            cast(ID, source["goal_id"]),
            cast(ID | None, source["run_id"]),
            cast(str | None, source["source_kind"]),
            cast(ID | None, source["source_id"]),
            cast(str | None, source["source_token"]),
        )
        if current != source:
            return "Approval, plan or process changed; create a note against the current source"
        run_id = cast(ID | None, source["run_id"])
        if run_id is not None:
            run = session.states.get_run(run_id)
            if run is not None and (
                run.status.value in {"completed", "cancelled", "failed"}
                or any(
                    r.predecessor_run_id == run_id for r in session.states.list_runs(run.goal_id)
                )
            ):
                return "Source Run is terminal or has a successor"
    except ValueError as error:
        return str(error)
    return None


class NoteService:
    def __init__(
        self, uow_factory: Callable[[], UnitOfWork], read_session_factory: Callable[[], ReadSession]
    ) -> None:
        self._uow = uow_factory
        self._read = read_session_factory

    def get(self, note_id: ID) -> NoteDocument:
        with self._read() as session:
            note = _get(session, note_id)
            note["stale_reason"] = note_stale_reason(session, note)
            return note

    def list(
        self,
        *,
        project_id: ID | None = None,
        goal_id: ID | None = None,
        run_id: ID | None = None,
        include_resolved: bool = False,
    ) -> NoteDocument:
        with self._read() as session:
            items: list[JsonValue] = []
            for note in note_documents(session):
                source = cast(NoteDocument, note["source"])
                if any(
                    value is not None and source[key] != normalize_id(value)
                    for key, value in (
                        ("project_id", project_id),
                        ("goal_id", goal_id),
                        ("run_id", run_id),
                    )
                ):
                    continue
                if not include_resolved and note["status"] == "resolved":
                    continue
                note["stale_reason"] = note_stale_reason(session, note)
                items.append(note)
            return {
                "items": items,
                "event_offset": session.events.latest_offset(),
                "observed_at": format_utc_datetime(utc_now()),
            }

    def create(
        self,
        *,
        idempotency_key: str,
        goal_id: ID,
        actor: str,
        question: str,
        evidence: str = "",
        run_id: ID | None = None,
        source_kind: str | None = None,
        source_id: ID | None = None,
        source_token: str | None = None,
        origin: str = "user",
    ) -> NoteDocument:
        if origin not in {"user", "agent", "planner"}:
            raise ValueError("Unknown note origin")
        payload: NoteDocument = {
            "goal_id": normalize_id(goal_id),
            "actor": _text(actor, "actor", 200),
            "question": _text(question, "question"),
            "evidence": redact_sensitive_text(evidence),
            "run_id": None if run_id is None else normalize_id(run_id),
            "source_kind": source_kind,
            "source_id": None if source_id is None else normalize_id(source_id),
            "source_token": source_token,
            "origin": origin,
        }
        with self._uow() as uow:
            existing = self._replay(uow, idempotency_key, "CreateNote", payload)
            if existing is not None:
                return _get(uow, cast(ID, existing["note_id"]))
            source = _source(
                uow,
                cast(ID, payload["goal_id"]),
                cast(ID | None, payload["run_id"]),
                source_kind,
                cast(ID | None, payload["source_id"]),
                source_token,
            )
            note_id = new_id()
            document: NoteDocument = {
                "note_id": note_id,
                "actor": payload["actor"],
                "origin": origin,
                "question": payload["question"],
                "evidence": payload["evidence"],
                "source": source,
                "request_token": str(new_id()),
            }
            self._append(uow, EventType.NOTE_CREATED, note_id, document, run_id)
            self._receipt(uow, idempotency_key, "CreateNote", payload, {"note_id": note_id})
            uow.commit()
        return self.get(note_id)

    def message(
        self, *, idempotency_key: str, note_id: ID, request_token: str, actor: str, message: str
    ) -> NoteDocument:
        payload: NoteDocument = {
            "note_id": normalize_id(note_id),
            "request_token": request_token,
            "actor": _text(actor, "actor", 200),
            "message": _text(message, "message"),
        }
        with self._uow() as uow:
            if self._replay(uow, idempotency_key, "AddNoteMessage", payload) is not None:
                return _get(uow, note_id)
            note = self._open(uow, note_id, request_token)
            self._append(
                uow,
                EventType.NOTE_MESSAGE_ADDED,
                note_id,
                {**payload, "message_id": new_id(), "created_at": format_utc_datetime(utc_now())},
                cast(ID | None, cast(NoteDocument, note["source"])["run_id"]),
            )
            self._receipt(uow, idempotency_key, "AddNoteMessage", payload, {"note_id": note_id})
            uow.commit()
        return self.get(note_id)

    async def decide(
        self,
        *,
        idempotency_key: str,
        note_id: ID,
        request_token: str,
        actor: str,
        action: str,
        message: str,
        executor: DecisionExecutor,
        passed: bool | None = None,
    ) -> NoteDocument:
        if action not in NOTE_ACTIONS:
            raise ValueError("Unknown note decision action")
        payload: NoteDocument = {
            "note_id": normalize_id(note_id),
            "request_token": request_token,
            "actor": _text(actor, "actor", 200),
            "action": action,
            "message": _text(message, "message"),
            "passed": passed,
        }
        with self._uow() as uow:
            previous = self._replay(uow, idempotency_key, "DecideNote", payload)
            if previous is not None:
                retained = _get(uow, note_id)
                decision = retained.get("decision")
                operation_key = previous.get("operation_key")
                if retained["status"] == "decision_pending" and isinstance(operation_key, str):
                    downstream = uow.command_receipts.get(operation_key)
                    if downstream is not None and isinstance(decision, dict):
                        if decision.get("action") == "propose_process":
                            draft_id = downstream.result.get("draft_id")
                            draft = (
                                uow.states.get_process_draft(normalize_id(draft_id))
                                if isinstance(draft_id, str)
                                else None
                            )
                            if draft is None or draft.status.value != "ready":
                                return retained
                        if decision.get("action") == "revise_plan":
                            conversation_id = downstream.result.get("conversation_id")
                            turn_id = downstream.result.get("turn_id")
                            if not any(
                                item.event.type is EventType.PLANNING_TURN_COMPLETED
                                and item.event.correlation_id == conversation_id
                                and item.event.payload.get("turn_id") == turn_id
                                for item in uow.events.list_events()
                            ):
                                return retained
                        source = cast(NoteDocument, retained["source"])
                        self._append(
                            uow,
                            EventType.NOTE_DECISION_COMPLETED,
                            note_id,
                            {**decision, "status": "completed", "result": downstream.result},
                            cast(ID | None, source["run_id"]),
                        )
                        uow.commit()
                        return self.get(note_id)
                return retained  # Pending/unknown operations are never blindly replayed.
            note = self._open(uow, note_id, request_token)
            if action != "resolve" and (reason := note_stale_reason(uow, note)) is not None:
                raise ValueError(reason)
            source = cast(NoteDocument, note["source"])
            if action == "continue" and source["source_kind"] is None:
                raise ValueError("Continue requires a bound intervention or human Check")
            if action == "propose_process" and source["run_id"] is None:
                raise ValueError("Process proposals require a Run")
            if action == "revise_plan" and source["run_id"] is not None:
                run = uow.states.get_run(cast(ID, source["run_id"]))
                if run is None or run.status.value != "paused":
                    raise ValueError(
                        "Pause the source Run explicitly before requesting plan revision"
                    )
            if action == "continue" and source["source_kind"] == "human_check" and passed is None:
                raise ValueError("A human Check continuation requires explicit passed")
            if passed is not None and not (
                action == "continue" and source["source_kind"] == "human_check"
            ):
                raise ValueError("passed is only valid for a human Check continuation")
            operation_key = "note-decision:" + str(new_id())
            decision = {
                **payload,
                "operation_key": operation_key,
                "status": "pending",
                "result": None,
            }
            self._append(
                uow,
                EventType.NOTE_DECISION_STARTED,
                note_id,
                decision,
                cast(ID | None, source["run_id"]),
            )
            self._receipt(
                uow,
                idempotency_key,
                "DecideNote",
                payload,
                {"note_id": note_id, "operation_key": operation_key},
            )
            uow.commit()
        try:
            result: NoteDocument = (
                {"meaning": "Discussion closed; no execution or approval changed"}
                if action == "resolve"
                else await executor(note, payload, operation_key)
            )
        except Exception as error:
            with self._uow() as uow:
                self._append(
                    uow,
                    EventType.NOTE_DECISION_FAILED,
                    note_id,
                    {**decision, "status": "unknown", "error": redact_sensitive_text(str(error))},
                    cast(ID | None, source["run_id"]),
                )
                uow.commit()
            raise
        with self._uow() as uow:
            # Re-read ownership and versions after dispatch. Source services have already
            # checked their own concurrency tokens; preserve their committed outcome even
            # when a subsequent independent change makes this note's baseline historical.
            after = _source(
                uow,
                cast(ID, source["goal_id"]),
                cast(ID | None, source["run_id"]),
                None,
                None,
                None,
            )
            after.update({key: source[key] for key in ("source_kind", "source_id", "source_token")})
            self._append(
                uow,
                EventType.NOTE_DECISION_COMPLETED,
                note_id,
                {
                    **decision,
                    "status": "completed",
                    "result": result,
                    "source_after": after,
                    "source_changed": after != source,
                },
                cast(ID | None, source["run_id"]),
            )
            uow.commit()
        return self.get(note_id)

    @staticmethod
    def _open(uow: UnitOfWork, note_id: ID, token: str) -> NoteDocument:
        note = _get(uow, note_id)
        if note["request_token"] != token:
            raise ValueError("Note request token is stale; read the latest discussion")
        if note["status"] != "open":
            raise ValueError(
                "Note is resolved or has a pending decision; inspect its persisted operation"
            )
        return note

    @staticmethod
    def _append(
        uow: UnitOfWork,
        event_type: EventType,
        note_id: ID,
        payload: NoteDocument,
        run_id: ID | None,
    ) -> None:
        uow.events.append(
            Event(type=event_type, correlation_id=note_id, run_id=run_id, payload=payload)
        )

    @staticmethod
    def _replay(uow: UnitOfWork, key: str, name: str, payload: NoteDocument) -> NoteDocument | None:
        _text(key, "idempotency_key", 500)
        receipt = uow.command_receipts.get(key)
        if receipt is None:
            return None
        if (
            receipt.command_name != name
            or receipt.command_fingerprint != sha256(json_dumps(payload).encode()).hexdigest()
        ):
            raise ValueError("Idempotency key was used with different content")
        return receipt.result

    @staticmethod
    def _receipt(
        uow: UnitOfWork, key: str, name: str, payload: NoteDocument, result: NoteDocument
    ) -> None:
        uow.command_receipts.put(
            CommandReceipt(
                idempotency_key=key,
                command_name=name,
                command_fingerprint=sha256(json_dumps(payload).encode()).hexdigest(),
                result=result,
                created_at=utc_now(),
            )
        )


if TYPE_CHECKING:
    from ehai.application.inbox import InboxKind
