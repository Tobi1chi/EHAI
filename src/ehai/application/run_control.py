"""Pause, resume, and cancel controls for a serial P1 Run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ehai import ID, JsonValue, normalize_id, utc_now
from ehai.application.pause_causes import (
    PauseCause,
    pause_event_payload,
    require_pause_cause,
)
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.process_control import (
    ProcessControlGuard,
    require_process_control,
    run_contract_is_current,
)
from ehai.application.workers import WorkerAdapter
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus

UnitOfWorkFactory = Callable[[], UnitOfWork]


class RunControlError(RuntimeError):
    """Base error for Run control operations."""


class RunControlConflictError(RunControlError):
    """Raised when persisted state changes during an external cancellation."""


class WorkerCancellationError(RunControlError):
    """Raised when a Worker cannot cancel the active Attempt."""


@dataclass(frozen=True, slots=True)
class _ControlSnapshot:
    run: Run
    plan: PlanRevision
    attempts: tuple[Attempt, ...]
    active_attempt: Attempt | None


class RunController:
    """Coordinate Run controls without holding a transaction across Worker calls."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        worker: WorkerAdapter,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._worker = worker
        self._clock = clock

    def pause(
        self,
        run_id: ID,
        *,
        receipt: CommandReceipt | None = None,
        pause_cause: PauseCause = PauseCause.OPERATOR,
    ) -> Run:
        """Pause a running Run after cancelling its active Worker Attempt."""
        cause = require_pause_cause(pause_cause)
        if receipt is not None and cause is not PauseCause.OPERATOR:
            raise ValueError("a command receipt can only record an operator pause")
        snapshot = self._read_snapshot(run_id)
        if snapshot.run.status is RunStatus.PAUSED:
            with self._uow_factory() as uow:
                current = self._read_snapshot_from(uow, snapshot.run.run_id)
                self._require_unchanged(snapshot, current)
                if receipt is not None:
                    _append_pause_event(uow, current.run, cause, at=self._clock())
                    uow.command_receipts.put(receipt)
                    uow.commit()
                return current.run
        snapshot.run.pause()  # Validate before the external call.
        self._cancel_worker(snapshot)
        at = self._clock()

        with self._uow_factory() as uow:
            current = self._read_snapshot_from(uow, snapshot.run.run_id)
            if self._is_settled_control(snapshot, current, RunStatus.PAUSED):
                if receipt is not None:
                    _append_pause_event(uow, current.run, cause, at=at)
                self._close_settled_control(uow, receipt)
                return current.run
            self._require_unchanged(snapshot, current)
            plan = current.plan
            active = current.active_attempt
            if active is not None:
                cancelled = active.cancel("Run paused", at=at)
                node = _required_node(plan, active.plan_node_id)
                failed_node = node.fail()
                plan = _replace_node(plan, failed_node)
                uow.states.put_attempt(cancelled)
                uow.states.put_execution_plan(current.run.run_id, plan)
                uow.events.append(
                    _event(
                        EventType.ATTEMPT_CANCELLED,
                        current.run,
                        active.attempt_id,
                        {"attempt_id": active.attempt_id, "reason": "Run paused"},
                        at,
                    )
                )
                uow.events.append(
                    _event(
                        EventType.PLAN_NODE_FAILED,
                        current.run,
                        node.plan_node_id,
                        {"plan_node_id": node.plan_node_id, "reason": "Run paused"},
                        at,
                    )
                )

            paused = current.run.pause()
            uow.states.put_run(paused)
            _append_pause_event(uow, paused, cause, at=at)
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return paused

    def resume(
        self,
        run_id: ID,
        *,
        receipt: CommandReceipt | None = None,
        control_guard: ProcessControlGuard | None = None,
    ) -> Run:
        """Resume a paused Run and make its retryable work schedulable."""
        normalized_id = normalize_id(run_id)
        at = self._clock()
        with self._uow_factory() as uow:
            if control_guard is not None:
                require_process_control(uow, normalized_id, control_guard)
            snapshot = self._read_snapshot_from(uow, normalized_id)
            current_node_ids = {node.plan_node_id for node in snapshot.plan.nodes}
            # Process replacement preserves history, including removed task Attempts.
            current_attempts = tuple(
                attempt for attempt in snapshot.attempts if attempt.plan_node_id in current_node_ids
            )
            if current_attempts:
                latest_attempt = current_attempts[-1]
                latest_node = _required_node(snapshot.plan, latest_attempt.plan_node_id)
                if (
                    latest_attempt.status is AttemptStatus.SUCCEEDED
                    and latest_node.status is PlanNodeStatus.FAILED
                ):
                    raise RunControlError(
                        f"run {snapshot.run.run_id} cannot resume after succeeded Attempt "
                        f"{latest_attempt.attempt_id} left PlanNode {latest_node.plan_node_id} "
                        "failed; restore a Checkpoint or recover checks manually"
                    )
            resumed = snapshot.run.resume()
            if not run_contract_is_current(uow, snapshot.run, snapshot.plan):
                raise RunControlError(
                    f"run {snapshot.run.run_id} cannot resume because its PlanRevision no "
                    "longer matches the Goal's current confirmed CompletionContract"
                )
            plan = snapshot.plan
            retry_attempt = _last_retryable_attempt(current_attempts)
            if retry_attempt is not None:
                node = _required_node(plan, retry_attempt.plan_node_id)
                if node.status is PlanNodeStatus.FAILED:
                    ready_node = node.retry()
                    plan = _replace_node(plan, ready_node)
                    uow.states.put_execution_plan(resumed.run_id, plan)
                    uow.events.append(
                        _event(
                            EventType.PLAN_NODE_READIED,
                            resumed,
                            ready_node.plan_node_id,
                            {"plan_node_id": ready_node.plan_node_id},
                            at,
                        )
                    )
                # Restore rewinds PlanGraph state but retains post-Checkpoint Attempts.
                elif not (
                    retry_attempt.status is AttemptStatus.INTERRUPTED
                    and node.status is PlanNodeStatus.PENDING
                ):
                    raise RunControlError(
                        f"run {snapshot.run.run_id} cannot resume Attempt "
                        f"{retry_attempt.attempt_id}: PlanNode {node.plan_node_id} is "
                        f"{node.status.value}, not failed or restored pending"
                    )

            uow.states.put_run(resumed)
            uow.events.append(
                _event(
                    EventType.RUN_RESUMED,
                    resumed,
                    resumed.run_id,
                    {"run_id": resumed.run_id},
                    at,
                )
            )
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return resumed

    def cancel(
        self,
        run_id: ID,
        reason: str | None = None,
        *,
        receipt: CommandReceipt | None = None,
    ) -> Run:
        """Cancel a pending, running, or paused Run without fabricating completion."""
        snapshot = self._read_snapshot(run_id)
        cancellation_reason = reason or "Run cancelled"
        snapshot.run.cancel(cancellation_reason, at=self._clock())  # Validate first.
        self._cancel_worker(snapshot)
        at = self._clock()

        with self._uow_factory() as uow:
            current = self._read_snapshot_from(uow, snapshot.run.run_id)
            if self._is_settled_control(
                snapshot,
                current,
                RunStatus.CANCELLED,
                reason=cancellation_reason,
            ):
                self._close_settled_control(uow, receipt)
                return current.run
            self._require_unchanged(snapshot, current)
            plan = current.plan
            active = current.active_attempt
            if active is not None:
                cancelled_attempt = active.cancel(cancellation_reason, at=at)
                node = _required_node(plan, active.plan_node_id)
                failed_node = node.fail()
                plan = _replace_node(plan, failed_node)
                uow.states.put_attempt(cancelled_attempt)
                uow.states.put_execution_plan(current.run.run_id, plan)
                uow.events.append(
                    _event(
                        EventType.ATTEMPT_CANCELLED,
                        current.run,
                        active.attempt_id,
                        {
                            "attempt_id": active.attempt_id,
                            "reason": cancellation_reason,
                        },
                        at,
                    )
                )
                uow.events.append(
                    _event(
                        EventType.PLAN_NODE_FAILED,
                        current.run,
                        node.plan_node_id,
                        {
                            "plan_node_id": node.plan_node_id,
                            "reason": cancellation_reason,
                        },
                        at,
                    )
                )

            cancelled_run = current.run.cancel(cancellation_reason, at=at)
            _cancel_human_checks(uow, cancelled_run.run_id, cancellation_reason, at)
            uow.states.put_run(cancelled_run)
            uow.events.append(
                _event(
                    EventType.RUN_CANCELLED,
                    cancelled_run,
                    cancelled_run.run_id,
                    {"run_id": cancelled_run.run_id, "reason": cancellation_reason},
                    at,
                )
            )
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return cancelled_run

    def _read_snapshot(self, run_id: ID) -> _ControlSnapshot:
        normalized_id = normalize_id(run_id)
        with self._uow_factory() as uow:
            return self._read_snapshot_from(uow, normalized_id)

    @staticmethod
    def _read_snapshot_from(uow: UnitOfWork, run_id: ID) -> _ControlSnapshot:
        run = uow.states.get_run(run_id)
        if run is None:
            raise RunControlError(f"Run {run_id} is not persisted")
        plan = uow.states.get_execution_plan(run.run_id)
        if plan is None:
            raise RunControlError(f"Run {run.run_id} execution PlanRevision is not persisted")
        attempts = uow.states.list_attempts(run.run_id)
        active_attempts = tuple(
            attempt for attempt in attempts if attempt.status is AttemptStatus.RUNNING
        )
        if len(active_attempts) > 1:
            raise RunControlError(f"Run {run.run_id} has multiple running Attempts")
        active = active_attempts[0] if active_attempts else None
        return _ControlSnapshot(run=run, plan=plan, attempts=attempts, active_attempt=active)

    def _cancel_worker(self, snapshot: _ControlSnapshot) -> None:
        active = snapshot.active_attempt
        if active is None:
            return
        try:
            self._worker.cancel(active.attempt_id)
        except Exception as error:
            raise WorkerCancellationError(
                f"run {snapshot.run.run_id} could not cancel Attempt "
                f"{active.attempt_id}: {type(error).__name__}: {error}"
            ) from error

    @staticmethod
    def _is_settled_control(
        expected: _ControlSnapshot,
        current: _ControlSnapshot,
        target: RunStatus,
        *,
        reason: str | None = None,
    ) -> bool:
        if current.run.status is not target:
            return False
        if target is RunStatus.CANCELLED and current.run.status_reason != reason:
            return False
        active = expected.active_attempt
        if active is None:
            return current.attempts == expected.attempts and current.plan == expected.plan
        settled = next(
            (item for item in current.attempts if item.attempt_id == active.attempt_id),
            None,
        )
        if settled is None or settled.status is not AttemptStatus.CANCELLED:
            return False
        return _required_node(current.plan, active.plan_node_id).status is PlanNodeStatus.FAILED

    @staticmethod
    def _close_settled_control(
        uow: UnitOfWork,
        receipt: CommandReceipt | None,
    ) -> None:
        if receipt is None:
            return
        uow.command_receipts.put(receipt)
        uow.commit()

    @staticmethod
    def _require_unchanged(expected: _ControlSnapshot, current: _ControlSnapshot) -> None:
        if current != expected:
            raise RunControlConflictError(
                f"Run {expected.run.run_id} changed while cancelling its Worker Attempt"
            )


class BackgroundRunController:
    """Commit controls after an asynchronous Runtime has quiesced active Attempts."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def pause(
        self,
        run_id: ID,
        *,
        receipt: CommandReceipt | None = None,
        pause_cause: PauseCause = PauseCause.OPERATOR,
    ) -> Run:
        cause = require_pause_cause(pause_cause)
        if receipt is not None and cause is not PauseCause.OPERATOR:
            raise ValueError("a command receipt can only record an operator pause")
        normalized_id = normalize_id(run_id)
        at = self._clock()
        with self._uow_factory() as uow:
            run, _, attempts = _background_snapshot(uow, normalized_id)
            if any(attempt.status is AttemptStatus.RUNNING for attempt in attempts):
                raise RunControlError(
                    f"run {run.run_id} must quiesce active Attempts before background pause"
                )
            for attempt in attempts:
                if attempt.status is not AttemptStatus.PENDING:
                    continue
                cancelled = attempt.cancel("Run paused", at=at)
                uow.states.put_attempt(cancelled)
                uow.events.append(
                    _event(
                        EventType.ATTEMPT_CANCELLED,
                        run,
                        attempt.attempt_id,
                        {"attempt_id": attempt.attempt_id, "reason": "Run paused"},
                        at,
                    )
                )
            if run.status is RunStatus.PENDING:
                run = run.start(at=at)
                uow.states.put_run(run)
                uow.events.append(
                    _event(EventType.RUN_STARTED, run, run.run_id, {"run_id": run.run_id}, at)
                )
            if run.status is RunStatus.PAUSED:
                paused = run
                if receipt is not None:
                    _append_pause_event(uow, paused, cause, at=at)
            else:
                paused = run.pause()
                uow.states.put_run(paused)
                _append_pause_event(uow, paused, cause, at=at)
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return paused

    def resume(
        self,
        run_id: ID,
        *,
        receipt: CommandReceipt | None = None,
        control_guard: ProcessControlGuard | None = None,
    ) -> Run:
        normalized_id = normalize_id(run_id)
        at = self._clock()
        with self._uow_factory() as uow:
            if control_guard is not None:
                require_process_control(uow, normalized_id, control_guard)
            run, plan, attempts = _background_snapshot(uow, normalized_id)
            if not run_contract_is_current(uow, run, plan):
                raise RunControlError(
                    f"run {run.run_id} cannot resume because its PlanRevision no longer "
                    "matches the Goal's current confirmed CompletionContract"
                )
            resumed = run.resume()
            # Run state, dispatch readiness, and the command receipt must commit
            # together. A stopped host may already have completed the old work.
            ensure_dispatch_queued(uow, resumed)
            nodes = list(plan.nodes)
            readied: list[PlanNode] = []
            for index, node in enumerate(nodes):
                if node.status is not PlanNodeStatus.FAILED:
                    continue
                node_attempts = tuple(
                    attempt for attempt in attempts if attempt.plan_node_id == node.plan_node_id
                )
                if not node_attempts:
                    continue
                latest = node_attempts[-1]
                if latest.status is AttemptStatus.SUCCEEDED:
                    raise RunControlError(
                        f"run {run.run_id} cannot resume after succeeded Attempt "
                        f"{latest.attempt_id} left PlanNode {node.plan_node_id} failed"
                    )
                if latest.status in {
                    AttemptStatus.CANCELLED,
                    AttemptStatus.FAILED,
                    AttemptStatus.INTERRUPTED,
                    AttemptStatus.TIMED_OUT,
                }:
                    ready = node.retry()
                    nodes[index] = ready
                    readied.append(ready)
            if readied:
                plan = _replace_nodes(plan, tuple(nodes))
                uow.states.put_execution_plan(resumed.run_id, plan)
                for node in readied:
                    uow.events.append(
                        _event(
                            EventType.PLAN_NODE_READIED,
                            resumed,
                            node.plan_node_id,
                            {"plan_node_id": node.plan_node_id, "reason": "Run resumed"},
                            at,
                        )
                    )
            uow.states.put_run(resumed)
            uow.events.append(
                _event(
                    EventType.RUN_RESUMED,
                    resumed,
                    resumed.run_id,
                    {"run_id": resumed.run_id},
                    at,
                )
            )
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return resumed

    def cancel(
        self,
        run_id: ID,
        reason: str | None = None,
        *,
        receipt: CommandReceipt | None = None,
    ) -> Run:
        normalized_id = normalize_id(run_id)
        at = self._clock()
        cancellation_reason = reason or "Run cancelled"
        with self._uow_factory() as uow:
            run, _, attempts = _background_snapshot(uow, normalized_id)
            if any(attempt.status is AttemptStatus.RUNNING for attempt in attempts):
                raise RunControlError(
                    f"run {run.run_id} must quiesce active Attempts before background cancel"
                )
            for attempt in attempts:
                if attempt.status is not AttemptStatus.PENDING:
                    continue
                cancelled = attempt.cancel(cancellation_reason, at=at)
                uow.states.put_attempt(cancelled)
                uow.events.append(
                    _event(
                        EventType.ATTEMPT_CANCELLED,
                        run,
                        attempt.attempt_id,
                        {"attempt_id": attempt.attempt_id, "reason": cancellation_reason},
                        at,
                    )
                )
            cancelled_run = run.cancel(cancellation_reason, at=at)
            _cancel_human_checks(uow, cancelled_run.run_id, cancellation_reason, at)
            uow.states.put_run(cancelled_run)
            uow.events.append(
                _event(
                    EventType.RUN_CANCELLED,
                    cancelled_run,
                    cancelled_run.run_id,
                    {"run_id": cancelled_run.run_id, "reason": cancellation_reason},
                    at,
                )
            )
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return cancelled_run


RunControllerPort = RunController | BackgroundRunController


def ensure_dispatch_queued(uow: UnitOfWork, run: Run) -> None:
    """Reuse a Run's dispatch identity without stealing an active host claim."""
    works = tuple(work for work in uow.states.list_dispatch_work() if work.run_id == run.run_id)
    if not works:
        uow.states.put_dispatch_work(DispatchWork(run_id=run.run_id))
    elif works[0].status is DispatchWorkStatus.COMPLETED:
        uow.states.put_dispatch_work(works[0].requeue())


def _cancel_human_checks(uow: UnitOfWork, run_id: ID, reason: str, at: datetime) -> None:
    for check in uow.states.list_check_runs(run_id):
        if check.human_request is not None and check.ended_at is None:
            uow.states.put_check_run(check.cancel(reason, at=at))


def _background_snapshot(
    uow: UnitOfWork,
    run_id: ID,
) -> tuple[Run, PlanRevision, tuple[Attempt, ...]]:
    run = uow.states.get_run(run_id)
    if run is None:
        raise RunControlError(f"Run {run_id} is not persisted")
    plan = uow.states.get_execution_plan(run.run_id)
    if plan is None:
        raise RunControlError(f"Run {run.run_id} execution PlanRevision is not persisted")
    return run, plan, uow.states.list_attempts(run.run_id)


def _last_retryable_attempt(attempts: tuple[Attempt, ...]) -> Attempt | None:
    if not attempts:
        return None
    attempt = attempts[-1]
    if attempt.status in {
        AttemptStatus.INTERRUPTED,
        AttemptStatus.CANCELLED,
        AttemptStatus.FAILED,
    }:
        return attempt
    return None


def _required_node(plan: PlanRevision, plan_node_id: ID) -> PlanNode:
    for node in plan.nodes:
        if node.plan_node_id == plan_node_id:
            return node
    raise RunControlError(f"PlanNode {plan_node_id} is not in PlanRevision {plan.plan_revision_id}")


def _replace_node(plan: PlanRevision, replacement: PlanNode) -> PlanRevision:
    nodes = tuple(
        replacement if node.plan_node_id == replacement.plan_node_id else node
        for node in plan.nodes
    )
    if nodes == plan.nodes:
        raise RunControlError(
            f"PlanNode {replacement.plan_node_id} is not in PlanRevision {plan.plan_revision_id}"
        )
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=nodes,
        edges=plan.edges,
        branches=plan.branches,
        phases=plan.phases,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
        design_document=plan.design_document,
    )


def _replace_nodes(plan: PlanRevision, nodes: tuple[PlanNode, ...]) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=nodes,
        edges=plan.edges,
        branches=plan.branches,
        phases=plan.phases,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
        design_document=plan.design_document,
    )


def _event(
    event_type: EventType,
    run: Run,
    correlation_id: ID,
    payload: dict[str, JsonValue],
    occurred_at: datetime,
) -> Event:
    return Event(
        type=event_type,
        run_id=run.run_id,
        correlation_id=correlation_id,
        payload=payload,
        occurred_at=occurred_at,
    )


def _append_pause_event(
    uow: UnitOfWork,
    run: Run,
    cause: PauseCause,
    *,
    at: datetime,
    reason: str | None = None,
    **extra: JsonValue,
) -> None:
    uow.events.append(
        _event(
            EventType.RUN_PAUSED,
            run,
            run.run_id,
            pause_event_payload(run.run_id, cause, reason=reason, **extra),
            at,
        )
    )
