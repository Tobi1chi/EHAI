"""Pure ready-node and branch-state computation over an approved plan revision."""

from __future__ import annotations

from ehai import ID
from ehai.application.orchestration.common import OrchestrationError
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)


def ready_nodes(plan_revision: PlanRevision) -> tuple[PlanNode, ...]:
    """Return pending nodes whose dependencies completed, preserving graph order."""
    if plan_revision.status is not PlanRevisionStatus.APPROVED:
        raise OrchestrationError(
            f"PlanRevision {plan_revision.plan_revision_id} must be approved before scheduling"
        )
    node_by_id = {node.plan_node_id: node for node in plan_revision.nodes}
    ready: list[PlanNode] = []
    for node in plan_revision.nodes:
        if node.status is not PlanNodeStatus.PENDING:
            continue
        if not _exploration_dependencies_completed(plan_revision, node):
            continue
        containing_branch = _branch_containing(plan_revision, node.plan_node_id)
        if containing_branch is not None and containing_branch.status is BranchStatus.PRUNED:
            continue
        incoming_branches = tuple(
            branch for branch in plan_revision.branches if branch.merge_node_id == node.plan_node_id
        )
        branch_endpoint_ids = {branch.node_ids[-1] for branch in plan_revision.branches}
        if node.kind is PlanNodeKind.EVALUATOR:
            if incoming_branches and not all(
                _branch_is_terminal(branch, node_by_id) for branch in incoming_branches
            ):
                continue
            dependencies_ready = all(
                node_by_id[dependency_id].status is PlanNodeStatus.COMPLETED
                or (
                    dependency_id in branch_endpoint_ids
                    and node_by_id[dependency_id].status is PlanNodeStatus.FAILED
                )
                or (
                    (branch := _branch_containing(plan_revision, dependency_id)) is not None
                    and _branch_has_failed(branch, node_by_id)
                )
                for dependency_id in node.required_dependency_ids
            )
        else:
            dependencies_ready = all(
                node_by_id[dependency_id].status is PlanNodeStatus.COMPLETED
                for dependency_id in node.required_dependency_ids
            )
        if not dependencies_ready:
            continue
        ready.append(node)
    return tuple(ready)


def _exploration_dependencies_completed(plan: PlanRevision, node: PlanNode) -> bool:
    node_by_id = {item.plan_node_id: item for item in plan.nodes}
    return all(
        node_by_id[edge.source_node_id].status is PlanNodeStatus.COMPLETED
        for edge in plan.edges
        if edge.target_node_id == node.plan_node_id and edge.edge_type is EdgeType.EXPLORATION
    )


def _branch_containing(plan: PlanRevision, plan_node_id: ID) -> Branch | None:
    return next(
        (branch for branch in plan.branches if plan_node_id in branch.node_ids),
        None,
    )


def _branch_is_terminal(branch: Branch, node_by_id: dict[ID, PlanNode]) -> bool:
    if any(
        node_by_id[node_id].status in {PlanNodeStatus.STALLED, PlanNodeStatus.SUSPENDED}
        for node_id in branch.node_ids
    ):
        return False
    return _branch_has_failed(branch, node_by_id) or node_by_id[branch.node_ids[-1]].status in {
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.FAILED,
    }


def _branch_has_failed(branch: Branch, node_by_id: dict[ID, PlanNode]) -> bool:
    if any(
        node_by_id[node_id].status in {PlanNodeStatus.STALLED, PlanNodeStatus.SUSPENDED}
        for node_id in branch.node_ids
    ):
        return False
    return any(node_by_id[node_id].status is PlanNodeStatus.FAILED for node_id in branch.node_ids)


def _exhausted_branch_reason(plan: PlanRevision) -> str | None:
    active = tuple(branch for branch in plan.branches if branch.status is BranchStatus.ACTIVE)
    if not active:
        return None
    node_by_id = {node.plan_node_id: node for node in plan.nodes}
    if not all(_branch_has_failed(branch, node_by_id) for branch in active):
        return None
    return "no viable Branch candidate; all active exploration branches failed"
