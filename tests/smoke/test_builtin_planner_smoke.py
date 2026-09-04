from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, json_loads
from ehai.application.builtin_agent import ModelClient, ModelRequest, ModelResponse, ModelRole
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
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    ConfiguredCheckPlanner,
    DeterministicPlanner,
)
from ehai.application.queries import QueryService
from ehai.application.service import ExecutionService
from ehai.application.workers import CandidateArtifact, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind
from ehai.domain.events import EventType
from ehai.domain.execution import RunStatus
from ehai.domain.planning import PlanNodeKind, PlanRevisionStatus
from ehai.domain.workers import WorkerProfile
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.openai_responses import OpenAIResponsesModelClient
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


class _RecordingResponsesClient:
    """Record sanitized real Responses decisions without replacing the provider client."""

    def __init__(self, delegate: ModelClient, evidence: dict[str, JsonValue]) -> None:
        self._delegate = delegate
        self._evidence = evidence

    async def complete(self, request: ModelRequest) -> ModelResponse:
        requests = cast(list[JsonValue], self._evidence["requests"])
        issue_codes: list[JsonValue] = []
        for message in request.input_messages:
            if message.role is not ModelRole.TOOL:
                continue
            payload = json_loads(message.content)
            if not isinstance(payload, dict):
                continue
            issues = payload.get("issues")
            if not isinstance(issues, list):
                continue
            for issue in issues:
                if isinstance(issue, dict) and isinstance(issue.get("code"), str):
                    issue_codes.append(issue["code"])
        requests.append(
            {
                "sequence": len(requests) + 1,
                "provider": "openai_responses",
                "available_tools": [tool.name for tool in request.tools],
                "previous_response_id_present": request.previous_response_id is not None,
                "tool_result_issue_codes": issue_codes,
            }
        )
        response = await self._delegate.complete(request)
        responses = cast(list[JsonValue], self._evidence["responses"])
        responses.append(
            {
                "sequence": len(responses) + 1,
                "provider_response_id": response.provider_response_id,
                "status": response.status,
                "usage_present": response.usage is not None,
                "tool_calls": [
                    {"call_id": call.call_id, "name": call.name} for call in response.tool_calls
                ],
            }
        )
        return response

    async def aclose(self) -> None:
        await self._delegate.aclose()


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
    model = os.environ.get("EHAI_BUILTIN_PLANNER_MODEL", "gpt-5.6-luna")
    reasoning_effort = cast(
        ReasoningEffort,
        os.environ.get("EHAI_BUILTIN_PLANNER_REASONING_EFFORT", "high"),
    )
    evidence: dict[str, JsonValue] = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "requests": [],
        "responses": [],
    }

    def model_client_factory(profile: WorkerProfile) -> ModelClient:
        return _RecordingResponsesClient(
            OpenAIResponsesModelClient(
                profile,
                reasoning_effort=reasoning_effort,
                background=True,
            ),
            evidence,
        )

    service = build_service(
        database_path,
        tmp_path / "artifacts",
        planner_kind="single",
    )
    service._planner = ConfiguredCheckPlanner(
        BuiltinPlannerAdapter(
            model=model,
            reasoning_effort=reasoning_effort,
            model_client_factory=model_client_factory,
        )
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
            "P4 Workflow, plugins, new Worker providers, and distributed scheduling out of scope. "
            "For this smoke, begin by calling finish_plan on the empty graph, observe its "
            "validation diagnostic, then construct and finish the required comparison plan.",
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
    assert len(plan.nodes) >= 3
    assert len(plan.branches) >= 2
    kinds = {node.kind for node in plan.nodes}
    assert PlanNodeKind.FORK in kinds
    assert PlanNodeKind.EVALUATOR in kinds
    assert PlanNodeKind.MERGE in kinds
    assert len({branch.label for branch in plan.branches}) == len(plan.branches)
    assert all(node.required_check_ids for node in plan.nodes)
    generated_text = " ".join(
        (node.title + " " + node.instruction).casefold() for node in plan.nodes
    )
    assert "control plane" in generated_text
    assert "typescript" in generated_text

    responses = cast(list[dict[str, JsonValue]], evidence["responses"])
    requests = cast(list[dict[str, JsonValue]], evidence["requests"])
    tool_names = [
        call["name"]
        for response in responses
        for call in cast(list[dict[str, JsonValue]], response["tool_calls"])
    ]
    assert {"add_plan_node", "add_plan_edge", "set_plan_branch", "finish_plan"}.issubset(tool_names)
    assert tool_names[-1] == "finish_plan"
    assert sum(name == "finish_plan" for name in tool_names) >= 2
    assert any(request["tool_result_issue_codes"] for request in requests[1:])
    assert all(response["provider_response_id"] for response in responses)
    assert any(response["usage_present"] for response in responses)

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

    evidence["plan"] = {
        "plan_revision_id": plan.plan_revision_id,
        "node_count": len(plan.nodes),
        "branch_count": len(plan.branches),
        "reloaded": True,
        "worker_attempt_count": 0,
    }
    evidence_path = tmp_path / "builtin-planner-smoke-evidence.json"
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
    assert os.environ["OPENAI_API_KEY"] not in serialized
    evidence_path.write_text(serialized, encoding="utf-8")


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

    service._planner = BuiltinPlannerAdapter(
        model=os.environ.get("EHAI_BUILTIN_PLANNER_MODEL", "gpt-5.6-luna"),
        reasoning_effort=cast(
            ReasoningEffort,
            os.environ.get("EHAI_BUILTIN_PLANNER_REASONING_EFFORT", "high"),
        ),
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
