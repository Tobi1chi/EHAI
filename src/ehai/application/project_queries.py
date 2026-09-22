"""Project discovery and compact, snapshot-consistent execution summaries."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ehai import ID, normalize_id, utc_now
from ehai.application.ports import ReadSession, StoredEvent
from ehai.application.queries import (
    ArtifactView,
    QueryNotFoundError,
    RunView,
    _artifact_view,
    _run_view,
)
from ehai.application.run_results import configured_workspace
from ehai.application.runtime_control import RuntimeHealthView
from ehai.domain.execution import Run
from ehai.domain.goal import Goal, GoalStatus, Project
from ehai.domain.planning import BranchStatus, PlanNodeStatus, PlanRevisionStatus


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: ID
    name: str
    created_at: datetime
    goal_count: int
    run_count: int


@dataclass(frozen=True, slots=True)
class ProjectListView:
    observed_at: datetime
    event_offset: int
    projects: tuple[ProjectSummary, ...]


@dataclass(frozen=True, slots=True)
class ProjectPlanSummary:
    plan_revision_id: ID
    version: int
    status: PlanRevisionStatus


@dataclass(frozen=True, slots=True)
class CompletedNodeResult:
    """Current completed node and the retained evidence for its passing Gate."""

    plan_node_id: ID
    title: str
    checkpoint_id: ID
    gate_id: ID
    attempt_id: ID
    adoption_id: ID | None
    artifacts: tuple[ArtifactView, ...]


@dataclass(frozen=True, slots=True)
class ProjectRunSummary:
    run: RunView
    process_revision_id: ID | None
    configured_workspace: str | None
    worker_endpoint_ids: tuple[ID, ...]
    node_counts: dict[str, int]
    completed_results: tuple[CompletedNodeResult, ...]


@dataclass(frozen=True, slots=True)
class ProjectGoalSummary:
    goal_id: ID
    objective: str
    status: GoalStatus
    created_at: datetime
    plans: tuple[ProjectPlanSummary, ...]
    runs: tuple[ProjectRunSummary, ...]


@dataclass(frozen=True, slots=True)
class ProjectDetailView:
    observed_at: datetime
    event_offset: int
    project: ProjectSummary
    goals: tuple[ProjectGoalSummary, ...]


@dataclass(frozen=True, slots=True)
class RuntimeContextView:
    """Live host configuration; never inferred from historical registry entries."""

    observed_at: datetime
    workspace: str | None
    worker_endpoint_id: ID | None
    execution_config_fingerprint: str | None
    health: RuntimeHealthView | None


def list_projects(session: ReadSession) -> ProjectListView:
    """Discover all projects without loading traces, model sessions or artifact bytes."""
    offset = session.events.latest_offset()
    return ProjectListView(
        observed_at=utc_now(),
        event_offset=offset,
        projects=tuple(_project_summary(session, p) for p in session.states.list_projects()),
    )


def get_project(session: ReadSession, project_id: ID) -> ProjectDetailView:
    """Read a project's complete navigation tree from the same database snapshot."""
    project_id = normalize_id(project_id)
    offset = session.events.latest_offset()
    project = session.states.get_project(project_id)
    if project is None:
        raise QueryNotFoundError("Project", project_id)
    # One event scan for the project, rather than one full trace scan per Run.
    events_by_run: dict[ID, list[StoredEvent]] = {}
    goals = session.states.list_goals(project_id)
    runs_by_goal = {g.goal_id: session.states.list_runs(g.goal_id) for g in goals}
    run_ids = {r.run_id for runs in runs_by_goal.values() for r in runs}
    if run_ids:
        for stored in session.events.list_events():
            if stored.event.run_id is not None and stored.event.run_id in run_ids:
                events_by_run.setdefault(stored.event.run_id, []).append(stored)
    return ProjectDetailView(
        observed_at=utc_now(),
        event_offset=offset,
        project=ProjectSummary(
            project.project_id, project.name, project.created_at, len(goals), len(run_ids)
        ),
        goals=tuple(
            _goal_summary(session, goal, runs_by_goal[goal.goal_id], events_by_run)
            for goal in goals
        ),
    )


def _project_summary(session: ReadSession, project: Project) -> ProjectSummary:
    goals = session.states.list_goals(project.project_id)
    return ProjectSummary(
        project_id=project.project_id,
        name=project.name,
        created_at=project.created_at,
        goal_count=len(goals),
        run_count=sum(len(session.states.list_runs(g.goal_id)) for g in goals),
    )


def _goal_summary(
    session: ReadSession,
    goal: Goal,
    runs: Sequence[Run],
    events_by_run: dict[ID, list[StoredEvent]],
) -> ProjectGoalSummary:
    return ProjectGoalSummary(
        goal_id=goal.goal_id,
        objective=goal.objective,
        status=goal.status,
        created_at=goal.created_at,
        plans=tuple(
            ProjectPlanSummary(p.plan_revision_id, p.version, p.status)
            for p in session.states.list_plan_revisions(goal.goal_id)
        ),
        runs=tuple(_project_run(session, r, events_by_run.get(r.run_id, ())) for r in runs),
    )


def _project_run(
    session: ReadSession, run: Run, events: Sequence[StoredEvent]
) -> ProjectRunSummary:
    plan = session.states.get_execution_plan(run.run_id)
    process = session.states.get_active_process_revision(run.run_id)
    attempts = session.states.list_attempts(run.run_id)
    results: list[CompletedNodeResult] = []
    if plan is not None:
        excluded = {
            node_id
            for branch in plan.branches
            if branch.status is not BranchStatus.SELECTED
            for node_id in branch.node_ids
        }
        latest_attempts = {a.plan_node_id: a for a in attempts}
        adoptions = {
            a.target_plan_node_id: a for a in session.states.list_result_adoptions(run.run_id)
        }
        checkpoints = {
            c.gate_decision.plan_node_id: c for c in session.states.list_checkpoints(run.run_id)
        }
        for node in plan.nodes:
            if node.status is not PlanNodeStatus.COMPLETED or node.plan_node_id in excluded:
                continue
            checkpoint = checkpoints.get(node.plan_node_id)
            if checkpoint is None:
                continue
            decision = checkpoint.gate_decision
            attempt = latest_attempts.get(node.plan_node_id)
            adoption = adoptions.get(node.plan_node_id)
            if attempt is not None:
                if decision.attempt_id != attempt.attempt_id or decision.adoption_id is not None:
                    continue
            elif (
                adoption is None
                or decision.adoption_id != adoption.adoption_id
                or decision.attempt_id != adoption.source_attempt_id
            ):
                continue
            artifacts = []
            for artifact_id in decision.evidence_artifact_ids:
                artifact = session.states.get_artifact(artifact_id)
                if artifact is None:
                    raise QueryNotFoundError("Artifact", artifact_id)
                artifacts.append(_artifact_view(artifact))
            results.append(
                CompletedNodeResult(
                    node.plan_node_id,
                    node.title,
                    checkpoint.checkpoint_id,
                    decision.gate_id,
                    decision.attempt_id,
                    decision.adoption_id,
                    tuple(artifacts),
                )
            )
    return ProjectRunSummary(
        run=_run_view(run, session),
        process_revision_id=None if process is None else process.process_revision_id,
        configured_workspace=configured_workspace(events, run.run_id),
        worker_endpoint_ids=tuple(
            sorted({a.worker_endpoint_id for a in attempts if a.worker_endpoint_id is not None})
        ),
        node_counts={} if plan is None else dict(Counter(n.status.value for n in plan.nodes)),
        completed_results=tuple(results),
    )
