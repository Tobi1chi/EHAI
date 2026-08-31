from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from ehai import ID, new_id
from ehai.application.evaluation import (
    FIRST_VIABLE_BRANCH_CRITERION,
    BranchEvaluationContext,
    BranchEvaluator,
    BranchSelection,
    DeterministicBranchEvaluator,
    NoViableBranchError,
)
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
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

NOW = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)


def _node(name: str, *, kind: PlanNodeKind = PlanNodeKind.WORK) -> PlanNode:
    return PlanNode(new_id(), name, name, kind=kind)


def _completed(node: PlanNode) -> PlanNode:
    return PlanNode.rehydrate(
        plan_node_id=node.plan_node_id,
        title=node.title,
        instruction=node.instruction,
        kind=node.kind,
        required_dependency_ids=node.required_dependency_ids,
        required_check_ids=node.required_check_ids,
        status=PlanNodeStatus.COMPLETED,
    )


def _failed(node: PlanNode) -> PlanNode:
    return PlanNode.rehydrate(
        plan_node_id=node.plan_node_id,
        title=node.title,
        instruction=node.instruction,
        kind=node.kind,
        required_dependency_ids=node.required_dependency_ids,
        required_check_ids=node.required_check_ids,
        status=PlanNodeStatus.FAILED,
    )


def _edge(
    source: PlanNode,
    target: PlanNode,
    edge_type: EdgeType,
    branch_id: ID,
) -> Edge:
    return Edge(new_id(), source.plan_node_id, target.plan_node_id, edge_type, branch_id)


def _plan(
    left_status: PlanNodeStatus,
    right_status: PlanNodeStatus,
) -> tuple[PlanRevision, Branch, Branch, PlanNode, PlanNode]:
    goal_id = new_id()
    contract = CompletionContract.draft(goal_id, ("choose",), (new_id(),), created_at=NOW)
    fork = _node("fork", kind=PlanNodeKind.FORK)
    left = _node("left")
    right = _node("right")
    merge = _node("evaluate", kind=PlanNodeKind.EVALUATOR)
    left_branch = Branch(
        new_id(), "left", fork.plan_node_id, (left.plan_node_id,), merge.plan_node_id
    )
    right_branch = Branch(
        new_id(), "right", fork.plan_node_id, (right.plan_node_id,), merge.plan_node_id
    )
    edges = (
        _edge(fork, left, EdgeType.EXPLORATION, left_branch.branch_id),
        _edge(left, merge, EdgeType.MERGE, left_branch.branch_id),
        _edge(fork, right, EdgeType.EXPLORATION, right_branch.branch_id),
        _edge(right, merge, EdgeType.MERGE, right_branch.branch_id),
    )
    draft = PlanRevision.draft(
        goal_id,
        contract,
        (fork, left, right, merge),
        edges,
        (left_branch, right_branch),
        created_at=NOW,
    )
    approved = draft.approve(contract.confirm(confirmed_at=NOW), approved_at=NOW)
    nodes = tuple(
        _completed(node)
        if (node.plan_node_id == left.plan_node_id and left_status is PlanNodeStatus.COMPLETED)
        or (node.plan_node_id == right.plan_node_id and right_status is PlanNodeStatus.COMPLETED)
        else _failed(node)
        if (node.plan_node_id == left.plan_node_id and left_status is PlanNodeStatus.FAILED)
        or (node.plan_node_id == right.plan_node_id and right_status is PlanNodeStatus.FAILED)
        else node
        for node in approved.nodes
    )
    plan = PlanRevision.rehydrate(
        plan_revision_id=approved.plan_revision_id,
        goal_id=approved.goal_id,
        version=approved.version,
        completion_contract_id=approved.completion_contract_id,
        completion_contract_version=approved.completion_contract_version,
        nodes=nodes,
        edges=approved.edges,
        branches=approved.branches,
        created_at=approved.created_at,
        status=PlanRevisionStatus.APPROVED,
        approved_at=approved.approved_at,
        supersedes_plan_revision_id=approved.supersedes_plan_revision_id,
    )
    return plan, left_branch, right_branch, left, right


def _run(plan: PlanRevision) -> Run:
    return Run(plan.goal_id, plan.plan_revision_id, created_at=NOW).start(at=NOW)


def _attempt_and_artifact(
    run: Run,
    node: PlanNode,
    *,
    kind: ArtifactKind = ArtifactKind.CANDIDATE,
) -> tuple[Attempt, Artifact]:
    artifact_id = new_id()
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=NOW).start(at=NOW)
    attempt = attempt.succeed((artifact_id,), at=NOW)
    content = b"candidate"
    artifact = Artifact(
        artifact_id=artifact_id,
        kind=kind,
        name=f"{node.title}.txt",
        media_type="text/plain",
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
        relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
        created_at=NOW,
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        attempt_id=attempt.attempt_id,
    )
    return attempt, artifact


def test_two_successes_select_first_branch_with_evidence_without_mutation() -> None:
    plan, left_branch, right_branch, left, right = _plan(
        PlanNodeStatus.COMPLETED, PlanNodeStatus.COMPLETED
    )
    run = _run(plan)
    left_attempt, left_artifact = _attempt_and_artifact(run, left)
    right_attempt, right_artifact = _attempt_and_artifact(run, right, kind=ArtifactKind.PATCH)
    context = BranchEvaluationContext(
        plan,
        run,
        (left_attempt, right_attempt),
        (left_artifact, right_artifact),
    )
    evaluator = DeterministicBranchEvaluator()

    selection = evaluator.evaluate(context)

    assert isinstance(evaluator, BranchEvaluator)
    assert selection.selected_branch_id == left_branch.branch_id
    assert selection.pruned_branch_ids == (right_branch.branch_id,)
    assert selection.criterion == FIRST_VIABLE_BRANCH_CRITERION
    assert selection.evidence_artifact_ids == (left_artifact.artifact_id,)
    assert tuple(branch.status for branch in plan.branches) == (
        BranchStatus.ACTIVE,
        BranchStatus.ACTIVE,
    )
    with pytest.raises(FrozenInstanceError):
        selection.explanation = "changed"  # type: ignore[misc]


def test_failed_first_branch_is_ignored_even_when_it_has_candidate_evidence() -> None:
    plan, left_branch, right_branch, left, right = _plan(
        PlanNodeStatus.FAILED, PlanNodeStatus.COMPLETED
    )
    run = _run(plan)
    left_attempt, left_artifact = _attempt_and_artifact(run, left)
    right_attempt, right_artifact = _attempt_and_artifact(run, right)

    selection = DeterministicBranchEvaluator().evaluate(
        BranchEvaluationContext(
            plan,
            run,
            (left_attempt, right_attempt),
            (left_artifact, right_artifact),
        )
    )

    assert selection.selected_branch_id == right_branch.branch_id
    assert selection.pruned_branch_ids == (left_branch.branch_id,)
    assert selection.evidence_artifact_ids == (right_artifact.artifact_id,)


def test_all_failed_branches_raise_no_viable_branch() -> None:
    plan, _, _, _, _ = _plan(PlanNodeStatus.FAILED, PlanNodeStatus.FAILED)

    with pytest.raises(NoViableBranchError, match="no active Branch"):
        DeterministicBranchEvaluator().evaluate(BranchEvaluationContext(plan, _run(plan), (), ()))


def test_completed_branches_without_candidate_or_patch_evidence_fail_closed() -> None:
    plan, _, _, left, right = _plan(PlanNodeStatus.COMPLETED, PlanNodeStatus.COMPLETED)
    run = _run(plan)
    left_attempt, left_log = _attempt_and_artifact(run, left, kind=ArtifactKind.LOG)
    right_attempt, right_log = _attempt_and_artifact(run, right, kind=ArtifactKind.LOG)

    with pytest.raises(NoViableBranchError, match="candidate or patch evidence"):
        DeterministicBranchEvaluator().evaluate(
            BranchEvaluationContext(
                plan,
                run,
                (left_attempt, right_attempt),
                (left_log, right_log),
            )
        )


def test_context_rejects_foreign_attempt_and_artifact_scopes() -> None:
    plan, _, _, left, _ = _plan(PlanNodeStatus.COMPLETED, PlanNodeStatus.COMPLETED)
    run = _run(plan)
    other_run = _run(plan)
    foreign_attempt, foreign_artifact = _attempt_and_artifact(other_run, left)
    with pytest.raises(ValueError, match="another Run"):
        BranchEvaluationContext(plan, run, (foreign_attempt,), (foreign_artifact,))

    attempt, artifact = _attempt_and_artifact(run, left)
    foreign_artifact = Artifact(
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        name=artifact.name,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        created_at=artifact.created_at,
        run_id=other_run.run_id,
        plan_node_id=artifact.plan_node_id,
        attempt_id=artifact.attempt_id,
    )
    with pytest.raises(ValueError, match="another Run"):
        BranchEvaluationContext(plan, run, (attempt,), (foreign_artifact,))


def test_context_rejects_artifact_from_unknown_attempt() -> None:
    plan, _, _, left, _ = _plan(PlanNodeStatus.COMPLETED, PlanNodeStatus.COMPLETED)
    run = _run(plan)
    attempt, artifact = _attempt_and_artifact(run, left)
    unknown_attempt_artifact = Artifact(
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        name=artifact.name,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        created_at=artifact.created_at,
        run_id=artifact.run_id,
        plan_node_id=artifact.plan_node_id,
        attempt_id=new_id(),
    )

    with pytest.raises(ValueError, match="unknown Attempt"):
        BranchEvaluationContext(plan, run, (attempt,), (unknown_attempt_artifact,))


def test_branch_selection_requires_evidence_and_disjoint_pruned_ids() -> None:
    selected_id = new_id()
    with pytest.raises(ValueError, match="requires evidence"):
        BranchSelection(selected_id, (), "criterion", (), "explanation")
    with pytest.raises(ValueError, match="cannot prune"):
        BranchSelection(selected_id, (selected_id,), "criterion", (new_id(),), "explanation")
