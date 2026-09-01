from pathlib import Path

import pytest

from ehai import ID
from ehai.application.checks import CheckRunner
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    ReplanPlan,
    StartRun,
)
from ehai.application.orchestrator import GateRejectedError, Orchestrator
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION, DeterministicPlanner
from ehai.application.queries import QueryService
from ehai.application.service import ExecutionService
from ehai.application.workers import CandidateArtifact, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind
from ehai.domain.execution import RunStatus
from ehai.domain.planning import PlanRevisionStatus
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase


class _LogOnlyWorker:
    def execute(self, request: WorkerRequest) -> WorkerResult:
        del request
        return WorkerResult(
            artifacts=(
                CandidateArtifact(
                    ArtifactKind.LOG,
                    "worker.log",
                    "text/plain",
                    b"diagnostic only",
                ),
            ),
            summary="no candidate evidence",
        )

    def cancel(self, attempt_id: ID) -> None:
        del attempt_id


def test_replan_versions_contract_and_preserves_base_run_trace(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "replan.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = _LogOnlyWorker()
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=Orchestrator(
            uow_factory=database.unit_of_work,
            worker=worker,
            artifact_store=artifact_store,
            check_runner=CheckRunner(
                {
                    CheckKind.ARTIFACT: ArtifactCheckAdapter(
                        artifact_store,
                        {},
                        default_rule=ArtifactCheckRule(),
                    )
                }
            ),
            workspace=tmp_path,
        ),
    )
    queries = QueryService(read_session_factory=database.read_session)
    project = service.create_project(CreateProject("project", "replan"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "revise the plan"))
    base = service.propose_plan(
        ProposePlan("base-plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )
    base = service.approve_plan(
        ApprovePlan("approve-base", base.plan_revision_id, base.completion_contract_id)
    )
    with pytest.raises(GateRejectedError):
        service.start_run(StartRun("base-run", base.plan_revision_id))
    with database.unit_of_work() as uow:
        base_contract = uow.states.get_completion_contract(base.completion_contract_id)
        base_after_run = uow.states.get_plan_revision(base.plan_revision_id)
        base_runs = uow.states.list_runs(goal.goal_id)
        base_checks = uow.states.list_check_specs(base.plan_revision_id)
    assert base_contract is not None
    assert base_after_run is not None
    assert len(base_runs) == 1 and base_runs[0].status is RunStatus.FAILED
    base_trace = queries.get_execution_trace(base_runs[0].run_id)

    command = ReplanPlan(
        "replan",
        base.plan_revision_id,
        (NON_EMPTY_ARTIFACT_CRITERION,),
    )
    revised = service.replan_plan(command)
    replayed = service.replan_plan(command)

    assert replayed == revised
    assert base.status is PlanRevisionStatus.APPROVED
    assert revised.status is PlanRevisionStatus.DRAFT
    assert revised.version == 2
    assert revised.supersedes_plan_revision_id == base.plan_revision_id
    assert revised.completion_contract_version == 2
    assert revised.completion_contract_id != base.completion_contract_id
    with database.unit_of_work() as uow:
        stored_base = uow.states.get_plan_revision(base.plan_revision_id)
        revised_contract = uow.states.get_completion_contract(revised.completion_contract_id)
        contracts = uow.states.list_completion_contracts(goal.goal_id)
        plans = uow.states.list_plan_revisions(goal.goal_id)
        revised_checks = uow.states.list_check_specs(revised.plan_revision_id)
        receipt = uow.command_receipts.get(command.idempotency_key)
    assert stored_base == base_after_run
    assert revised_contract is not None and not revised_contract.is_confirmed
    assert revised_contract.version == 2
    assert (
        revised_contract.supersedes_completion_contract_id == base_contract.completion_contract_id
    )
    assert tuple(contract.version for contract in contracts) == (1, 2)
    assert tuple(plan.version for plan in plans) == (1, 2)
    assert len(revised_checks) == 1
    assert revised_checks[0].check_id != base_checks[0].check_id
    assert receipt is not None and receipt.result["plan_revision_id"] == revised.plan_revision_id
    assert queries.get_execution_trace(base_runs[0].run_id) == base_trace

    approved = service.approve_plan(
        ApprovePlan(
            "approve-replan",
            revised.plan_revision_id,
            revised.completion_contract_id,
        )
    )
    assert approved.status is PlanRevisionStatus.APPROVED
    assert approved.version == 2
    replayed_after_approval = service.replan_plan(command)
    assert replayed_after_approval == approved
    with database.unit_of_work() as uow:
        assert tuple(
            contract.version for contract in uow.states.list_completion_contracts(goal.goal_id)
        ) == (1, 2)
        assert tuple(plan.version for plan in uow.states.list_plan_revisions(goal.goal_id)) == (
            1,
            2,
        )
    assert queries.get_execution_trace(base_runs[0].run_id) == base_trace
