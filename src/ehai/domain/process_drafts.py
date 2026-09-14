"""Durable Planner drafts, separate from the sequence of applied process versions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from ehai import ID, normalize_id
from ehai.domain.planning import PlanRevision, PlanRevisionStatus
from ehai.domain.process import ProcessRevision, ProcessRevisionSource, process_approval_identity


class ProcessDraftStatus(StrEnum):
    PLANNING = "planning"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessDraft:
    """One immutable request baseline and its single, retained generation outcome.

    READY means generated, not reviewed or eligible for application. A stale
    baseline remains readable but cannot silently be rebased onto another Run
    state. The candidate is not registered in the applied process history.
    """

    draft_id: ID
    run_id: ID
    parent_process_revision_id: ID
    planner_session_ref_id: ID
    base_execution_plan: PlanRevision
    reason: str
    created_at: datetime
    status: ProcessDraftStatus = ProcessDraftStatus.PLANNING
    candidate: ProcessRevision | None = None
    error: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("draft_id", "run_id", "parent_process_revision_id", "planner_session_ref_id"):
            object.__setattr__(self, name, normalize_id(getattr(self, name)))
        object.__setattr__(self, "status", ProcessDraftStatus(self.status))
        if not isinstance(self.base_execution_plan, PlanRevision) or (
            self.base_execution_plan.status is not PlanRevisionStatus.APPROVED
        ):
            raise ValueError("Process draft must retain its approved execution baseline")
        if (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or len(self.reason.encode("utf-8")) > 16_000
        ):
            raise ValueError("Process draft reason must contain 1-16000 UTF-8 bytes")
        object.__setattr__(self, "reason", self.reason.strip())
        for name in ("created_at", "completed_at"):
            value = getattr(self, name)
            if name == "completed_at" and value is None:
                continue
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"Process draft {name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("Process draft completion cannot precede its request")
        if self.status is ProcessDraftStatus.PLANNING:
            if any(value is not None for value in (self.candidate, self.error, self.completed_at)):
                raise ValueError("An unresolved process draft cannot carry an outcome")
        elif self.status is ProcessDraftStatus.READY:
            if self.candidate is None or self.error is not None or self.completed_at is None:
                raise ValueError("A ready process draft requires exactly one completed candidate")
            candidate = self.candidate
            if (
                candidate.run_id != self.run_id
                or candidate.parent_process_revision_id != self.parent_process_revision_id
                or candidate.source is not ProcessRevisionSource.PLANNER_ADJUSTMENT
                or process_approval_identity(candidate.graph)
                != process_approval_identity(self.base_execution_plan)
                or candidate.reason != self.reason
                or candidate.created_at < self.created_at
                or candidate.created_at > self.completed_at
            ):
                raise ValueError("Process candidate does not belong to this draft request")
        elif (
            self.candidate is not None
            or not isinstance(self.error, str)
            or not self.error.strip()
            or len(self.error.encode("utf-8")) > 16_000
            or self.completed_at is None
        ):
            raise ValueError("A failed process draft requires a bounded error and completion time")

    def finish(self, candidate: ProcessRevision, *, at: datetime) -> ProcessDraft:
        self._require_planning()
        return replace(self, status=ProcessDraftStatus.READY, candidate=candidate, completed_at=at)

    def fail(self, error: str, *, at: datetime) -> ProcessDraft:
        self._require_planning()
        return replace(self, status=ProcessDraftStatus.FAILED, error=error, completed_at=at)

    def _require_planning(self) -> None:
        if self.status is not ProcessDraftStatus.PLANNING:
            raise ValueError("A completed process draft cannot be rewritten")


def process_draft_identity(draft: ProcessDraft) -> tuple[object, ...]:
    return (
        draft.draft_id,
        draft.run_id,
        draft.parent_process_revision_id,
        draft.planner_session_ref_id,
        draft.base_execution_plan,
        draft.reason,
        draft.created_at,
    )
