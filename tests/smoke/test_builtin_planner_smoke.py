from __future__ import annotations

import json
import os
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
from ehai.domain.events import EventType
from ehai.domain.execution import RunStatus
from ehai.domain.planning import PlanNodeKind, PlanRevisionStatus
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.planners import BuiltinPlannerAdapter
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.cli import build_service


class _FailThenRecoverWorker:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: WorkerRequest) -> WorkerResult:
        self.calls += 1
        if self.calls == 1:
            return WorkerResult(
                artifacts=(
                    CandidateArtifact(
                        ArtifactKind.LOG,
                        "failed.log",
                        "text/plain",
                        b"candidate evidence was not produced",
                    ),
                ),
                summary="injected missing-candidate failure",
            )
        if request.plan_node.kind is PlanNodeKind.EVALUATOR:
            branches = request.context["candidate_branches"]
            assert isinstance(branches, list)
            selected = branches[0]
            assert isinstance(selected, dict)
            selected_node_ids = selected["node_ids"]
            assert isinstance(selected_node_ids, list)
            selected_artifacts = [
                artifact
                for artifact in request.artifact_inputs
                if artifact.plan_node_id in selected_node_ids
            ]
            content = json.dumps(
                {
                    "selected_branch_id": selected["branch_id"],
                    "pruned_branch_ids": [
                        branch["branch_id"] for branch in branches[1:] if isinstance(branch, dict)
                    ],
                    "criterion": "select verified candidate evidence",
                    "explanation": "selected the first valid recovery branch",
                    "compared_artifact_ids": [
                        artifact.artifact_id for artifact in request.artifact_inputs
                    ],
                    "selected_artifact_ids": [
                        artifact.artifact_id for artifact in selected_artifacts
                    ],
                }
            ).encode()
            return WorkerResult(
                artifacts=(
                    CandidateArtifact(
                        ArtifactKind.CANDIDATE,
                        "selection.json",
                        "application/json",
                        content,
                    ),
                ),
                summary="selected recovery branch",
            )
        return WorkerResult(
            artifacts=(
                CandidateArtifact(
                    ArtifactKind.CANDIDATE,
                    f"recovered-{self.calls}.txt",
                    "text/plain",
                    b"recovered candidate evidence",
                ),
            ),
            summary="recovered candidate",
        )

    def cancel(self, attempt_id: ID) -> None:
        del attempt_id


@pytest.mark.skipif(
    os.environ.get("EHAI_RUN_BUILTIN_PLANNER_SMOKE") != "1",
    reason="set EHAI_RUN_BUILTIN_PLANNER_SMOKE=1 for the explicit Planner smoke",
)
def test_real_builtin_planner_proposes_grounded_plan_without_execution(
    tmp_path: Path,
) -> None:
    if "OPENAI_API_KEY" not in os.environ:
        pytest.skip("OPENAI_API_KEY is not available")
    database_path = tmp_path / "planner-smoke.sqlite3"
    service = build_service(
        database_path,
        tmp_path / "artifacts",
        planner_kind="builtin",
        builtin_planner_model=os.environ.get(
            "EHAI_BUILTIN_PLANNER_MODEL",
            "gpt-5.6-luna",
        ),
        builtin_planner_reasoning_effort=os.environ.get(
            "EHAI_BUILTIN_PLANNER_REASONING_EFFORT",
            "high",
        ),
    )
    project = service.create_project(CreateProject("planner-project", "P3 readiness"))
    goal = service.create_goal(
        CreateGoal(
            "planner-goal",
            project.project_id,
            "Plan the next bounded EHAI P3 Control Plane increment after P2. Compare a "
            "contract-and-observability-first sequence with a vertical-user-loop sequence. "
            "Use the generated strict TypeScript API client and versioned Events; the Control "
            "Plane must not write SQLite or duplicate Execution Plane state transitions. Keep "
            "P4 Workflow, plugins, new Worker providers, and distributed scheduling out of scope.",
        )
    )

    plan = service.propose_plan(
        ProposePlan(
            "planner-plan",
            goal.goal_id,
            (NON_EMPTY_ARTIFACT_CRITERION,),
        )
    )

    assert plan.status is PlanRevisionStatus.DRAFT
    assert tuple(node.kind for node in plan.nodes) == (
        PlanNodeKind.FORK,
        PlanNodeKind.WORK,
        PlanNodeKind.WORK,
        PlanNodeKind.EVALUATOR,
        PlanNodeKind.MERGE,
    )
    assert len(plan.branches) == 2
    assert len({branch.label for branch in plan.branches}) == 2
    assert all(node.required_check_ids for node in plan.nodes)
    generated_text = " ".join(
        (node.title + " " + node.instruction).casefold() for node in plan.nodes
    )
    assert "control plane" in generated_text
    assert "typescript" in generated_text

    database = SQLiteDatabase(database_path)
    with database.read_session() as session:
        assert session.states.get_plan_revision(plan.plan_revision_id) == plan
        assert session.states.list_runs(goal.goal_id) == ()
        stored_goal = session.states.get_goal(goal.goal_id)
        planner_events = tuple(
            stored.event
            for stored in session.events.list_events()
            if stored.event.type is EventType.PLAN_REVISION_PROPOSED
        )
    assert stored_goal is not None
    assert stored_goal.completion_contract is not None
    assert not stored_goal.completion_contract.is_confirmed
    assert len(planner_events) == 1
    assert planner_events[0].payload["planner_event_types"] == ["planner.responses.completed"]


@pytest.mark.skipif(
    os.environ.get("EHAI_RUN_BUILTIN_REPLAN_SMOKE") != "1",
    reason="set EHAI_RUN_BUILTIN_REPLAN_SMOKE=1 for the explicit Replan smoke",
)
def test_real_builtin_planner_recovers_same_goal_from_failed_run(tmp_path: Path) -> None:
    if "OPENAI_API_KEY" not in os.environ:
        pytest.skip("OPENAI_API_KEY is not available")
    database = SQLiteDatabase(tmp_path / "replan-smoke.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = _FailThenRecoverWorker()
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=Orchestrator(
            uow_factory=database.unit_of_work,
            worker=worker,
            artifact_store=artifacts,
            check_runner=CheckRunner(
                {
                    CheckKind.ARTIFACT: ArtifactCheckAdapter(
                        artifacts,
                        {},
                        default_rule=ArtifactCheckRule(),
                    )
                }
            ),
            workspace=tmp_path,
        ),
    )
    project = service.create_project(CreateProject("replan-project", "failure recovery"))
    goal = service.create_goal(
        CreateGoal(
            "replan-goal",
            project.project_id,
            "Produce verified candidate evidence after inspecting the failed Run context.",
        )
    )
    base = service.propose_plan(
        ProposePlan("base-plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )
    base = service.approve_plan(
        ApprovePlan("approve-base", base.plan_revision_id, base.completion_contract_id)
    )
    with pytest.raises(GateRejectedError):
        service.start_run(StartRun("failed-run", base.plan_revision_id))
    with database.read_session() as session:
        failed_run = session.states.list_runs(goal.goal_id)[0]
    assert failed_run.status is RunStatus.FAILED
    old_trace = QueryService(read_session_factory=database.read_session).get_execution_trace(
        failed_run.run_id
    )

    service._planner = BuiltinPlannerAdapter(  # type: ignore[assignment]
        model=os.environ.get("EHAI_BUILTIN_PLANNER_MODEL", "gpt-5.6-luna"),
        reasoning_effort=os.environ.get("EHAI_BUILTIN_PLANNER_REASONING_EFFORT", "high"),
    )
    revised = service.replan_plan(
        ReplanPlan(
            "replan",
            base.plan_revision_id,
            (NON_EMPTY_ARTIFACT_CRITERION,),
            failed_run.run_id,
        )
    )
    approved = service.approve_plan(
        ApprovePlan(
            "approve-replan",
            revised.plan_revision_id,
            revised.completion_contract_id,
        )
    )
    recovered = service.start_run(StartRun("recovered-run", approved.plan_revision_id))

    assert revised.version == 2
    assert revised.supersedes_plan_revision_id == base.plan_revision_id
    assert recovered.goal_id == goal.goal_id
    assert recovered.status is RunStatus.COMPLETED
    queries = QueryService(read_session_factory=database.read_session)
    assert queries.get_execution_trace(failed_run.run_id) == old_trace
    with database.read_session() as session:
        replan_event = next(
            stored.event
            for stored in session.events.list_events()
            if stored.event.type is EventType.PLAN_REVISION_PROPOSED
            and stored.event.correlation_id == revised.plan_revision_id
        )
    context = replan_event.payload["replan_context"]
    assert isinstance(context, dict)
    assert context["source_run_id"] == failed_run.run_id
    assert context["failed_plan_node_ids"]
    assert context["failed_checks"]
