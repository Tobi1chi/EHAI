from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ehai.application.async_runtime import SingleSlotRuntime
from ehai.application.builtin_agent import BuiltinSessionEventType
from ehai.application.checks import CheckRunner
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    StartRun,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    PlanProposal,
    ReplanContext,
)
from ehai.application.service import ExecutionService
from ehai.domain.checking import CheckKind
from ehai.domain.execution import Run, RunStatus
from ehai.domain.goal import Goal
from ehai.domain.planning import PlanNodeKind, PlanRevision
from ehai.domain.workers import (
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.builtin_sessions import SQLiteBuiltinSessionStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import BuiltinAgentConnector
from ehai.interfaces import cli as cli_module
from ehai.interfaces.runtime import create_local_app

type JSONObject = dict[str, Any]


@pytest.mark.skipif(
    os.environ.get("EHAI_RUN_OPENAI_SMOKE") != "1",
    reason="set EHAI_RUN_OPENAI_SMOKE=1 for the explicit Responses smoke",
)
def test_real_openai_responses_runs_builtin_runtime_tools_artifact_and_gate(
    tmp_path: Path,
) -> None:
    if "OPENAI_API_KEY" not in os.environ:
        pytest.skip("OPENAI_API_KEY is not available")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "smoke.txt"
    target.write_text("BEFORE", encoding="utf-8")
    database = SQLiteDatabase(tmp_path / "responses-smoke.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    sessions = SQLiteBuiltinSessionStore(database)
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=None,
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
        workspace=workspace,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    profile = WorkerProfile(
        "responses-smoke",
        WorkerKind.BUILTIN,
        "gpt-5.6-luna",
        frozenset(
            {
                WorkerCapability("worker.builtin"),
                WorkerCapability("workspace.read"),
                WorkerCapability("workspace.write"),
            }
        ),
        credential_ref=OPENAI_CREDENTIAL_REF,
    )
    endpoint = WorkerEndpoint(
        "responses-smoke",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "responses-smoke",
        1,
    )
    connector = BuiltinAgentConnector(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        session_store=sessions,
        artifact_store=artifacts,
        profile=profile,
        default_workspace=workspace,
        allowed_commands=(("uv", "--version"),),
        reasoning_effort="high",
    )
    runtime = SingleSlotRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        connector=connector,
        profile=profile,
        endpoint=endpoint,
        workspace=workspace,
    )
    project = service.create_project(CreateProject("smoke-project", "responses-smoke"))
    goal = service.create_goal(
        CreateGoal(
            "smoke-goal",
            project.project_id,
            "Use workspace_read on smoke.txt, replace BEFORE with AFTER using "
            'workspace_patch, run command argv ["uv", "--version"], then call '
            "submit_candidate with name smoke.txt, media_type text/plain, and content AFTER. "
            "Do not finish with ordinary assistant text.",
        )
    )
    plan = service.propose_plan(
        ProposePlan(
            "smoke-plan",
            goal.goal_id,
            (NON_EMPTY_ARTIFACT_CRITERION,),
        )
    )
    approved = service.approve_plan(
        ApprovePlan(
            "smoke-approve",
            plan.plan_revision_id,
            plan.completion_contract_id,
        )
    )
    started = service.start_run(StartRun("smoke-run", approved.plan_revision_id))

    async def execute() -> Run | None:
        try:
            return await runtime.run_once()
        finally:
            await connector.close()

    completed = asyncio.run(execute())

    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "AFTER"
    with database.read_session() as session:
        stored_artifacts = session.states.list_artifacts_for_run(started.run_id)
        refs = session.states.list_agent_session_refs(started.run_id)
        checkpoints = session.states.list_checkpoints(started.run_id)
    assert len(stored_artifacts) == 1
    assert artifacts.read(stored_artifacts[0].artifact_id) == b"AFTER"
    assert checkpoints and checkpoints[-1].gate_decision.passed
    durable = sessions.load(refs[0].agent_session_ref_id)
    tool_names = tuple(
        event.payload.get("name")
        for event in durable.events
        if event.type is BuiltinSessionEventType.TOOL_CALLED
    )
    assert "workspace_read" in tool_names
    assert "workspace_patch" in tool_names
    assert "command" in tool_names
    assert tool_names[-1] == "submit_candidate"


@pytest.mark.skipif(
    os.environ.get("EHAI_RUN_OPENAI_EXPLORATION_E2E") != "1",
    reason="set EHAI_RUN_OPENAI_EXPLORATION_E2E=1 for the explicit exploration E2E",
)
def test_real_openai_responses_completes_two_branch_exploration_api_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "OPENAI_API_KEY" not in os.environ:
        pytest.skip("OPENAI_API_KEY is not available")

    run_root = _external_run_root()
    repository_root = Path.cwd().resolve()
    assert run_root != repository_root and not run_root.is_relative_to(repository_root)
    attempt = os.environ.get("EHAI_OPENAI_EXPLORATION_E2E_ATTEMPT", "attempt-1")
    model = os.environ.get("EHAI_OPENAI_EXPLORATION_MODEL", "gpt-5.6-luna")
    reasoning_effort = os.environ.get("EHAI_OPENAI_EXPLORATION_REASONING_EFFORT", "high")
    expected_head = os.environ.get("EHAI_OPENAI_EXPLORATION_EXPECTED_HEAD") or _git_output(
        "rev-parse", "HEAD"
    )
    observed_head_before = _git_output("rev-parse", "HEAD")
    git_status_before = _git_output("status", "--porcelain")
    assert observed_head_before == expected_head
    assert git_status_before == ""

    summary: JSONObject = {
        "status": "FAIL",
        "classification": "HARNESS",
        "attempt": attempt,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "expected_head": expected_head,
        "observed_head_before": observed_head_before,
        "observed_head_after": None,
        "git_status_before": git_status_before,
        "git_status_after": None,
        "run_root": str(run_root),
        "database_path": str(run_root / "exploration.sqlite3"),
        "artifact_root": str(run_root / "artifacts"),
        "workspace_path": str(run_root / "workspace"),
        "api_route_contract": {"plan_route": "/api/v1/plans/{plan_revision_id}"},
        "api_server_closed": False,
        "runtime_closed": False,
    }
    failure_path: Path | None = None
    client_entered = False

    try:
        workspace = run_root / "workspace"
        database_path = run_root / "exploration.sqlite3"
        artifacts_root = run_root / "artifacts"
        _init_git_workspace(workspace)
        monkeypatch.setattr(cli_module, "_ExplorationPlannerAdapter", _RealE2EPlannerAdapter)
        app = create_local_app(
            database_path,
            artifacts_root,
            worker_kind="builtin",
            worker_workspace=workspace,
            planner_kind="exploration",
            worker_timeout_seconds=240.0,
            builtin_model=model,
            builtin_reasoning_effort=reasoning_effort,
            builtin_capacity=2,
            p2_runtime=True,
        )
        with TestClient(app) as client:
            client_entered = True
            project = _api_object(
                client,
                "/api/v1/projects",
                {"idempotency_key": "project", "name": "real exploration e2e"},
            )
            goal = _api_object(
                client,
                "/api/v1/goals",
                {
                    "idempotency_key": "goal",
                    "project_id": project["project_id"],
                    "objective": (
                        "Create two tiny text candidates, select approach-b, and merge the "
                        "selected candidate only."
                    ),
                },
            )
            proposed = _api_object(
                client,
                "/api/v1/plans/propose",
                {
                    "idempotency_key": "plan",
                    "goal_id": goal["goal_id"],
                    "criteria": [NON_EMPTY_ARTIFACT_CRITERION],
                },
            )
            plan_id = cast(str, proposed["plan_revision_id"])
            approved = _api_object(
                client,
                "/api/v1/plans/approve",
                {
                    "idempotency_key": "approve",
                    "plan_revision_id": plan_id,
                    "completion_contract_id": proposed["completion_contract_id"],
                },
            )
            assert approved["status"] == "approved"

            start_begin = time.monotonic()
            started = _api_object(
                client,
                "/api/v1/runs/start",
                {"idempotency_key": "run", "plan_revision_id": approved["plan_revision_id"]},
            )
            start_elapsed_seconds = time.monotonic() - start_begin
            assert started["status"] == "pending"

            run_id = cast(str, started["run_id"])
            run = _wait_for_run(client, run_id)
            completed_plan = _api_object(client, f"/api/v1/plans/{plan_id}")
            trace = _api_object(client, f"/api/v1/runs/{run_id}/trace")
            checks = _api_items(client, f"/api/v1/runs/{run_id}/checks")
            checkpoints = _api_items(client, f"/api/v1/runs/{run_id}/checkpoints")
            artifacts = _api_items(client, f"/api/v1/runs/{run_id}/artifacts")
            check_specs = _api_items(client, f"/api/v1/plans/{plan_id}/checks")
            runtime_health = _api_object(client, "/api/v1/runtime/health")
            attempts = cast(list[JSONObject], trace["attempts"])
            attempt_runtime = [
                _api_object(client, f"/api/v1/attempts/{item['attempt_id']}/runtime")
                for item in attempts
            ]
            summary["observed_run"] = run
            summary["observed_runtime_health"] = runtime_health
            summary.update(
                _exploration_evidence(
                    database_path=database_path,
                    artifacts_root=artifacts_root,
                    project=project,
                    goal=goal,
                    plan=completed_plan,
                    started=started,
                    start_elapsed_seconds=start_elapsed_seconds,
                    run=run,
                    trace=trace,
                    checks=checks,
                    checkpoints=checkpoints,
                    artifacts=artifacts,
                    check_specs=check_specs,
                    attempt_runtime=attempt_runtime,
                    runtime_health=runtime_health,
                )
            )

        summary["api_server_closed"] = True
        summary["runtime_closed"] = True
        observed_head_after = _git_output("rev-parse", "HEAD")
        git_status_after = _git_output("status", "--porcelain")
        summary["observed_head_after"] = observed_head_after
        summary["git_status_after"] = git_status_after
        summary["worktree_clean"] = git_status_after == ""
        summary["secret_scan"] = _secret_scan(run_root)
        assert observed_head_after == expected_head
        assert summary["worktree_clean"] is True
        assert cast(JSONObject, summary["secret_scan"])["match_count"] == 0
        summary.pop("observed_run", None)
        summary.pop("observed_runtime_health", None)
        summary["status"] = "PASS"
        summary["classification"] = "PASS"
    except Exception as error:
        if client_entered:
            summary["api_server_closed"] = True
            summary["runtime_closed"] = True
        summary["classification"] = _classify_failure(error, summary)
        summary["failure"] = {"type": type(error).__name__, "message": str(error)}
        failure_path = run_root / f"{attempt}-{summary['classification']}.json"
        raise
    finally:
        if summary["observed_head_after"] is None:
            try:
                summary["observed_head_after"] = _git_output("rev-parse", "HEAD")
                summary["git_status_after"] = _git_output("status", "--porcelain")
            except (OSError, subprocess.SubprocessError):
                pass
        summary["secret_scan"] = _secret_scan(run_root)
        trajectory_path = run_root / "trajectory.json"
        summary["trajectory_path"] = str(trajectory_path)
        summary["evidence_paths"] = [str(trajectory_path)]
        if failure_path is not None:
            summary["evidence_paths"].append(str(failure_path))
        _write_json(trajectory_path, summary)
        if failure_path is not None:
            _write_json(failure_path, summary)
        assert _secret_scan(run_root)["match_count"] == 0
        assert not tuple(run_root.rglob("*-failure.json"))


class _RealE2EPlannerAdapter:
    def __init__(self) -> None:
        self._planner = DeterministicExplorationPlanner()
        self._budget = ExplorationBudget(max_attempts=5)

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        proposal = self._planner.propose(
            ExplorationPlanRequest(goal=goal, criteria=criteria, budget=self._budget)
        )
        return _with_real_e2e_instructions(proposal)

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        proposal = self._planner.replan(
            ExplorationPlanRequest(goal=goal, criteria=criteria, budget=self._budget),
            base,
            context,
        )
        return _with_real_e2e_instructions(proposal)


def _with_real_e2e_instructions(proposal: PlanProposal) -> PlanProposal:
    nodes = tuple(
        replace(node, instruction=_real_e2e_instruction(node.kind, node.title))
        for node in proposal.plan_revision.nodes
    )
    return replace(proposal, plan_revision=replace(proposal.plan_revision, nodes=nodes))


def _real_e2e_instruction(kind: PlanNodeKind, title: str) -> str:
    if kind is PlanNodeKind.FORK:
        return (
            "Call submit_candidate once with name fork.txt, media_type text/plain, and content "
            "'fork-ready'. Do not use other tools."
        )
    if kind is PlanNodeKind.WORK:
        assert title.endswith(("A", "B"))
        branch = title[-1].lower()
        content = (
            "approach-a: compact alpha candidate"
            if branch == "a"
            else "approach-b: selected beta candidate"
        )
        return (
            f"Call submit_candidate once with name branch-{branch}.txt, media_type text/plain, "
            f"and content '{content}'. Do not use other tools."
        )
    if kind is PlanNodeKind.EVALUATOR:
        return (
            "Use context.candidate_branches. Select the active branch whose label is exactly "
            "'approach-b' after comparing every viable branch. Follow context.role_protocol for "
            "the required output and evidence fields."
        )
    if kind is PlanNodeKind.MERGE:
        return (
            "Use only context.selected_artifacts and context.branch_selection. Call "
            "submit_candidate once with name merged.txt, media_type application/json, and content "
            "as minified JSON with keys selected_branch_id, selected_artifact_ids, "
            "merged_from_artifact_ids, merged_text. merged_from_artifact_ids must equal the "
            "selected_artifact_ids from context.branch_selection and merged_text must be copied "
            "only from context.selected_artifacts content. Do not include pruned branch content."
        )
    raise AssertionError(f"unexpected node {kind} {title!r}")


def _exploration_evidence(
    *,
    database_path: Path,
    artifacts_root: Path,
    project: JSONObject,
    goal: JSONObject,
    plan: JSONObject,
    started: JSONObject,
    start_elapsed_seconds: float,
    run: JSONObject,
    trace: JSONObject,
    checks: list[JSONObject],
    checkpoints: list[JSONObject],
    artifacts: list[JSONObject],
    check_specs: list[JSONObject],
    attempt_runtime: list[JSONObject],
    runtime_health: JSONObject,
) -> JSONObject:
    nodes = cast(list[JSONObject], plan["nodes"])
    branches = cast(list[JSONObject], plan["branches"])
    attempts = cast(list[JSONObject], trace["attempts"])
    events = cast(list[JSONObject], trace["events"])
    session_events = cast(list[JSONObject], trace["session_events"])
    event_types = [item["event"]["type"] for item in events]
    session_event_types = {item["type"] for item in session_events}
    tool_calls = [
        {"attempt_id": item["attempt_id"], "name": item["payload"]["name"]}
        for item in session_events
        if item["type"] == "tool/call"
    ]

    assert run["status"] == "completed"
    assert [node["kind"] for node in nodes] == ["fork", "work", "work", "evaluator", "merge"]
    assert all(node["status"] == "completed" for node in nodes)
    assert len(attempts) == 5
    assert all(attempt["status"] == "succeeded" for attempt in attempts)

    runtime_by_attempt = {item["attempt_id"]: item for item in attempt_runtime}
    assert set(runtime_by_attempt) == {item["attempt_id"] for item in attempts}
    agent_session_ids = [item["agent_session_ref_id"] for item in attempt_runtime]
    provider_session_ids = [item["provider_session_id"] for item in attempt_runtime]
    assert None not in agent_session_ids and len(set(agent_session_ids)) == 5
    assert None not in provider_session_ids and len(set(provider_session_ids)) == 5
    assert all(
        item["event_cursor"] == "completed"
        and item["activity"] is None
        and item["lease_expires_at"] is None
        for item in attempt_runtime
    )
    attempt_evidence = [
        {
            "attempt_id": attempt["attempt_id"],
            "plan_node_id": attempt["plan_node_id"],
            "sequence": attempt["sequence"],
            "status": attempt["status"],
            "agent_session_ref_id": runtime_by_attempt[attempt["attempt_id"]][
                "agent_session_ref_id"
            ],
            "provider_session_id": runtime_by_attempt[attempt["attempt_id"]]["provider_session_id"],
        }
        for attempt in attempts
    ]

    branches_by_status = {branch["status"]: branch for branch in branches}
    assert set(branches_by_status) == {"selected", "pruned"}
    selected_branch = branches_by_status["selected"]
    pruned_branch = branches_by_status["pruned"]
    artifacts_by_node: dict[str, list[JSONObject]] = {}
    for artifact in artifacts:
        artifacts_by_node.setdefault(artifact["plan_node_id"], []).append(artifact)
    selected_artifact_ids = [
        artifact["artifact_id"]
        for node_id in selected_branch["node_ids"]
        for artifact in artifacts_by_node.get(node_id, ())
    ]
    pruned_artifact_ids = [
        artifact["artifact_id"]
        for node_id in pruned_branch["node_ids"]
        for artifact in artifacts_by_node.get(node_id, ())
    ]
    assert len(selected_artifact_ids) == len(pruned_artifact_ids) == 1

    attempt_by_node = {attempt["plan_node_id"]: attempt for attempt in attempts}
    evaluator_node = next(node for node in nodes if node["kind"] == "evaluator")
    merge_node = next(node for node in nodes if node["kind"] == "merge")
    evaluator_attempt = attempt_by_node[evaluator_node["plan_node_id"]]
    evaluator_runtime = runtime_by_attempt[evaluator_attempt["attempt_id"]]
    evaluator_context = _turn_context(
        database_path,
        evaluator_runtime["agent_session_ref_id"],
    )
    evaluator_protocol = evaluator_context["role_protocol"]
    assert isinstance(evaluator_protocol, dict) and evaluator_protocol["role"] == "evaluator"
    selection = _artifact_json(
        artifacts_root,
        artifacts_by_node[evaluator_node["plan_node_id"]][0]["artifact_id"],
    )
    assert set(selection) == {
        "selected_branch_id",
        "pruned_branch_ids",
        "criterion",
        "explanation",
        "compared_artifact_ids",
        "selected_artifact_ids",
    }
    assert selection["selected_branch_id"] == selected_branch["branch_id"]
    assert selection["pruned_branch_ids"] == [pruned_branch["branch_id"]]
    assert set(selection["compared_artifact_ids"]) == {
        *selected_artifact_ids,
        *pruned_artifact_ids,
    }
    assert selection["selected_artifact_ids"] == selected_artifact_ids

    merge_attempt = attempt_by_node[merge_node["plan_node_id"]]
    merge_runtime = runtime_by_attempt[merge_attempt["attempt_id"]]
    merge_context = _turn_context(database_path, merge_runtime["agent_session_ref_id"])
    merge_protocol = merge_context["role_protocol"]
    assert isinstance(merge_protocol, dict) and merge_protocol["role"] == "merge"
    merge_input_artifact_ids = [item["artifact_id"] for item in merge_context["selected_artifacts"]]
    merge_output = _artifact_json(
        artifacts_root,
        artifacts_by_node[merge_node["plan_node_id"]][0]["artifact_id"],
    )
    assert merge_input_artifact_ids == selected_artifact_ids
    assert not set(pruned_artifact_ids).intersection(merge_input_artifact_ids)
    assert merge_context["branch_selection"]["selected_artifact_ids"] == selected_artifact_ids
    assert merge_output["merged_from_artifact_ids"] == selected_artifact_ids
    assert merge_output["merged_text"] == "approach-b: selected beta candidate"
    assert "approach-a" not in merge_output["merged_text"]

    assert len(check_specs) == 1 and check_specs[0]["required"] is True
    required_check_ids = [check_specs[0]["check_id"]]
    assert all(node["required_check_ids"] == required_check_ids for node in nodes)
    assert len(checks) == 5
    assert all(
        check["status"] == "completed" and check["result"]["passed"] is True for check in checks
    )
    gates = [checkpoint["gate_decision"]["passed"] for checkpoint in checkpoints]
    assert len(gates) == 5 and all(gates)
    assert checkpoints[-1]["branch_selections"] == [
        {
            "branch_id": selected_branch["branch_id"],
            "fork_node_id": merge_context["branch_selection"]["fork_node_id"],
        }
    ]

    leases = _workspace_leases(database_path)
    assert len(leases) == 5 and all(lease["status"] == "released" for lease in leases)
    assert runtime_health["status"] == "healthy" and runtime_health["loop_active"] is True
    assert not [
        attempt for attempt in attempts if attempt["status"] in {"pending", "running", "stalled"}
    ]
    assert event_types.count("BranchSelected") == 1
    assert event_types.count("BranchPruned") == 1
    assert event_types.count("GatePassed") == 5
    assert event_types.count("CheckpointCreated") == 5
    assert event_types[-1] == "RunCompleted"
    assert not [item for item in session_events if item["type"] == "tool/error"]
    assert trace["session_events_truncated"] is False
    assert not [item for item in session_events if item["payload_truncated"] is True]
    assert {
        "step/start",
        "model/message",
        "tool/call",
        "tool/result",
        "step/end",
    }.issubset(session_event_types)
    assert len(tool_calls) == 5 and all(item["name"] == "submit_candidate" for item in tool_calls)

    return {
        "project": project,
        "goal": goal,
        "completion_contract": {
            "completion_contract_id": plan["completion_contract_id"],
            "version": plan["completion_contract_version"],
            "required_check_ids": required_check_ids,
        },
        "start_run": {"status": started["status"], "elapsed_seconds": start_elapsed_seconds},
        "run": {"run_id": run["run_id"], "status": run["status"]},
        "plan_nodes": nodes,
        "branches": branches,
        "branch_selection": selection,
        "selected_branch_id": selected_branch["branch_id"],
        "pruned_branch_ids": [pruned_branch["branch_id"]],
        "attempts": attempt_evidence,
        "artifact_ids": [artifact["artifact_id"] for artifact in artifacts],
        "branch_artifact_ids": {
            "selected": selected_artifact_ids,
            "pruned": pruned_artifact_ids,
        },
        "merge": {
            "artifact_id": artifacts_by_node[merge_node["plan_node_id"]][0]["artifact_id"],
            "input_artifact_ids": merge_input_artifact_ids,
            "persisted_selection": merge_context["branch_selection"],
            "output": merge_output,
        },
        "checks": checks,
        "gates": gates,
        "checkpoints": checkpoints,
        "workspace_leases": leases,
        "active_workspace_lease_count": 0,
        "pending_running_stalled_attempt_count": 0,
        "attempt_runtime": attempt_runtime,
        "runtime_health": runtime_health,
        "trace_event_count": len(event_types),
        "trace_event_types": event_types,
        "trace_error_count": 0,
        "session_event_count": len(session_events),
        "session_events_truncated": False,
        "session_payload_truncated_count": 0,
        "session_event_types": sorted(session_event_types),
        "tool_call_order": tool_calls,
    }


def _external_run_root() -> Path:
    configured = os.environ.get("EHAI_OPENAI_EXPLORATION_E2E_ROOT")
    if configured:
        root = Path(configured).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise AssertionError(f"configured E2E Run Root is not empty: {root}")
        return root
    return Path(tempfile.mkdtemp(prefix="ehai-p2-exploration-real-e2e-")).resolve()


def _api_object(
    client: TestClient,
    path: str,
    payload: JSONObject | None = None,
) -> JSONObject:
    response = client.get(path) if payload is None else client.post(path, json=payload)
    expected = {200} if payload is None else {200, 201}
    assert response.status_code in expected, f"{path} -> {response.status_code}: {response.text}"
    data: object = response.json()["data"]
    assert isinstance(data, dict)
    return cast(JSONObject, data)


def _api_items(client: TestClient, path: str) -> list[JSONObject]:
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    data: object = response.json()["data"]
    assert isinstance(data, list) and all(isinstance(item, dict) for item in data)
    return cast(list[JSONObject], data)


def _wait_for_run(client: TestClient, run_id: str) -> JSONObject:
    deadline = time.monotonic() + float(os.environ.get("EHAI_OPENAI_E2E_TIMEOUT_SECONDS", "300"))
    while time.monotonic() < deadline:
        run = _api_object(client, f"/api/v1/runs/{run_id}")
        if run["status"] in {"completed", "failed", "paused", "cancelled"}:
            return run
        time.sleep(0.25)
    raise AssertionError(f"Run {run_id} did not finish before timeout")


def _init_git_workspace(path: Path) -> None:
    path.mkdir()
    _git_in(path, "init")
    _git_in(path, "config", "user.email", "test@example.invalid")
    _git_in(path, "config", "user.name", "EHAI Test")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git_in(path, "add", "base.txt")
    _git_in(path, "commit", "-m", "baseline")


def _git_in(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True)


def _git_output(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace_leases(database_path: Path) -> list[JSONObject]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT snapshot_json FROM workspace_leases").fetchall()
    finally:
        connection.close()
    return [cast(JSONObject, json.loads(row[0])) for row in rows]


def _artifact_json(artifacts_root: Path, artifact_id: str) -> JSONObject:
    value: object = json.loads(
        FilesystemArtifactStore(artifacts_root).read(artifact_id).decode("utf-8")
    )
    assert isinstance(value, dict)
    return cast(JSONObject, value)


def _turn_context(database_path: Path, agent_session_ref_id: str) -> JSONObject:
    session = SQLiteBuiltinSessionStore(SQLiteDatabase(database_path)).load(agent_session_ref_id)
    turn = next(
        event for event in session.events if event.type is BuiltinSessionEventType.TURN_STARTED
    )
    messages = turn.payload["messages"]
    assert isinstance(messages, list)
    user_message = next(
        item for item in messages if isinstance(item, dict) and item.get("role") == "user"
    )
    content = user_message["content"]
    assert isinstance(content, str)
    document: object = json.loads(content)
    assert isinstance(document, dict) and isinstance(document.get("context"), dict)
    return cast(JSONObject, document["context"])


def _secret_scan(root: Path) -> JSONObject:
    key = os.environ.get("OPENAI_API_KEY", "")
    matches: list[JSONObject] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if key and key.encode() in content:
            matches.append({"path": str(path), "kind": "exact_openai_api_key"})
        if b"sk-" in content:
            matches.append({"path": str(path), "kind": "secret_pattern"})
    return {"root": str(root), "match_count": len(matches), "matches": matches}


def _write_json(path: Path, value: object) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, default=str)
    for secret in (
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_BASE_URL", ""),
    ):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    path.write_text(text + "\n", encoding="utf-8")


def _classify_failure(error: Exception, summary: JSONObject) -> str:
    evidence = " ".join(
        (
            type(error).__name__,
            str(error),
            json.dumps(summary.get("observed_run", {}), default=str),
        )
    )
    if any(
        term in evidence
        for term in (
            "OpenAI",
            "Responses",
            "APIConnection",
            "APIStatus",
            "Authentication",
            "APITimeout",
            "RateLimit",
            "BadRequest",
        )
    ):
        return "EXTERNAL"
    observed_run = summary.get("observed_run")
    observed_health = summary.get("observed_runtime_health")
    if (
        isinstance(observed_run, dict)
        and observed_run.get("status") in {"failed", "paused", "cancelled"}
    ) or (isinstance(observed_health, dict) and observed_health.get("status") == "failed"):
        return "CODE"
    if any(
        term in evidence
        for term in (
            "state_conflict",
            "Branch evaluation failed",
            "BranchSelectionProtocolError",
            "OrchestrationError",
        )
    ):
        return "CODE"
    return "HARNESS"
