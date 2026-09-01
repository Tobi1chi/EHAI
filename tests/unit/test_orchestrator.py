import json
import sys
from base64 import b64encode
from datetime import UTC, datetime

import pytest

from ehai import ID, new_id
from ehai.application.checks import CheckRunner
from ehai.application.evaluation import BranchEvaluationContext, BranchSelection
from ehai.application.orchestrator import (
    ArtifactPersistenceError,
    AttemptBudgetExceededError,
    BranchEvaluationError,
    GateRejectedError,
    OrchestrationError,
    Orchestrator,
    WorkerCancellationUnsettledError,
    WorkerFailedError,
    ready_nodes,
)
from ehai.application.workers import (
    CandidateArtifact,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
    WorkerTimedOutError,
)
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, CheckRunStatus, CheckSpec
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, GoalStatus, Project
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import (
    ArtifactCheckAdapter,
    ArtifactCheckRule,
    CommandCheckAdapter,
    SemanticCheckAdapter,
    SemanticRubric,
)
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


class _FailingArtifactStore:
    def put(self, artifact: Artifact, content: bytes) -> None:
        del artifact, content
        raise OSError("controlled Artifact failure")

    def get(self, artifact_id: ID) -> Artifact | None:
        del artifact_id
        return None

    def read(self, artifact_id: ID) -> bytes:
        raise FileNotFoundError(artifact_id)

    def list_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        del run_id
        return ()


class _ErrorWorker:
    def __init__(self, error_type: type[WorkerExecutionError]) -> None:
        self._error_type = error_type

    def execute(self, request: WorkerRequest) -> WorkerResult:
        raise self._error_type(
            request.run_id,
            request.attempt_id,
            request.plan_node_id,
            "controlled worker error",
            raw_output=b'{"type":"turn.failed"}\n',
            log_output=b"controlled stderr\n",
            diagnostics=("exit code 7",),
        )

    def cancel(self, attempt_id: ID) -> None:
        del attempt_id


class _MissingEvidenceEvaluator:
    def evaluate(self, context: BranchEvaluationContext) -> BranchSelection:
        selected, *pruned = context.plan.branches
        return BranchSelection(
            selected.branch_id,
            tuple(branch.branch_id for branch in pruned),
            "controlled criterion",
            (new_id(),),
            "controlled invalid evidence",
        )


class _FailedBranchEvaluator:
    def evaluate(self, context: BranchEvaluationContext) -> BranchSelection:
        selected, *pruned = context.plan.branches
        evidence = next(
            artifact
            for artifact in context.artifacts
            if artifact.plan_node_id in selected.node_ids
            and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
        )
        return BranchSelection(
            selected.branch_id,
            tuple(branch.branch_id for branch in pruned),
            "controlled criterion",
            (evidence.artifact_id,),
            "controlled invalid branch selection",
        )


def _check_runner(store) -> CheckRunner:
    return CheckRunner(
        {
            CheckKind.ARTIFACT: ArtifactCheckAdapter(
                store,
                {},
                default_rule=ArtifactCheckRule(),
            )
        },
        clock=lambda: NOW,
    )


def _approved_plan(
    goal: Goal,
    contract: CompletionContract,
    nodes: tuple[PlanNode, ...],
    edges: tuple[Edge, ...] = (),
) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=nodes,
        edges=edges,
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )


def _seed_single_node(
    database: SQLiteDatabase,
    *,
    check_kind: CheckKind = CheckKind.ARTIFACT,
    check_description: str = "candidate exists",
    check_required: bool = True,
) -> tuple[Goal, PlanRevision, Run, PlanNode]:
    project = Project.create("I3", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "execute one node", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    check_spec = CheckSpec(
        "candidate",
        check_kind,
        check_description,
        required=check_required,
        check_id=check_id,
    )
    contract = CompletionContract.draft(
        goal.goal_id,
        ("candidate exists",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = PlanNode(
        plan_node_id=new_id(),
        title="produce candidate",
        instruction="return one candidate artifact",
        required_check_ids=(check_id,),
    )
    plan = _approved_plan(goal, contract, (node,))
    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    )
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_check_spec(plan.plan_revision_id, check_spec)
        uow.states.put_run(run)
        uow.commit()
    return goal, plan, run, node


def _seed_exploration(
    database: SQLiteDatabase,
    *,
    depth_two: bool = False,
) -> tuple[Goal, PlanRevision, Run, dict[str, PlanNode], tuple[Branch, Branch]]:
    project = Project.create("I6", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "explore two routes", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    check_spec = CheckSpec(
        "candidate",
        CheckKind.ARTIFACT,
        "candidate exists",
        check_id=check_id,
    )
    contract = CompletionContract.draft(
        goal.goal_id,
        ("candidate exists",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    required_checks = (check_id,)
    fork = PlanNode(
        new_id(),
        "fork",
        "start exploration",
        kind=PlanNodeKind.FORK,
        required_check_ids=required_checks,
    )
    left = PlanNode(new_id(), "left", "try left", required_check_ids=required_checks)
    right = PlanNode(new_id(), "right", "try right", required_check_ids=required_checks)
    left_tail = (
        PlanNode(
            new_id(),
            "left tail",
            "finish left",
            required_dependency_ids=(left.plan_node_id,),
            required_check_ids=required_checks,
        )
        if depth_two
        else None
    )
    right_tail = (
        PlanNode(
            new_id(),
            "right tail",
            "finish right",
            required_dependency_ids=(right.plan_node_id,),
            required_check_ids=required_checks,
        )
        if depth_two
        else None
    )
    left_endpoint = left if left_tail is None else left_tail
    right_endpoint = right if right_tail is None else right_tail
    evaluator = PlanNode(
        new_id(),
        "evaluate",
        "evaluate branches",
        kind=PlanNodeKind.EVALUATOR,
        required_dependency_ids=(left_endpoint.plan_node_id, right_endpoint.plan_node_id),
        required_check_ids=required_checks,
    )
    merge = PlanNode(
        new_id(),
        "merge",
        "merge selected route",
        kind=PlanNodeKind.MERGE,
        required_dependency_ids=(evaluator.plan_node_id,),
        required_check_ids=required_checks,
    )
    left_branch = Branch(
        new_id(),
        "left",
        fork.plan_node_id,
        (left.plan_node_id,) if left_tail is None else (left.plan_node_id, left_tail.plan_node_id),
        merge.plan_node_id,
    )
    right_branch = Branch(
        new_id(),
        "right",
        fork.plan_node_id,
        (right.plan_node_id,)
        if right_tail is None
        else (right.plan_node_id, right_tail.plan_node_id),
        merge.plan_node_id,
    )
    edges = [
        Edge(
            new_id(),
            fork.plan_node_id,
            left.plan_node_id,
            EdgeType.EXPLORATION,
            left_branch.branch_id,
        ),
        Edge(
            new_id(),
            left_endpoint.plan_node_id,
            merge.plan_node_id,
            EdgeType.MERGE,
            left_branch.branch_id,
        ),
        Edge(
            new_id(),
            fork.plan_node_id,
            right.plan_node_id,
            EdgeType.EXPLORATION,
            right_branch.branch_id,
        ),
        Edge(
            new_id(),
            right_endpoint.plan_node_id,
            merge.plan_node_id,
            EdgeType.MERGE,
            right_branch.branch_id,
        ),
        Edge(
            new_id(),
            left_endpoint.plan_node_id,
            evaluator.plan_node_id,
            EdgeType.DEPENDENCY,
        ),
        Edge(
            new_id(),
            right_endpoint.plan_node_id,
            evaluator.plan_node_id,
            EdgeType.DEPENDENCY,
        ),
        Edge(new_id(), evaluator.plan_node_id, merge.plan_node_id, EdgeType.DEPENDENCY),
    ]
    if left_tail is not None and right_tail is not None:
        edges.extend(
            (
                Edge(
                    new_id(),
                    left.plan_node_id,
                    left_tail.plan_node_id,
                    EdgeType.DEPENDENCY,
                    left_branch.branch_id,
                ),
                Edge(
                    new_id(),
                    right.plan_node_id,
                    right_tail.plan_node_id,
                    EdgeType.DEPENDENCY,
                    right_branch.branch_id,
                ),
            )
        )
    branch_nodes = (
        (left, right)
        if left_tail is None or right_tail is None
        else (left, left_tail, right, right_tail)
    )
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(fork, *branch_nodes, evaluator, merge),
        edges=tuple(edges),
        branches=(left_branch, right_branch),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(goal.goal_id, plan.plan_revision_id, run_id=new_id(), created_at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_check_spec(plan.plan_revision_id, check_spec)
        uow.states.put_run(run)
        uow.commit()
    nodes_by_name = {
        "fork": fork,
        "left": left,
        "right": right,
        "evaluator": evaluator,
        "merge": merge,
    }
    if left_tail is not None and right_tail is not None:
        nodes_by_name["left_tail"] = left_tail
        nodes_by_name["right_tail"] = right_tail
    return (
        goal,
        plan,
        run,
        nodes_by_name,
        (left_branch, right_branch),
    )


def _rehydrated_node(node: PlanNode, status: PlanNodeStatus) -> PlanNode:
    return PlanNode.rehydrate(
        plan_node_id=node.plan_node_id,
        title=node.title,
        instruction=node.instruction,
        kind=node.kind,
        required_dependency_ids=node.required_dependency_ids,
        required_check_ids=node.required_check_ids,
        status=status,
    )


def test_ready_nodes_requires_approval_and_preserves_node_order() -> None:
    project = Project.create("ready", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "ready", goal_id=new_id(), created_at=NOW)
    contract = CompletionContract.draft(
        goal.goal_id,
        ("done",),
        (new_id(),),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    dependency = _rehydrated_node(
        PlanNode(new_id(), "dependency", "done"), PlanNodeStatus.COMPLETED
    )
    first_ready = PlanNode(new_id(), "first", "ready")
    second_ready = PlanNode(
        new_id(),
        "second",
        "ready after dependency",
        required_dependency_ids=(dependency.plan_node_id,),
    )
    pruned = _rehydrated_node(PlanNode(new_id(), "pruned", "ignored"), PlanNodeStatus.PRUNED)
    edge = Edge(new_id(), dependency.plan_node_id, second_ready.plan_node_id, EdgeType.DEPENDENCY)
    plan = _approved_plan(goal, contract, (first_ready, second_ready, dependency, pruned), (edge,))

    assert ready_nodes(plan) == (first_ready, second_ready)

    draft = PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=plan.nodes,
        edges=plan.edges,
        branches=(),
        created_at=plan.created_at,
        status=PlanRevisionStatus.DRAFT,
        approved_at=None,
        supersedes_plan_revision_id=None,
    )
    with pytest.raises(OrchestrationError, match="must be approved"):
        ready_nodes(draft)


def test_orchestrator_completes_single_node_through_gate_and_checkpoint(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "happy.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    goal, plan, run, _ = _seed_single_node(database)
    worker = FakeWorker()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    completed_run = orchestrator.execute(run.run_id)

    assert completed_run.status is RunStatus.COMPLETED
    assert len(worker.calls) == 1
    assert worker.calls[0].run.status is RunStatus.RUNNING
    assert worker.calls[0].attempt.status is AttemptStatus.RUNNING
    assert worker.calls[0].plan_node.status is PlanNodeStatus.RUNNING
    with database.unit_of_work() as uow:
        stored_goal = uow.states.get_goal(goal.goal_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        check_runs = uow.states.list_check_runs(run.run_id)
        checkpoints = uow.states.list_checkpoints(run.run_id)
        event_types = tuple(item.event.type for item in uow.events.list_events())

    assert stored_goal is not None and stored_goal.status is GoalStatus.SATISFIED
    assert stored_plan is not None and stored_plan.nodes[0].status is PlanNodeStatus.COMPLETED
    assert len(attempts) == 1 and attempts[0].status is AttemptStatus.SUCCEEDED
    assert len(artifacts) == 1
    assert artifact_store.get(artifacts[0].artifact_id) == artifacts[0]
    assert len(check_runs) == 1 and check_runs[0].result is not None
    assert check_runs[0].result.passed
    assert len(checkpoints) == 1
    assert EventType.GATE_PASSED in event_types
    assert EventType.CHECKPOINT_CREATED in event_types
    assert event_types[-1] is EventType.RUN_COMPLETED


def test_orchestrator_records_worker_failure_before_raising(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "failure.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, plan, run, _ = _seed_single_node(database)
    worker = _ErrorWorker(WorkerExecutionError)
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(WorkerFailedError, match="controlled worker error"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        failed_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        events = tuple(item.event.type for item in uow.events.list_events())
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert failed_plan is not None and failed_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert len(attempts) == 1 and attempts[0].status is AttemptStatus.FAILED
    assert [artifact.kind for artifact in artifacts].count(ArtifactKind.WORKER_OUTPUT) == 1
    assert [artifact.kind for artifact in artifacts].count(ArtifactKind.LOG) == 2
    content_by_name = {
        artifact.name: artifact_store.read(artifact.artifact_id) for artifact in artifacts
    }
    assert content_by_name == {
        "worker-error-output.txt": b'{"type":"turn.failed"}\n',
        "worker-error-stderr.log": b"controlled stderr\n",
        "worker-error-diagnostics.log": b"exit code 7",
    }
    assert events.count(EventType.ARTIFACT_CREATED) == 3
    assert events[-3:] == (
        EventType.ATTEMPT_FAILED,
        EventType.PLAN_NODE_FAILED,
        EventType.RUN_FAILED,
    )


def test_orchestrator_maps_worker_timeout_and_persists_diagnostics(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "worker-timeout.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, plan, run, _ = _seed_single_node(database)
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=_ErrorWorker(WorkerTimedOutError),
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(WorkerFailedError, match="timed out"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        failed_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        events = tuple(item.event.type for item in uow.events.list_events())
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert failed_plan is not None and failed_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert attempts[0].status is AttemptStatus.TIMED_OUT
    assert len(artifacts) == 3
    assert EventType.ATTEMPT_TIMED_OUT in events
    assert EventType.ATTEMPT_FAILED not in events


def test_timeout_diagnostic_store_failure_still_records_timed_out_state(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "worker-timeout-artifact-failure.sqlite3")
    _, plan, run, _ = _seed_single_node(database)
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=_ErrorWorker(WorkerTimedOutError),
        artifact_store=_FailingArtifactStore(),
        check_runner=_check_runner(_FailingArtifactStore()),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(ArtifactPersistenceError, match="diagnostics could not be persisted"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        events = tuple(item.event.type for item in uow.events.list_events())
    assert stored_run is not None and stored_run.status is RunStatus.FAILED
    assert stored_plan is not None and stored_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert attempts[0].status is AttemptStatus.TIMED_OUT
    assert "diagnostic Artifact persistence failed" in (attempts[0].outcome_reason or "")
    assert artifacts == ()
    assert EventType.ATTEMPT_TIMED_OUT in events


def test_worker_output_and_logs_are_persisted_but_excluded_from_gate_evidence(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "diagnostic-evidence.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, _, run, node = _seed_single_node(database)
    result = WorkerResult(
        artifacts=(
            CandidateArtifact(
                ArtifactKind.CANDIDATE,
                "candidate.txt",
                "text/plain",
                b"candidate",
            ),
            CandidateArtifact(
                ArtifactKind.WORKER_OUTPUT,
                "worker-output.jsonl",
                "application/x-ndjson",
                b'{"type":"turn.completed"}\n',
            ),
            CandidateArtifact(
                ArtifactKind.LOG,
                "worker-stderr.log",
                "text/plain",
                b"diagnostic",
            ),
        ),
        summary="candidate plus diagnostics",
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=FakeWorker(results_by_node={node.plan_node_id: result}),
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    completed = orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        check_run = uow.states.list_check_runs(run.run_id)[0]
    candidate_ids = tuple(
        artifact.artifact_id for artifact in artifacts if artifact.kind is ArtifactKind.CANDIDATE
    )
    assert completed.status is RunStatus.COMPLETED
    assert check_run.result is not None
    assert check_run.result.evidence_artifact_ids == candidate_ids
    assert len(artifacts) == 3


def test_worker_cancel_without_run_control_fails_closed_without_failed_state(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "unsettled-cancel.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, plan, run, _ = _seed_single_node(database)
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=_ErrorWorker(WorkerCancelledError),
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
        cancellation_settle_seconds=0.02,
    )

    with pytest.raises(WorkerCancellationUnsettledError, match="no RunController"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        events = tuple(item.event.type for item in uow.events.list_events())
    assert stored_run is not None and stored_run.status is RunStatus.RUNNING
    assert stored_plan is not None and stored_plan.nodes[0].status is PlanNodeStatus.RUNNING
    assert attempts[0].status is AttemptStatus.RUNNING
    assert len(artifacts) == 3
    assert EventType.ATTEMPT_FAILED not in events
    assert EventType.RUN_FAILED not in events


def test_claim_rolls_back_run_and_node_when_attempt_cannot_be_created(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "claim-rollback.sqlite3")
    _, plan, run, node = _seed_single_node(database)
    existing_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    )
    with database.unit_of_work() as uow:
        uow.states.put_attempt(existing_attempt)
        uow.commit()
    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
        id_factory=lambda: existing_attempt.attempt_id,
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
    assert stored_run is not None and stored_run.status is RunStatus.PENDING
    assert stored_plan is not None and stored_plan.nodes[0].status is PlanNodeStatus.PENDING
    assert attempts == (existing_attempt,)
    assert worker.calls == ()


def test_artifact_failure_keeps_worker_success_semantics(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "artifact-failure.sqlite3")
    _, plan, run, _ = _seed_single_node(database)
    worker = FakeWorker()
    artifact_store = _FailingArtifactStore()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(ArtifactPersistenceError, match="controlled Artifact failure"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        failed_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        events = tuple(item.event.type for item in uow.events.list_events())
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert failed_plan is not None and failed_plan.nodes[0].status is PlanNodeStatus.FAILED
    assert len(attempts) == 1 and attempts[0].status is AttemptStatus.SUCCEEDED
    assert attempts[0].artifact_ids == ()
    assert artifacts == ()
    assert EventType.ATTEMPT_SUCCEEDED in events
    assert EventType.ATTEMPT_FAILED not in events
    assert events[-2:] == (EventType.PLAN_NODE_FAILED, EventType.RUN_FAILED)


def test_contract_mismatch_is_rejected_before_worker_or_artifact_side_effects(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "contract-mismatch.sqlite3")
    goal, _, run, _ = _seed_single_node(database)
    current = goal.completion_contract
    assert current is not None
    replacement = current.revise(
        ("new current contract",),
        current.required_check_ids,
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    changed_goal = goal.use_completion_contract(replacement)
    with database.unit_of_work() as uow:
        uow.states.put_completion_contract(replacement)
        uow.states.put_goal(changed_goal)
        uow.commit()
    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(OrchestrationError, match="current CompletionContract version"):
        orchestrator.execute(run.run_id)

    assert worker.calls == ()
    assert artifact_store.list_for_run(run.run_id) == ()
    with database.unit_of_work() as uow:
        unchanged = uow.states.get_run(run.run_id)
    assert unchanged is not None and unchanged.status is RunStatus.PENDING


@pytest.mark.parametrize("invalid_spec", ["missing", "optional"])
def test_invalid_required_check_spec_is_rejected_before_worker_side_effects(
    tmp_path,
    invalid_spec: str,
) -> None:
    database = SQLiteDatabase(tmp_path / f"{invalid_spec}-check-spec.sqlite3")
    _, plan, run, node = _seed_single_node(
        database,
        check_required=invalid_spec != "optional",
    )
    if invalid_spec == "missing":
        connection = database.connect()
        try:
            connection.execute(
                "DELETE FROM check_specs WHERE check_id = ?",
                (node.required_check_ids[0],),
            )
        finally:
            connection.close()
    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(OrchestrationError, match="CheckSpecs are missing or optional"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        artifacts = uow.states.list_artifacts_for_run(run.run_id)
        check_runs = uow.states.list_check_runs(run.run_id)
    assert worker.calls == ()
    assert stored_run is not None and stored_run.status is RunStatus.PENDING
    assert stored_plan is not None and stored_plan.nodes[0].status is PlanNodeStatus.PENDING
    assert attempts == ()
    assert artifacts == ()
    assert check_runs == ()


def test_orchestrator_executes_exploration_serially_and_completes_only_merge(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "exploration.sqlite3")
    goal, plan, run, nodes, branches = _seed_exploration(database)
    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    completed = orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_goal = uow.states.get_goal(goal.goal_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        checkpoints = uow.states.list_checkpoints(run.run_id)
        events = tuple(item.event for item in uow.events.list_events())
    assert completed.status is RunStatus.COMPLETED
    assert stored_goal is not None and stored_goal.status is GoalStatus.SATISFIED
    assert stored_plan is not None
    assert tuple(node.status for node in stored_plan.nodes) == (
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.COMPLETED,
    )
    assert tuple(branch.status for branch in stored_plan.branches) == (
        BranchStatus.SELECTED,
        BranchStatus.PRUNED,
    )
    assert tuple(call.plan_node_id for call in worker.calls) == tuple(
        nodes[name].plan_node_id for name in ("fork", "left", "right", "evaluator", "merge")
    )
    evaluator_call = worker.calls[3]
    assert {artifact.plan_node_id for artifact in evaluator_call.artifact_inputs} == {
        nodes["left"].plan_node_id,
        nodes["right"].plan_node_id,
    }
    assert len(attempts) == 5
    assert len(checkpoints) == 5
    attempt_events = tuple(event for event in events if event.type is EventType.ATTEMPT_STARTED)
    assert [event.payload["attempt_budget_consumed"] for event in attempt_events] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert all(event.payload["attempt_budget_limit"] == 5 for event in attempt_events)
    selected_event = next(event for event in events if event.type is EventType.BRANCH_SELECTED)
    pruned_event = next(event for event in events if event.type is EventType.BRANCH_PRUNED)
    assert selected_event.correlation_id == branches[0].branch_id
    assert pruned_event.correlation_id == branches[1].branch_id
    assert selected_event.payload["criterion"]
    assert selected_event.payload["evidence_artifact_ids"]
    evaluator_completed_index = next(
        index
        for index, event in enumerate(events)
        if event.type is EventType.PLAN_NODE_COMPLETED
        and event.payload["plan_node_id"] == nodes["evaluator"].plan_node_id
    )
    selection_index = events.index(selected_event)
    assert evaluator_completed_index < selection_index
    assert sum(event.type is EventType.GATE_PASSED for event in events[:selection_index]) == 4
    assert events[-1].type is EventType.RUN_COMPLETED


def test_merge_snapshots_non_utf8_selected_candidate_as_base64(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "binary-selected.sqlite3")
    _, _, run, nodes, _ = _seed_exploration(database)
    selected_content = b"\xff\x00selected"
    worker = FakeWorker(
        results_by_node={
            nodes["left"].plan_node_id: WorkerResult(
                artifacts=(
                    CandidateArtifact(
                        ArtifactKind.CANDIDATE,
                        "selected.bin",
                        "application/octet-stream",
                        selected_content,
                    ),
                ),
                summary="binary selected candidate",
            )
        }
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "binary-artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    completed = orchestrator.execute(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    merge_context = worker.calls[-1].context
    selected_artifacts = merge_context["selected_artifacts"]
    assert isinstance(selected_artifacts, list)
    binary_snapshot = next(
        artifact
        for artifact in selected_artifacts
        if isinstance(artifact, dict) and artifact["media_type"] == "application/octet-stream"
    )
    assert binary_snapshot["encoding"] == "base64"
    assert binary_snapshot["content"] == b64encode(selected_content).decode("ascii")


@pytest.mark.parametrize("failure_kind", ["worker", "check"])
def test_one_failed_branch_is_pruned_and_other_branch_completes(
    tmp_path,
    failure_kind: str,
) -> None:
    database = SQLiteDatabase(tmp_path / f"one-failed-{failure_kind}.sqlite3")
    _, plan, run, nodes, branches = _seed_exploration(database)
    worker = (
        FakeWorker(failures_by_node={nodes["left"].plan_node_id: "left failed"})
        if failure_kind == "worker"
        else FakeWorker(
            results_by_node={
                nodes["left"].plan_node_id: WorkerResult(
                    artifacts=(
                        CandidateArtifact(
                            ArtifactKind.LOG,
                            "left.log",
                            "text/plain",
                            b"no candidate evidence",
                        ),
                    ),
                    summary="log only",
                )
            }
        )
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    completed = orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        event_types = tuple(item.event.type for item in uow.events.list_events())
    assert completed.status is RunStatus.COMPLETED
    assert stored_plan is not None
    assert stored_plan.nodes[1].status is PlanNodeStatus.FAILED
    assert tuple(branch.status for branch in stored_plan.branches) == (
        BranchStatus.PRUNED,
        BranchStatus.SELECTED,
    )
    assert attempts[1].status is (
        AttemptStatus.FAILED if failure_kind == "worker" else AttemptStatus.SUCCEEDED
    )
    assert EventType.RUN_FAILED not in event_types
    assert event_types.count(EventType.BRANCH_SELECTED) == 1
    assert event_types.count(EventType.BRANCH_PRUNED) == 1
    assert branches[1].branch_id == stored_plan.branches[1].branch_id


def test_depth_two_branch_failure_terminates_path_and_prunes_pending_tail(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "depth-two-failure.sqlite3")
    _, plan, run, nodes, _ = _seed_exploration(database, depth_two=True)
    worker = FakeWorker(failures_by_node={nodes["left"].plan_node_id: "left failed"})
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
        attempt_budget=7,
    )

    completed = orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        event_types = tuple(item.event.type for item in uow.events.list_events())
    assert completed.status is RunStatus.COMPLETED
    assert stored_plan is not None
    node_by_id = {node.plan_node_id: node for node in stored_plan.nodes}
    assert node_by_id[nodes["left"].plan_node_id].status is PlanNodeStatus.FAILED
    assert node_by_id[nodes["left_tail"].plan_node_id].status is PlanNodeStatus.PRUNED
    assert nodes["left_tail"].plan_node_id not in {attempt.plan_node_id for attempt in attempts}
    assert tuple(branch.status for branch in stored_plan.branches) == (
        BranchStatus.PRUNED,
        BranchStatus.SELECTED,
    )
    evaluator_call = next(
        call for call in worker.calls if call.plan_node_id == nodes["evaluator"].plan_node_id
    )
    assert {artifact.plan_node_id for artifact in evaluator_call.artifact_inputs} == {
        nodes["right"].plan_node_id,
        nodes["right_tail"].plan_node_id,
    }
    assert EventType.PLAN_NODE_PRUNED in event_types


def test_all_failed_branches_fail_after_evaluator_cannot_select(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "all-branches-failed.sqlite3")
    _, _, run, nodes, _ = _seed_exploration(database)
    worker = FakeWorker(
        failures_by_node={
            nodes["left"].plan_node_id: "left failed",
            nodes["right"].plan_node_id: "right failed",
        }
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(BranchEvaluationError, match="no active Branch"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        attempts = uow.states.list_attempts(run.run_id)
        checkpoints = uow.states.list_checkpoints(run.run_id)
        event_types = tuple(item.event.type for item in uow.events.list_events())
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert tuple(attempt.plan_node_id for attempt in attempts) == (
        nodes["fork"].plan_node_id,
        nodes["left"].plan_node_id,
        nodes["right"].plan_node_id,
        nodes["evaluator"].plan_node_id,
    )
    assert worker.calls[-1].plan_node_id == nodes["evaluator"].plan_node_id
    assert worker.calls[-1].artifact_inputs == ()
    assert len(checkpoints) == 2
    assert EventType.BRANCH_SELECTED not in event_types
    assert event_types[-1] is EventType.RUN_FAILED


def test_invalid_selection_evidence_fails_closed_after_evaluator(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "invalid-selection-evidence.sqlite3")
    _, _, run, nodes, _ = _seed_exploration(database)
    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        branch_evaluator=_MissingEvidenceEvaluator(),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(BranchEvaluationError, match="evidence"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        attempts = uow.states.list_attempts(run.run_id)
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert nodes["evaluator"].plan_node_id in {attempt.plan_node_id for attempt in attempts}


def test_evaluator_cannot_select_a_branch_that_failed_its_local_gate(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "failed-selection.sqlite3")
    _, plan, run, nodes, _ = _seed_exploration(database)
    worker = FakeWorker(
        results_by_node={
            nodes["left"].plan_node_id: WorkerResult(
                artifacts=(
                    CandidateArtifact(
                        ArtifactKind.CANDIDATE,
                        "empty-left.txt",
                        "text/plain",
                        b"",
                    ),
                ),
                summary="empty candidate",
            )
        }
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        branch_evaluator=_FailedBranchEvaluator(),
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(BranchEvaluationError, match="non-viable Branch"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert stored_plan is not None
    assert tuple(branch.status for branch in stored_plan.branches) == (
        BranchStatus.ACTIVE,
        BranchStatus.ACTIVE,
    )
    assert nodes["evaluator"].plan_node_id in {attempt.plan_node_id for attempt in attempts}


def test_attempt_budget_exhaustion_fails_before_dispatch(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "attempt-budget.sqlite3")
    _, _, run, nodes, _ = _seed_exploration(database)
    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=_check_runner(artifact_store),
        workspace=tmp_path,
        clock=lambda: NOW,
        attempt_budget=4,
    )

    with pytest.raises(AttemptBudgetExceededError, match="consumed=4, limit=4"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        stored_plan = uow.states.get_plan_revision(run.plan_revision_id)
        attempts = uow.states.list_attempts(run.run_id)
        events = tuple(item.event for item in uow.events.list_events())
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert stored_plan is not None
    assert stored_plan.nodes[-1].status is PlanNodeStatus.PENDING
    assert len(attempts) == 4
    assert nodes["merge"].plan_node_id not in {attempt.plan_node_id for attempt in attempts}
    assert [
        event.payload["attempt_budget_consumed"]
        for event in events
        if event.type is EventType.ATTEMPT_STARTED
    ] == [1, 2, 3, 4]
    assert not any(
        event.type is EventType.PLAN_NODE_READIED
        and event.payload["plan_node_id"] == nodes["merge"].plan_node_id
        for event in events
    )
    assert events[-1].type is EventType.RUN_FAILED


def test_orchestrator_semantic_check_rejects_missing_required_term(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "semantic.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, _, run, node = _seed_single_node(
        database,
        check_kind=CheckKind.SEMANTIC,
        check_description="candidate must contain NEVER_PRESENT_TERM",
    )
    check_id = node.required_check_ids[0]
    check_runner = CheckRunner(
        {
            CheckKind.SEMANTIC: SemanticCheckAdapter(
                artifact_store,
                {
                    check_id: SemanticRubric(
                        "required term must appear",
                        ("NEVER_PRESENT_TERM",),
                    )
                },
            )
        },
        clock=lambda: NOW,
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=FakeWorker(),
        artifact_store=artifact_store,
        check_runner=check_runner,
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(GateRejectedError, match="failed Gate"):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        failed_run = uow.states.get_run(run.run_id)
        check_runs = uow.states.list_check_runs(run.run_id)
        checkpoints = uow.states.list_checkpoints(run.run_id)
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
    assert check_runs[0].result is not None and not check_runs[0].result.passed
    output = json.loads(check_runs[0].result.output or "")
    assert output["score"] == 0.0
    assert output["rubric"]["required_terms"] == ["never_present_term"]
    assert checkpoints == ()


def test_orchestrator_artifact_rule_failure_is_fail_closed(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "artifact-check.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, _, run, node = _seed_single_node(database)
    check_id = node.required_check_ids[0]
    check_runner = CheckRunner(
        {
            CheckKind.ARTIFACT: ArtifactCheckAdapter(
                artifact_store,
                {check_id: ArtifactCheckRule(allowed_media_types=("application/json",))},
            )
        },
        clock=lambda: NOW,
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=FakeWorker(),
        artifact_store=artifact_store,
        check_runner=check_runner,
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(GateRejectedError):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        check_run = uow.states.list_check_runs(run.run_id)[0]
        failed_run = uow.states.get_run(run.run_id)
    assert check_run.result is not None and not check_run.result.passed
    assert "not allowed" in (check_run.result.failure_reason or "")
    assert failed_run is not None and failed_run.status is RunStatus.FAILED


def test_orchestrator_command_timeout_persists_terminal_check(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "command-timeout.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    _, _, run, node = _seed_single_node(database, check_kind=CheckKind.COMMAND)
    check_id = node.required_check_ids[0]
    check_runner = CheckRunner(
        {
            CheckKind.COMMAND: CommandCheckAdapter(
                {check_id: (sys.executable, "-c", "import time; time.sleep(5)")},
                timeout_seconds=0.05,
            )
        },
        clock=lambda: NOW,
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=FakeWorker(),
        artifact_store=artifact_store,
        check_runner=check_runner,
        workspace=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(GateRejectedError):
        orchestrator.execute(run.run_id)

    with database.unit_of_work() as uow:
        check_run = uow.states.list_check_runs(run.run_id)[0]
        failed_run = uow.states.get_run(run.run_id)
    assert check_run.status is CheckRunStatus.TIMED_OUT
    assert check_run.failure_reason is not None and "timed out" in check_run.failure_reason
    assert failed_run is not None and failed_run.status is RunStatus.FAILED
