"""Append-only process review history; a review is not a user approval."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from ehai import ID, JsonValue, normalize_id
from ehai.application.ports import StoredEvent
from ehai.domain.events import EventType


class ProcessReviewStatus(StrEnum):
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessReviewView:
    review_id: ID
    draft_id: ID
    run_id: ID
    reviewer_session_ref_id: ID
    status: ProcessReviewStatus
    created_at: datetime
    completed_at: datetime | None = None
    preserves_boundary: bool | None = None
    report: dict[str, JsonValue] | None = None
    error: str | None = None


def process_review_history(events: tuple[StoredEvent, ...]) -> tuple[ProcessReviewView, ...]:
    """Fold only the explicit review events, retaining request order and terminal facts."""
    views: dict[ID, ProcessReviewView] = {}
    for stored in events:
        event = stored.event
        if event.type not in {
            EventType.PROCESS_REVIEW_STARTED,
            EventType.PROCESS_REVIEW_COMPLETED,
            EventType.PROCESS_REVIEW_FAILED,
        }:
            continue
        value = event.payload
        review_id = _id(value.get("review_id"), "review_id")
        draft_id = _id(value.get("draft_id"), "draft_id")
        session_id = _id(value.get("reviewer_session_ref_id"), "reviewer_session_ref_id")
        if event.correlation_id != review_id or event.run_id is None:
            raise ValueError("Process review event has invalid Run/correlation identity")
        if event.type is EventType.PROCESS_REVIEW_STARTED:
            if review_id in views:
                raise ValueError("Process review has multiple generation requests")
            views[review_id] = ProcessReviewView(
                review_id,
                draft_id,
                event.run_id,
                session_id,
                ProcessReviewStatus.REVIEWING,
                event.occurred_at,
            )
            continue
        previous = views.get(review_id)
        if (
            previous is None
            or previous.status is not ProcessReviewStatus.REVIEWING
            or previous.draft_id != draft_id
            or previous.run_id != event.run_id
            or previous.reviewer_session_ref_id != session_id
            or event.occurred_at < previous.created_at
        ):
            raise ValueError("Process review outcome does not match an unresolved request")
        if event.type is EventType.PROCESS_REVIEW_COMPLETED:
            report = value.get("report")
            preserves = value.get("preserves_boundary")
            if not isinstance(report, dict) or type(preserves) is not bool:
                raise ValueError("Completed process review has no report/conclusion")
            views[review_id] = replace(
                previous,
                status=ProcessReviewStatus.COMPLETED,
                completed_at=event.occurred_at,
                report=report,
                preserves_boundary=preserves,
            )
        else:
            error = value.get("error")
            if not isinstance(error, str) or not error.strip():
                raise ValueError("Failed process review has no error")
            views[review_id] = replace(
                previous,
                status=ProcessReviewStatus.FAILED,
                completed_at=event.occurred_at,
                error=error,
            )
    return tuple(views.values())


def _id(value: JsonValue, name: str) -> ID:
    if not isinstance(value, str):
        raise ValueError(f"Process review {name} must be an ID")
    return normalize_id(value)
