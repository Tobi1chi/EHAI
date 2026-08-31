"""Pure, evidence-based branch selection for the P1 exploration loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ehai import ID, normalize_id
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)

FIRST_VIABLE_BRANCH_CRITERION = (
    "first active branch in PlanRevision order with a completed node and candidate evidence"
)


class NoViableBranchError(RuntimeError):
    """Raised when no active exploration Branch satisfies the fixed P1 criterion."""


@dataclass(frozen=True, slots=True)
class BranchEvaluationContext:
    """A fully scoped immutable execution snapshot for branch evaluation."""

    plan: PlanRevision
    run: Run
    attempts: tuple[Attempt, ...]
    artifacts: tuple[Artifact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, PlanRevision):
            raise TypeError("BranchEvaluationContext plan must be a PlanRevision")
        if not isinstance(self.run, Run):
            raise TypeError("BranchEvaluationContext run must be a Run")
        attempts = tuple(self.attempts)
        artifacts = tuple(self.artifacts)
        if not all(isinstance(attempt, Attempt) for attempt in attempts):
            raise TypeError("BranchEvaluationContext attempts must contain Attempts")
        if not all(isinstance(artifact, Artifact) for artifact in artifacts):
            raise TypeError("BranchEvaluationContext artifacts must contain Artifacts")
        if self.plan.status is not PlanRevisionStatus.APPROVED:
            raise ValueError("BranchEvaluationContext requires an approved PlanRevision")
        if not self.plan.branches:
            raise ValueError("BranchEvaluationContext PlanRevision has no exploration Branches")
        if self.run.status is not RunStatus.RUNNING:
            raise ValueError("BranchEvaluationContext requires a running Run")
        if (
            self.run.plan_revision_id != self.plan.plan_revision_id
            or self.run.goal_id != self.plan.goal_id
        ):
            raise ValueError("BranchEvaluationContext Run belongs to another PlanRevision")

        branch_node_ids = {node_id for branch in self.plan.branches for node_id in branch.node_ids}
        if len({attempt.attempt_id for attempt in attempts}) != len(attempts):
            raise ValueError("BranchEvaluationContext contains duplicate Attempt IDs")
        attempt_by_id = {attempt.attempt_id: attempt for attempt in attempts}
        for attempt in attempts:
            if attempt.run_id != self.run.run_id:
                raise ValueError(f"Attempt {attempt.attempt_id} belongs to another Run")
            if attempt.plan_node_id not in branch_node_ids:
                raise ValueError(
                    f"Attempt {attempt.attempt_id} is outside the PlanRevision Branches"
                )

        if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise ValueError("BranchEvaluationContext contains duplicate Artifact IDs")
        artifact_ids = {artifact.artifact_id for artifact in artifacts}
        for artifact in artifacts:
            if artifact.run_id != self.run.run_id:
                raise ValueError(f"Artifact {artifact.artifact_id} belongs to another Run")
            if artifact.plan_node_id not in branch_node_ids:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} is outside the PlanRevision Branches"
                )
            if artifact.attempt_id is None or artifact.attempt_id not in attempt_by_id:
                raise ValueError(f"Artifact {artifact.artifact_id} references an unknown Attempt")
            attempt = attempt_by_id[artifact.attempt_id]
            if attempt.plan_node_id != artifact.plan_node_id:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} belongs to another Attempt scope"
                )
            if artifact.artifact_id not in attempt.artifact_ids:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} is absent from its Attempt snapshot"
                )
        missing_artifacts = {
            artifact_id
            for attempt in attempts
            for artifact_id in attempt.artifact_ids
            if artifact_id not in artifact_ids
        }
        if missing_artifacts:
            raise ValueError(
                "BranchEvaluationContext omits Attempt Artifacts: "
                + ", ".join(sorted(missing_artifacts))
            )
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True, slots=True)
class BranchSelection:
    """An immutable, evidenced selection proposal without state mutation authority."""

    selected_branch_id: ID
    pruned_branch_ids: tuple[ID, ...]
    criterion: str
    evidence_artifact_ids: tuple[ID, ...]
    explanation: str

    def __post_init__(self) -> None:
        selected_id = normalize_id(self.selected_branch_id)
        pruned_ids = tuple(normalize_id(branch_id) for branch_id in self.pruned_branch_ids)
        evidence_ids = tuple(
            normalize_id(artifact_id) for artifact_id in self.evidence_artifact_ids
        )
        if len(set(pruned_ids)) != len(pruned_ids):
            raise ValueError("BranchSelection pruned_branch_ids must not contain duplicates")
        if selected_id in pruned_ids:
            raise ValueError("BranchSelection cannot prune its selected Branch")
        if not evidence_ids:
            raise ValueError("BranchSelection requires evidence Artifacts")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("BranchSelection evidence_artifact_ids must not contain duplicates")
        if not isinstance(self.criterion, str) or not self.criterion.strip():
            raise ValueError("BranchSelection criterion must not be blank")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("BranchSelection explanation must not be blank")
        object.__setattr__(self, "selected_branch_id", selected_id)
        object.__setattr__(self, "pruned_branch_ids", pruned_ids)
        object.__setattr__(self, "evidence_artifact_ids", evidence_ids)
        object.__setattr__(self, "criterion", self.criterion.strip())
        object.__setattr__(self, "explanation", self.explanation.strip())


@runtime_checkable
class BranchEvaluator(Protocol):
    """Select an exploration Branch from a read-only execution snapshot."""

    def evaluate(self, context: BranchEvaluationContext) -> BranchSelection:
        """Return an evidenced selection without mutating Plan or execution state."""
        ...


class DeterministicBranchEvaluator:
    """Select the first viable active Branch in persisted PlanRevision order."""

    def evaluate(self, context: BranchEvaluationContext) -> BranchSelection:
        if not isinstance(context, BranchEvaluationContext):
            raise TypeError("context must be a BranchEvaluationContext")
        node_by_id = {node.plan_node_id: node for node in context.plan.nodes}
        attempt_by_id = {attempt.attempt_id: attempt for attempt in context.attempts}
        for branch in context.plan.branches:
            if branch.status is not BranchStatus.ACTIVE:
                continue
            nodes = tuple(node_by_id[node_id] for node_id in branch.node_ids)
            if any(node.status is PlanNodeStatus.FAILED for node in nodes):
                continue
            if not any(node.status is PlanNodeStatus.COMPLETED for node in nodes):
                continue
            evidence = tuple(
                artifact.artifact_id
                for artifact in context.artifacts
                if artifact.plan_node_id in branch.node_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                and artifact.attempt_id is not None
                and attempt_by_id[artifact.attempt_id].status is AttemptStatus.SUCCEEDED
            )
            if not evidence:
                continue
            return BranchSelection(
                selected_branch_id=branch.branch_id,
                pruned_branch_ids=_active_sibling_ids(context.plan.branches, branch),
                criterion=FIRST_VIABLE_BRANCH_CRITERION,
                evidence_artifact_ids=evidence,
                explanation=(
                    f"Selected Branch {branch.branch_id} ({branch.label}) because it is the "
                    "first active Branch in PlanRevision order with a completed node and "
                    "candidate or patch evidence from this Run."
                ),
            )
        raise NoViableBranchError(
            f"Run {context.run.run_id} has no active Branch with completed work and "
            "candidate or patch evidence"
        )


def _active_sibling_ids(branches: tuple[Branch, ...], selected: Branch) -> tuple[ID, ...]:
    return tuple(
        branch.branch_id
        for branch in branches
        if branch.branch_id != selected.branch_id
        and branch.status is BranchStatus.ACTIVE
        and branch.fork_node_id == selected.fork_node_id
        and branch.merge_node_id == selected.merge_node_id
    )
