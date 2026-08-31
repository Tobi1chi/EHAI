from datetime import UTC, datetime

from ehai import new_id
from ehai.application.checkpointing import RecoveryService
from ehai.application.checks import CheckRunner
from ehai.application.commands import ResumeRun
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import DeterministicPlanner
from ehai.application.ports import CommandReceipt
from ehai.application.run_control import RunController
from ehai.application.service import ExecutionService
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import (
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker

NOW = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)


def test_resume_command_retries_once_and_is_idempotent(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "resume.sqlite3")
    project = Project.create("resume", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "resume", goal_id=new_id(), created_at=NOW)
    contract = CompletionContract.draft(
        goal.goal_id,
        ("candidate exists",),
        (new_id(),),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    check_spec = CheckSpec(
        "candidate",
        CheckKind.ARTIFACT,
        "candidate exists",
        check_id=contract.required_check_ids[0],
    )
    goal = goal.use_completion_contract(contract)
    node = PlanNode.rehydrate(
        plan_node_id=new_id(),
        title="retry",
        instruction="retry once",
        kind=PlanNodeKind.WORK,
        required_dependency_ids=(),
        required_check_ids=contract.required_check_ids,
        status=PlanNodeStatus.FAILED,
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
    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    run = run.pause()
    first_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    first_attempt = first_attempt.cancel("interrupted", at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_check_spec(plan.plan_revision_id, check_spec)
        uow.states.put_run(run)
        uow.states.put_attempt(first_attempt)
        uow.commit()

    worker = FakeWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    check_runner = CheckRunner(
        {
            CheckKind.ARTIFACT: ArtifactCheckAdapter(
                artifact_store,
                {},
                default_rule=ArtifactCheckRule(),
            )
        },
        clock=lambda: NOW,
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=check_runner,
        workspace=tmp_path,
        clock=lambda: NOW,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(clock=lambda: NOW),
        orchestrator=orchestrator,
        run_controller=RunController(database.unit_of_work, worker, clock=lambda: NOW),
        recovery_service=RecoveryService(uow_factory=database.unit_of_work, clock=lambda: NOW),
        clock=lambda: NOW,
    )
    command = ResumeRun("resume-once", run.run_id)
    with database.unit_of_work() as uow:
        uow.events.append(
            Event(
                type=EventType.RUN_PAUSED,
                correlation_id=run.run_id,
                run_id=run.run_id,
                payload={
                    "run_id": run.run_id,
                    "reason": "running Attempts were interrupted",
                },
                occurred_at=NOW,
            )
        )
        uow.command_receipts.put(
            CommandReceipt(
                idempotency_key=command.idempotency_key,
                command_name=type(command).__name__,
                command_fingerprint=command.fingerprint,
                result={"run_id": run.run_id},
                created_at=NOW,
            )
        )
        uow.commit()

    completed = service.resume_run(command)
    retried = service.resume_run(command)

    assert completed.status is RunStatus.COMPLETED
    assert retried == completed
    with database.unit_of_work() as uow:
        attempts = uow.states.list_attempts(run.run_id)
        receipt = uow.command_receipts.get("resume-once")
        checkpoints = uow.states.list_checkpoints(run.run_id)
    assert tuple(attempt.status for attempt in attempts) == (
        AttemptStatus.CANCELLED,
        AttemptStatus.SUCCEEDED,
    )
    assert receipt is not None and receipt.result == {"run_id": run.run_id}
    assert len(checkpoints) == 1
