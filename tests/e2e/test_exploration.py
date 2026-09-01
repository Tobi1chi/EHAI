from __future__ import annotations

from dataclasses import fields
from pathlib import Path

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
    ExplorationBudget,
    ExplorationPlanRequest,
    PlanProposal,
)
from ehai.application.queries import QueryService
from ehai.application.run_control import RunController
from ehai.application.service import ExecutionService
from ehai.domain.checking import CheckKind, CheckRunStatus
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import Goal
from ehai.domain.planning import BranchStatus, PlanNodeKind, PlanNodeStatus
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker
from ehai.infrastructure.workers.codex_protocol import build_codex_prompt


class _ExplorationPlannerAdapter:
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


def test_public_service_completes_and_reloads_exploration_trace(tmp_path: Path) -> None:
    database_path = tmp_path / "exploration.sqlite3"
    database = SQLiteDatabase(database_path)
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = FakeWorker()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=CheckRunner(
            {
                CheckKind.ARTIFACT: ArtifactCheckAdapter(
                    artifact_store,
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
        planner=_ExplorationPlannerAdapter(),
        orchestrator=orchestrator,
        run_controller=RunController(database.unit_of_work, worker),
    )

    project = service.create_project(CreateProject("project", "P1 exploration"))
    goal = service.create_goal(
        CreateGoal("goal", project.project_id, "compare two approaches and merge the winner")
    )
    proposed = service.propose_plan(
        ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )
    approved = service.approve_plan(
        ApprovePlan("approve", proposed.plan_revision_id, proposed.completion_contract_id)
    )
    start = StartRun("start", approved.plan_revision_id)

    completed = service.start_run(start)
    calls_after_completion = worker.calls
    retried = service.start_run(start)

    assert completed.status is RunStatus.COMPLETED
    assert retried == completed
    assert worker.calls == calls_after_completion
    assert len(worker.calls) == 5
    assert worker.cancel_calls == ()

    restarted_database = SQLiteDatabase(database_path)
    queries = QueryService(read_session_factory=restarted_database.read_session)
    graph = queries.get_plan_graph(approved.plan_revision_id)
    trace = queries.get_execution_trace(completed.run_id)

    assert trace.run.status is RunStatus.COMPLETED
    assert trace.run.plan_revision_id == graph.plan_revision_id
    assert graph.status == approved.status
    assert tuple(node.kind for node in graph.nodes) == (
        PlanNodeKind.FORK,
        PlanNodeKind.WORK,
        PlanNodeKind.WORK,
        PlanNodeKind.EVALUATOR,
        PlanNodeKind.MERGE,
    )
    assert len(graph.edges) == 7
    assert all(node.status is PlanNodeStatus.COMPLETED for node in graph.nodes)
    assert tuple(branch.status for branch in graph.branches) == (
        BranchStatus.SELECTED,
        BranchStatus.PRUNED,
    )

    graph_fields = {field.name for field in fields(graph)}
    trace_fields = {field.name for field in fields(trace)}
    assert graph_fields.isdisjoint({"attempts", "artifacts", "check_runs", "checkpoints", "events"})
    assert trace_fields.isdisjoint({"nodes", "edges", "branches"})

    assert len(trace.attempts) == 5
    assert tuple(attempt.sequence for attempt in trace.attempts) == (1, 2, 3, 4, 5)
    assert all(attempt.status is AttemptStatus.SUCCEEDED for attempt in trace.attempts)
    assert tuple(attempt.plan_node_id for attempt in trace.attempts) == tuple(
        node.plan_node_id for node in graph.nodes
    )
    assert tuple(call.attempt_id for call in worker.calls) == tuple(
        attempt.attempt_id for attempt in trace.attempts
    )
    assert len({attempt.attempt_id for attempt in trace.attempts}) == 5

    branch_node_ids = {node_id for branch in graph.branches for node_id in branch.node_ids}
    evaluator_call = worker.calls[3]
    assert {artifact.plan_node_id for artifact in evaluator_call.artifact_inputs} == branch_node_ids
    merge_call = worker.calls[-1]
    assert merge_call.plan_node_id == graph.nodes[-1].plan_node_id

    selected_branch, pruned_branch = graph.branches
    selected_node_ids = set(selected_branch.node_ids)
    pruned_node_ids = set(pruned_branch.node_ids)
    merge_input_node_ids = {artifact.plan_node_id for artifact in merge_call.artifact_inputs}
    assert selected_node_ids.issubset(merge_input_node_ids)
    assert merge_input_node_ids.isdisjoint(pruned_node_ids)

    merge_context = merge_call.context
    branch_selection = merge_context["branch_selection"]
    assert isinstance(branch_selection, dict)
    assert branch_selection["selected_branch_id"] == selected_branch.branch_id
    assert branch_selection["fork_node_id"] == selected_branch.fork_node_id
    assert branch_selection["criterion"]
    assert branch_selection["explanation"]
    evidence_artifact_ids = branch_selection["evidence_artifact_ids"]
    assert isinstance(evidence_artifact_ids, list)

    selected_artifacts = merge_context["selected_artifacts"]
    assert isinstance(selected_artifacts, list)
    selected_content = "\n".join(
        artifact["content"]
        for artifact in selected_artifacts
        if isinstance(artifact, dict)
        and artifact["encoding"] == "utf-8"
        and isinstance(artifact["content"], str)
    )
    assert selected_content
    assert all(str(node_id) in selected_content for node_id in selected_node_ids)
    assert all(str(node_id) not in selected_content for node_id in pruned_node_ids)
    selected_artifact_ids = {
        artifact["artifact_id"] for artifact in selected_artifacts if isinstance(artifact, dict)
    }
    assert set(evidence_artifact_ids).issubset(selected_artifact_ids)
    merge_prompt = build_codex_prompt(merge_call)
    assert all(str(node_id) in merge_prompt for node_id in selected_node_ids)
    assert all(str(node_id) not in merge_prompt for node_id in pruned_node_ids)

    assert len(trace.artifacts) == 5
    merge_artifact = next(
        artifact for artifact in trace.artifacts if artifact.plan_node_id == merge_call.plan_node_id
    )
    merge_output = artifact_store.read(merge_artifact.artifact_id).decode("utf-8")
    assert all(str(node_id) in merge_output for node_id in selected_node_ids)
    assert all(str(node_id) not in merge_output for node_id in pruned_node_ids)
    assert len(trace.check_runs) == 5
    assert all(check.status is CheckRunStatus.COMPLETED for check in trace.check_runs)
    assert all(check.result is not None and check.result.passed for check in trace.check_runs)
    assert len(trace.checkpoints) == 5
    assert all(checkpoint.gate_decision.passed for checkpoint in trace.checkpoints)
    assert tuple(checkpoint.gate_decision.attempt_id for checkpoint in trace.checkpoints) == tuple(
        attempt.attempt_id for attempt in trace.attempts
    )
    event_by_offset = {stored.offset: stored.event for stored in trace.events}
    for checkpoint in trace.checkpoints:
        gate_event = event_by_offset[checkpoint.event_offset]
        assert gate_event.type is EventType.GATE_PASSED
        assert gate_event.correlation_id == checkpoint.gate_decision.gate_id
        assert gate_event.payload["gate_id"] == checkpoint.gate_decision.gate_id
    assert all(not checkpoint.branch_selections for checkpoint in trace.checkpoints[:-1])
    final_selection = trace.checkpoints[-1].branch_selections
    assert len(final_selection) == 1
    assert final_selection[0].fork_node_id == graph.branches[0].fork_node_id
    assert final_selection[0].branch_id == graph.branches[0].branch_id

    event_types = tuple(stored.event.type for stored in trace.events)
    assert event_types.count(EventType.ATTEMPT_STARTED) == 5
    assert event_types.count(EventType.ATTEMPT_SUCCEEDED) == 5
    assert event_types.count(EventType.ARTIFACT_CREATED) == 5
    assert event_types.count(EventType.CHECK_STARTED) == 5
    assert event_types.count(EventType.CHECK_PASSED) == 5
    assert event_types.count(EventType.GATE_PASSED) == 5
    assert event_types.count(EventType.CHECKPOINT_CREATED) == 5
    assert event_types.count(EventType.BRANCH_SELECTED) == 1
    assert event_types.count(EventType.BRANCH_PRUNED) == 1
    assert event_types.count(EventType.RUN_COMPLETED) == 1
    assert trace.events[-1].event.type is EventType.RUN_COMPLETED

    selected_event = next(
        stored.event for stored in trace.events if stored.event.type is EventType.BRANCH_SELECTED
    )
    pruned_event = next(
        stored.event for stored in trace.events if stored.event.type is EventType.BRANCH_PRUNED
    )
    assert selected_event.correlation_id == graph.branches[0].branch_id
    assert pruned_event.correlation_id == graph.branches[1].branch_id
    selected_evidence_ids = selected_event.payload["evidence_artifact_ids"]
    assert isinstance(selected_evidence_ids, list)
    selected_node_ids = set(graph.branches[0].node_ids)
    artifact_by_id = {artifact.artifact_id: artifact for artifact in trace.artifacts}
    assert selected_evidence_ids
    assert all(
        artifact_by_id[artifact_id].plan_node_id in selected_node_ids
        for artifact_id in selected_evidence_ids
    )

    offsets = tuple(stored.offset for stored in trace.events)
    assert offsets == tuple(sorted(offsets))
    assert len(offsets) == len(set(offsets))
