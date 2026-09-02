from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ehai import ID, new_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
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
from ehai.application.scheduler import (
    CapacityPolicy,
    CapacityUsage,
    ConcurrentRuntime,
    Dispatcher,
    WorkspaceAllocationPort,
)
from ehai.application.service import ExecutionService
from ehai.application.workers import WorkerRequest
from ehai.domain.checking import CheckKind
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import Goal
from ehai.domain.planning import BranchStatus, PlanNode
from ehai.domain.workers import (
    AttemptActivity,
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointStatus,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.domain.workspaces import WorkspaceKind, WorkspaceLease, WorkspaceRef
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


class _BlockingConnector:
    def __init__(self, worker: FakeWorker) -> None:
        self.worker = worker
        self.requests: dict[ID, WorkerRequest] = {}
        self.executions: dict[ID, ConnectorExecution] = {}
        self.start_calls: list[ID] = []
        self.release_work = asyncio.Event()
        self.two_work_active = asyncio.Event()
        self.active_work = 0
        self.max_active_work = 0

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        self.requests[request.attempt_id] = request.request
        self.start_calls.append(request.attempt_id)
        execution = ConnectorExecution(
            request.attempt_id,
            f"session-{request.attempt_id}",
            str(new_id()),
            True,
        )
        self.executions[request.attempt_id] = execution
        return execution

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        request = self.requests[execution.attempt_id]
        is_branch_work = request.plan_node.kind.value == "work"
        if is_branch_work:
            self.active_work += 1
            self.max_active_work = max(self.max_active_work, self.active_work)
            if self.active_work == 2:
                self.two_work_active.set()
            await self.release_work.wait()
        try:
            result = self.worker.execute(request)
            yield WorkerEvent(
                f"{execution.attempt_id}:candidate",
                execution.attempt_id,
                WorkerEventType.CANDIDATE,
                "1",
                result=result,
            )
            yield WorkerEvent(
                f"{execution.attempt_id}:completed",
                execution.attempt_id,
                WorkerEventType.COMPLETED,
                "2",
            )
        finally:
            if is_branch_work:
                self.active_work -= 1

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        return AttemptActivity.RUNNING

    async def cancel(self, execution: ConnectorExecution) -> None:
        del execution

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        return self.executions.get(request.attempt_id)


@dataclass(frozen=True, slots=True)
class _WorkspaceAllocation:
    reference: WorkspaceRef
    lease: WorkspaceLease


class _RecordingWorkspaceManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.cleaned: list[_WorkspaceAllocation] = []

    def can_isolate_writes(self) -> bool:
        return True

    def allocate(
        self,
        *,
        run_id: ID,
        attempt_id: ID,
        write_capable: bool,
        isolate: bool,
    ) -> _WorkspaceAllocation:
        del isolate
        reference = WorkspaceRef(run_id, str(self.workspace), WorkspaceKind.DIRECTORY, False)
        return _WorkspaceAllocation(
            reference,
            WorkspaceLease(attempt_id, reference.workspace_ref_id, write_capable),
        )

    def cleanup(self, allocation: WorkspaceAllocationPort) -> WorkspaceLease:
        assert isinstance(allocation, _WorkspaceAllocation)
        self.cleaned.append(allocation)
        return allocation.lease.release()


def test_dispatcher_filters_capability_state_capacity_and_uses_stable_order() -> None:
    capability = WorkerCapability("workspace.read")
    preferred = WorkerProfile(
        "preferred",
        WorkerKind.BUILTIN,
        "model",
        frozenset({capability}),
        priority=10,
    )
    fallback = WorkerProfile(
        "fallback",
        WorkerKind.BUILTIN,
        "model",
        frozenset({capability}),
        priority=1,
    )
    enabled = WorkerEndpoint(
        "enabled",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "enabled",
        2,
    )
    disabled = WorkerEndpoint(
        "disabled",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "disabled",
        2,
        WorkerEndpointStatus.DISABLED,
    )
    policy = CapacityPolicy(2, 2, 2, {}, {})
    dispatcher = Dispatcher(
        profiles=(fallback, preferred),
        endpoints=(disabled, enabled),
        capacity=policy,
    )
    node = PlanNode(
        new_id(),
        "read",
        "read",
        required_capabilities=frozenset({capability}),
    )
    project_id = new_id()
    run_id = new_id()
    empty = CapacityUsage(0, {}, {}, {}, {})

    selected = dispatcher.select(
        node,
        project_id=project_id,
        run_id=run_id,
        usage=empty,
        workspace_conflict=False,
    )
    assert selected.assignment is not None
    assert selected.assignment.profile == preferred
    assert selected.assignment.endpoint == enabled
    exhausted = dispatcher.select(
        node,
        project_id=project_id,
        run_id=run_id,
        usage=CapacityUsage(2, {}, {}, {}, {}),
        workspace_conflict=False,
    )
    assert exhausted.assignment is None and exhausted.reason == "global capacity exhausted"


def test_scheduler_releases_workspace_when_attempt_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = SQLiteDatabase(tmp_path / "scheduler-start-failure.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts-start-failure")
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
        planner=DeterministicPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    project = service.create_project(CreateProject("project-start-failure", "scheduler"))
    goal = service.create_goal(
        CreateGoal("goal-start-failure", project.project_id, "release Workspace")
    )
    plan = service.propose_plan(
        ProposePlan("plan-start-failure", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )
    approved = service.approve_plan(
        ApprovePlan(
            "approve-start-failure",
            plan.plan_revision_id,
            plan.completion_contract_id,
        )
    )
    service.start_run(StartRun("run-start-failure", approved.plan_revision_id))

    profile = WorkerProfile("start-failure", WorkerKind.BUILTIN, "scripted")
    endpoint = WorkerEndpoint(
        "start-failure",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "start-failure",
        1,
    )
    workspaces = _RecordingWorkspaceManager(tmp_path)
    runtime = ConcurrentRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        dispatcher=Dispatcher(
            profiles=(profile,),
            endpoints=(endpoint,),
            capacity=CapacityPolicy(1, 1, 1, {}, {}),
        ),
        connectors={endpoint.worker_endpoint_id: _BlockingConnector(worker)},
        workspace_manager=workspaces,
    )

    def fail_start(attempt_id: ID) -> WorkerRequest:
        raise RuntimeError(f"cannot start {attempt_id}")

    monkeypatch.setattr(orchestrator, "start_queued_attempt", fail_start)

    with pytest.raises(RuntimeError, match="cannot start"):
        asyncio.run(runtime.run_until_idle())

    assert len(workspaces.cleaned) == 1
    assert workspaces.cleaned[0].lease.status.value == "active"


def test_concurrent_branches_overlap_third_queues_and_queued_cancel_uses_no_slot(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "scheduler.sqlite3")
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
        attempt_budget=10,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=_ExplorationPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    project = service.create_project(CreateProject("project", "scheduler"))
    run_ids: list[ID] = []
    for index in range(2):
        goal = service.create_goal(
            CreateGoal(f"goal-{index}", project.project_id, f"explore {index}")
        )
        plan = service.propose_plan(
            ProposePlan(f"plan-{index}", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,))
        )
        approved = service.approve_plan(
            ApprovePlan(
                f"approve-{index}",
                plan.plan_revision_id,
                plan.completion_contract_id,
            )
        )
        run_ids.append(
            service.start_run(StartRun(f"start-{index}", approved.plan_revision_id)).run_id
        )
    profile = WorkerProfile("concurrent", WorkerKind.BUILTIN, "scripted", priority=1)
    endpoint = WorkerEndpoint(
        "concurrent",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "concurrent",
        2,
    )
    connector = _BlockingConnector(worker)
    runtime = ConcurrentRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        dispatcher=Dispatcher(
            profiles=(profile,),
            endpoints=(endpoint,),
            capacity=CapacityPolicy(2, 2, 2, {}, {}),
        ),
        connectors={endpoint.worker_endpoint_id: connector},
    )

    async def scenario() -> tuple[RunStatus, ...]:
        running = asyncio.create_task(runtime.run_until_idle())
        await asyncio.wait_for(connector.two_work_active.wait(), timeout=5)
        with database.read_session() as session:
            queued = tuple(
                attempt
                for run_id in run_ids
                for attempt in session.states.list_attempts(run_id)
                if attempt.status is AttemptStatus.PENDING
            )
        assert queued
        cancelled = runtime.cancel_queued_attempt(queued[0].attempt_id)
        assert cancelled.status is RunStatus.CANCELLED
        assert queued[0].attempt_id not in connector.start_calls
        connector.release_work.set()
        completed = await asyncio.wait_for(running, timeout=10)
        return tuple(run.status for run in completed)

    statuses = asyncio.run(scenario())

    assert RunStatus.COMPLETED in statuses
    assert connector.max_active_work == 2
    with database.read_session() as session:
        completed_run = next(
            run
            for run_id in run_ids
            if (run := session.states.get_run(run_id)) is not None
            and run.status is RunStatus.COMPLETED
        )
        plan = session.states.get_plan_revision(completed_run.plan_revision_id)
        assert plan is not None
        assert {branch.status for branch in plan.branches} == {
            BranchStatus.SELECTED,
            BranchStatus.PRUNED,
        }
