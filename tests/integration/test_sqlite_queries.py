from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ehai import new_id
from ehai.application.queries import QueryService
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanRevision
from ehai.infrastructure.sqlite.database import SQLiteDatabase

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_sqlite_read_session_is_query_only_deferred_and_short_lived(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "read-session.sqlite3")
    first = Project.create("first", project_id=new_id(), created_at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(first)
        uow.commit()

    second = Project.create(
        "second",
        project_id=new_id(),
        created_at=NOW + timedelta(seconds=1),
    )
    with database.read_session() as read_session:
        stale_reader = read_session.states
        assert read_session.states.list_projects() == (first,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_session.states.put_project(second)

        # A WAL writer can commit while the deferred read snapshot remains open.
        with database.unit_of_work() as uow:
            uow.states.put_project(second)
            uow.commit()
        assert read_session.states.list_projects() == (first,)

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        stale_reader.list_projects()
    with database.read_session() as read_session:
        assert read_session.states.list_projects() == (first, second)


def test_query_service_builds_separate_sanitized_views_from_sqlite(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "queries.sqlite3")
    project = Project.create("P1", project_id=new_id(), created_at=NOW)
    goal = Goal.create(
        project.project_id,
        "query a trace",
        goal_id=new_id(),
        created_at=NOW,
    )
    check_spec = CheckSpec(
        name="artifact",
        kind=CheckKind.ARTIFACT,
        description="candidate exists",
        check_id=new_id(),
    )
    contract = CompletionContract.draft(
        goal.goal_id,
        ("candidate exists",),
        (check_spec.check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = PlanNode(
        plan_node_id=new_id(),
        title="work",
        instruction="produce a candidate",
        required_check_ids=(check_spec.check_id,),
    )
    plan = PlanRevision.draft(
        goal.goal_id,
        contract,
        (node,),
        (),
        plan_revision_id=new_id(),
        created_at=NOW,
    ).approve(contract, approved_at=NOW)
    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    )
    attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    )
    artifact = Artifact(
        artifact_id=new_id(),
        kind=ArtifactKind.CANDIDATE,
        name="candidate.txt",
        media_type="text/plain",
        size_bytes=9,
        sha256="a" * 64,
        relative_path="private/candidate.txt",
        created_at=NOW,
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        attempt_id=attempt.attempt_id,
    )
    event = Event(
        type=EventType.RUN_STARTED,
        correlation_id=run.run_id,
        run_id=run.run_id,
        payload={"run_id": run.run_id},
        occurred_at=NOW,
    )
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_check_spec(plan.plan_revision_id, check_spec)
        uow.states.put_run(run)
        uow.states.put_attempt(attempt)
        uow.states.put_artifact(artifact)
        uow.events.append(event)
        uow.commit()

    queries = QueryService(read_session_factory=database.read_session)
    graph = queries.get_plan_graph(plan.plan_revision_id)
    trace = queries.get_execution_trace(run.run_id)

    assert graph.nodes[0].plan_node_id == node.plan_node_id
    assert not hasattr(graph, "attempts")
    assert trace.run.run_id == run.run_id
    assert trace.events[0].event == event
    assert not hasattr(trace, "nodes")
    assert not hasattr(trace.artifacts[0], "relative_path")
    assert queries.list_check_specs(plan.plan_revision_id)[0].check_id == check_spec.check_id
