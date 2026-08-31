import json
import sys
from datetime import UTC, datetime

import pytest

from ehai import ID, new_id
from ehai.application.checks import CheckRunner
from ehai.application.orchestrator import (
    ArtifactPersistenceError,
    GateRejectedError,
    OrchestrationError,
    Orchestrator,
    UnsupportedPlanError,
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
    Edge,
    EdgeType,
    PlanNode,
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


def test_orchestrator_rejects_non_single_node_plan_without_calling_worker(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "multi.sqlite3")
    project = Project.create("multi", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "multi", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("done",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    nodes = (
        PlanNode(new_id(), "one", "one", required_check_ids=(check_id,)),
        PlanNode(new_id(), "two", "two"),
    )
    plan = _approved_plan(goal, contract, nodes)
    run = Run(goal.goal_id, plan.plan_revision_id, run_id=new_id(), created_at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
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

    with pytest.raises(UnsupportedPlanError, match="supports one PlanNode"):
        orchestrator.execute(run.run_id)

    assert worker.calls == ()
    with database.unit_of_work() as uow:
        unchanged = uow.states.get_run(run.run_id)
    assert unchanged is not None and unchanged.status is RunStatus.PENDING


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
