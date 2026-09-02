from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

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
from ehai.application.execution_policy import ExecutionReplay
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicExplorationPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    PlanProposal,
)
from ehai.application.queries import QueryService
from ehai.application.scheduler import CapacityPolicy, ConcurrentRuntime, Dispatcher
from ehai.application.service import ExecutionService
from ehai.application.workers import WorkerRequest
from ehai.domain.checking import CheckKind
from ehai.domain.execution import RunStatus
from ehai.domain.goal import Goal
from ehai.domain.planning import BranchStatus, PlanNodeKind
from ehai.domain.workers import (
    AttemptActivity,
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker
from ehai.infrastructure.workspaces import WorkspaceManager


class _RoutedPlanner:
    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        proposal = DeterministicExplorationPlanner().propose(
            ExplorationPlanRequest(
                goal,
                criteria,
                ExplorationBudget(max_attempts=5),
            )
        )
        nodes = []
        for node in proposal.plan_revision.nodes:
            route = "worker.codex" if node.title == "Explore approach B" else "worker.builtin"
            capabilities = {WorkerCapability(route)}
            if node.kind is PlanNodeKind.WORK:
                capabilities.add(WorkerCapability("workspace.write"))
            nodes.append(replace(node, required_capabilities=frozenset(capabilities)))
        return replace(
            proposal,
            plan_revision=replace(proposal.plan_revision, nodes=tuple(nodes)),
        )


class _BranchBarrier:
    def __init__(self) -> None:
        self.active = 0
        self.two_active = asyncio.Event()
        self.release = asyncio.Event()


class _RoutedConnector:
    def __init__(self, name: str, worker: FakeWorker, barrier: _BranchBarrier) -> None:
        self.name = name
        self.worker = worker
        self.barrier = barrier
        self.requests: dict[ID, WorkerRequest] = {}
        self.workspaces: dict[ID, str | None] = {}
        self.executions: dict[ID, ConnectorExecution] = {}

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        self.requests[request.attempt_id] = request.request
        self.workspaces[request.attempt_id] = request.workspace
        execution = ConnectorExecution(
            request.attempt_id,
            f"{self.name}-session-{request.attempt_id}",
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
        if request.plan_node.kind is PlanNodeKind.WORK:
            self.barrier.active += 1
            if self.barrier.active == 2:
                self.barrier.two_active.set()
            await self.barrier.release.wait()
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

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        return AttemptActivity.RUNNING

    async def cancel(self, execution: ConnectorExecution) -> None:
        del execution

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        return self.executions.get(request.attempt_id)


def test_p2_mixed_workers_isolate_branches_gate_and_replay(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "EHAI Test", cwd=repository)
    (repository / "base.txt").write_text("base", encoding="utf-8")
    _git("add", "base.txt", cwd=repository)
    _git("commit", "-m", "baseline", cwd=repository)

    database = SQLiteDatabase(tmp_path / "p2.sqlite3")
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
        workspace=repository,
        attempt_budget=5,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=_RoutedPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    project = service.create_project(CreateProject("project", "p2-e2e"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "mixed worker runtime"))
    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,)))
    approved = service.approve_plan(
        ApprovePlan("approve", plan.plan_revision_id, plan.completion_contract_id)
    )
    run = service.start_run(StartRun("run", approved.plan_revision_id))

    builtin_capabilities = frozenset(
        {
            WorkerCapability("worker.builtin"),
            WorkerCapability("workspace.write"),
        }
    )
    codex_capabilities = frozenset(
        {
            WorkerCapability("worker.codex"),
            WorkerCapability("workspace.write"),
        }
    )
    builtin_profile = WorkerProfile(
        "builtin",
        WorkerKind.BUILTIN,
        "scripted",
        builtin_capabilities,
    )
    codex_profile = WorkerProfile(
        "codex",
        WorkerKind.CODEX_CLI,
        "test-codex",
        codex_capabilities,
    )
    builtin_endpoint = WorkerEndpoint(
        "builtin",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "builtin",
        2,
    )
    codex_endpoint = WorkerEndpoint(
        "codex",
        WorkerKind.CODEX_CLI,
        WorkerEndpointType.COMMAND,
        "codex",
        1,
    )
    barrier = _BranchBarrier()
    builtin_connector = _RoutedConnector("builtin", worker, barrier)
    codex_connector = _RoutedConnector("codex", worker, barrier)
    workspace_manager = WorkspaceManager(
        database=database,
        base_workspace=repository,
        owned_root=tmp_path / "owned-worktrees",
    )
    runtime = ConcurrentRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        dispatcher=Dispatcher(
            profiles=(builtin_profile, codex_profile),
            endpoints=(builtin_endpoint, codex_endpoint),
            capacity=CapacityPolicy(2, 2, 2, {}, {}),
        ),
        connectors={
            builtin_endpoint.worker_endpoint_id: builtin_connector,
            codex_endpoint.worker_endpoint_id: codex_connector,
        },
        workspace_manager=workspace_manager,
    )

    async def scenario() -> tuple[RunStatus, ...]:
        running = asyncio.create_task(runtime.run_until_idle())
        await asyncio.wait_for(barrier.two_active.wait(), timeout=10)
        branch_workspaces = tuple(
            workspace
            for connector in (builtin_connector, codex_connector)
            for attempt_id, workspace in connector.workspaces.items()
            if connector.requests[attempt_id].plan_node.kind is PlanNodeKind.WORK
        )
        assert len(branch_workspaces) == 2
        assert all(workspace is not None for workspace in branch_workspaces)
        assert len(set(branch_workspaces)) == 2
        assert all(Path(workspace).exists() for workspace in branch_workspaces if workspace)
        barrier.release.set()
        completed = await asyncio.wait_for(running, timeout=20)
        return tuple(item.status for item in completed)

    statuses = asyncio.run(scenario())

    assert statuses == (RunStatus.COMPLETED,)
    queries = QueryService(read_session_factory=database.read_session)
    trace = queries.get_execution_trace(run.run_id)
    completed_plan = queries.get_plan_graph(plan.plan_revision_id)
    runtimes = tuple(queries.get_attempt_runtime(attempt.attempt_id) for attempt in trace.attempts)
    assert {item.worker_profile_id for item in runtimes} == {
        builtin_profile.worker_profile_id,
        codex_profile.worker_profile_id,
    }
    assert {branch.status for branch in completed_plan.branches} == {
        BranchStatus.SELECTED,
        BranchStatus.PRUNED,
    }
    assert trace.checkpoints and trace.checkpoints[-1].gate_decision.passed
    replay = ExecutionReplay().replay(trace.events)
    assert set(replay.bindings) == {attempt.attempt_id for attempt in trace.attempts}
    assert all(
        not Path(workspace).exists()
        for connector in (builtin_connector, codex_connector)
        for workspace in connector.workspaces.values()
        if workspace is not None and "owned-worktrees" in workspace
    )


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True)
