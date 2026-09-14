"""Durable, bounded coordination for approved in-run process adjustments.

The coordinator is deliberately a thin host-side workflow.  It never edits a
PlanGraph or changes Run state directly; it invokes the existing process draft,
independent review, application, and guarded resume commands.  A persisted
``ProcessAdjustmentStarted`` event is the point at which the model budget is
consumed, and every terminal outcome is recorded against that same pause event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from ehai import ID, JsonValue, json_dumps, normalize_id, utc_now
from ehai.application.commands import ApplyProcess, ProposeProcess, ResumeRun, ReviewProcess
from ehai.application.interventions import list_interventions
from ehai.application.pause_causes import PauseCause, pause_cause_from_payload
from ehai.application.ports import StateConflictError, StoredEvent, UnitOfWork
from ehai.application.process_control import (
    ProcessControlGuard,
    latest_run_control,
    require_process_control,
    run_contract_is_current,
)
from ehai.application.process_reviews import ProcessReviewStatus
from ehai.application.sanitization import bounded_redacted_text
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckRun, CheckRunStatus
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, RunStatus
from ehai.domain.planning import PlanNodeStatus, PlanRevision
from ehai.domain.process_drafts import ProcessDraftStatus

if TYPE_CHECKING:
    from ehai.application.service import ExecutionService


UnitOfWorkFactory = Callable[[], UnitOfWork]

_POLICY_KEYS = frozenset({"max_per_goal", "model", "reasoning_effort"})
_ELIGIBLE_PAUSE_CAUSES = frozenset({PauseCause.BRANCH_EXHAUSTED, PauseCause.REVIEW_REWORK})
_CONTROL_EVENTS = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_PAUSED,
        EventType.RUN_RESUMED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
        EventType.CHECKPOINT_RESTORED,
    }
)
_TERMINAL_ADJUSTMENT_EVENTS = frozenset(
    {EventType.PROCESS_ADJUSTMENT_FINISHED, EventType.PROCESS_ADJUSTMENT_SKIPPED}
)
_CANDIDATE_KINDS = frozenset({ArtifactKind.CANDIDATE, ArtifactKind.PATCH})
_ADJUSTMENT_NAMESPACE = NAMESPACE_URL
_ADJUSTMENT_KEY_PREFIX = "ehai:process-adjustment:v1:"
_MAX_REASON_BYTES = 15_000
_MAX_FAILURE_BYTES = 2_000


@dataclass(frozen=True, slots=True)
class ProcessAdjustmentPolicy:
    """Explicit authorization for automatic Planner process adjustments."""

    max_per_goal: int
    model: str
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if type(self.max_per_goal) is not int or self.max_per_goal < 1:
            raise ValueError("max_per_goal must be a positive integer")
        object.__setattr__(self, "model", _required_text(self.model, "model"))
        if self.reasoning_effort is not None:
            object.__setattr__(
                self,
                "reasoning_effort",
                _required_text(self.reasoning_effort, "reasoning_effort"),
            )

    @classmethod
    def from_document(cls, document: Mapping[str, JsonValue]) -> ProcessAdjustmentPolicy:
        """Parse one closed policy object without filling implicit defaults."""

        if not isinstance(document, Mapping):
            raise ValueError("process_adjustment must be an object")
        keys = set(document)
        if keys != _POLICY_KEYS:
            missing = sorted(_POLICY_KEYS - keys)
            extra = sorted(keys - _POLICY_KEYS)
            raise ValueError(f"invalid process_adjustment keys; missing={missing}, extra={extra}")
        max_per_goal = document["max_per_goal"]
        if type(max_per_goal) is not int or max_per_goal < 1:
            raise ValueError("process_adjustment.max_per_goal must be a positive integer")
        model = document["model"]
        if not isinstance(model, str):
            raise ValueError("process_adjustment.model must be non-empty text")
        reasoning_effort = document["reasoning_effort"]
        if reasoning_effort is not None and not isinstance(reasoning_effort, str):
            raise ValueError("process_adjustment.reasoning_effort must be text or null")
        return cls(
            max_per_goal=max_per_goal,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def to_document(self) -> dict[str, JsonValue]:
        """Return the exact closed JSON representation used in authorization."""

        return {
            "max_per_goal": self.max_per_goal,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class ProcessAdjustmentResult:
    """Observable outcome of one automatic adjustment trigger."""

    resumed: bool
    reason: str
    trigger_event_id: ID | None = None
    draft_id: ID | None = None
    review_id: ID | None = None
    process_revision_id: ID | None = None
    outcome: str | None = None
    run_id: ID | None = None

    def __post_init__(self) -> None:
        if type(self.resumed) is not bool:
            raise TypeError("resumed must be a boolean")
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        if self.outcome is not None:
            object.__setattr__(self, "outcome", _required_text(self.outcome, "outcome"))
        for name in (
            "trigger_event_id",
            "draft_id",
            "review_id",
            "process_revision_id",
            "run_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, normalize_id(value))

    def to_document(self) -> dict[str, JsonValue]:
        """Return a detached, query-friendly result document."""

        return {
            "resumed": self.resumed,
            "reason": self.reason,
            "trigger_event_id": self.trigger_event_id,
            "draft_id": self.draft_id,
            "review_id": self.review_id,
            "process_revision_id": self.process_revision_id,
            "outcome": self.outcome,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class _PreparedAdjustment:
    run_id: ID
    goal_id: ID
    trigger_event_id: ID
    parent_process_revision_id: ID
    cause: PauseCause
    reason: str
    draft_id: ID | None = None
    review_id: ID | None = None
    process_revision_id: ID | None = None


class ProcessAdjustments:
    """Coordinate one authorized automatic adjustment at a time per Run.

    ``advance`` starts a child task for the model workflow.  A caller can cancel
    that child through :meth:`cancel` without cancelling the surrounding Runtime
    loop.  A second ``advance`` for the same Run joins the existing child and
    cannot replace it.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        service: ExecutionService,
        planner_model: str,
        planner_reasoning_effort: str | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise TypeError("uow_factory must be callable")
        self._uow_factory = uow_factory
        self._service = service
        self._planner_model = _required_text(planner_model, "planner_model")
        self._planner_reasoning_effort = (
            None
            if planner_reasoning_effort is None
            else _required_text(planner_reasoning_effort, "planner_reasoning_effort")
        )
        self._event_cursor: ID | None = None
        self._pending_pauses: dict[ID, ID] = {}
        self._terminal_triggers: set[ID] = set()
        self._active_tasks: dict[ID, asyncio.Task[ProcessAdjustmentResult]] = {}
        self._active_contexts: dict[ID, _PreparedAdjustment] = {}

    def pending_run_ids(self) -> tuple[ID, ...]:
        """Return eligible paused Runs discovered since the last Event cursor.

        The cursor is process-local and intentionally reset on a new coordinator;
        a restart therefore replays the append-only log.  Durable adjustment
        events still prevent another model call after that replay.
        """

        with self._uow_factory() as uow:
            events = uow.events.list_events(after_event_id=self._event_cursor)
        self._consume_events(events)
        return tuple(
            run_id
            for run_id in sorted(self._pending_pauses)
            if self._pending_pauses[run_id] not in self._terminal_triggers
        )

    async def advance(self, run_id: ID) -> ProcessAdjustmentResult:
        """Advance one eligible trigger, joining an existing child if present."""

        normalized = normalize_id(run_id)
        task = self._active_tasks.get(normalized)
        if task is None or task.done():
            task = asyncio.create_task(
                self._advance_child(normalized),
                name=f"ehai-process-adjustment-{normalized}",
            )
            self._active_tasks[normalized] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            # A child cancelled before its first step cannot catch cancellation
            # itself. This is a Run-local stop, not a Runtime shutdown.
            return self._result(
                normalized,
                "automatic process adjustment cancelled before it started",
                outcome="cancelled",
            )
        finally:
            if task.done() and self._active_tasks.get(normalized) is task:
                self._active_tasks.pop(normalized, None)

    async def cancel(self, run_id: ID) -> None:
        """Cancel only the active adjustment child for ``run_id``."""

        normalized = normalize_id(run_id)
        self._pending_pauses.pop(normalized, None)
        task = self._active_tasks.get(normalized)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))

    async def _advance_child(self, run_id: ID) -> ProcessAdjustmentResult:
        try:
            return await self._advance_impl(run_id)
        except asyncio.CancelledError:
            context = self._active_contexts.get(run_id)
            if context is None:
                return ProcessAdjustmentResult(
                    resumed=False,
                    reason="automatic process adjustment cancelled before it started",
                    outcome="cancelled",
                    run_id=run_id,
                )
            return self._finish(
                context,
                outcome="cancelled",
                reason="automatic process adjustment cancelled",
                draft_id=context.draft_id,
                review_id=context.review_id,
                process_revision_id=context.process_revision_id,
            )
        finally:
            self._active_contexts.pop(run_id, None)

    async def _advance_impl(self, run_id: ID) -> ProcessAdjustmentResult:
        prepared = self._prepare(run_id)
        if isinstance(prepared, ProcessAdjustmentResult):
            return prepared
        self._active_contexts[run_id] = prepared
        propose_key = _idempotency_key(prepared.trigger_event_id, "propose")
        try:
            draft = await self._service.propose_process_async(
                ProposeProcess(propose_key, run_id, prepared.reason)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._finish(
                prepared,
                outcome="draft_failed",
                reason=f"automatic process draft failed: {_error_text(error)}",
            )
        draft_id = draft.draft_id
        prepared = replace(prepared, draft_id=draft_id)
        self._active_contexts[run_id] = prepared
        if draft.status is not ProcessDraftStatus.READY:
            return self._finish(
                prepared,
                outcome="draft_failed",
                reason=(
                    "automatic process draft did not complete: "
                    + _error_text(draft.error or "unknown draft outcome")
                ),
                draft_id=draft_id,
            )

        hold_reason = self._recheck(prepared)
        if hold_reason is not None:
            return self._finish(
                prepared,
                outcome="control_changed" if hold_reason.startswith("control") else "held",
                reason=hold_reason,
                draft_id=draft_id,
            )

        try:
            review = await self._service.review_process_async(
                ReviewProcess(_idempotency_key(prepared.trigger_event_id, "review"), draft_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._finish(
                prepared,
                outcome="review_failed",
                reason=f"automatic process review failed: {_error_text(error)}",
                draft_id=draft_id,
            )
        review_id = review.review_id
        prepared = replace(prepared, review_id=review_id)
        self._active_contexts[run_id] = prepared
        if review.status is not ProcessReviewStatus.COMPLETED:
            return self._finish(
                prepared,
                outcome="review_failed",
                reason="automatic process review did not complete",
                draft_id=draft_id,
                review_id=review_id,
            )
        if review.preserves_boundary is not True:
            return self._finish(
                prepared,
                outcome="review_rejected",
                reason="independent process review did not preserve the approved boundary",
                draft_id=draft_id,
                review_id=review_id,
            )

        hold_reason = self._recheck(prepared)
        if hold_reason is not None:
            return self._finish(
                prepared,
                outcome="control_changed" if hold_reason.startswith("control") else "held",
                reason=hold_reason,
                draft_id=draft_id,
                review_id=review_id,
            )

        guard = ProcessControlGuard(
            run_id=run_id,
            pause_event_id=prepared.trigger_event_id,
            process_revision_id=prepared.parent_process_revision_id,
        )
        try:
            process = self._service.apply_process(
                ApplyProcess(
                    _idempotency_key(prepared.trigger_event_id, "apply"),
                    review_id,
                ),
                control_guard=guard,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._finish(
                prepared,
                outcome="apply_failed",
                reason=f"automatic process application failed: {_error_text(error)}",
                draft_id=draft_id,
                review_id=review_id,
            )
        process_revision_id = process.process_revision_id
        prepared = replace(prepared, process_revision_id=process_revision_id)
        self._active_contexts[run_id] = prepared
        if process_revision_id == prepared.parent_process_revision_id:
            return self._finish(
                prepared,
                outcome="apply_failed",
                reason="automatic process application returned no new process revision",
                draft_id=draft_id,
                review_id=review_id,
                process_revision_id=process_revision_id,
            )

        try:
            resumed = self._service.resume_run(
                ResumeRun(
                    _idempotency_key(prepared.trigger_event_id, "resume"),
                    run_id,
                ),
                control_guard=replace(guard, process_revision_id=process_revision_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._finish(
                prepared,
                outcome="resume_failed",
                reason=f"automatic process resume failed: {_error_text(error)}",
                draft_id=draft_id,
                review_id=review_id,
                process_revision_id=process_revision_id,
            )
        actually_resumed = resumed.status is RunStatus.RUNNING
        return self._finish(
            prepared,
            outcome="resumed" if actually_resumed else "resume_not_running",
            reason=(
                "automatic process adjustment applied and Run resumed"
                if actually_resumed
                else "automatic process adjustment applied but Run did not remain running"
            ),
            draft_id=draft_id,
            review_id=review_id,
            process_revision_id=process_revision_id,
            resumed=actually_resumed,
        )

    def _prepare(self, run_id: ID) -> ProcessAdjustmentResult | _PreparedAdjustment:
        with self._uow_factory() as uow:
            run = uow.states.get_run(run_id)
            if run is None:
                return self._result(run_id, "Run is not persisted", outcome="held")
            if run.status is not RunStatus.PAUSED:
                return self._result(run_id, "Run is not paused", outcome="held")
            control = latest_run_control(uow, run_id)
            if control is None or control.event.type is not EventType.RUN_PAUSED:
                return self._result(
                    run_id,
                    "control is not an eligible latest pause",
                    outcome="held",
                )
            cause = pause_cause_from_payload(control.event.payload)
            if cause not in _ELIGIBLE_PAUSE_CAUSES:
                return self._result(
                    run_id,
                    f"pause cause {cause.value!r} is not eligible for automatic adjustment",
                    outcome="held",
                )
            trigger_event_id = control.event.id
            terminal = _terminal_event(uow.events.list_events(), trigger_event_id)
            if terminal is not None:
                return _result_from_terminal(terminal, run_id)
            if _trigger_started(uow.events.list_events(), trigger_event_id):
                return self._result(
                    run_id,
                    "this automatic process adjustment trigger already started",
                    trigger_event_id=trigger_event_id,
                    outcome="held",
                )
            process = uow.states.get_active_process_revision(run_id)
            if process is None:
                return self._result(
                    run_id,
                    "Run has no active process revision",
                    trigger_event_id=trigger_event_id,
                    outcome="held",
                )
            goal = uow.states.get_goal(run.goal_id)
            if goal is None:
                return self._result(
                    run_id,
                    "Run Goal is unavailable",
                    trigger_event_id=trigger_event_id,
                    outcome="held",
                )
            plan = uow.states.get_execution_plan(run_id)
            if plan is None:
                return self._result(
                    run_id,
                    "Run has no current execution plan",
                    trigger_event_id=trigger_event_id,
                    outcome="held",
                )
            if not run_contract_is_current(uow, run, plan):
                return self._skip_in_uow(
                    uow,
                    run_id,
                    run.goal_id,
                    trigger_event_id,
                    process.process_revision_id,
                    "Run no longer matches the Goal's current CompletionContract",
                )
            authorization = _authorized_adjustment_policy(uow.events.list_events(), run_id)
            if isinstance(authorization, str):
                return self._skip_in_uow(
                    uow,
                    run_id,
                    run.goal_id,
                    trigger_event_id,
                    process.process_revision_id,
                    authorization,
                )
            policy = authorization
            if (
                policy.model != self._planner_model
                or policy.reasoning_effort != self._planner_reasoning_effort
            ):
                return self._skip_in_uow(
                    uow,
                    run_id,
                    run.goal_id,
                    trigger_event_id,
                    process.process_revision_id,
                    "process adjustment policy does not match the configured Planner",
                )
            starts = _goal_adjustment_starts(uow.events.list_events(), run.goal_id)
            if starts >= policy.max_per_goal:
                return self._skip_in_uow(
                    uow,
                    run_id,
                    run.goal_id,
                    trigger_event_id,
                    process.process_revision_id,
                    "process adjustment authorization budget is exhausted for this Goal",
                )
            attempts = uow.states.list_attempts(run_id)
            checks = uow.states.list_check_runs(run_id)
            artifacts = uow.states.list_artifacts_for_run(run_id)
            hold_reason = _safety_hold_reason(
                uow,
                run_id,
                plan,
                attempts,
                checks,
                artifacts,
                process_revision_id=process.process_revision_id,
            )
            if hold_reason is not None:
                if not any(
                    attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
                    for attempt in attempts
                ) and not any(
                    check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                    for check in checks
                ):
                    return self._skip_in_uow(
                        uow,
                        run_id,
                        run.goal_id,
                        trigger_event_id,
                        process.process_revision_id,
                        hold_reason,
                    )
                return self._result(
                    run_id,
                    hold_reason,
                    trigger_event_id=trigger_event_id,
                    outcome="held",
                )
            reason = _adjustment_reason(
                run_id,
                trigger_event_id,
                cause,
                control.event.payload,
                plan,
                attempts,
                checks,
                artifacts,
            )
            prepared = _PreparedAdjustment(
                run_id=run_id,
                goal_id=run.goal_id,
                trigger_event_id=trigger_event_id,
                parent_process_revision_id=process.process_revision_id,
                cause=cause,
                reason=reason,
            )
            uow.events.append(
                Event(
                    type=EventType.PROCESS_ADJUSTMENT_STARTED,
                    run_id=run_id,
                    correlation_id=trigger_event_id,
                    payload={
                        "goal_id": run.goal_id,
                        "run_id": run_id,
                        "trigger_event_id": trigger_event_id,
                        "parent_process_revision_id": process.process_revision_id,
                        "pause_cause": cause.value,
                        "reason": reason,
                        "outcome": "started",
                    },
                    occurred_at=utc_now(),
                )
            )
            uow.commit()
            return prepared

    def _recheck(self, prepared: _PreparedAdjustment) -> str | None:
        with self._uow_factory() as uow:
            try:
                require_process_control(
                    uow,
                    prepared.run_id,
                    ProcessControlGuard(
                        run_id=prepared.run_id,
                        pause_event_id=prepared.trigger_event_id,
                        process_revision_id=prepared.parent_process_revision_id,
                    ),
                )
            except StateConflictError as error:
                return f"control changed: {error}"
            plan = uow.states.get_execution_plan(prepared.run_id)
            if plan is None:
                return "current execution plan is unavailable"
            hold_reason = _safety_hold_reason(
                uow,
                prepared.run_id,
                plan,
                uow.states.list_attempts(prepared.run_id),
                uow.states.list_check_runs(prepared.run_id),
                uow.states.list_artifacts_for_run(prepared.run_id),
                process_revision_id=prepared.parent_process_revision_id,
            )
            if hold_reason is not None:
                return hold_reason
            return None

    def _skip_in_uow(
        self,
        uow: UnitOfWork,
        run_id: ID,
        goal_id: ID,
        trigger_event_id: ID,
        parent_process_revision_id: ID,
        reason: str,
    ) -> ProcessAdjustmentResult:
        terminal = _terminal_event(uow.events.list_events(), trigger_event_id)
        if terminal is not None:
            return _result_from_terminal(terminal, run_id)
        uow.events.append(
            Event(
                type=EventType.PROCESS_ADJUSTMENT_SKIPPED,
                run_id=run_id,
                correlation_id=trigger_event_id,
                payload={
                    "goal_id": goal_id,
                    "run_id": run_id,
                    "trigger_event_id": trigger_event_id,
                    "parent_process_revision_id": parent_process_revision_id,
                    "reason": _bounded(reason),
                    "outcome": "skipped",
                    "resumed": False,
                },
                occurred_at=utc_now(),
            )
        )
        uow.commit()
        self._terminal_triggers.add(trigger_event_id)
        return self._result(
            run_id,
            reason,
            trigger_event_id=trigger_event_id,
            outcome="skipped",
        )

    def _finish(
        self,
        prepared: _PreparedAdjustment,
        *,
        outcome: str,
        reason: str,
        draft_id: ID | None = None,
        review_id: ID | None = None,
        process_revision_id: ID | None = None,
        resumed: bool = False,
    ) -> ProcessAdjustmentResult:
        with self._uow_factory() as uow:
            terminal = _terminal_event(uow.events.list_events(), prepared.trigger_event_id)
            if terminal is not None:
                return _result_from_terminal(terminal, prepared.run_id)
            payload: dict[str, JsonValue] = {
                "goal_id": prepared.goal_id,
                "run_id": prepared.run_id,
                "trigger_event_id": prepared.trigger_event_id,
                "parent_process_revision_id": prepared.parent_process_revision_id,
                "reason": _bounded(reason),
                "outcome": outcome,
                "resumed": resumed,
                "draft_id": draft_id,
                "review_id": review_id,
                "process_revision_id": process_revision_id,
            }
            uow.events.append(
                Event(
                    type=EventType.PROCESS_ADJUSTMENT_FINISHED,
                    run_id=prepared.run_id,
                    correlation_id=prepared.trigger_event_id,
                    payload=payload,
                    occurred_at=utc_now(),
                )
            )
            uow.commit()
        self._terminal_triggers.add(prepared.trigger_event_id)
        return self._result(
            prepared.run_id,
            reason,
            trigger_event_id=prepared.trigger_event_id,
            draft_id=draft_id,
            review_id=review_id,
            process_revision_id=process_revision_id,
            outcome=outcome,
            resumed=resumed,
        )

    def _result(
        self,
        run_id: ID,
        reason: str,
        *,
        trigger_event_id: ID | None = None,
        draft_id: ID | None = None,
        review_id: ID | None = None,
        process_revision_id: ID | None = None,
        outcome: str | None = None,
        resumed: bool = False,
    ) -> ProcessAdjustmentResult:
        return ProcessAdjustmentResult(
            resumed=resumed,
            reason=reason,
            trigger_event_id=trigger_event_id,
            draft_id=draft_id,
            review_id=review_id,
            process_revision_id=process_revision_id,
            outcome=outcome,
            run_id=run_id,
        )

    def _consume_events(self, events: Iterable[StoredEvent]) -> None:
        last_event_id: ID | None = None
        for stored in events:
            last_event_id = stored.event.id
            event = stored.event
            run_id = event.run_id
            if run_id is None:
                continue
            payload = event.payload
            if event.type is EventType.RUN_PAUSED:
                cause = pause_cause_from_payload(payload)
                if cause in _ELIGIBLE_PAUSE_CAUSES:
                    self._pending_pauses[run_id] = event.id
                else:
                    self._pending_pauses.pop(run_id, None)
            elif event.type in _CONTROL_EVENTS:
                self._pending_pauses.pop(run_id, None)
            elif event.type is EventType.PROCESS_ADJUSTMENT_STARTED:
                trigger = _optional_id(payload.get("trigger_event_id"))
                if trigger is not None and self._pending_pauses.get(run_id) == trigger:
                    self._pending_pauses.pop(run_id, None)
            elif event.type in _TERMINAL_ADJUSTMENT_EVENTS:
                trigger = _optional_id(payload.get("trigger_event_id"))
                if trigger is not None:
                    self._terminal_triggers.add(trigger)
                    if self._pending_pauses.get(run_id) == trigger:
                        self._pending_pauses.pop(run_id, None)
        if last_event_id is not None:
            self._event_cursor = last_event_id


def _authorized_adjustment_policy(
    events: Iterable[StoredEvent], run_id: ID
) -> ProcessAdjustmentPolicy | str:
    for stored in events:
        event = stored.event
        if event.run_id != run_id or event.type is not EventType.RUN_STARTED:
            continue
        payload = event.payload
        authorization = payload.get("authorization")
        config = payload.get("execution_config")
        if not isinstance(authorization, dict) or authorization.get("explicit") is not True:
            continue
        if not isinstance(config, dict):
            return "Run has no explicit execution configuration"
        document = config.get("process_adjustment")
        if document is None:
            return "automatic process adjustment is not explicitly authorized"
        if not isinstance(document, Mapping):
            return "automatic process adjustment policy is invalid"
        try:
            return ProcessAdjustmentPolicy.from_document(document)
        except (TypeError, ValueError):
            return "automatic process adjustment policy is invalid"
    return "Run has no explicit execution authorization"


def _goal_adjustment_starts(events: Iterable[StoredEvent], goal_id: ID) -> int:
    return sum(
        1
        for stored in events
        if stored.event.type is EventType.PROCESS_ADJUSTMENT_STARTED
        and stored.event.payload.get("goal_id") == goal_id
    )


def _terminal_event(events: Iterable[StoredEvent], trigger_event_id: ID) -> StoredEvent | None:
    found: StoredEvent | None = None
    for stored in events:
        if stored.event.type not in _TERMINAL_ADJUSTMENT_EVENTS:
            continue
        if _optional_id(stored.event.payload.get("trigger_event_id")) != trigger_event_id:
            continue
        if found is not None:
            raise ValueError(
                "automatic process adjustment trigger "
                f"{trigger_event_id} has multiple terminal facts"
            )
        found = stored
    return found


def _trigger_started(events: Iterable[StoredEvent], trigger_event_id: ID) -> bool:
    return any(
        stored.event.type is EventType.PROCESS_ADJUSTMENT_STARTED
        and stored.event.payload.get("trigger_event_id") == trigger_event_id
        for stored in events
    )


def _safety_hold_reason(
    uow: UnitOfWork,
    run_id: ID,
    plan: PlanRevision,
    attempts: tuple[Attempt, ...],
    checks: tuple[CheckRun, ...],
    artifacts: tuple[Artifact, ...],
    *,
    process_revision_id: ID | None,
) -> str | None:
    if any(
        attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING} for attempt in attempts
    ):
        return "Attempts have not converged; automatic process adjustment is held"
    blocked = tuple(
        node.plan_node_id for node in plan.nodes if node.status is PlanNodeStatus.BLOCKED
    )
    if blocked:
        return f"blocked PlanNodes require intervention before automatic adjustment: {blocked}"
    pending_checks = tuple(
        check.check_run_id
        for check in checks
        if check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
    )
    if pending_checks:
        return f"Checks are still pending or running: {pending_checks}"
    try:
        interventions = list_interventions(uow.events, run_id)
    except ValueError:
        return "intervention state could not be verified; automatic adjustment is held"
    open_interventions = tuple(
        item.get("intervention_id") for item in interventions if item.get("status") == "open"
    )
    if open_interventions:
        return f"unresolved interventions require user input: {open_interventions}"
    failed_nodes = tuple(node for node in plan.nodes if node.status is PlanNodeStatus.FAILED)
    attempts_by_node: dict[ID, tuple[Attempt, ...]] = {}
    for attempt in attempts:
        attempts_by_node.setdefault(attempt.plan_node_id, ())
        attempts_by_node[attempt.plan_node_id] = (*attempts_by_node[attempt.plan_node_id], attempt)
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    for node in failed_nodes:
        node_attempts = attempts_by_node.get(node.plan_node_id, ())
        latest = node_attempts[-1] if node_attempts else None
        if latest is None or latest.status is not AttemptStatus.SUCCEEDED:
            return (
                f"failed PlanNode {node.plan_node_id} has no latest succeeded Attempt evidence; "
                "automatic adjustment is held"
            )
        if process_revision_id is not None and latest.process_revision_id != process_revision_id:
            return (
                f"latest succeeded Attempt {latest.attempt_id} for failed PlanNode "
                f"{node.plan_node_id} belongs to another process revision; "
                "automatic adjustment is held"
            )
        if not any(
            artifact_id in artifacts_by_id
            and artifacts_by_id[artifact_id].kind in _CANDIDATE_KINDS
            and artifacts_by_id[artifact_id].run_id == latest.run_id
            and artifacts_by_id[artifact_id].plan_node_id == latest.plan_node_id
            and artifacts_by_id[artifact_id].attempt_id == latest.attempt_id
            for artifact_id in latest.artifact_ids
        ):
            return (
                f"latest succeeded Attempt {latest.attempt_id} for failed PlanNode "
                f"{node.plan_node_id} has no candidate evidence; automatic adjustment is held"
            )
    return None


def _adjustment_reason(
    run_id: ID,
    trigger_event_id: ID,
    cause: PauseCause,
    pause_payload: Mapping[str, JsonValue],
    plan: PlanRevision,
    attempts: tuple[Attempt, ...],
    checks: tuple[CheckRun, ...],
    artifacts: tuple[Artifact, ...],
) -> str:
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    attempts_by_node: dict[ID, tuple[Attempt, ...]] = {}
    for attempt in attempts:
        attempts_by_node.setdefault(attempt.plan_node_id, ())
        attempts_by_node[attempt.plan_node_id] = (*attempts_by_node[attempt.plan_node_id], attempt)
    failed_nodes: list[JsonValue] = []
    for node in plan.nodes:
        if node.status is not PlanNodeStatus.FAILED:
            continue
        node_attempts = attempts_by_node.get(node.plan_node_id, ())
        latest = node_attempts[-1] if node_attempts else None
        evidence = (
            ()
            if latest is None
            else tuple(
                artifact_id
                for artifact_id in latest.artifact_ids
                if (
                    artifact_id in artifacts_by_id
                    and artifacts_by_id[artifact_id].kind in _CANDIDATE_KINDS
                )
            )
        )
        failed_nodes.append(
            {
                "plan_node_id": node.plan_node_id,
                "title": _bounded(node.title),
                "latest_succeeded_attempt_id": None if latest is None else latest.attempt_id,
                "candidate_artifact_ids": list(evidence),
            }
        )
    failed_checks: list[JsonValue] = []
    for check in checks:
        result = check.result
        if not (
            (result is not None and result.passed is False)
            or check.status
            in {
                CheckRunStatus.FAILED,
                CheckRunStatus.TIMED_OUT,
                CheckRunStatus.INTERRUPTED,
            }
        ):
            continue
        failed_checks.append(
            {
                "check_run_id": check.check_run_id,
                "check_id": check.check_id,
                "plan_node_id": check.plan_node_id,
                "attempt_id": check.attempt_id,
                "status": check.status.value,
                "reason": _bounded(
                    (None if result is None else result.failure_reason) or check.failure_reason,
                    max_bytes=_MAX_FAILURE_BYTES,
                ),
                "output": _bounded(
                    None if result is None else result.output,
                    max_bytes=_MAX_FAILURE_BYTES,
                ),
            }
        )
    source_reason = pause_payload.get("reason")
    if not isinstance(source_reason, str) or not source_reason.strip():
        source_reason = f"Run paused with cause {cause.value}"
    document: dict[str, JsonValue] = {
        "automatic_process_adjustment": True,
        "run_id": run_id,
        "trigger_event_id": trigger_event_id,
        "pause_cause": cause.value,
        "pause_reason": _bounded(source_reason),
        "failed_nodes": failed_nodes,
        "failed_checks": failed_checks,
    }
    encoded = json_dumps(document)
    bounded = bounded_redacted_text(encoded, max_bytes=_MAX_REASON_BYTES)
    return bounded or "automatic process adjustment requested from retained execution evidence"


def _result_from_terminal(stored: StoredEvent, run_id: ID) -> ProcessAdjustmentResult:
    payload = stored.event.payload
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "automatic process adjustment already has a durable terminal outcome"
    outcome = payload.get("outcome")
    if not isinstance(outcome, str):
        outcome = stored.event.type.value
    return ProcessAdjustmentResult(
        resumed=False,
        reason=reason,
        trigger_event_id=_optional_id(payload.get("trigger_event_id")),
        draft_id=_optional_id(payload.get("draft_id")),
        review_id=_optional_id(payload.get("review_id")),
        process_revision_id=_optional_id(payload.get("process_revision_id")),
        outcome=outcome,
        run_id=run_id,
    )


def _idempotency_key(trigger_event_id: ID, operation: str) -> str:
    name = f"{_ADJUSTMENT_KEY_PREFIX}{trigger_event_id}:{operation}"
    # UUID keys keep the application receipt compact while retaining a stable
    # namespace derived from the immutable trigger and operation name.
    return str(uuid5(_ADJUSTMENT_NAMESPACE, name))


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _bounded(value: str | None, *, max_bytes: int = _MAX_FAILURE_BYTES) -> str | None:
    return bounded_redacted_text(value, max_bytes=max_bytes)


def _error_text(value: object) -> str:
    if isinstance(value, BaseException):
        value = f"{type(value).__name__}: {value}"
    text = _bounded(str(value))
    return text or "unknown error"


def _optional_id(value: object) -> ID | None:
    if not isinstance(value, str):
        return None
    try:
        return normalize_id(value)
    except ValueError:
        return None


__all__ = [
    "ProcessAdjustmentPolicy",
    "ProcessAdjustmentResult",
    "ProcessAdjustments",
]
