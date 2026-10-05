"""Draft, review and apply process revisions within an approved boundary."""

from __future__ import annotations

import inspect
from collections.abc import Mapping

from ehai import ID, JsonValue
from ehai.application.commands import (
    ApplyProcess,
    ProposeProcess,
    ReviewProcess,
)
from ehai.application.execution_service.common import (
    ApplicationError,
    EntityNotFoundError,
    _PreparedProcessDraft,
    _PreparedProcessReview,
    _required_execution_plan,
    _required_goal,
    _required_plan,
    _required_run,
    _result_id,
)
from ehai.application.execution_service.host import ExecutionServiceHost
from ehai.application.interventions import process_intervention_context
from ehai.application.planner import (
    AsyncProcessPlanner,
    AsyncProcessReviewer,
    ProcessPlanner,
    ProcessReviewer,
)
from ehai.application.planner_capacity import (
    async_planning_operation,
    planning_operation,
)
from ehai.application.ports import UnitOfWork
from ehai.application.process_control import (
    ProcessControlGuard,
    require_process_control,
)
from ehai.application.process_outputs import completed_output_context
from ehai.application.process_review import (
    ProcessBoundaryReport,
    approved_source_documents,
    parse_process_boundary_report,
    process_boundary_report_document,
)
from ehai.application.process_review_context import (
    load_process_review_context,
)
from ehai.application.process_reviews import (
    ProcessReviewStatus,
    ProcessReviewView,
    process_review_history,
)
from ehai.application.sanitization import bounded_redacted_text
from ehai.domain.events import Event, EventType
from ehai.domain.execution import RunStatus
from ehai.domain.process import ProcessRevision
from ehai.domain.process_drafts import ProcessDraft, ProcessDraftStatus


class ProcessCommandsMixin:
    """Draft, review and apply process revisions within an approved boundary."""

    @planning_operation
    def propose_process(self: ExecutionServiceHost, command: ProposeProcess) -> ProcessDraft:
        """Retain a single generation attempt; never publish the proposed process."""
        prepared = self._prepare_process_draft(command, asynchronous=False)
        if isinstance(prepared, ProcessDraft):
            return prepared
        if not isinstance(self._planner, ProcessPlanner):  # pragma: no cover - checked at prepare
            raise ApplicationError("Configured Planner does not support process drafts")
        try:
            candidate = self._planner.propose_process(
                prepared.goal,
                prepared.approved,
                prepared.previous,
                prepared.current,
                prepared.checks,
                prepared.draft.reason,
                session_ref_id=prepared.draft.planner_session_ref_id,
                intervention_context=prepared.intervention_context,
                completed_outputs=prepared.completed_outputs,
            )
            return self._complete_process_draft(prepared, candidate)
        except BaseException as error:
            retained = self._fail_process_draft(prepared, error)
            if not isinstance(error, Exception):
                raise
            return retained

    @async_planning_operation
    async def propose_process_async(
        self: ExecutionServiceHost, command: ProposeProcess
    ) -> ProcessDraft:
        """Retain one asynchronously cancellable process-draft generation attempt."""
        prepared = self._prepare_process_draft(command, asynchronous=True)
        if isinstance(prepared, ProcessDraft):
            return prepared
        if not isinstance(self._planner, AsyncProcessPlanner):  # pragma: no cover
            raise ApplicationError("Configured Planner does not support async process drafts")
        try:
            candidate = await self._planner.propose_process_async(
                prepared.goal,
                prepared.approved,
                prepared.previous,
                prepared.current,
                prepared.checks,
                prepared.draft.reason,
                session_ref_id=prepared.draft.planner_session_ref_id,
                intervention_context=prepared.intervention_context,
                completed_outputs=prepared.completed_outputs,
            )
            return self._complete_process_draft(prepared, candidate)
        except BaseException as error:
            retained = self._fail_process_draft(prepared, error)
            if not isinstance(error, Exception):
                raise
            return retained

    def _prepare_process_draft(
        self: ExecutionServiceHost, command: ProposeProcess, *, asynchronous: bool
    ) -> ProcessDraft | _PreparedProcessDraft:
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                existing = uow.states.get_process_draft(_result_id(receipt, "draft_id"))
                if existing is None:
                    raise ApplicationError("Process draft receipt has no retained draft")
                return existing
            if asynchronous:
                if not _has_async_process_planner(self._planner):
                    raise ApplicationError(
                        "Configured Planner does not support async process drafts"
                    )
            elif not isinstance(self._planner, ProcessPlanner):
                raise ApplicationError("Configured Planner does not support process drafts")
            run = _required_run(uow, command.run_id)
            if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                raise ApplicationError("Process drafting requires a nonterminal started Run")
            goal = _required_goal(uow, run.goal_id)
            approved = _required_plan(uow, run.plan_revision_id)
            current = _required_execution_plan(uow, run.run_id)
            previous = uow.states.get_active_process_revision(run.run_id)
            if previous is None:
                raise ApplicationError("Run has no active process baseline")
            checks = uow.states.list_check_specs(approved.plan_revision_id)
            draft = ProcessDraft(
                draft_id=self._id_factory(),
                run_id=run.run_id,
                parent_process_revision_id=previous.process_revision_id,
                planner_session_ref_id=self._id_factory(),
                base_execution_plan=current,
                reason=command.reason,
                created_at=self._clock(),
            )
            uow.states.put_process_draft(draft)
            self._append_process_draft_event(uow, draft, EventType.PROCESS_DRAFT_STARTED)
            intervention_context = process_intervention_context(uow, draft)
            completed_outputs = completed_output_context(uow, run.run_id, current, checks)
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"draft_id": draft.draft_id},
            )
            uow.commit()
            return _PreparedProcessDraft(
                draft=draft,
                goal=goal,
                approved=approved,
                previous=previous,
                current=current,
                checks=checks,
                intervention_context=intervention_context,
                completed_outputs=completed_outputs,
            )

    def _complete_process_draft(
        self: ExecutionServiceHost, prepared: _PreparedProcessDraft, candidate: ProcessRevision
    ) -> ProcessDraft:
        completed = prepared.draft.finish(candidate, at=self._clock())
        with self._uow_factory() as uow:
            uow.states.put_process_draft(completed)
            self._append_process_draft_event(uow, completed, EventType.PROCESS_DRAFT_COMPLETED)
            uow.commit()
        return completed

    def _fail_process_draft(
        self: ExecutionServiceHost, prepared: _PreparedProcessDraft, error: BaseException
    ) -> ProcessDraft:
        message = bounded_redacted_text(f"{type(error).__name__}: {error}", max_bytes=16_000)
        assert message is not None
        with self._uow_factory() as uow:
            retained = uow.states.get_process_draft(prepared.draft.draft_id)
            if retained is None:
                raise ApplicationError("Process generation lost its durable request") from error
            if retained.status is ProcessDraftStatus.PLANNING:
                retained = retained.fail(message, at=self._clock())
                uow.states.put_process_draft(retained)
                self._append_process_draft_event(uow, retained, EventType.PROCESS_DRAFT_FAILED)
                uow.commit()
            return retained

    def _append_process_draft_event(
        self: ExecutionServiceHost, uow: UnitOfWork, draft: ProcessDraft, event_type: EventType
    ) -> None:
        uow.events.append(
            Event(
                type=event_type,
                run_id=draft.run_id,
                correlation_id=draft.draft_id,
                payload={
                    "draft_id": draft.draft_id,
                    "parent_process_revision_id": draft.parent_process_revision_id,
                    "planner_session_ref_id": draft.planner_session_ref_id,
                    "status": draft.status.value,
                    "candidate_process_revision_id": (
                        None if draft.candidate is None else draft.candidate.process_revision_id
                    ),
                    "error": draft.error,
                },
                occurred_at=self._clock(),
            )
        )

    def apply_process(
        self: ExecutionServiceHost,
        command: ApplyProcess,
        *,
        control_guard: ProcessControlGuard | None = None,
    ) -> ProcessRevision:
        """Publish one reviewed candidate atomically, leaving the drained Run paused."""
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                process_id = _result_id(receipt, "process_revision_id")
                process = uow.states.get_process_revision(process_id)
                if process is None:
                    raise ApplicationError("Process application receipt has no retained version")
                return process
            review = _required_process_review(uow, command.review_id)
            if control_guard is not None:
                require_process_control(uow, review.run_id, control_guard)
            if (
                review.status is not ProcessReviewStatus.COMPLETED
                or not review.preserves_boundary
                or review.report is None
            ):
                raise ApplicationError("Only a completed preserving review can support application")
            context = load_process_review_context(uow, review.draft_id)
            if context.run.run_id != review.run_id or (
                review.reviewer_session_ref_id == context.draft.planner_session_ref_id
            ):
                raise ApplicationError("Review identity does not belong to this independent draft")
            if context.run.status is not RunStatus.PAUSED:
                raise ApplicationError("Pause and drain the Run before applying a process change")
            report = parse_process_boundary_report(review.report, context)
            if not report.preserves_boundary or report.obligation_mapping is None:
                raise ApplicationError(
                    "Review no longer establishes the original approval boundary"
                )
            candidate = context.draft.candidate
            assert candidate is not None
            uow.states.publish_process_revision(
                candidate,
                expected_current=context.draft.base_execution_plan,
                obligation_mapping=report.obligation_mapping,
                source_documents=approved_source_documents(context),
            )
            uow.events.append(
                Event(
                    type=EventType.PROCESS_REVISION_APPLIED,
                    run_id=context.run.run_id,
                    correlation_id=candidate.process_revision_id,
                    payload={
                        "process_revision_id": candidate.process_revision_id,
                        "parent_process_revision_id": context.previous.process_revision_id,
                        "draft_id": context.draft.draft_id,
                        "review_id": review.review_id,
                        "reason": candidate.reason,
                    },
                    occurred_at=self._clock(),
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"process_revision_id": candidate.process_revision_id},
            )
            uow.commit()
            return candidate

    @planning_operation
    def review_process(self: ExecutionServiceHost, command: ReviewProcess) -> ProcessReviewView:
        """Retain the independent model review; its conclusion does not apply the candidate."""
        prepared = self._prepare_process_review(command, asynchronous=False)
        if isinstance(prepared, ProcessReviewView):
            return prepared
        if not isinstance(self._planner, ProcessReviewer):  # pragma: no cover - checked at prepare
            raise ApplicationError("Configured Planner cannot independently review processes")
        try:
            report = self._planner.review_process(
                prepared.context, session_ref_id=prepared.session_id
            )
            return self._complete_process_review(prepared, report)
        except BaseException as error:
            result = self._fail_process_review(prepared, error)
            if not isinstance(error, Exception):
                raise
            return result

    @async_planning_operation
    async def review_process_async(
        self: ExecutionServiceHost, command: ReviewProcess
    ) -> ProcessReviewView:
        """Retain one independently reviewable result from a cancellable async model call."""
        prepared = self._prepare_process_review(command, asynchronous=True)
        if isinstance(prepared, ProcessReviewView):
            return prepared
        if not isinstance(self._planner, AsyncProcessReviewer):  # pragma: no cover
            raise ApplicationError("Configured Planner does not support async process reviews")
        try:
            report = await self._planner.review_process_async(
                prepared.context, session_ref_id=prepared.session_id
            )
            return self._complete_process_review(prepared, report)
        except BaseException as error:
            result = self._fail_process_review(prepared, error)
            if not isinstance(error, Exception):
                raise
            return result

    def _prepare_process_review(
        self: ExecutionServiceHost, command: ReviewProcess, *, asynchronous: bool
    ) -> ProcessReviewView | _PreparedProcessReview:
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                return _required_process_review(uow, _result_id(receipt, "review_id"))
            if asynchronous:
                if not _has_async_process_reviewer(self._planner):
                    raise ApplicationError(
                        "Configured Planner does not support async process reviews"
                    )
            elif not isinstance(self._planner, ProcessReviewer):
                raise ApplicationError("Configured Planner cannot independently review processes")
            context = load_process_review_context(uow, command.draft_id)
            review_id = self._id_factory()
            session_id = self._id_factory()
            self._append_process_review_event(
                uow, review_id, context.draft, session_id, EventType.PROCESS_REVIEW_STARTED, {}
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"review_id": review_id},
            )
            uow.commit()
            return _PreparedProcessReview(context, review_id, session_id)

    def _complete_process_review(
        self: ExecutionServiceHost, prepared: _PreparedProcessReview, report: ProcessBoundaryReport
    ) -> ProcessReviewView:
        document = process_boundary_report_document(report)
        validated = parse_process_boundary_report(document, prepared.context)
        with self._uow_factory() as uow:
            self._append_process_review_event(
                uow,
                prepared.review_id,
                prepared.context.draft,
                prepared.session_id,
                EventType.PROCESS_REVIEW_COMPLETED,
                {"report": document, "preserves_boundary": validated.preserves_boundary},
            )
            result = _required_process_review(uow, prepared.review_id)
            uow.commit()
            return result

    def _fail_process_review(
        self: ExecutionServiceHost, prepared: _PreparedProcessReview, error: BaseException
    ) -> ProcessReviewView:
        message = bounded_redacted_text(f"{type(error).__name__}: {error}", max_bytes=16_000)
        assert message is not None
        with self._uow_factory() as uow:
            result = _required_process_review(uow, prepared.review_id)
            if result.status is ProcessReviewStatus.REVIEWING:
                self._append_process_review_event(
                    uow,
                    prepared.review_id,
                    prepared.context.draft,
                    prepared.session_id,
                    EventType.PROCESS_REVIEW_FAILED,
                    {"error": message},
                )
                result = _required_process_review(uow, prepared.review_id)
                uow.commit()
            return result

    def _append_process_review_event(
        self: ExecutionServiceHost,
        uow: UnitOfWork,
        review_id: ID,
        draft: ProcessDraft,
        session_id: ID,
        event_type: EventType,
        outcome: Mapping[str, JsonValue],
    ) -> None:
        uow.events.append(
            Event(
                type=event_type,
                run_id=draft.run_id,
                correlation_id=review_id,
                payload={
                    "review_id": review_id,
                    "draft_id": draft.draft_id,
                    "reviewer_session_ref_id": session_id,
                    **outcome,
                },
                occurred_at=self._clock(),
            )
        )


def _required_process_review(uow: UnitOfWork, review_id: ID) -> ProcessReviewView:
    result = next(
        (
            view
            for view in process_review_history(uow.events.list_events())
            if view.review_id == review_id
        ),
        None,
    )
    if result is None:
        raise EntityNotFoundError(f"Process review {review_id} is not retained")
    return result


def _has_async_process_planner(value: object) -> bool:
    """Require both the async port and a coroutine function, never a sync fallback."""
    return isinstance(value, AsyncProcessPlanner) and inspect.iscoroutinefunction(
        getattr(value, "propose_process_async", None)
    )


def _has_async_process_reviewer(value: object) -> bool:
    """Require a real coroutine Reviewer entry point."""
    return isinstance(value, AsyncProcessReviewer) and inspect.iscoroutinefunction(
        getattr(value, "review_process_async", None)
    )
