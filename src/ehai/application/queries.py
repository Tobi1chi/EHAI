"""Read-only P1 query models kept separate from the mutable execution domain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ehai import ID, normalize_id
from ehai.application.ports import ReadSession, StoredEvent
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import (
    CheckKind,
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    CheckSpec,
    GateDecision,
)
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)

ReadSessionFactory = Callable[[], ReadSession]


class QueryNotFoundError(LookupError):
    """Raised when a query owner or requested entity does not exist."""

    def __init__(self, entity_name: str, entity_id: ID) -> None:
        self.entity_name = entity_name
        self.entity_id = entity_id
        super().__init__(f"{entity_name} {entity_id} was not found")


@dataclass(frozen=True, slots=True)
class RunView:
    """Public current-state view of one Run."""

    run_id: ID
    goal_id: ID
    plan_revision_id: ID
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    status_reason: str | None


@dataclass(frozen=True, slots=True)
class PlanNodeView:
    """One node in a PlanGraph query result."""

    plan_node_id: ID
    title: str
    instruction: str
    kind: PlanNodeKind
    required_dependency_ids: tuple[ID, ...]
    required_check_ids: tuple[ID, ...]
    status: PlanNodeStatus


@dataclass(frozen=True, slots=True)
class EdgeView:
    """One directed relationship in a PlanGraph query result."""

    edge_id: ID
    source_node_id: ID
    target_node_id: ID
    edge_type: EdgeType
    branch_id: ID | None
    condition: str | None


@dataclass(frozen=True, slots=True)
class BranchView:
    """One exploration branch in a PlanGraph query result."""

    branch_id: ID
    label: str
    fork_node_id: ID
    node_ids: tuple[ID, ...]
    merge_node_id: ID
    status: BranchStatus


@dataclass(frozen=True, slots=True)
class PlanGraphView:
    """Plan metadata and graph structure, deliberately excluding execution trace data."""

    plan_revision_id: ID
    goal_id: ID
    version: int
    completion_contract_id: ID
    completion_contract_version: int
    created_at: datetime
    status: PlanRevisionStatus
    approved_at: datetime | None
    supersedes_plan_revision_id: ID | None
    nodes: tuple[PlanNodeView, ...]
    edges: tuple[EdgeView, ...]
    branches: tuple[BranchView, ...]


@dataclass(frozen=True, slots=True)
class AttemptView:
    """Public execution metadata for one Worker Attempt."""

    attempt_id: ID
    run_id: ID
    plan_node_id: ID
    sequence: int
    status: AttemptStatus
    artifact_ids: tuple[ID, ...]
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    outcome_reason: str | None


@dataclass(frozen=True, slots=True)
class ArtifactView:
    """Public Artifact metadata; storage-relative paths are intentionally private."""

    artifact_id: ID
    kind: ArtifactKind
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    run_id: ID | None
    plan_node_id: ID | None
    attempt_id: ID | None


@dataclass(frozen=True, slots=True)
class CheckSpecView:
    """Public immutable Check definition for a PlanRevision."""

    check_id: ID
    name: str
    kind: CheckKind
    description: str
    required: bool


@dataclass(frozen=True, slots=True)
class CheckResultView:
    """Public Checker verdict and its evidence references."""

    check_id: ID
    check_run_id: ID
    run_id: ID
    plan_node_id: ID
    attempt_id: ID
    passed: bool
    evaluated_at: datetime
    evidence_artifact_ids: tuple[ID, ...]
    output: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class CheckRunView:
    """Public current-state view of one Check execution."""

    check_run_id: ID
    run_id: ID
    plan_node_id: ID
    attempt_id: ID
    check_id: ID
    status: CheckRunStatus
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    result: CheckResultView | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class GateDecisionView:
    """Auditable Gate outcome embedded in a Checkpoint summary."""

    gate_id: ID
    run_id: ID
    plan_node_id: ID
    attempt_id: ID
    passed: bool
    evaluated_at: datetime
    required_check_ids: tuple[ID, ...]
    failed_check_ids: tuple[ID, ...]
    evidence_artifact_ids: tuple[ID, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class BranchSelectionView:
    """One stable fork-to-selected-branch mapping in a Checkpoint."""

    fork_node_id: ID
    branch_id: ID


@dataclass(frozen=True, slots=True)
class CheckpointSummary:
    """Recovery metadata without embedded Run or PlanGraph snapshots."""

    checkpoint_id: ID
    plan_revision_id: ID
    run_id: ID
    event_offset: int
    gate_decision: GateDecisionView
    branch_selections: tuple[BranchSelectionView, ...]
    artifact_ids: tuple[ID, ...]
    created_at: datetime


# A concise endpoint-facing name while retaining the trace-specific summary term.
CheckpointView = CheckpointSummary


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    """Execution facts only; no PlanNode, Edge, or Branch graph structures."""

    run: RunView
    attempts: tuple[AttemptView, ...]
    artifacts: tuple[ArtifactView, ...]
    check_runs: tuple[CheckRunView, ...]
    checkpoints: tuple[CheckpointSummary, ...]
    events: tuple[StoredEvent, ...]


@dataclass(frozen=True, slots=True)
class EventPage:
    """One stable Event Log page using Event IDs as resume cursors."""

    events: tuple[StoredEvent, ...]
    after_event_id: ID | None
    next_after_event_id: ID | None
    latest_offset: int
    has_more: bool


class QueryService:
    """Build P1 read models from one short-lived snapshot per query."""

    def __init__(self, *, read_session_factory: ReadSessionFactory) -> None:
        self._read_session_factory = read_session_factory

    def get_run(self, run_id: ID) -> RunView:
        """Return current Run state and fail closed when it is absent."""
        normalized_id = normalize_id(run_id)
        with self._read_session_factory() as session:
            run = _required_run(session.states.get_run(normalized_id), normalized_id)
            return _run_view(run)

    def get_plan_graph(self, plan_revision_id: ID) -> PlanGraphView:
        """Return only versioned plan metadata and graph structure."""
        normalized_id = normalize_id(plan_revision_id)
        with self._read_session_factory() as session:
            plan = _required_plan(session.states.get_plan_revision(normalized_id), normalized_id)
            return _plan_graph_view(plan)

    def get_execution_trace(self, run_id: ID) -> ExecutionTraceView:
        """Return immutable execution facts without copying PlanGraph structure."""
        normalized_id = normalize_id(run_id)
        with self._read_session_factory() as session:
            run = _required_run(session.states.get_run(normalized_id), normalized_id)
            attempts = tuple(
                _attempt_view(attempt)
                for attempt in sorted(
                    session.states.list_attempts(normalized_id),
                    key=lambda item: (item.sequence, item.attempt_id),
                )
            )
            artifacts = tuple(
                _artifact_view(artifact)
                for artifact in sorted(
                    session.states.list_artifacts_for_run(normalized_id),
                    key=lambda item: (item.created_at, item.artifact_id),
                )
            )
            check_runs = tuple(
                _check_run_view(check_run)
                for check_run in sorted(
                    session.states.list_check_runs(normalized_id),
                    key=lambda item: (item.created_at, item.check_run_id),
                )
            )
            checkpoints = tuple(
                _checkpoint_summary(checkpoint)
                for checkpoint in sorted(
                    session.states.list_checkpoints(normalized_id),
                    key=lambda item: (item.event_offset, item.checkpoint_id),
                )
            )
            events = tuple(
                stored
                for stored in sorted(session.events.list_events(), key=lambda item: item.offset)
                if stored.event.run_id == normalized_id
            )
            return ExecutionTraceView(
                run=_run_view(run),
                attempts=attempts,
                artifacts=artifacts,
                check_runs=check_runs,
                checkpoints=checkpoints,
                events=events,
            )

    def list_check_specs(self, plan_revision_id: ID) -> tuple[CheckSpecView, ...]:
        """List the retained Check definitions for an existing PlanRevision."""
        normalized_id = normalize_id(plan_revision_id)
        with self._read_session_factory() as session:
            _required_plan(session.states.get_plan_revision(normalized_id), normalized_id)
            return tuple(
                _check_spec_view(check_spec)
                for check_spec in session.states.list_check_specs(normalized_id)
            )

    def list_check_runs(self, run_id: ID) -> tuple[CheckRunView, ...]:
        """List Check executions for an existing Run in stable order."""
        normalized_id = normalize_id(run_id)
        with self._read_session_factory() as session:
            _required_run(session.states.get_run(normalized_id), normalized_id)
            return tuple(
                _check_run_view(check_run)
                for check_run in sorted(
                    session.states.list_check_runs(normalized_id),
                    key=lambda item: (item.created_at, item.check_run_id),
                )
            )

    def list_checkpoints(self, run_id: ID) -> tuple[CheckpointSummary, ...]:
        """List recovery summaries for an existing Run in Event order."""
        normalized_id = normalize_id(run_id)
        with self._read_session_factory() as session:
            _required_run(session.states.get_run(normalized_id), normalized_id)
            return tuple(
                _checkpoint_summary(checkpoint)
                for checkpoint in sorted(
                    session.states.list_checkpoints(normalized_id),
                    key=lambda item: (item.event_offset, item.checkpoint_id),
                )
            )

    def list_artifacts(self, run_id: ID) -> tuple[ArtifactView, ...]:
        """List public Artifact metadata for an existing Run."""
        normalized_id = normalize_id(run_id)
        with self._read_session_factory() as session:
            _required_run(session.states.get_run(normalized_id), normalized_id)
            return tuple(
                _artifact_view(artifact)
                for artifact in sorted(
                    session.states.list_artifacts_for_run(normalized_id),
                    key=lambda item: (item.created_at, item.artifact_id),
                )
            )

    def get_artifact(self, artifact_id: ID) -> ArtifactView:
        """Return public Artifact metadata and fail closed when it is absent."""
        normalized_id = normalize_id(artifact_id)
        with self._read_session_factory() as session:
            artifact = session.states.get_artifact(normalized_id)
            if artifact is None:
                raise QueryNotFoundError("Artifact", normalized_id)
            return _artifact_view(artifact)

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int = 100,
    ) -> EventPage:
        """Return one cursor page; unknown cursors are rejected by the Event reader."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Event page limit must be an integer between 1 and 1000")
        cursor = None if after_event_id is None else normalize_id(after_event_id)
        with self._read_session_factory() as session:
            try:
                candidates = session.events.list_events(
                    after_event_id=cursor,
                    limit=limit + 1,
                )
            except LookupError as error:
                if cursor is None:
                    raise
                raise QueryNotFoundError("Event", cursor) from error
            events = tuple(sorted(candidates[:limit], key=lambda item: item.offset))
            next_cursor = cursor if not events else events[-1].event.id
            return EventPage(
                events=events,
                after_event_id=cursor,
                next_after_event_id=next_cursor,
                latest_offset=session.events.latest_offset(),
                has_more=len(candidates) > limit,
            )


def _required_run(run: Run | None, run_id: ID) -> Run:
    if run is None:
        raise QueryNotFoundError("Run", run_id)
    return run


def _required_plan(plan: PlanRevision | None, plan_revision_id: ID) -> PlanRevision:
    if plan is None:
        raise QueryNotFoundError("PlanRevision", plan_revision_id)
    return plan


def _run_view(run: Run) -> RunView:
    return RunView(
        run_id=run.run_id,
        goal_id=run.goal_id,
        plan_revision_id=run.plan_revision_id,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        status_reason=run.status_reason,
    )


def _plan_graph_view(plan: PlanRevision) -> PlanGraphView:
    return PlanGraphView(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
        nodes=tuple(_plan_node_view(node) for node in plan.nodes),
        edges=tuple(_edge_view(edge) for edge in plan.edges),
        branches=tuple(_branch_view(branch) for branch in plan.branches),
    )


def _plan_node_view(node: PlanNode) -> PlanNodeView:
    return PlanNodeView(
        plan_node_id=node.plan_node_id,
        title=node.title,
        instruction=node.instruction,
        kind=node.kind,
        required_dependency_ids=node.required_dependency_ids,
        required_check_ids=node.required_check_ids,
        status=node.status,
    )


def _edge_view(edge: Edge) -> EdgeView:
    return EdgeView(
        edge_id=edge.edge_id,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        edge_type=edge.edge_type,
        branch_id=edge.branch_id,
        condition=edge.condition,
    )


def _branch_view(branch: Branch) -> BranchView:
    return BranchView(
        branch_id=branch.branch_id,
        label=branch.label,
        fork_node_id=branch.fork_node_id,
        node_ids=branch.node_ids,
        merge_node_id=branch.merge_node_id,
        status=branch.status,
    )


def _attempt_view(attempt: Attempt) -> AttemptView:
    return AttemptView(
        attempt_id=attempt.attempt_id,
        run_id=attempt.run_id,
        plan_node_id=attempt.plan_node_id,
        sequence=attempt.sequence,
        status=attempt.status,
        artifact_ids=attempt.artifact_ids,
        created_at=attempt.created_at,
        started_at=attempt.started_at,
        ended_at=attempt.ended_at,
        outcome_reason=attempt.outcome_reason,
    )


def _artifact_view(artifact: Artifact) -> ArtifactView:
    return ArtifactView(
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        name=artifact.name,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        created_at=artifact.created_at,
        run_id=artifact.run_id,
        plan_node_id=artifact.plan_node_id,
        attempt_id=artifact.attempt_id,
    )


def _check_spec_view(check_spec: CheckSpec) -> CheckSpecView:
    return CheckSpecView(
        check_id=check_spec.check_id,
        name=check_spec.name,
        kind=check_spec.kind,
        description=check_spec.description,
        required=check_spec.required,
    )


def _check_result_view(result: CheckResult) -> CheckResultView:
    return CheckResultView(
        check_id=result.check_id,
        check_run_id=result.check_run_id,
        run_id=result.run_id,
        plan_node_id=result.plan_node_id,
        attempt_id=result.attempt_id,
        passed=result.passed,
        evaluated_at=result.evaluated_at,
        evidence_artifact_ids=result.evidence_artifact_ids,
        output=result.output,
        failure_reason=result.failure_reason,
    )


def _check_run_view(check_run: CheckRun) -> CheckRunView:
    return CheckRunView(
        check_run_id=check_run.check_run_id,
        run_id=check_run.run_id,
        plan_node_id=check_run.plan_node_id,
        attempt_id=check_run.attempt_id,
        check_id=check_run.check_id,
        status=check_run.status,
        created_at=check_run.created_at,
        started_at=check_run.started_at,
        ended_at=check_run.ended_at,
        result=(None if check_run.result is None else _check_result_view(check_run.result)),
        failure_reason=check_run.failure_reason,
    )


def _gate_decision_view(decision: GateDecision) -> GateDecisionView:
    return GateDecisionView(
        gate_id=decision.gate_id,
        run_id=decision.run_id,
        plan_node_id=decision.plan_node_id,
        attempt_id=decision.attempt_id,
        passed=decision.passed,
        evaluated_at=decision.evaluated_at,
        required_check_ids=decision.required_check_ids,
        failed_check_ids=decision.failed_check_ids,
        evidence_artifact_ids=decision.evidence_artifact_ids,
        reason=decision.reason,
    )


def _checkpoint_summary(checkpoint: Checkpoint) -> CheckpointSummary:
    return CheckpointSummary(
        checkpoint_id=checkpoint.checkpoint_id,
        plan_revision_id=checkpoint.plan_revision_id,
        run_id=checkpoint.run_id,
        event_offset=checkpoint.event_offset,
        gate_decision=_gate_decision_view(checkpoint.gate_decision),
        branch_selections=tuple(
            BranchSelectionView(fork_node_id=fork_id, branch_id=branch_id)
            for fork_id, branch_id in sorted(checkpoint.branch_selections.items())
        ),
        artifact_ids=checkpoint.artifact_refs,
        created_at=checkpoint.created_at,
    )
