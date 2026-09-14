"""Public discussion views projected from the existing durable Event Log."""

from dataclasses import dataclass
from typing import Literal

from ehai import ID, JsonValue, normalize_id
from ehai.application.ports import StoredEvent
from ehai.domain.events import EventType


@dataclass(frozen=True, slots=True)
class PlanningTurnView:
    turn_id: ID
    message: str
    status: Literal["running", "completed", "failed"]
    reply: str | None = None
    plan_revision_id: ID | None = None
    agent_session_ref_id: ID | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningConversationView:
    conversation_id: ID
    goal_id: ID
    workspace: str | None
    turns: tuple[PlanningTurnView, ...]
    source_run_id: ID | None = None


def planning_conversation(
    events: tuple[StoredEvent, ...], conversation_id: ID
) -> PlanningConversationView | None:
    relevant = tuple(item.event for item in events if item.event.correlation_id == conversation_id)
    started = tuple(item for item in relevant if item.type is EventType.PLANNING_TURN_STARTED)
    if not started:
        return None
    source_ids: list[ID] = []
    for event in started:
        value = event.payload.get("source_run_id")
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("Planning event source_run_id must be a string")
        try:
            source_ids.append(normalize_id(value))
        except ValueError as error:
            raise ValueError("Planning event source_run_id is invalid") from error
    if len(set(source_ids)) > 1:
        raise ValueError("Planning conversation cannot be bound to multiple source Runs")
    terminal = {
        _text(item.payload, "turn_id"): item
        for item in relevant
        if item.type in {EventType.PLANNING_TURN_COMPLETED, EventType.PLANNING_TURN_FAILED}
    }
    turns: list[PlanningTurnView] = []
    for event in started:
        turn_id = _text(event.payload, "turn_id")
        outcome = terminal.get(turn_id)
        payload = {} if outcome is None else outcome.payload
        status: Literal["running", "completed", "failed"] = (
            "running"
            if outcome is None
            else "completed"
            if outcome.type is EventType.PLANNING_TURN_COMPLETED
            else "failed"
        )
        plan_id = _optional_text(payload, "plan_revision_id")
        session_id = _optional_text(payload, "agent_session_ref_id")
        started_session_id = _optional_text(event.payload, "agent_session_ref_id")
        if started_session_id is not None:
            if session_id is not None and session_id != started_session_id:
                raise ValueError("Planning outcome does not match its started Session identity")
            session_id = started_session_id
        turns.append(
            PlanningTurnView(
                ID(turn_id),
                _text(event.payload, "message"),
                status,
                _optional_text(payload, "reply"),
                None if plan_id is None else ID(plan_id),
                None if session_id is None else ID(session_id),
                _optional_text(payload, "error"),
            )
        )
    return PlanningConversationView(
        conversation_id,
        ID(_text(started[0].payload, "goal_id")),
        _optional_text(started[0].payload, "workspace"),
        tuple(turns),
        source_ids[0] if source_ids else None,
    )


def _text(document: dict[str, JsonValue], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise ValueError(f"Planning event has no string {name}")
    return value


def _optional_text(document: dict[str, JsonValue], name: str) -> str | None:
    value = document.get(name)
    if value is None:
        return None
    return _text(document, name)
