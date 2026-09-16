"""Immutable process graph versions under a Run's original approval."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from ehai import ID, normalize_id
from ehai.domain.blocks import BlockChange, BlockChangeKind
from ehai.domain.planning import (
    Branch,
    PlanNode,
    PlanNodeKind,
    PlanRevision,
    PlanRevisionStatus,
)


class ProcessRevisionSource(StrEnum):
    """Distinguish a new Run baseline from a snapshot of pre-versioned execution."""

    RUN_STARTED = "run_started"
    LEGACY_SNAPSHOT = "legacy_snapshot"
    PLANNER_ADJUSTMENT = "planner_adjustment"


@dataclass(frozen=True, slots=True)
class ProcessRevision:
    """A frozen graph; its node states are observations at version creation only.

    The current execution state is stored separately. Approval identity in graph
    names the original approved PlanRevision, not a new user approval.
    """

    process_revision_id: ID
    run_id: ID
    version: int
    graph: PlanRevision
    created_at: datetime
    reason: str
    source: ProcessRevisionSource
    parent_process_revision_id: ID | None = None
    gate_owners: Mapping[ID, ID] = field(default_factory=dict)
    block_changes: tuple[BlockChange, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "process_revision_id", normalize_id(self.process_revision_id))
        object.__setattr__(self, "run_id", normalize_id(self.run_id))
        object.__setattr__(self, "source", ProcessRevisionSource(self.source))
        if type(self.version) is not int or self.version < 1:
            raise ValueError("ProcessRevision version must be a positive integer")
        if self.parent_process_revision_id is not None:
            object.__setattr__(
                self, "parent_process_revision_id", normalize_id(self.parent_process_revision_id)
            )
        if (self.version == 1) != (self.parent_process_revision_id is None):
            raise ValueError("Only the first ProcessRevision can omit its parent")
        if self.parent_process_revision_id == self.process_revision_id:
            raise ValueError("ProcessRevision cannot be its own parent")
        if (
            not isinstance(self.graph, PlanRevision)
            or self.graph.status is not PlanRevisionStatus.APPROVED
        ):
            raise ValueError("ProcessRevision graph must retain an approved PlanRevision identity")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("ProcessRevision created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("ProcessRevision reason must be non-blank text")
        if len(self.reason.encode("utf-8")) > 16_000:
            raise ValueError("ProcessRevision reason exceeds 16000 UTF-8 bytes")
        object.__setattr__(self, "reason", self.reason.strip())
        owners = {node.plan_node_id for node in self.graph.nodes if node.required_check_ids}
        bindings = {
            normalize_id(approved): normalize_id(current)
            for approved, current in self.gate_owners.items()
        }
        if self.source is not ProcessRevisionSource.PLANNER_ADJUSTMENT:
            if self.version != 1:
                raise ValueError("A process baseline must be the first version")
            identity = {node_id: node_id for node_id in owners}
            if bindings and bindings != identity:
                raise ValueError("A process baseline must retain original Gate owners")
            bindings = identity
        elif self.version == 1:
            raise ValueError("A Planner adjustment must follow a process baseline")
        if set(bindings.values()) != owners or len(bindings) != len(owners):
            raise ValueError(
                "Process Gate bindings must cover each current Gate owner exactly once"
            )
        object.__setattr__(self, "gate_owners", MappingProxyType(bindings))
        if self.block_changes is not None:
            changes = tuple(self.block_changes)
            if not all(isinstance(item, BlockChange) for item in changes):
                raise TypeError("Process block_changes must contain BlockChange values")
            current_ids = [item.node_id for item in changes if item.node_id is not None]
            previous_ids = [
                item.previous_node_id for item in changes if item.previous_node_id is not None
            ]
            if (
                len({item.block_id for item in changes}) != len(changes)
                or len(set(current_ids)) != len(current_ids)
                or len(set(previous_ids)) != len(previous_ids)
                or set(current_ids) != {node.plan_node_id for node in self.graph.nodes}
            ):
                raise ValueError("Process block changes must cover its nodes exactly once")
            if self.version == 1 and any(
                item.change is not BlockChangeKind.ADDED for item in changes
            ):
                raise ValueError("A process baseline can only anchor added blocks")
            object.__setattr__(self, "block_changes", changes)


def process_approval_identity(plan: PlanRevision) -> tuple[object, ...]:
    """Original approval identity, excluding the separately versioned process explanation."""
    return (
        plan.plan_revision_id,
        plan.goal_id,
        plan.version,
        plan.completion_contract_id,
        plan.completion_contract_version,
        plan.created_at,
        plan.status,
        plan.approved_at,
        plan.supersedes_plan_revision_id,
    )


def node_definition(node: PlanNode) -> tuple[object, ...]:
    """Task definition, without the current execution state."""
    return (
        node.plan_node_id,
        node.title,
        node.instruction,
        node.kind,
        node.required_dependency_ids,
        node.required_check_ids,
        node.required_capabilities,
        node.session_policy,
    )


def branch_definition(branch: Branch) -> tuple[object, ...]:
    return (
        branch.branch_id,
        branch.label,
        branch.fork_node_id,
        branch.merge_node_id,
        branch.node_ids,
    )


def branch_selection_signature(plan: PlanRevision, branch_id: ID) -> tuple[object, ...]:
    """The ordered candidate inputs to a choice, independent of its downstream join ID."""
    branch = next(item for item in plan.branches if item.branch_id == branch_id)
    return _selection_signature(
        plan,
        tuple(
            item
            for item in plan.branches
            if (item.fork_node_id, item.merge_node_id)
            == (branch.fork_node_id, branch.merge_node_id)
        ),
    )


def node_input_scope(plan: PlanRevision, node_id: ID) -> tuple[object, ...]:
    """Declared graph inputs; semantic obligation/provenance review remains separate."""
    node = next(item for item in plan.nodes if item.plan_node_id == node_id)
    branch = next((item for item in plan.branches if node_id in item.node_ids), None)
    selection_inputs: tuple[object, ...] = ()
    if node.kind is PlanNodeKind.EVALUATOR:
        groups: dict[tuple[ID, ID], list[Branch]] = {}
        for item in plan.branches:
            groups.setdefault((item.fork_node_id, item.merge_node_id), []).append(item)
        selection_inputs = tuple(
            _candidate_signature(tuple(group))
            for group in groups.values()
            if {item.node_ids[-1] for item in group}.issubset(node.required_dependency_ids)
        )
    elif node.kind is PlanNodeKind.MERGE:
        selection_inputs = _selection_signature(
            plan, tuple(item for item in plan.branches if item.merge_node_id == node_id)
        )
    return (
        frozenset(
            (edge.source_node_id, edge.edge_type, edge.branch_id, edge.condition)
            for edge in plan.edges
            if edge.target_node_id == node_id
        ),
        None if branch is None else (branch.branch_id, branch.fork_node_id),
        next((phase.phase_id for phase in plan.phases if node_id in phase.node_ids), None),
        selection_inputs,
    )


def _candidate_signature(branches: tuple[Branch, ...]) -> tuple[object, ...]:
    return tuple(
        (branch.branch_id, branch.label, branch.fork_node_id, branch.node_ids)
        for branch in branches
    )


def _selection_signature(plan: PlanRevision, branches: tuple[Branch, ...]) -> tuple[object, ...]:
    terminals = {branch.node_ids[-1] for branch in branches}
    return (
        _candidate_signature(branches),
        tuple(
            node.plan_node_id
            for node in plan.nodes
            if node.kind is PlanNodeKind.EVALUATOR
            and terminals
            and terminals.issubset(node.required_dependency_ids)
        ),
    )


def process_graph_definition(plan: PlanRevision) -> tuple[object, ...]:
    """Versioned graph definition, excluding node/branch and approval lifecycle state."""
    return (
        plan.plan_revision_id,
        plan.goal_id,
        plan.version,
        plan.completion_contract_id,
        plan.completion_contract_version,
        plan.created_at,
        plan.supersedes_plan_revision_id,
        plan.design_document,
        tuple(node_definition(node) for node in plan.nodes),
        plan.edges,
        tuple(branch_definition(branch) for branch in plan.branches),
        plan.phases,
    )
