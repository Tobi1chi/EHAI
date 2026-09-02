from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ehai import new_id
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanRevision, PlanRevisionStatus
from ehai.domain.workers import AgentSessionRef, SessionPolicy
from ehai.domain.workspaces import WorkspaceLeaseStatus
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workspaces import WorkspaceBusyError, WorkspaceManager

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _seed(database: SQLiteDatabase, attempt_count: int) -> tuple[Run, tuple[Attempt, ...]]:
    project = Project.create("workspace", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "isolate", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("isolate",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    nodes = tuple(
        PlanNode(new_id(), f"node-{index}", "write", required_check_ids=(check_id,))
        for index in range(attempt_count)
    )
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=nodes,
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(goal.goal_id, plan.plan_revision_id, created_at=NOW)
    attempts = tuple(
        Attempt(run.run_id, node.plan_node_id, index + 1, created_at=NOW)
        for index, node in enumerate(nodes)
    )
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
        for attempt in attempts:
            uow.states.put_attempt(attempt)
        uow.commit()
    return run, attempts


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True)


def test_git_branch_workspaces_isolate_same_file_and_preserve_dirty_resources(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "EHAI Test", cwd=repository)
    (repository / "shared.txt").write_text("base", encoding="utf-8")
    _git("add", "shared.txt", cwd=repository)
    _git("commit", "-m", "test baseline", cwd=repository)
    database = SQLiteDatabase(tmp_path / "workspace.sqlite3")
    run, attempts = _seed(database, 3)
    manager = WorkspaceManager(
        database=database,
        base_workspace=repository,
        owned_root=tmp_path / "owned-worktrees",
    )

    first = manager.allocate(
        run_id=run.run_id,
        attempt_id=attempts[0].attempt_id,
        write_capable=True,
        isolate=True,
    )
    second = manager.allocate(
        run_id=run.run_id,
        attempt_id=attempts[1].attempt_id,
        write_capable=True,
        isolate=True,
    )
    first_file = Path(first.reference.path) / "shared.txt"
    second_file = Path(second.reference.path) / "shared.txt"
    first_file.write_text("selected", encoding="utf-8")
    second_file.write_text("pruned", encoding="utf-8")

    selected_merge_input = first_file.read_text(encoding="utf-8")
    assert selected_merge_input == "selected"
    assert second_file.read_text(encoding="utf-8") == "pruned"
    assert (repository / "shared.txt").read_text(encoding="utf-8") == "base"
    assert manager.cleanup(first).status is WorkspaceLeaseStatus.PRESERVED
    assert manager.cleanup(second).status is WorkspaceLeaseStatus.PRESERVED
    assert Path(first.reference.path).exists() and Path(second.reference.path).exists()

    clean = manager.allocate(
        run_id=run.run_id,
        attempt_id=attempts[2].attempt_id,
        write_capable=True,
        isolate=True,
    )
    clean_path = Path(clean.reference.path)
    assert manager.cleanup(clean).status is WorkspaceLeaseStatus.RELEASED
    assert not clean_path.exists()
    with database.read_session() as session:
        preserved = tuple(
            item.event
            for item in session.events.list_events()
            if item.event.type.value == "WorkspacePreserved"
        )
        assert len(preserved) == 2


def test_non_git_writers_serialize_and_session_policy_is_explicit(tmp_path: Path) -> None:
    workspace = tmp_path / "directory"
    workspace.mkdir()
    database = SQLiteDatabase(tmp_path / "directory.sqlite3")
    run, attempts = _seed(database, 3)
    manager = WorkspaceManager(
        database=database,
        base_workspace=workspace,
        owned_root=tmp_path / "owned",
    )
    first = manager.allocate(
        run_id=run.run_id,
        attempt_id=attempts[0].attempt_id,
        write_capable=True,
        isolate=True,
    )
    with pytest.raises(WorkspaceBusyError):
        manager.allocate(
            run_id=run.run_id,
            attempt_id=attempts[1].attempt_id,
            write_capable=True,
            isolate=True,
        )
    manager.allocate(
        run_id=run.run_id,
        attempt_id=attempts[2].attempt_id,
        write_capable=False,
        isolate=False,
    )
    predecessor = AgentSessionRef(
        run.run_id,
        new_id(),
        new_id(),
        "session-old",
        True,
        created_at=NOW,
    )
    assert (
        manager.select_provider_session_id(
            SessionPolicy.REUSE,
            predecessor=predecessor,
            new_provider_session_id="ignored",
        )
        == "session-old"
    )
    assert (
        manager.select_provider_session_id(
            SessionPolicy.NEW,
            predecessor=predecessor,
            new_provider_session_id="session-new",
        )
        == "session-new"
    )
    assert (
        manager.select_provider_session_id(
            SessionPolicy.FORK,
            predecessor=predecessor,
            new_provider_session_id="session-fork",
        )
        == "session-fork"
    )
    with pytest.raises(ValueError, match="fork"):
        manager.select_provider_session_id(
            SessionPolicy.FORK,
            predecessor=predecessor,
            new_provider_session_id="session-old",
        )
    assert manager.cleanup(first).status is WorkspaceLeaseStatus.RELEASED
