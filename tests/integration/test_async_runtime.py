from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ehai import ID, new_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    SingleSlotRuntime,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.checks import CheckRunner
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    StartRun,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    PlanProposal,
)
from ehai.application.service import ExecutionService
from ehai.application.workers import WorkerRequest
from ehai.domain.checking import CheckKind
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import Goal
from ehai.domain.runtime import DispatchWorkStatus
from ehai.domain.workers import (
    AttemptActivity,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker


class _ExplorationPlanner:
    def __init__(self) -> None:
        self._planner = DeterministicExplorationPlanner()

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        return self._planner.propose(
            ExplorationPlanRequest(
                goal=goal,
                criteria=criteria,
                budget=ExplorationBudget(max_attempts=5),
            )
        )


class _TestConnector:
    def __init__(self, worker: FakeWorker, *, fail_first_stream: bool = False) -> None:
        self.worker = worker
        self.fail_first_stream = fail_first_stream
        self.requests: dict[ID, WorkerRequest] = {}
        self.executions: dict[ID, ConnectorExecution] = {}
        self.start_calls: list[ID] = []
        self.recover_calls: list[ID] = []
        self.cancel_calls: list[ID] = []
        self._failed_streams: set[ID] = set()

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        attempt_id = request.attempt_id
        self.requests[attempt_id] = request.request
        if attempt_id not in self.executions:
            self.start_calls.append(attempt_id)
            self.executions[attempt_id] = ConnectorExecution(
                attempt_id,
                f"session-{attempt_id}",
                str(new_id()),
                True,
            )
        return self.executions[attempt_id]

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        attempt_id = execution.attempt_id
        if self.fail_first_stream and attempt_id not in self._failed_streams:
            self._failed_streams.add(attempt_id)
            raise RuntimeError("simulated Runtime process loss")
        result = self.worker.execute(self.requests[attempt_id])
        candidate = WorkerEvent(
            f"{attempt_id}:candidate",
            attempt_id,
            WorkerEventType.CANDIDATE,
            "1",
            result=result,
        )
        yield candidate
        yield candidate
        yield WorkerEvent(
            f"{attempt_id}:completed",
            attempt_id,
            WorkerEventType.COMPLETED,
            "2",
        )

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        return AttemptActivity.RUNNING

    async def cancel(self, execution: ConnectorExecution) -> None:
        self.cancel_calls.append(execution.attempt_id)

    async def recover(
        self,
        request: ConnectorRecoveryRequest,
    ) -> ConnectorExecution | None:
        self.recover_calls.append(request.attempt_id)
        return self.executions.get(request.attempt_id)


def _build_runtime(
    tmp_path: Path,
    *,
    exploration: bool,
    fail_first_stream: bool = False,
) -> tuple[ExecutionService, SingleSlotRuntime, _TestConnector, SQLiteDatabase, FakeWorker]:
    database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = FakeWorker()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifacts,
        check_runner=CheckRunner(
            {
                CheckKind.ARTIFACT: ArtifactCheckAdapter(
                    artifacts,
                    {},
                    default_rule=ArtifactCheckRule(minimum_count=1, require_non_empty=True),
                )
            }
        ),
        workspace=tmp_path,
        attempt_budget=5,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=_ExplorationPlanner() if exploration else DeterministicPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    connector = _TestConnector(worker, fail_first_stream=fail_first_stream)
    profile = WorkerProfile("test", WorkerKind.BUILTIN, "scripted")
    endpoint = WorkerEndpoint(
        "test",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "test",
        1,
    )
    runtime = SingleSlotRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        connector=connector,
        profile=profile,
        endpoint=endpoint,
    )
    return service, runtime, connector, database, worker


def _start(service: ExecutionService) -> tuple[ID, ID]:
    project = service.create_project(CreateProject("project", "background"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "finish in background"))
    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,)))
    approved = service.approve_plan(
        ApprovePlan("approve", plan.plan_revision_id, plan.completion_contract_id)
    )
    run = service.start_run(StartRun("start", approved.plan_revision_id))
    assert run.status is RunStatus.PENDING
    return run.run_id, goal.goal_id


def test_start_run_returns_before_single_slot_runtime_completes_exploration(
    tmp_path: Path,
) -> None:
    service, runtime, connector, database, worker = _build_runtime(
        tmp_path,
        exploration=True,
    )
    run_id, _ = _start(service)

    assert connector.start_calls == []
    with database.read_session() as session:
        work = session.states.list_dispatch_work()
        assert len(work) == 1 and work[0].status is DispatchWorkStatus.PENDING

    completed = asyncio.run(runtime.run_once())

    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert len(connector.start_calls) == 5
    assert len(worker.calls) == 5
    assert asyncio.run(runtime.run_once()) is None
    with database.read_session() as session:
        assert session.states.list_dispatch_work()[0].status is DispatchWorkStatus.COMPLETED
        assert all(
            attempt.status is AttemptStatus.SUCCEEDED
            for attempt in session.states.list_attempts(run_id)
        )


def test_runtime_restart_recovers_original_execution_without_duplicate_start(
    tmp_path: Path,
) -> None:
    service, runtime, connector, database, _ = _build_runtime(
        tmp_path,
        exploration=False,
        fail_first_stream=True,
    )
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="process loss"):
        asyncio.run(runtime.run_once())
    assert len(connector.start_calls) == 1

    recovered = asyncio.run(runtime.recover_startup())

    assert len(recovered) == 1 and recovered[0].status is RunStatus.COMPLETED
    assert len(connector.start_calls) == 1
    assert len(connector.recover_calls) == 1
    connection = database.connect()
    try:
        receipts = connection.execute(
            "SELECT COUNT(*) FROM worker_event_receipts WHERE attempt_id IN "
            "(SELECT attempt_id FROM attempts WHERE run_id = ?)",
            (run_id,),
        ).fetchone()[0]
        assert receipts == 2
    finally:
        connection.close()


def test_completed_result_wins_cancel_race(tmp_path: Path) -> None:
    service, runtime, connector, database, _ = _build_runtime(
        tmp_path,
        exploration=False,
        fail_first_stream=True,
    )
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="process loss"):
        asyncio.run(runtime.run_once())
    with database.read_session() as session:
        attempt = session.states.list_attempts(run_id)[0]

    completed = asyncio.run(runtime.cancel_attempt(attempt.attempt_id))

    assert completed.status is RunStatus.COMPLETED
    assert connector.cancel_calls == [attempt.attempt_id]
    with database.read_session() as session:
        assert session.states.list_dispatch_work()[0].status is DispatchWorkStatus.COMPLETED


def test_recovery_marks_missing_original_execution_interrupted_without_restart(
    tmp_path: Path,
) -> None:
    service, runtime, connector, database, _ = _build_runtime(
        tmp_path,
        exploration=False,
        fail_first_stream=True,
    )
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="process loss"):
        asyncio.run(runtime.run_once())
    with database.read_session() as session:
        attempt = session.states.list_attempts(run_id)[0]
    connector.executions.clear()

    recovered = asyncio.run(runtime.recover_startup())

    assert len(recovered) == 1 and recovered[0].status is RunStatus.PAUSED
    assert connector.start_calls == [attempt.attempt_id]
    with database.read_session() as session:
        stored = session.states.get_attempt(attempt.attempt_id)
        assert stored is not None and stored.status is AttemptStatus.INTERRUPTED
