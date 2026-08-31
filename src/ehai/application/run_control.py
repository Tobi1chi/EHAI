"""Pause, resume, and cancel controls for a serial P1 Run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ehai import ID, JsonValue, normalize_id, utc_now
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.workers import WorkerAdapter
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision

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

    def pause(self, run_id: ID, *, receipt: CommandReceipt | None = None) -> Run:
        """Pause a running Run after cancelling its active Worker Attempt."""
        snapshot = self._read_snapshot(run_id)
        snapshot.run.pause()  # Validate before the external call.
        self._cancel_worker(snapshot)
        at = self._clock()

        with self._uow_factory() as uow:
            current = self._read_snapshot_from(uow, snapshot.run.run_id)
            self._require_unchanged(snapshot, current)
            plan = current.plan
            active = current.active_attempt
            if active is not None:
                cancelled = active.cancel("Run paused", at=at)
                node = _required_node(plan, active.plan_node_id)
                failed_node = node.fail()
                plan = _replace_node(plan, failed_node)
                uow.states.put_attempt(cancelled)
                uow.states.put_plan_revision(plan)
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
            uow.events.append(
                _event(
                    EventType.RUN_PAUSED,
                    paused,
                    paused.run_id,
                    {"run_id": paused.run_id},
                    at,
                )
            )
            if receipt is not None:
                uow.command_receipts.put(receipt)
            uow.commit()
            return paused

    def resume(self, run_id: ID, *, receipt: CommandReceipt | None = None) -> Run:
        """Resume a paused Run and ready the last failed Attempt's node."""
        normalized_id = normalize_id(run_id)
        at = self._clock()
        with self._uow_factory() as uow:
            snapshot = self._read_snapshot_from(uow, normalized_id)
            if snapshot.attempts:
                latest_attempt = snapshot.attempts[-1]
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
            plan = snapshot.plan
            retry_attempt = _last_retryable_attempt(snapshot.attempts)
            if retry_attempt is not None:
                node = _required_node(plan, retry_attempt.plan_node_id)
                if node.status is not PlanNodeStatus.FAILED:
                    raise RunControlError(
                        f"run {snapshot.run.run_id} cannot resume Attempt "
                        f"{retry_attempt.attempt_id}: PlanNode {node.plan_node_id} is "
                        f"{node.status.value}, not failed"
                    )
                ready_node = node.retry()
                plan = _replace_node(plan, ready_node)
                uow.states.put_plan_revision(plan)
                uow.events.append(
                    _event(
                        EventType.PLAN_NODE_READIED,
                        resumed,
                        ready_node.plan_node_id,
                        {"plan_node_id": ready_node.plan_node_id},
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
        """Cancel a pending, running, or paused Run without fabricating completion."""
        snapshot = self._read_snapshot(run_id)
        cancellation_reason = reason or "Run cancelled"
        snapshot.run.cancel(cancellation_reason, at=self._clock())  # Validate first.
        self._cancel_worker(snapshot)
        at = self._clock()

        with self._uow_factory() as uow:
            current = self._read_snapshot_from(uow, snapshot.run.run_id)
            self._require_unchanged(snapshot, current)
            plan = current.plan
            active = current.active_attempt
            if active is not None:
                cancelled_attempt = active.cancel(cancellation_reason, at=at)
                node = _required_node(plan, active.plan_node_id)
                failed_node = node.fail()
                plan = _replace_node(plan, failed_node)
                uow.states.put_attempt(cancelled_attempt)
                uow.states.put_plan_revision(plan)
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
        plan = uow.states.get_plan_revision(run.plan_revision_id)
        if plan is None:
            raise RunControlError(
                f"Run {run.run_id} PlanRevision {run.plan_revision_id} is not persisted"
            )
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
    def _require_unchanged(expected: _ControlSnapshot, current: _ControlSnapshot) -> None:
        if current != expected:
            raise RunControlConflictError(
                f"Run {expected.run.run_id} changed while cancelling its Worker Attempt"
            )


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
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
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
