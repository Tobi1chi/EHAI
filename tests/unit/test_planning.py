from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from ehai import ID, new_id
from ehai.domain.checking import GateDecision
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    EdgeType,
    PlanInvariantError,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
    PlanTransitionError,
)

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _node(
    title: str,
    *,
    kind: PlanNodeKind = PlanNodeKind.WORK,
    dependencies: tuple[ID, ...] = (),
    checks: tuple[ID, ...] = (),
) -> PlanNode:
    return PlanNode(
        plan_node_id=new_id(),
        title=title,
        instruction=f"execute {title}",
        kind=kind,
        required_dependency_ids=dependencies,
        required_check_ids=checks,
    )


def _edge(
    source: PlanNode,
    target: PlanNode,
    edge_type: EdgeType,
    *,
    branch_id: ID | None = None,
) -> Edge:
    return Edge(
        edge_id=new_id(),
        source_node_id=source.plan_node_id,
        target_node_id=target.plan_node_id,
        edge_type=edge_type,
        branch_id=branch_id,
    )


def _contract(goal_id: ID) -> CompletionContract:
    return CompletionContract.draft(
        goal_id,
        ("final evidence accepted",),
        (new_id(),),
        created_at=NOW,
    )


def _gate_decision(
    node: PlanNode,
    check_id: ID,
    *,
    passed: bool = True,
    plan_node_id: ID | None = None,
) -> GateDecision:
    return GateDecision(
        gate_id=new_id(),
        run_id=new_id(),
        plan_node_id=plan_node_id or node.plan_node_id,
        attempt_id=new_id(),
        passed=passed,
        evaluated_at=NOW,
        required_check_ids=(check_id,),
        failed_check_ids=() if passed else (check_id,),
        evidence_artifact_ids=(new_id(),),
        reason=None if passed else "required check failed",
    )


def _single_node_revision() -> tuple[PlanRevision, CompletionContract, PlanNode]:
    goal_id = new_id()
    contract = _contract(goal_id)
    node = _node("work")
    revision = PlanRevision.draft(goal_id, contract, (node,), (), created_at=NOW)
    return revision, contract, node


def _exploration_revision() -> tuple[PlanRevision, CompletionContract, tuple[Branch, Branch]]:
    goal_id = new_id()
    contract = _contract(goal_id)
    fork = _node("fork", kind=PlanNodeKind.FORK)
    left = _node("left")
    right = _node("right")
    merge = _node("merge", kind=PlanNodeKind.MERGE)
    left_branch_id = new_id()
    right_branch_id = new_id()
    left_branch = Branch(
        left_branch_id, "left", fork.plan_node_id, (left.plan_node_id,), merge.plan_node_id
    )
    right_branch = Branch(
        right_branch_id,
        "right",
        fork.plan_node_id,
        (right.plan_node_id,),
        merge.plan_node_id,
    )
    edges = (
        _edge(fork, left, EdgeType.EXPLORATION, branch_id=left_branch_id),
        _edge(left, merge, EdgeType.MERGE, branch_id=left_branch_id),
        _edge(fork, right, EdgeType.EXPLORATION, branch_id=right_branch_id),
        _edge(right, merge, EdgeType.MERGE, branch_id=right_branch_id),
    )
    revision = PlanRevision.draft(
        goal_id,
        contract,
        (fork, left, right, merge),
        edges,
        (left_branch, right_branch),
        created_at=NOW,
    )
    return revision, contract, (left_branch, right_branch)


def test_single_node_plan_can_be_approved_against_confirmed_contract() -> None:
    revision, contract, _ = _single_node_revision()

    approved = revision.approve(contract.confirm(confirmed_at=NOW), approved_at=NOW)

    assert revision.status is PlanRevisionStatus.DRAFT
    assert approved.status is PlanRevisionStatus.APPROVED
    with pytest.raises(FrozenInstanceError):
        approved.nodes = ()  # type: ignore[misc]


def test_replanning_creates_a_new_draft_version_and_preserves_old_graph() -> None:
    revision, contract, node = _single_node_revision()
    approved = revision.approve(contract.confirm(confirmed_at=NOW), approved_at=NOW)
    replacement = _node("replacement")

    revised = approved.revise(contract, (replacement,), (), created_at=NOW + timedelta(minutes=1))

    assert revised.version == 2
    assert revised.plan_revision_id != approved.plan_revision_id
    assert revised.supersedes_plan_revision_id == approved.plan_revision_id
    assert revised.status is PlanRevisionStatus.DRAFT
    assert approved.nodes == (node,)


def test_plan_node_worker_success_is_only_a_candidate_until_gate_passes() -> None:
    check_id = new_id()
    node = _node("work", checks=(check_id,))
    candidate = node.mark_ready().start().submit_candidate()

    assert candidate.status is PlanNodeStatus.CANDIDATE
    with pytest.raises(PlanTransitionError, match=str(node.plan_node_id)):
        candidate.complete(_gate_decision(node, check_id))

    completed = candidate.begin_verification().complete(_gate_decision(node, check_id))
    assert completed.status is PlanNodeStatus.COMPLETED


def test_plan_node_rejects_completion_from_failed_gate() -> None:
    check_id = new_id()
    verifying = (
        _node("work", checks=(check_id,))
        .mark_ready()
        .start()
        .submit_candidate()
        .begin_verification()
    )

    with pytest.raises(PlanTransitionError, match=str(verifying.plan_node_id)):
        verifying.complete(_gate_decision(verifying, check_id, passed=False))


def test_plan_node_rejects_gate_for_another_node() -> None:
    check_id = new_id()
    verifying = (
        _node("work", checks=(check_id,))
        .mark_ready()
        .start()
        .submit_candidate()
        .begin_verification()
    )

    with pytest.raises(PlanTransitionError, match="for node"):
        verifying.complete(_gate_decision(verifying, check_id, plan_node_id=new_id()))


def test_completed_node_requires_gate_transition_or_rehydration_boundary() -> None:
    node = _node("work")
    with pytest.raises(PlanInvariantError, match=r"complete\(\) or rehydrate\(\)"):
        replace(node, status=PlanNodeStatus.COMPLETED)

    restored = PlanNode.rehydrate(
        plan_node_id=node.plan_node_id,
        title=node.title,
        instruction=node.instruction,
        kind=node.kind,
        required_dependency_ids=node.required_dependency_ids,
        required_check_ids=node.required_check_ids,
        status=PlanNodeStatus.COMPLETED,
    )
    assert restored.status is PlanNodeStatus.COMPLETED


def test_failed_node_can_retry_and_only_unexecuted_node_can_be_pruned() -> None:
    node = _node("work")

    assert node.mark_ready().start().fail().retry().status is PlanNodeStatus.READY
    assert node.prune().status is PlanNodeStatus.PRUNED
    with pytest.raises(PlanTransitionError, match=str(node.plan_node_id)):
        node.mark_ready().start().prune()


def test_exploration_graph_validates_fork_branches_and_merge() -> None:
    revision, _, branches = _exploration_revision()

    assert len(revision.branches) == 2
    assert branches[0].select().status is BranchStatus.SELECTED
    pruned = branches[1].prune()
    assert pruned.status is BranchStatus.PRUNED
    assert pruned.node_ids == branches[1].node_ids


def test_non_active_branch_requires_transition_or_rehydration_boundary() -> None:
    _, _, branches = _exploration_revision()
    branch = branches[0]

    with pytest.raises(PlanInvariantError, match="transition or rehydrate"):
        replace(branch, status=BranchStatus.PRUNED)

    restored = Branch.rehydrate(
        branch_id=branch.branch_id,
        label=branch.label,
        fork_node_id=branch.fork_node_id,
        node_ids=branch.node_ids,
        merge_node_id=branch.merge_node_id,
        status=BranchStatus.PRUNED,
    )
    assert restored.status is BranchStatus.PRUNED


def test_approved_revision_requires_approval_or_rehydration_boundary() -> None:
    revision, _, _ = _single_node_revision()
    with pytest.raises(PlanInvariantError, match=r"approve\(\) or rehydrate\(\)"):
        replace(revision, status=PlanRevisionStatus.APPROVED, approved_at=NOW)

    restored = PlanRevision.rehydrate(
        plan_revision_id=revision.plan_revision_id,
        goal_id=revision.goal_id,
        version=revision.version,
        completion_contract_id=revision.completion_contract_id,
        completion_contract_version=revision.completion_contract_version,
        nodes=revision.nodes,
        edges=revision.edges,
        branches=revision.branches,
        created_at=revision.created_at,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=revision.supersedes_plan_revision_id,
    )
    assert restored.status is PlanRevisionStatus.APPROVED


def test_graph_models_snapshot_mutable_container_inputs() -> None:
    dependency_ids = [new_id()]
    check_ids = [new_id()]
    node = PlanNode(
        new_id(),
        "work",
        "execute work",
        required_dependency_ids=dependency_ids,  # type: ignore[arg-type]
        required_check_ids=check_ids,  # type: ignore[arg-type]
    )
    branch_nodes = [node.plan_node_id]
    branch = Branch(
        new_id(),
        "branch",
        new_id(),
        branch_nodes,  # type: ignore[arg-type]
        new_id(),
    )
    revision, contract, revision_node = _single_node_revision()
    nodes = [revision_node]
    edges: list[Edge] = []
    branches: list[Branch] = []
    snapshotted = PlanRevision(
        revision.plan_revision_id,
        revision.goal_id,
        revision.version,
        contract.completion_contract_id,
        contract.version,
        nodes,  # type: ignore[arg-type]
        edges,  # type: ignore[arg-type]
        branches,  # type: ignore[arg-type]
        NOW,
    )

    dependency_ids.append(new_id())
    check_ids.append(new_id())
    branch_nodes.append(new_id())
    nodes.append(_node("late"))

    assert len(node.required_dependency_ids) == 1
    assert len(node.required_check_ids) == 1
    assert branch.node_ids == (node.plan_node_id,)
    assert snapshotted.nodes == (revision_node,)


def test_graph_rejects_duplicate_ids_unknown_references_and_self_loops() -> None:
    revision, contract, node = _single_node_revision()
    duplicate = PlanNode(node.plan_node_id, "duplicate", "execute duplicate")
    with pytest.raises(PlanInvariantError, match="duplicate PlanNode ID"):
        PlanRevision.draft(
            revision.goal_id,
            contract,
            (node, duplicate),
            (),
            created_at=NOW,
        )

    unknown = _node("unknown")
    with pytest.raises(PlanInvariantError, match="unknown PlanNode"):
        PlanRevision.draft(
            revision.goal_id,
            contract,
            (node,),
            (_edge(node, unknown, EdgeType.DEPENDENCY),),
            created_at=NOW,
        )

    with pytest.raises(PlanInvariantError, match="self-loop"):
        _edge(node, node, EdgeType.DEPENDENCY)


def test_graph_rejects_cycle_and_dependency_declaration_mismatch() -> None:
    goal_id = new_id()
    contract = _contract(goal_id)
    first_id = new_id()
    second_id = new_id()
    first = PlanNode(first_id, "first", "execute first", required_dependency_ids=(second_id,))
    second = PlanNode(second_id, "second", "execute second", required_dependency_ids=(first_id,))

    with pytest.raises(PlanInvariantError, match="illegal cycle"):
        PlanRevision.draft(
            goal_id,
            contract,
            (first, second),
            (
                _edge(second, first, EdgeType.DEPENDENCY),
                _edge(first, second, EdgeType.DEPENDENCY),
            ),
            created_at=NOW,
        )

    independent = _node("independent")
    undeclared_dependent = _node("undeclared dependent")
    with pytest.raises(PlanInvariantError, match="dependency Edges"):
        PlanRevision.draft(
            goal_id,
            contract,
            (independent, undeclared_dependent),
            (_edge(independent, undeclared_dependent, EdgeType.DEPENDENCY),),
            created_at=NOW,
        )


def test_graph_rejects_invalid_branch_relationships() -> None:
    goal_id = new_id()
    contract = _contract(goal_id)
    not_a_fork = _node("ordinary")
    candidate = _node("candidate")
    merge = _node("merge", kind=PlanNodeKind.MERGE)
    branch_id = new_id()
    branch = Branch(
        branch_id,
        "only",
        not_a_fork.plan_node_id,
        (candidate.plan_node_id,),
        merge.plan_node_id,
    )

    with pytest.raises(PlanInvariantError, match="fork and merge node roles"):
        PlanRevision.draft(
            goal_id,
            contract,
            (not_a_fork, candidate, merge),
            (
                _edge(not_a_fork, candidate, EdgeType.EXPLORATION, branch_id=branch_id),
                _edge(candidate, merge, EdgeType.MERGE, branch_id=branch_id),
            ),
            (branch,),
            created_at=NOW,
        )


def test_graph_rejects_edges_between_sibling_branch_interiors() -> None:
    revision, contract, branches = _exploration_revision()
    fork, left, right, merge = revision.nodes
    right_with_cross_dependency = replace(
        right,
        required_dependency_ids=(left.plan_node_id,),
    )
    cross_edge = _edge(left, right_with_cross_dependency, EdgeType.DEPENDENCY)

    with pytest.raises(PlanInvariantError, match="crosses sibling Branch interiors"):
        PlanRevision.draft(
            revision.goal_id,
            contract,
            (fork, left, right_with_cross_dependency, merge),
            (*revision.edges, cross_edge),
            branches,
            created_at=NOW,
        )


def test_conditional_edge_requires_condition() -> None:
    source = _node("source")
    target = _node("target")
    with pytest.raises(PlanInvariantError, match="requires a condition"):
        Edge(new_id(), source.plan_node_id, target.plan_node_id, EdgeType.CONDITIONAL)
