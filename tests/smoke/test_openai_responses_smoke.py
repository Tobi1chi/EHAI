from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

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
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION, DeterministicPlanner
from ehai.application.service import ExecutionService
from ehai.domain.checking import CheckKind
from ehai.domain.execution import Run, RunStatus
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

pytestmark = pytest.mark.skipif(
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
