"""Note transport and explicit dispatch into existing planning/execution commands."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Literal, cast

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ehai import ID, JsonValue, json_dumps, normalize_id
from ehai.application.commands import (
    DecideHumanCheck,
    DiscussPlan,
    ProposeProcess,
    ReplyIntervention,
)
from ehai.application.notes import NoteDocument, NoteService
from ehai.application.ports import StateConflictError
from ehai.application.runtime_control import RuntimeControlService
from ehai.application.service import ExecutionService
from ehai.domain.execution import RunStatus
from ehai.domain.process_drafts import ProcessDraftStatus
from ehai.interfaces.http_models import DataResponse, NonBlank, UuidInput
from ehai.interfaces.public_documents import public_json_value


class CreateNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: NonBlank
    goal_id: UuidInput
    actor: NonBlank
    question: str = Field(min_length=1, max_length=8000)
    evidence: str = Field(default="", max_length=8000)
    run_id: UuidInput | None = None
    source_kind: Literal["intervention", "human_check"] | None = None
    source_id: UuidInput | None = None
    source_token: str | None = None
    origin: Literal["user", "agent"] = "user"


class AddNoteMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: NonBlank
    request_token: NonBlank
    actor: NonBlank
    message: str = Field(min_length=1, max_length=8000)


class DecideNoteRequest(AddNoteMessageRequest):
    action: Literal["resolve", "continue", "propose_process", "revise_plan"]
    passed: bool | None = None


def _note_planning_message(note: NoteDocument, message: str) -> str:
    context: dict[str, JsonValue] = {
        "note_id": note["note_id"],
        "question": note["question"],
        "evidence": note["evidence"],
        "messages": note["messages"],
        "user_decision": message,
    }
    text = (
        "Discuss this explicit note decision and propose a draft; do not approve it.\n"
        + json_dumps(context)
    )
    if len(text) > 8000:
        raise ValueError(
            "Note discussion exceeds the planning input limit; create a concise follow-up note"
        )
    return text


def build_notes_router(
    notes: NoteService,
    execution: ExecutionService,
    runtime_control: RuntimeControlService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/notes", tags=["notes"])

    async def execute(
        note: NoteDocument, decision: NoteDocument, operation_key: str
    ) -> NoteDocument:
        source = cast(NoteDocument, note["source"])
        action = decision["action"]
        actor, message = str(decision["actor"]), str(decision["message"])
        if action == "continue":
            source_id = normalize_id(str(source["source_id"]))
            token = str(source["source_token"])
            if source["source_kind"] == "intervention":
                run = execution.reply_intervention(
                    ReplyIntervention(operation_key, source_id, token, actor, message)
                )
            else:
                passed = decision["passed"]
                if not isinstance(passed, bool):
                    raise ValueError("Human Check decision requires explicit passed")
                run = execution.decide_human_check(
                    DecideHumanCheck(operation_key, source_id, token, passed, actor, message)
                )
            if runtime_control is not None and run.status is RunStatus.RUNNING:
                runtime_control.resume_run_scheduling(run.run_id)
            return {
                "run_id": run.run_id,
                "run_status": run.status.value,
                "meaning": "Source decision recorded; inspect the Run for remaining blockers",
            }
        if action == "propose_process":
            draft = await execution.propose_process_async(
                ProposeProcess(
                    operation_key,
                    normalize_id(str(source["run_id"])),
                    _note_planning_message(note, message),
                )
            )
            if draft.status is not ProcessDraftStatus.READY:
                raise StateConflictError(
                    f"Process draft {draft.draft_id} is {draft.status.value}; inspect its outcome"
                )
            return {
                "draft_id": draft.draft_id,
                "status": draft.status.value,
                "meaning": "Process proposal requires the existing review/apply flow",
            }
        if action == "revise_plan":
            conversation = await asyncio.to_thread(
                execution.discuss_plan,
                DiscussPlan(
                    operation_key,
                    normalize_id(str(source["goal_id"])),
                    _note_planning_message(note, message),
                    source_run_id=None
                    if source["run_id"] is None
                    else normalize_id(str(source["run_id"])),
                ),
            )
            return {
                "conversation_id": conversation.conversation_id,
                "discussion": public_json_value(conversation),
                "meaning": "Discussion/draft recorded; approval and authorization remain separate",
            }
        raise ValueError("Unsupported note decision action")

    @router.post(
        "",
        response_model=DataResponse,
        operation_id="createNote",
        responses=_responses("NoteResponse"),
    )
    def create_note(body: CreateNoteRequest) -> DataResponse:
        return DataResponse(data=notes.create(**body.model_dump()))

    @router.get(
        "",
        response_model=DataResponse,
        operation_id="listNotes",
        responses=_responses("NoteListResponse"),
    )
    def list_notes(
        project_id: UuidInput | None = None,
        goal_id: UuidInput | None = None,
        run_id: UuidInput | None = None,
        include_resolved: bool = False,
    ) -> DataResponse:
        return DataResponse(
            data=notes.list(
                project_id=None if project_id is None else normalize_id(project_id),
                goal_id=None if goal_id is None else normalize_id(goal_id),
                run_id=None if run_id is None else normalize_id(run_id),
                include_resolved=include_resolved,
            )
        )

    @router.get(
        "/{note_id}",
        response_model=DataResponse,
        operation_id="getNote",
        responses=_responses("NoteResponse"),
    )
    def get_note(note_id: UuidInput) -> DataResponse:
        return DataResponse(data=notes.get(normalize_id(note_id)))

    @router.post(
        "/{note_id}/messages",
        response_model=DataResponse,
        operation_id="addNoteMessage",
        responses=_responses("NoteResponse"),
    )
    def add_note_message(note_id: UuidInput, body: AddNoteMessageRequest) -> DataResponse:
        return DataResponse(data=notes.message(note_id=normalize_id(note_id), **body.model_dump()))

    @router.post(
        "/{note_id}/decisions",
        response_model=DataResponse,
        operation_id="decideNote",
        responses=_responses("NoteResponse"),
    )
    async def decide_note(note_id: UuidInput, body: DecideNoteRequest) -> DataResponse:
        # Reject an obviously unusable context before reserving a durable decision intent.
        note = notes.get(normalize_id(note_id))
        source = cast(NoteDocument, note["source"])
        if (
            note["status"] == "open"
            and body.action == "revise_plan"
            and source["run_id"] is not None
            and execution.get_run(cast(ID, source["run_id"])).status is not RunStatus.PAUSED
        ):
            raise StateConflictError("Pause the source Run explicitly before requesting a revision")
        if note["status"] == "open" and body.action in {"revise_plan", "propose_process"}:
            _note_planning_message(note, body.message)
        # Reserve before NoteDecisionStarted. The model-facing service call borrows
        # this lease through ContextVar propagation, including asyncio.to_thread.
        admission = (
            execution.planner_capacity.slot()
            if note["status"] == "open" and body.action in {"revise_plan", "propose_process"}
            else nullcontext()
        )
        with admission:
            return DataResponse(
                data=await notes.decide(
                    note_id=normalize_id(note_id), executor=execute, **body.model_dump()
                )
            )

    return router


def _responses(name: str) -> dict[int | str, dict[str, object]]:
    return {
        200: {
            "description": name,
            "content": {
                "application/json": {"schema": {"$ref": f"notes.schema.json#/$defs/{name}"}}
            },
        }
    }
