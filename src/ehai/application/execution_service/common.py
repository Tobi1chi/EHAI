"""Errors, prepared-command types and lookups shared by the ExecutionService responsibilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256

from ehai import ID, JsonValue, normalize_id
from ehai.application.interventions import list_interventions
from ehai.application.planner import (
    MAX_REPLAN_ATTEMPT_SUMMARIES,
    MAX_REPLAN_CHECK_SUMMARIES,
    MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS,
    MAX_REPLAN_INTERVENTION_SUMMARIES,
    ReplanAttemptSummary,
    ReplanCheckpointSummary,
    ReplanCheckSummary,
    ReplanContext,
    ReplanInterventionSummary,
)
from ehai.application.ports import UnitOfWork
from ehai.application.process_control import (
    latest_run_control,
)
from ehai.application.process_review_context import (
    ProcessReviewContext,
)
from ehai.application.sanitization import bounded_redacted_text
from ehai.domain.checking import CheckKind, CheckRun, CheckRunStatus, CheckSpec
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import PlanNodeStatus, PlanRevision, PlanRevisionStatus
from ehai.domain.process import ProcessRevision
from ehai.domain.process_drafts import ProcessDraft

UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class _PreparedProcessDraft:
    """Durable process-draft request state captured before the model call."""

    draft: ProcessDraft
    goal: Goal
    approved: PlanRevision
    previous: ProcessRevision
    current: PlanRevision
    checks: tuple[CheckSpec, ...]
    intervention_context: tuple[dict[str, JsonValue], ...]
    completed_outputs: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _PreparedProcessReview:
    """Durable independent-review request state captured before the model call."""

    context: ProcessReviewContext
    review_id: ID
    session_id: ID


@dataclass(frozen=True, slots=True)
class _DiscussionSource:
    """Immutable source facts captured before a source-bound Planner call."""

    run: Run
    pause_event_id: ID
    approved_plan: PlanRevision
    execution_plan: PlanRevision
    process_revision: ProcessRevision
    context: ReplanContext
    interventions: tuple[dict[str, JsonValue], ...]


class ApplicationError(RuntimeError):
    """Base error for P1 application use cases."""


class EntityNotFoundError(ApplicationError):
    """Raised when a Command references a missing persisted entity."""


class IdempotencyConflictError(ApplicationError):
    """Raised before side effects when an idempotency key changes meaning."""


def _required_contract_for_plan(uow: UnitOfWork, plan: PlanRevision) -> CompletionContract:
    contract = _required_contract(uow, plan.completion_contract_id)
    if contract.goal_id != plan.goal_id or contract.version != plan.completion_contract_version:
        raise ApplicationError("Plan and CompletionContract identities do not match")
    return contract


def _validate_source_discussion_run(uow: UnitOfWork, goal: Goal, run: Run) -> PlanRevision:
    if run.goal_id != goal.goal_id or run.status is not RunStatus.PAUSED:
        raise ApplicationError("Discussion source must be a paused Run of this Goal")
    approved = _required_plan(uow, run.plan_revision_id)
    contract = _required_contract_for_plan(uow, approved)
    if (
        approved.status is not PlanRevisionStatus.APPROVED
        or not contract.is_confirmed
        or goal.completion_contract != contract
    ):
        raise ApplicationError("Source Run no longer owns the Goal's effective approval")
    return approved


def _require_no_other_active_runs(uow: UnitOfWork, goal_id: ID, source_run_id: ID) -> None:
    for run in uow.states.list_runs(goal_id):
        if run.run_id == source_run_id:
            continue
        human_check_ids = {
            spec.check_id
            for spec in uow.states.list_check_specs(run.plan_revision_id)
            if spec.kind is CheckKind.HUMAN
        }
        if (
            run.status in {RunStatus.PENDING, RunStatus.RUNNING}
            or any(
                attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
                for attempt in uow.states.list_attempts(run.run_id)
            )
            or any(
                check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                and check.check_id not in human_check_ids
                for check in uow.states.list_check_runs(run.run_id)
            )
        ):
            raise ApplicationError("Stop other active Goal execution before source discussion")


def _source_discussion_base(uow: UnitOfWork, approved: PlanRevision) -> PlanRevision:
    plans = uow.states.list_plan_revisions(approved.goal_id)
    return plans[-1] if plans else approved


def _capture_discussion_source(
    uow: UnitOfWork, *, source_run: Run, approved_plan: PlanRevision
) -> _DiscussionSource:
    control = latest_run_control(uow, source_run.run_id)
    if control is None or control.event.type is not EventType.RUN_PAUSED:
        raise ApplicationError("Source discussion requires a retained pause control event")
    process = uow.states.get_active_process_revision(source_run.run_id)
    if process is None:
        raise ApplicationError("Source Run has no active process revision")
    return _DiscussionSource(
        run=source_run,
        pause_event_id=control.event.id,
        approved_plan=approved_plan,
        execution_plan=_required_execution_plan(uow, source_run.run_id),
        process_revision=process,
        context=_build_replan_context(uow, source_run=source_run, base=approved_plan),
        interventions=list_interventions(uow.events, source_run.run_id),
    )


def _config_fingerprint(config_json: str) -> str:
    return sha256(config_json.encode("utf-8")).hexdigest()


def _result_id(result: Mapping[str, JsonValue], key: str) -> ID:
    value = result.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"stored Command receipt has no {key}")
    return normalize_id(value)


def _required_goal(uow: UnitOfWork, goal_id: ID) -> Goal:
    goal = uow.states.get_goal(goal_id)
    if goal is None:
        raise EntityNotFoundError(f"Goal {goal_id} does not exist")
    return goal


def _required_contract(uow: UnitOfWork, contract_id: ID) -> CompletionContract:
    contract = uow.states.get_completion_contract(contract_id)
    if contract is None:
        raise EntityNotFoundError(f"CompletionContract {contract_id} does not exist")
    return contract


def _required_plan(uow: UnitOfWork, plan_revision_id: ID) -> PlanRevision:
    plan = uow.states.get_plan_revision(plan_revision_id)
    if plan is None:
        raise EntityNotFoundError(f"PlanRevision {plan_revision_id} does not exist")
    return plan


def _required_run(uow: UnitOfWork, run_id: ID) -> Run:
    run = uow.states.get_run(run_id)
    if run is None:
        raise EntityNotFoundError(f"Run {run_id} does not exist")
    return run


def _required_execution_plan(uow: UnitOfWork, run_id: ID) -> PlanRevision:
    plan = uow.states.get_execution_plan(run_id)
    if plan is None:
        raise EntityNotFoundError(f"Run {run_id} has no execution plan")
    return plan


def _build_replan_context(
    uow: UnitOfWork,
    *,
    source_run: Run,
    base: PlanRevision,
) -> ReplanContext:
    if source_run.plan_revision_id != base.plan_revision_id or source_run.goal_id != base.goal_id:
        raise ApplicationError(
            f"Run {source_run.run_id} does not belong to base PlanRevision {base.plan_revision_id}"
        )
    approved_design_document = base.design_document
    approved_checks = uow.states.list_check_specs(base.plan_revision_id)
    base = _required_execution_plan(uow, source_run.run_id)
    if source_run.status not in {RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ApplicationError(
            f"Run {source_run.run_id} must be paused, failed or cancelled to guide replanning"
        )

    all_attempts = uow.states.list_attempts(source_run.run_id)
    if any(item.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING} for item in all_attempts):
        raise ApplicationError("Replanning requires the source Run's execution to be stopped")
    check_specs = {
        item.check_id: item for item in uow.states.list_check_specs(base.plan_revision_id)
    }
    if any(
        item.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
        and (
            item.check_id not in check_specs
            or check_specs[item.check_id].kind is not CheckKind.HUMAN
        )
        for item in uow.states.list_check_runs(source_run.run_id)
    ):
        raise ApplicationError("Stop active automatic Checks before replanning")
    suspended_node_ids = tuple(
        node.plan_node_id for node in base.nodes if node.status is PlanNodeStatus.SUSPENDED
    )
    stalled_node_ids = tuple(
        node.plan_node_id for node in base.nodes if node.status is PlanNodeStatus.STALLED
    )
    notices = list_interventions(uow.events, source_run.run_id)
    attempts_by_id = {attempt.attempt_id: attempt for attempt in all_attempts}
    intervention_summaries: list[ReplanInterventionSummary] = []
    for notice in notices[-MAX_REPLAN_INTERVENTION_SUMMARIES:]:
        source_attempt = attempts_by_id.get(_result_id(notice, "attempt_id"))
        if source_attempt is None or source_attempt.plan_node_id != _result_id(
            notice, "plan_node_id"
        ):
            raise ApplicationError("Replan intervention has no matching source Attempt")
        reply = notice.get("reply")
        reply_text = reply.get("message") if isinstance(reply, dict) else None

        def summary_text(key: str, document: Mapping[str, JsonValue] = notice) -> str | None:
            value = document.get(key)
            return bounded_redacted_text(value if isinstance(value, str) else None, max_bytes=2000)

        intervention_summaries.append(
            ReplanInterventionSummary(
                intervention_id=_result_id(notice, "intervention_id"),
                plan_node_id=_result_id(notice, "plan_node_id"),
                attempt_id=source_attempt.attempt_id,
                source_process_revision_id=source_attempt.process_revision_id,
                kind=str(notice["kind"]),
                status=str(notice.get("status")),
                reason=summary_text("reason"),
                evidence=summary_text("evidence"),
                needed=summary_text("needed"),
                reply=bounded_redacted_text(
                    reply_text if isinstance(reply_text, str) else None, max_bytes=2000
                ),
            )
        )
    attempts = tuple(
        ReplanAttemptSummary(
            attempt_id=attempt.attempt_id,
            plan_node_id=attempt.plan_node_id,
            sequence=attempt.sequence,
            status=attempt.status,
            reason=bounded_redacted_text(attempt.outcome_reason or attempt.queue_reason),
        )
        for attempt in all_attempts[-MAX_REPLAN_ATTEMPT_SUMMARIES:]
    )
    failed_check_runs = tuple(
        check_run
        for check_run in uow.states.list_check_runs(source_run.run_id)
        if _check_failed(check_run)
    )
    failed_checks = tuple(
        ReplanCheckSummary(
            check_run_id=check_run.check_run_id,
            check_id=check_run.check_id,
            plan_node_id=check_run.plan_node_id,
            attempt_id=check_run.attempt_id,
            status=check_run.status,
            passed=None if check_run.result is None else check_run.result.passed,
            reason=bounded_redacted_text(_check_failure_reason(check_run)),
        )
        for check_run in failed_check_runs[-MAX_REPLAN_CHECK_SUMMARIES:]
    )
    failed_node_ids = {
        node.plan_node_id for node in base.nodes if node.status is PlanNodeStatus.FAILED
    }
    failed_node_ids.update(
        attempt.plan_node_id
        for attempt in all_attempts
        if attempt.status
        in {
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.INTERRUPTED,
        }
    )
    failed_node_ids.update(check_run.plan_node_id for check_run in failed_check_runs)
    failed_node_ids.difference_update((*suspended_node_ids, *stalled_node_ids))
    ordered_failed_node_ids = tuple(
        node.plan_node_id for node in base.nodes if node.plan_node_id in failed_node_ids
    )
    checkpoints = uow.states.list_checkpoints(source_run.run_id)
    latest = checkpoints[-1] if checkpoints else None
    checkpoint = (
        None
        if latest is None
        else ReplanCheckpointSummary(
            checkpoint_id=latest.checkpoint_id,
            event_offset=latest.event_offset,
            artifact_ids=latest.artifact_refs[-MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS:],
        )
    )
    return ReplanContext(
        source_run_id=source_run.run_id,
        source_run_status=source_run.status,
        source_run_reason=bounded_redacted_text(source_run.status_reason),
        failed_plan_node_ids=ordered_failed_node_ids,
        attempts=attempts,
        failed_checks=failed_checks,
        consumed_attempt_count=len(all_attempts),
        latest_checkpoint=checkpoint,
        stalled_plan_node_ids=stalled_node_ids,
        suspended_plan_node_ids=suspended_node_ids,
        interventions=tuple(intervention_summaries),
        intervention_count=len(notices),
        approved_checks=approved_checks,
        approved_design_document=approved_design_document,
    )


def _check_failed(check_run: CheckRun) -> bool:
    if check_run.status is CheckRunStatus.COMPLETED:
        return check_run.result is not None and not check_run.result.passed
    return check_run.status in {
        CheckRunStatus.FAILED,
        CheckRunStatus.TIMED_OUT,
        CheckRunStatus.CANCELLED,
        CheckRunStatus.INTERRUPTED,
    }


def _check_failure_reason(check_run: CheckRun) -> str | None:
    if check_run.result is not None and not check_run.result.passed:
        return check_run.result.failure_reason
    return check_run.failure_reason
