"""Describe compiler-authored block changes without making approval decisions."""

from __future__ import annotations

from collections.abc import Mapping

from ehai import ID
from ehai.domain.blocks import BlockChange, BlockChangeKind
from ehai.domain.planning import PlanRevision
from ehai.domain.process import ProcessRevision, node_input_scope

_DEFINITION_FIELDS = (
    "title",
    "instruction",
    "kind",
    "required_dependency_ids",
    "required_check_ids",
    "required_capabilities",
    "session_policy",
)


def initial_block_changes(graph: PlanRevision) -> tuple[BlockChange, ...]:
    """Anchor new or previously untracked nodes; never guess historical lineage."""
    return tuple(
        BlockChange(node.plan_node_id, 1, None, node.plan_node_id, BlockChangeKind.ADDED)
        for node in graph.nodes
    )


def build_block_changes(
    previous: ProcessRevision,
    graph: PlanRevision,
    predecessors: Mapping[ID, ID],
) -> tuple[BlockChange, ...]:
    """Use retained draft keys, not title similarity, to map old nodes to new ones.

    The compiler has already invalidated downstream execution identities. This
    report describes that decision; it cannot relax it or authorize result reuse.
    An old revision without lineage is an explicit new tracking anchor.
    """
    old_nodes = {node.plan_node_id: node for node in previous.graph.nodes}
    new_ids = {node.plan_node_id for node in graph.nodes}
    if not set(predecessors).issubset(new_ids) or not set(predecessors.values()).issubset(
        old_nodes
    ):
        raise ValueError("Block predecessor mapping references nodes outside its revisions")
    if len(set(predecessors.values())) != len(predecessors):
        raise ValueError("One previous block cannot become multiple successor blocks")
    for node_id in new_ids.intersection(old_nodes):
        if predecessors.get(node_id) != node_id:
            raise ValueError("Retained execution identities must retain their block lineage")
    anchors = previous.block_changes
    if anchors is None:
        anchors = initial_block_changes(previous.graph)
    old_blocks = {item.node_id: item for item in anchors if item.node_id is not None}
    changes: list[BlockChange] = []
    for node in graph.nodes:
        node_id = node.plan_node_id
        old_id = predecessors.get(node_id)
        if old_id is None:
            changes.append(BlockChange(node_id, 1, None, node_id, BlockChangeKind.ADDED))
            continue
        old_node, block = old_nodes[old_id], old_blocks[old_id]
        fields = tuple(
            name for name in _DEFINITION_FIELDS if getattr(old_node, name) != getattr(node, name)
        )
        if node_input_scope(previous.graph, old_id) != node_input_scope(graph, node_id):
            fields += ("input_scope",)
        if old_id == node_id:
            if fields:
                raise ValueError("Changed block definition or inputs retained execution identity")
            kind = BlockChangeKind.UNCHANGED
        else:
            kind = BlockChangeKind.MODIFIED
            # E.g. a pruned branch becomes eligible again without changing its
            # literal definition. The compiler still requires a fresh execution.
            if not fields:
                fields = ("execution_identity",)
        changes.append(
            BlockChange(
                block.block_id,
                block.version + (kind is BlockChangeKind.MODIFIED),
                old_id,
                node_id,
                kind,
                fields,
            )
        )
    retained = set(predecessors.values())
    for node in previous.graph.nodes:
        if node.plan_node_id not in retained:
            block = old_blocks[node.plan_node_id]
            changes.append(
                BlockChange(
                    block.block_id, block.version, node.plan_node_id, None, BlockChangeKind.REMOVED
                )
            )
    return tuple(changes)


def validate_block_changes(previous: ProcessRevision, proposed: ProcessRevision) -> None:
    """Check persisted lineage against the actual predecessor and candidate graphs."""
    changes = proposed.block_changes
    if changes is None:
        # Pre-feature drafts remain readable/applicable under existing approval
        # checks. No inferred manifest is substituted for missing historical data.
        return
    predecessors = {
        item.node_id: item.previous_node_id
        for item in changes
        if item.node_id is not None and item.previous_node_id is not None
    }
    if changes != build_block_changes(previous, proposed.graph, predecessors):
        raise ValueError("Block change manifest does not match its process predecessor")
