from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ehai import new_id
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    ReplanPlan,
)
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    Planner,
)
from ehai.application.workers import WorkerAdapter
from ehai.domain.events import EventType
from ehai.domain.goal import Goal
from ehai.domain.planning import PlanRevisionStatus
from ehai.infrastructure.codex_transport import CodexProcessTransport
from ehai.infrastructure.planners import (
    CodexPlannerAdapter,
    CodexPlannerError,
    CodexPlannerTimedOutError,
)
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import CodexWorkerAdapter
from ehai.interfaces import cli

_FAKE_CODEX_PLANNER = r"""
import json
import pathlib
import sys
import time

mode = sys.argv[1]
record_path = pathlib.Path(sys.argv[2])
args = sys.argv[3:]
schema_path = pathlib.Path(args[args.index("--output-schema") + 1])
last_path = pathlib.Path(args[args.index("--output-last-message") + 1])
prompt = sys.stdin.read()
planner_input = json.loads(
    prompt.split("--- PLANNER_INPUT_JSON ---", 1)[1]
    .split("--- OUTPUT_REQUIREMENTS ---", 1)[0]
    .strip()
)
record_path.write_text(
    json.dumps(
        {
            "args": args,
            "input": planner_input,
            "prompt": prompt,
            "schema": json.loads(schema_path.read_text()),
        }
    ),
    encoding="utf-8",
)
print(json.dumps({"type": "thread.started"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "future.event", "ignored": True}), flush=True)
if mode == "timeout":
    time.sleep(5)
if mode == "nonzero":
    print("planner failed", file=sys.stderr, flush=True)
    raise SystemExit(7)
if mode == "invalid":
    last_path.write_text('{"summary":"broken"}', encoding="utf-8")
    raise SystemExit(0)
last_path.write_text(
    json.dumps(
        {
            "summary": "two bounded approaches",
            "fork": {"title": "Fork", "instruction": "Start both approaches."},
            "branches": [
                {"label": "alpha", "title": "Alpha", "instruction": "Try alpha."},
                {"label": "beta", "title": "Beta", "instruction": "Try beta."},
            ],
            "evaluator": {"title": "Evaluate", "instruction": "Compare evidence."},
            "merge": {"title": "Merge", "instruction": "Merge the selected result."},
        }
    ),
    encoding="utf-8",
)
print(json.dumps({"type": "turn.completed"}), flush=True)
"""


def _adapter(
    tmp_path: Path,
    mode: str = "success",
    *,
    budget: ExplorationBudget | None = None,
    timeout_seconds: float = 2.0,
) -> tuple[CodexPlannerAdapter, Path]:
    executable = tmp_path / "fake_codex_planner.py"
    executable.write_text(_FAKE_CODEX_PLANNER, encoding="utf-8")
    record = tmp_path / f"{mode}.json"
    return (
        CodexPlannerAdapter(
            workspace=tmp_path,
            executable=(sys.executable, str(executable), mode, str(record)),
            budget=budget or ExplorationBudget(max_attempts=5),
            timeout_seconds=timeout_seconds,
        ),
        record,
    )


def _goal() -> Goal:
    return Goal.create(new_id(), "produce verified evidence")


def test_codex_planner_uses_independent_protocol_transport_and_event_mapping(
    tmp_path: Path,
) -> None:
    planner, record_path = _adapter(tmp_path)

    proposal = planner.propose(_goal(), (NON_EMPTY_ARTIFACT_CRITERION,))

    assert isinstance(planner, Planner)
    assert isinstance(planner, CodexProcessTransport)
    assert not isinstance(planner, WorkerAdapter)
    assert not hasattr(planner, "execute")
    assert not hasattr(planner, "cancel")
    assert proposal.plan_revision.status is PlanRevisionStatus.DRAFT
    assert len(proposal.plan_revision.branches) == 2
    assert proposal.budget == ExplorationBudget(max_attempts=5)
    assert proposal.planner_event_types == (
        "planner.provider.started",
        "planner.started",
        "planner.provider.event",
        "planner.completed",
    )
    assert "codex jsonl event: thread.started" in proposal.planner_diagnostics

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["schema"]["title"] == "EHAI Codex exploration plan result"
    assert "EHAI CODEX PLANNER PROTOCOL v1" in record["prompt"]
    assert "WORKER PROTOCOL" not in record["prompt"]
    assert '"max_attempts":5' in record["prompt"]
    assert record["input"]["operation"] == "propose"
    assert "base_plan_revision" not in record["input"]
    assert "base_plan_revision" not in record["prompt"]
    assert record["args"][:8] == [
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--json",
    ]


@pytest.mark.parametrize(
    ("mode", "error_type", "message"),
    [
        ("invalid", CodexPlannerError, "invalid structured result"),
        ("nonzero", CodexPlannerError, "exited with code 7"),
        ("timeout", CodexPlannerTimedOutError, "timed out"),
    ],
)
def test_codex_planner_fails_closed_on_invalid_nonzero_and_timeout(
    tmp_path: Path,
    mode: str,
    error_type: type[CodexPlannerError],
    message: str,
) -> None:
    timeout_seconds = 0.1 if mode == "timeout" else 2.0
    planner, _ = _adapter(tmp_path, mode, timeout_seconds=timeout_seconds)

    with pytest.raises(error_type, match=message):
        planner.propose(_goal(), (NON_EMPTY_ARTIFACT_CRITERION,))


def test_codex_planner_rejects_budget_before_process_and_replans_with_lineage(
    tmp_path: Path,
) -> None:
    constrained, record_path = _adapter(
        tmp_path,
        budget=ExplorationBudget(max_attempts=4),
    )
    with pytest.raises(ValueError, match="attempts"):
        constrained.propose(_goal(), (NON_EMPTY_ARTIFACT_CRITERION,))
    assert not record_path.exists()

    goal = _goal()
    base_proposal = DeterministicExplorationPlanner().propose(
        ExplorationPlanRequest(
            goal,
            (NON_EMPTY_ARTIFACT_CRITERION,),
            ExplorationBudget(max_attempts=5),
        )
    )
    confirmed = base_proposal.contract.confirm()
    aligned = goal.use_completion_contract(confirmed)
    base = base_proposal.plan_revision.approve(confirmed)
    planner, record_path = _adapter(tmp_path)

    replanned = planner.replan(aligned, base, (NON_EMPTY_ARTIFACT_CRITERION,))

    assert replanned.plan_revision.version == 2
    assert replanned.plan_revision.supersedes_plan_revision_id == base.plan_revision_id
    assert replanned.contract.version == 2
    assert replanned.contract.supersedes_completion_contract_id == confirmed.completion_contract_id
    record = json.loads(record_path.read_text(encoding="utf-8"))
    planner_input = record["input"]
    assert planner_input["operation"] == "replan"
    assert "base_plan_revision" in record["prompt"]
    base_input = planner_input["base_plan_revision"]
    assert base_input["plan_revision_id"] == base.plan_revision_id
    assert base_input["version"] == base.version
    assert len(base_input["nodes"]) == 5
    assert len(base_input["edges"]) == 7
    assert len(base_input["branches"]) == 2
    assert "artifacts" not in json.dumps(base_input).lower()
    assert "executiontrace" not in json.dumps(base_input).lower()


def test_codex_planner_rejects_invalid_replan_context_before_process(tmp_path: Path) -> None:
    goal = _goal()
    base_proposal = DeterministicPlanner().propose(goal, (NON_EMPTY_ARTIFACT_CRITERION,))
    confirmed = base_proposal.contract.confirm()
    aligned = goal.use_completion_contract(confirmed)
    planner, record_path = _adapter(tmp_path)

    with pytest.raises(ValueError, match="approved"):
        planner.replan(
            aligned,
            base_proposal.plan_revision,
            (NON_EMPTY_ARTIFACT_CRITERION,),
        )

    assert not record_path.exists()


def test_planner_and_worker_use_distinct_transport_instances(tmp_path: Path) -> None:
    planner, _ = _adapter(tmp_path)
    worker = CodexWorkerAdapter(workspace=tmp_path)

    assert isinstance(worker, CodexProcessTransport)
    assert planner is not worker
    assert planner._environment is not worker._environment


def test_codex_planner_diagnostics_are_persisted_on_propose_and_replan(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    service = cli.build_service(database_path, tmp_path / "artifacts")
    planner, _ = _adapter(tmp_path)
    service._planner = planner  # type: ignore[assignment]
    project = service.create_project(CreateProject("project", "planner events"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "persist planner events"))
    proposed = service.propose_plan(
        ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )
    approved = service.approve_plan(
        ApprovePlan("approve", proposed.plan_revision_id, proposed.completion_contract_id)
    )
    service.replan_plan(
        ReplanPlan("replan", approved.plan_revision_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )

    database = SQLiteDatabase(database_path)
    with database.unit_of_work() as uow:
        planner_events = tuple(
            stored.event
            for stored in uow.events.list_events()
            if stored.event.type is EventType.PLAN_REVISION_PROPOSED
        )

    assert len(planner_events) == 2
    for event in planner_events:
        assert event.payload["planner_event_types"] == [
            "planner.provider.started",
            "planner.started",
            "planner.provider.event",
            "planner.completed",
        ]
        assert "codex jsonl event: thread.started" in event.payload["planner_diagnostics"]
