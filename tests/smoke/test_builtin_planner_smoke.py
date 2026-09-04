from __future__ import annotations

import os
from pathlib import Path

import pytest

from ehai.application.commands import CreateGoal, CreateProject, ProposePlan
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION
from ehai.domain.events import EventType
from ehai.domain.planning import PlanNodeKind, PlanRevisionStatus
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.cli import build_service


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
