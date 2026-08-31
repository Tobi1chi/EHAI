from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ehai import ID, new_id
from ehai.application.ports import CommandReceipt
from ehai.application.run_control import RunControlError, RunController, WorkerCancellationError
from ehai.application.workers import WorkerRequest, WorkerResult
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt, AttemptStatus, InvalidRunTransition, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import (
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker

NOW = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)


class _InspectingWorker(FakeWorker):
    def __init__(self, database: SQLiteDatabase, run_id: ID) -> None:
        super().__init__()
        self._database = database
        self._run_id = run_id
        self.observed_run: Run | None = None

    def cancel(self, attempt_id: ID) -> None:
        with self._database.unit_of_work() as uow:
            self.observed_run = uow.states.get_run(self._run_id)
        super().cancel(attempt_id)


class _FailingCancelWorker:
    def execute(self, request: WorkerRequest) -> WorkerResult:
        del request
        raise AssertionError("execute must not be called")

    def cancel(self, attempt_id: ID) -> None:
        raise OSError(f"cannot cancel {attempt_id}")


def _seed(
    database: SQLiteDatabase,
    *,
    run_status: RunStatus,
    with_attempt: bool,
) -> tuple[Run, PlanRevision, Attempt | None]:
    project = Project.create("control", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "control run", goal_id=new_id(), created_at=NOW)
    contract = CompletionContract.draft(
        goal.goal_id,
        ("controlled",),
        (new_id(),),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node_status = PlanNodeStatus.RUNNING if with_attempt else PlanNodeStatus.PENDING
    node = PlanNode.rehydrate(
        plan_node_id=new_id(),
        title="controlled node",
        instruction="wait for control",
        kind=PlanNodeKind.WORK,
        required_dependency_ids=(),
        required_check_ids=contract.required_check_ids,
        status=node_status,
    )
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(node,),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    base_run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    )
    run = base_run if run_status is RunStatus.PENDING else base_run.start(at=NOW)
    if run_status is RunStatus.PAUSED:
        run = run.pause()
    attempt = None
    if with_attempt:
        attempt = Attempt(
            run_id=run.run_id,
            plan_node_id=node.plan_node_id,
            sequence=1,
            attempt_id=new_id(),
            created_at=NOW,
        ).start(at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
        if attempt is not None:
            uow.states.put_attempt(attempt)
        uow.commit()
    return run, plan, attempt


def _state(
    database: SQLiteDatabase,
    run: Run,
) -> tuple[Run, PlanRevision, tuple[Attempt, ...], tuple[EventType, ...]]:
    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(run.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        event_types = tuple(item.event.type for item in uow.events.list_events())
    assert stored_run is not None
    assert stored_plan is not None
    return stored_run, stored_plan, attempts, event_types


def _plan_with_node(plan: PlanRevision, node: PlanNode) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=(node,),
        edges=plan.edges,
        branches=plan.branches,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
    )


def test_pause_cancels_worker_outside_transaction_and_persists_atomically(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "pause.sqlite3")
    run, _, attempt = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    assert attempt is not None
    worker = _InspectingWorker(database, run.run_id)
    controller = RunController(database.unit_of_work, worker, clock=lambda: NOW)

    paused = controller.pause(run.run_id)

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert paused.status is RunStatus.PAUSED
    assert worker.observed_run is not None and worker.observed_run.status is RunStatus.RUNNING
    assert worker.cancel_calls == (attempt.attempt_id,)
    assert stored_run.status is RunStatus.PAUSED
    assert attempts[0].status is AttemptStatus.CANCELLED
    assert stored_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert events == (
        EventType.ATTEMPT_CANCELLED,
        EventType.PLAN_NODE_FAILED,
        EventType.RUN_PAUSED,
    )


def test_control_receipt_commits_with_state_and_events(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "receipt.sqlite3")
    run, _, _ = _seed(database, run_status=RunStatus.RUNNING, with_attempt=False)
    receipt = CommandReceipt(
        idempotency_key="pause-once",
        command_name="PauseRun",
        command_fingerprint="pause-fingerprint",
        result={"run_id": run.run_id},
        created_at=NOW,
    )
    controller = RunController(database.unit_of_work, FakeWorker(), clock=lambda: NOW)

    controller.pause(run.run_id, receipt=receipt)

    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run.run_id)
        stored_receipt = uow.command_receipts.get("pause-once")
    assert stored_run is not None and stored_run.status is RunStatus.PAUSED
    assert stored_receipt == receipt


def test_resume_readies_failed_node_and_preserves_cancelled_attempt(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "resume.sqlite3")
    run, _, _ = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    worker = FakeWorker()
    controller = RunController(database.unit_of_work, worker, clock=lambda: NOW)
    controller.pause(run.run_id)

    resumed = controller.resume(run.run_id)

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert resumed.status is RunStatus.RUNNING
    assert stored_run.status is RunStatus.RUNNING
    assert attempts[0].status is AttemptStatus.CANCELLED
    assert stored_plan.nodes[0].status is PlanNodeStatus.READY
    assert events[-2:] == (EventType.PLAN_NODE_READIED, EventType.RUN_RESUMED)
    assert EventType.PLAN_NODE_COMPLETED not in events
    assert EventType.RUN_COMPLETED not in events


def test_resume_rejects_succeeded_attempt_with_failed_node_without_mutation(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "resume-check-failure.sqlite3")
    run, plan, attempt = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    assert attempt is not None
    failed_node = plan.nodes[0].fail()
    succeeded_attempt = attempt.succeed((), at=NOW)
    paused_run = run.pause()
    with database.unit_of_work() as uow:
        uow.states.put_attempt(succeeded_attempt)
        uow.states.put_plan_revision(_plan_with_node(plan, failed_node))
        uow.states.put_run(paused_run)
        uow.commit()
    controller = RunController(database.unit_of_work, FakeWorker(), clock=lambda: NOW)

    with pytest.raises(RunControlError, match="restore a Checkpoint or recover checks manually"):
        controller.resume(run.run_id)

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert stored_run == paused_run
    assert stored_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert attempts == (succeeded_attempt,)
    assert events == ()


def test_resume_preserves_ready_legacy_window_after_succeeded_attempt(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "resume-ready-window.sqlite3")
    run, plan, attempt = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    assert attempt is not None
    failed_plan = _plan_with_node(plan, plan.nodes[0].fail())
    ready_plan = _plan_with_node(failed_plan, failed_plan.nodes[0].retry())
    succeeded_attempt = attempt.succeed((), at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_attempt(succeeded_attempt)
        uow.states.put_plan_revision(failed_plan)
        uow.states.put_plan_revision(ready_plan)
        uow.states.put_run(run.pause())
        uow.commit()
    controller = RunController(database.unit_of_work, FakeWorker(), clock=lambda: NOW)

    resumed = controller.resume(run.run_id)

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert resumed.status is RunStatus.RUNNING
    assert stored_run.status is RunStatus.RUNNING
    assert stored_plan.nodes[0].status is PlanNodeStatus.READY
    assert attempts == (succeeded_attempt,)
    assert events == (EventType.RUN_RESUMED,)


def test_cancel_pending_run_does_not_touch_plan_or_worker(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "cancel-pending.sqlite3")
    run, plan, _ = _seed(database, run_status=RunStatus.PENDING, with_attempt=False)
    worker = FakeWorker()
    controller = RunController(database.unit_of_work, worker, clock=lambda: NOW)

    cancelled = controller.cancel(run.run_id, "user cancelled")

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert cancelled.status is RunStatus.CANCELLED
    assert stored_run.status is RunStatus.CANCELLED
    assert stored_run.status_reason == "user cancelled"
    assert stored_plan == plan
    assert attempts == ()
    assert worker.cancel_calls == ()
    assert events == (EventType.RUN_CANCELLED,)


def test_cancel_running_attempt_preserves_history_without_completion(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "cancel-running.sqlite3")
    run, _, attempt = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    assert attempt is not None
    worker = FakeWorker()
    controller = RunController(database.unit_of_work, worker, clock=lambda: NOW)

    cancelled = controller.cancel(run.run_id)

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert cancelled.status is RunStatus.CANCELLED
    assert stored_run.status is RunStatus.CANCELLED
    assert attempts[0].attempt_id == attempt.attempt_id
    assert attempts[0].status is AttemptStatus.CANCELLED
    assert stored_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert EventType.PLAN_NODE_COMPLETED not in events
    assert EventType.RUN_COMPLETED not in events
    assert EventType.CHECKPOINT_CREATED not in events


def test_cancel_paused_run_keeps_cancelled_attempt_and_failed_node(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "cancel-paused.sqlite3")
    run, _, attempt = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    assert attempt is not None
    worker = FakeWorker()
    controller = RunController(database.unit_of_work, worker, clock=lambda: NOW)
    controller.pause(run.run_id)

    cancelled = controller.cancel(run.run_id, "stop while paused")

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert cancelled.status is RunStatus.CANCELLED
    assert stored_run.status_reason == "stop while paused"
    assert attempts[0].status is AttemptStatus.CANCELLED
    assert stored_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert worker.cancel_calls == (attempt.attempt_id,)
    assert events[-1] is EventType.RUN_CANCELLED


def test_invalid_control_transition_is_rejected_without_worker_call(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "invalid.sqlite3")
    run, _, _ = _seed(database, run_status=RunStatus.PENDING, with_attempt=False)
    worker = FakeWorker()
    controller = RunController(database.unit_of_work, worker, clock=lambda: NOW)

    with pytest.raises(InvalidRunTransition):
        controller.pause(run.run_id)

    assert worker.cancel_calls == ()
    stored_run, _, _, events = _state(database, run)
    assert stored_run.status is RunStatus.PENDING
    assert events == ()


def test_worker_cancel_failure_does_not_mutate_persisted_state(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "cancel-failure.sqlite3")
    run, plan, attempt = _seed(database, run_status=RunStatus.RUNNING, with_attempt=True)
    assert attempt is not None
    controller = RunController(database.unit_of_work, _FailingCancelWorker(), clock=lambda: NOW)

    with pytest.raises(WorkerCancellationError, match=str(attempt.attempt_id)):
        controller.pause(run.run_id)

    stored_run, stored_plan, attempts, events = _state(database, run)
    assert stored_run == run
    assert stored_plan == plan
    assert attempts == (attempt,)
    assert events == ()
