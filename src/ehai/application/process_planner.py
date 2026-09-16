"""Compile a retained process draft without publishing it.

This module turns the existing keyed :class:`PlanTemplate` graph language into
an immutable ``ProcessRevision`` successor.  It is deliberately a compiler,
not an approval or persistence entry point: it does not inspect Artifact
freshness, prove requirements or interface semantics, or authorize a release.

The previous process graph is the definition baseline.  ``current`` may differ
from it only in live node and Branch status.  Reused task IDs therefore retain
their current status, while a definition or declared input-scope change gets a
new task identity.  The fixed-point pass is important because changing one
node's identity can change the declared input scope of its dependants.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from ehai import ID, new_id, normalize_id, utc_now
from ehai.application.planner import (
    BranchTemplate,
    EdgeTemplate,
    PhaseTemplate,
    PlanNodeTemplate,
    PlanTemplate,
)
from ehai.application.process_blocks import build_block_changes
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    PlanNode,
    PlanNodeStatus,
    PlanPhase,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.process import (
    ProcessRevision,
    ProcessRevisionSource,
    branch_selection_signature,
    node_definition,
    node_input_scope,
    process_approval_identity,
    process_graph_definition,
)


class ProcessPlannerError(ValueError):
    """Raised when a retained process draft cannot be compiled safely."""


def build_process_proposal(
    previous: ProcessRevision,
    current: PlanRevision,
    template: PlanTemplate,
    reason: str,
    *,
    id_factory: Callable[[], ID] = new_id,
    clock: Callable[[], datetime] = utc_now,
) -> ProcessRevision:
    """Compile a retained process draft as a v+1 ``ProcessRevision``.

    ``previous`` supplies the immutable approved/contract identity and frozen
    Gate Check groups.  ``current`` supplies only live execution statuses; its
    node, edge, Branch, Phase, and selection definitions must still match
    ``previous.graph``.  ``template`` must come from process mode and therefore
    carry retained Check/Gate bindings instead of local Gate definitions.
    """
    _validate_inputs(previous, current, template, reason)
    _validate_current_snapshot(previous, current)
    _validate_retained_bindings(previous, template)

    previous_graph = previous.graph
    current_nodes = {node.plan_node_id: node for node in current.nodes}
    previous_nodes = {node.plan_node_id: node for node in previous_graph.nodes}
    previous_branches = {branch.branch_id: branch for branch in previous_graph.branches}
    previous_phases = {phase.phase_id: phase for phase in previous_graph.phases}

    reserved_ids = _graph_ids(previous_graph) | {
        previous.process_revision_id,
        previous.run_id,
        *previous.gate_owners,
    }
    if previous.parent_process_revision_id is not None:
        reserved_ids.add(previous.parent_process_revision_id)
    if previous_graph.supersedes_plan_revision_id is not None:
        reserved_ids.add(previous_graph.supersedes_plan_revision_id)
    allocated_ids: set[ID] = set()

    def allocate_id(kind: str) -> ID:
        return _allocate_id(id_factory, kind, reserved_ids, allocated_ids)

    node_ids = _seed_node_ids(template.nodes, previous_nodes, allocate_id)
    seeded_node_ids = dict(node_ids)
    changed_nodes: set[str] = {
        key for key, node_id in node_ids.items() if node_id not in previous_nodes
    }
    branch_ids = _seed_branch_ids(template.branches, previous_branches, allocate_id)
    phase_ids = _seed_phase_ids(template.phases, previous_phases, allocate_id)
    edge_ids: dict[int, ID] = {}

    max_passes = len(template.nodes) + 1
    candidate: PlanRevision | None = None
    for _ in range(max_passes):
        candidate = _compile_candidate_parts(
            previous=previous,
            current_nodes=current_nodes,
            template=template,
            node_ids=node_ids,
            changed_nodes=changed_nodes,
            branch_ids=branch_ids,
            phase_ids=phase_ids,
            edge_ids=edge_ids,
            previous_nodes=previous_nodes,
            allocate_id=allocate_id,
        )
        changed = False
        for node_template in template.nodes:
            key = node_template.key
            node_id = node_ids[key]
            old_node = previous_nodes.get(node_id)
            if old_node is None or key in changed_nodes:
                continue
            candidate_node = _node_by_id(candidate, node_id)
            if _node_definition_without_id(candidate_node) != _node_definition_without_id(old_node):
                node_ids[key] = allocate_id("PlanNode")
                changed_nodes.add(key)
                changed = True
                continue
            if node_input_scope(candidate, node_id) != node_input_scope(previous_graph, node_id):
                node_ids[key] = allocate_id("PlanNode")
                changed_nodes.add(key)
                changed = True
        if _invalidate_pruned_branch_nodes(
            previous=previous_graph,
            current=current,
            candidate=candidate,
            node_ids=node_ids,
            seeded_node_ids=seeded_node_ids,
            changed_nodes=changed_nodes,
            allocate_id=allocate_id,
        ):
            changed = True
        if not changed:
            break
    else:
        raise ProcessPlannerError("process node identity allocation did not reach a fixed point")

    if candidate is None:  # pragma: no cover - template validation guarantees a pass
        raise ProcessPlannerError("process graph compilation produced no candidate")

    _validate_design_document(previous.graph, candidate, template)
    candidate_plan = _apply_branch_statuses(
        previous=previous.graph,
        current=current,
        candidate=candidate,
    )
    _validate_final_gate_owner(previous, candidate_plan, template, node_ids)

    gate_owners = {
        approved_gate_id: node_ids[owner_key]
        for approved_gate_id, owner_key in template.retained_gate_owners.items()
    }
    process_id = allocate_id("ProcessRevision")
    return ProcessRevision(
        process_revision_id=process_id,
        run_id=previous.run_id,
        version=previous.version + 1,
        graph=candidate_plan,
        created_at=clock(),
        reason=reason,
        source=ProcessRevisionSource.PLANNER_ADJUSTMENT,
        parent_process_revision_id=previous.process_revision_id,
        gate_owners=gate_owners,
        block_changes=build_block_changes(
            previous,
            candidate_plan,
            {
                node_ids[node.key]: old_id
                for node in template.nodes
                if (old_id := _existing_id(node.key, previous_nodes)) is not None
            },
        ),
    )


def _validate_inputs(
    previous: ProcessRevision,
    current: PlanRevision,
    template: PlanTemplate,
    reason: str,
) -> None:
    if not isinstance(previous, ProcessRevision):
        raise TypeError("previous must be a ProcessRevision")
    if not isinstance(current, PlanRevision):
        raise TypeError("current must be a PlanRevision")
    if not isinstance(template, PlanTemplate):
        raise TypeError("template must be a PlanTemplate")
    if not isinstance(reason, str) or not reason.strip():
        raise ProcessPlannerError("process proposal reason must be non-blank text")
    if template.node_gates or template.final_gate_argv or template.final_human_question:
        raise ProcessPlannerError(
            "process proposal templates cannot define new local or final Gate behavior"
        )
    if any(node.require_completion_checks for node in template.nodes):
        raise ProcessPlannerError(
            "process proposal templates cannot request newly generated completion Checks"
        )


def _validate_current_snapshot(previous: ProcessRevision, current: PlanRevision) -> None:
    if process_approval_identity(current) != process_approval_identity(previous.graph):
        raise ProcessPlannerError(
            "current graph no longer matches the ProcessRevision approval identity"
        )
    if process_graph_definition(current) != process_graph_definition(previous.graph):
        raise ProcessPlannerError(
            "current graph definition changed; only live node/Branch status may differ"
        )


def _validate_retained_bindings(previous: ProcessRevision, template: PlanTemplate) -> None:
    previous_gate_ids = set(previous.gate_owners)
    template_gate_ids = set(template.retained_gate_owners)
    if template_gate_ids != previous_gate_ids:
        missing = sorted(str(item) for item in previous_gate_ids - template_gate_ids)
        extra = sorted(str(item) for item in template_gate_ids - previous_gate_ids)
        raise ProcessPlannerError(
            "retained Gate bindings must preserve every approved Gate; "
            f"missing={missing}, extra={extra}"
        )
    previous_nodes = {node.plan_node_id: node for node in previous.graph.nodes}
    for approved_gate_id, owner_id in previous.gate_owners.items():
        source = previous_nodes.get(owner_id)
        if source is None or not source.required_check_ids:
            raise ProcessPlannerError(
                f"Process Gate {approved_gate_id} has no frozen Check group in its parent graph"
            )
        owner_key = template.retained_gate_owners[approved_gate_id]
        retained = template.retained_check_ids.get(owner_key)
        if retained != source.required_check_ids:
            raise ProcessPlannerError(
                f"Process Gate {approved_gate_id} changed its frozen Check grouping"
            )
    retained_owner_keys = set(template.retained_check_ids)
    if retained_owner_keys != set(template.retained_gate_owners.values()):
        raise ProcessPlannerError("retained Check and Gate bindings must cover the same owners")


def _seed_node_ids(
    templates: tuple[PlanNodeTemplate, ...],
    previous_nodes: Mapping[ID, PlanNode],
    allocate_id: Callable[[str], ID],
) -> dict[str, ID]:
    result: dict[str, ID] = {}
    for node in templates:
        existing = _existing_id(node.key, previous_nodes)
        result[node.key] = existing if existing is not None else allocate_id("PlanNode")
    return result


def _seed_branch_ids(
    templates: tuple[BranchTemplate, ...],
    previous_branches: Mapping[ID, Branch],
    allocate_id: Callable[[str], ID],
) -> dict[str, ID]:
    """Reuse a keyed Branch's logical container across process revisions."""
    result: dict[str, ID] = {}
    for branch in templates:
        existing = _existing_id(branch.key, previous_branches)
        result[branch.key] = existing if existing is not None else allocate_id("Branch")
    return result


def _seed_phase_ids(
    templates: tuple[PhaseTemplate, ...],
    previous_phases: Mapping[ID, PlanPhase],
    allocate_id: Callable[[str], ID],
) -> dict[str, ID]:
    result: dict[str, ID] = {}
    for phase in templates:
        existing = _existing_id(phase.key, previous_phases)
        result[phase.key] = existing if existing is not None else allocate_id("PlanPhase")
    return result


def _compile_candidate_parts(
    *,
    previous: ProcessRevision,
    current_nodes: Mapping[ID, PlanNode],
    template: PlanTemplate,
    node_ids: Mapping[str, ID],
    changed_nodes: set[str],
    branch_ids: dict[str, ID],
    phase_ids: Mapping[str, ID],
    edge_ids: dict[int, ID],
    previous_nodes: Mapping[ID, PlanNode],
    allocate_id: Callable[[str], ID],
) -> PlanRevision:
    nodes = tuple(
        _compile_node(
            node,
            node_ids,
            template.retained_check_ids,
            current_nodes,
            previous_nodes,
            changed_nodes,
        )
        for node in template.nodes
    )
    branch_templates = tuple(template.branches)
    branches = _compile_branches(
        branch_templates,
        node_ids,
        branch_ids,
    )
    phases = tuple(
        PlanPhase(
            phase_id=phase_ids[phase.key],
            title=phase.title,
            node_ids=tuple(node_ids[node_key] for node_key in phase.node_keys),
            reviewer_node_id=node_ids[phase.reviewer_node_key],
            gate_node_id=node_ids[phase.gate_node_key],
            rework_node_ids=tuple(node_ids[node_key] for node_key in phase.rework_node_keys),
        )
        for phase in template.phases
    )
    edges = _compile_edges(
        template.edges,
        node_ids,
        branch_ids,
        edge_ids,
        previous.graph.edges,
        allocate_id,
    )
    design_document = (
        previous.graph.design_document
        if template.design_document is None
        else template.design_document
    )
    plan = PlanRevision.rehydrate(
        plan_revision_id=previous.graph.plan_revision_id,
        goal_id=previous.graph.goal_id,
        version=previous.graph.version,
        completion_contract_id=previous.graph.completion_contract_id,
        completion_contract_version=previous.graph.completion_contract_version,
        nodes=nodes,
        edges=edges,
        branches=branches,
        phases=phases,
        created_at=previous.graph.created_at,
        status=PlanRevisionStatus.APPROVED,
        approved_at=previous.graph.approved_at,
        supersedes_plan_revision_id=previous.graph.supersedes_plan_revision_id,
        design_document=design_document,
    )
    return plan


def _compile_node(
    template: PlanNodeTemplate,
    node_ids: Mapping[str, ID],
    retained_check_ids: Mapping[str, tuple[ID, ...]],
    current_nodes: Mapping[ID, PlanNode],
    previous_nodes: Mapping[ID, PlanNode],
    changed_nodes: set[str],
) -> PlanNode:
    node_id = node_ids[template.key]
    current = current_nodes.get(node_id)
    status = (
        current.status
        if current is not None and template.key not in changed_nodes
        else PlanNodeStatus.PENDING
    )
    checks = tuple(retained_check_ids.get(template.key, ()))
    dependency_ids = tuple(node_ids[dependency] for dependency in template.required_dependency_keys)
    previous_node = previous_nodes.get(node_id)
    if previous_node is not None:
        dependency_set = set(dependency_ids)
        dependency_ids = tuple(
            dependency_id
            for dependency_id in previous_node.required_dependency_ids
            if dependency_id in dependency_set
        ) + tuple(
            dependency_id
            for dependency_id in dependency_ids
            if dependency_id not in previous_node.required_dependency_ids
        )
    return PlanNode.rehydrate(
        plan_node_id=node_id,
        title=template.title,
        instruction=template.instruction,
        kind=template.kind,
        required_dependency_ids=dependency_ids,
        required_check_ids=checks,
        required_capabilities=template.required_capabilities,
        session_policy=template.session_policy,
        status=status,
    )


def _compile_branches(
    templates: tuple[BranchTemplate, ...],
    node_ids: Mapping[str, ID],
    branch_ids: Mapping[str, ID],
) -> tuple[Branch, ...]:
    compiled: list[Branch] = []
    for template in templates:
        branch_id = branch_ids[template.key]
        compiled.append(
            Branch.rehydrate(
                branch_id=branch_id,
                label=template.label,
                fork_node_id=node_ids[template.fork_node_key],
                node_ids=tuple(node_ids[node_key] for node_key in template.node_keys),
                merge_node_id=node_ids[template.merge_node_key],
                status=BranchStatus.ACTIVE,
            )
        )
    return tuple(compiled)


def _compile_edges(
    templates: tuple[EdgeTemplate, ...],
    node_ids: Mapping[str, ID],
    branch_ids: Mapping[str, ID],
    edge_ids: dict[int, ID],
    previous_edges: tuple[Edge, ...],
    allocate_id: Callable[[str], ID],
) -> tuple[Edge, ...]:
    previous_by_identity = {_edge_identity(edge): edge.edge_id for edge in previous_edges}
    used: set[ID] = set()
    result: list[Edge] = []
    for index, template in enumerate(templates):
        branch_id = None if template.branch_key is None else branch_ids[template.branch_key]
        identity = (
            node_ids[template.source_node_key],
            node_ids[template.target_node_key],
            template.edge_type,
            branch_id,
            template.condition,
        )
        edge_id = previous_by_identity.get(identity)
        if edge_id is None or edge_id in used:
            edge_id = edge_ids.get(index)
            if edge_id is None:
                edge_id = allocate_id("Edge")
                edge_ids[index] = edge_id
        used.add(edge_id)
        result.append(
            Edge(
                edge_id=edge_id,
                source_node_id=identity[0],
                target_node_id=identity[1],
                edge_type=identity[2],
                branch_id=identity[3],
                condition=identity[4],
            )
        )
    return tuple(result)


def _invalidate_pruned_branch_nodes(
    *,
    previous: PlanRevision,
    current: PlanRevision,
    candidate: PlanRevision,
    node_ids: dict[str, ID],
    seeded_node_ids: Mapping[str, ID],
    changed_nodes: set[str],
    allocate_id: Callable[[str], ID],
) -> bool:
    """Give invalidated, previously pruned candidates fresh pending identities."""
    previous_branches = {branch.branch_id: branch for branch in previous.branches}
    current_nodes = {node.plan_node_id: node for node in current.nodes}
    candidate_branches = {branch.branch_id: branch for branch in candidate.branches}
    changed_branch_ids: set[ID] = set()
    affected_groups: set[tuple[ID, ID]] = set()

    for branch in candidate.branches:
        previous_branch = previous_branches.get(branch.branch_id)
        if previous_branch is None:
            continue
        if branch_selection_signature(previous, branch.branch_id) != branch_selection_signature(
            candidate, branch.branch_id
        ):
            changed_branch_ids.add(branch.branch_id)
            affected_groups.add((previous_branch.fork_node_id, previous_branch.merge_node_id))
            affected_groups.add((branch.fork_node_id, branch.merge_node_id))

    if not changed_branch_ids:
        return False

    affected_node_ids: set[ID] = set()
    for branch in previous.branches:
        if (branch.fork_node_id, branch.merge_node_id) in affected_groups:
            affected_node_ids.update(branch.node_ids)
    for branch in candidate_branches.values():
        if (branch.fork_node_id, branch.merge_node_id) in affected_groups:
            # Candidate nodes with an existing seeded identity can also be
            # current PRUNED nodes after a move or a changed fork/join.
            affected_node_ids.update(
                seeded_node_ids[key]
                for key in node_ids
                if key in seeded_node_ids
                and node_ids[key] == seeded_node_ids[key]
                and seeded_node_ids[key] in branch.node_ids
            )

    key_by_seeded_id = {node_id: key for key, node_id in seeded_node_ids.items()}
    changed = False
    for old_node_id in affected_node_ids:
        current_node = current_nodes.get(old_node_id)
        key = key_by_seeded_id.get(old_node_id)
        if (
            current_node is None
            or current_node.status is not PlanNodeStatus.PRUNED
            or key is None
            or key not in node_ids
            or node_ids[key] != old_node_id
            or key in changed_nodes
        ):
            continue
        node_ids[key] = allocate_id("PlanNode")
        changed_nodes.add(key)
        changed = True
    return changed


def _apply_branch_statuses(
    *,
    previous: PlanRevision,
    current: PlanRevision,
    candidate: PlanRevision,
) -> PlanRevision:
    """Restore live Branch states only when their selection inputs are unchanged."""
    previous_branches = {branch.branch_id: branch for branch in previous.branches}
    current_branches = {branch.branch_id: branch for branch in current.branches}
    branches: list[Branch] = []
    for branch in candidate.branches:
        previous_branch = previous_branches.get(branch.branch_id)
        current_branch = current_branches.get(branch.branch_id)
        status = BranchStatus.ACTIVE
        if previous_branch is not None and current_branch is not None:
            unchanged = branch_selection_signature(
                previous, branch.branch_id
            ) == branch_selection_signature(candidate, branch.branch_id)
            if unchanged:
                status = current_branch.status
        branches.append(
            Branch.rehydrate(
                branch_id=branch.branch_id,
                label=branch.label,
                fork_node_id=branch.fork_node_id,
                node_ids=branch.node_ids,
                merge_node_id=branch.merge_node_id,
                status=status,
            )
        )
    return _rehydrate_plan(candidate, branches=tuple(branches))


def _rehydrate_plan(plan: PlanRevision, *, branches: tuple[Branch, ...]) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=plan.nodes,
        edges=plan.edges,
        branches=branches,
        phases=plan.phases,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
        design_document=plan.design_document,
    )


def _validate_design_document(
    previous: PlanRevision,
    candidate: PlanRevision,
    template: PlanTemplate,
) -> None:
    """Require a refreshed readable design whenever the graph definition changes."""
    if process_graph_definition(previous) == process_graph_definition(candidate):
        return
    if not isinstance(template.design_document, str) or not template.design_document.strip():
        raise ProcessPlannerError(
            "process graph changes require a non-blank complete design_document"
        )


def _validate_final_gate_owner(
    previous: ProcessRevision,
    candidate: PlanRevision,
    template: PlanTemplate,
    node_ids: Mapping[str, ID],
) -> None:
    previous_terminals = _terminal_ids(previous.graph)
    candidate_terminals = _terminal_ids(candidate)
    if len(previous_terminals) != 1 or len(candidate_terminals) != 1:
        raise ProcessPlannerError(
            "process proposal requires a unique current and proposed terminal node"
        )
    previous_final = tuple(
        gate_id
        for gate_id, owner_id in previous.gate_owners.items()
        if owner_id == previous_terminals[0]
    )
    if len(previous_final) > 1:
        raise ProcessPlannerError("the original final Gate has multiple owners")
    if not previous_final:
        if template.retained_gate_owners:
            raise ProcessPlannerError("the original Gate bindings have no final Gate owner")
        return
    candidate_owner = template.retained_gate_owners.get(previous_final[0])
    if candidate_owner is None or candidate_owner not in node_ids:
        raise ProcessPlannerError("the original final Gate owner is missing from the proposal")
    target_id = node_ids[candidate_owner]
    if target_id not in {node.plan_node_id for node in candidate.nodes}:
        raise ProcessPlannerError("the original final Gate owner is missing from the proposal")
    if target_id != candidate_terminals[0]:
        raise ProcessPlannerError(
            "the original final Gate must remain bound to the unique terminal node"
        )


def _node_by_id(plan: PlanRevision, node_id: ID) -> PlanNode:
    for node in plan.nodes:
        if node.plan_node_id == node_id:
            return node
    raise ProcessPlannerError(f"compiled graph is missing PlanNode {node_id}")


def _node_definition_without_id(node: PlanNode) -> tuple[object, ...]:
    return node_definition(node)[1:]


def _edge_identity(edge: Edge) -> tuple[object, ...]:
    return (
        edge.source_node_id,
        edge.target_node_id,
        edge.edge_type,
        edge.branch_id,
        edge.condition,
    )


def _terminal_ids(plan: PlanRevision) -> tuple[ID, ...]:
    sources = {edge.source_node_id for edge in plan.edges}
    return tuple(node.plan_node_id for node in plan.nodes if node.plan_node_id not in sources)


def _existing_id(key: str, values: Mapping[ID, object]) -> ID | None:
    try:
        candidate = normalize_id(key)
    except (TypeError, ValueError):
        return None
    return candidate if candidate in values else None


def _graph_ids(plan: PlanRevision) -> set[ID]:
    ids = {
        *[node.plan_node_id for node in plan.nodes],
        *[edge.edge_id for edge in plan.edges],
        *[branch.branch_id for branch in plan.branches],
        *[phase.phase_id for phase in plan.phases],
        plan.plan_revision_id,
        plan.goal_id,
        plan.completion_contract_id,
    }
    ids.update(check_id for node in plan.nodes for check_id in node.required_check_ids)
    return ids


def _allocate_id(
    id_factory: Callable[[], ID],
    kind: str,
    reserved_ids: set[ID],
    allocated_ids: set[ID],
) -> ID:
    try:
        candidate = normalize_id(id_factory())
    except (TypeError, ValueError) as error:
        raise ProcessPlannerError(f"{kind} id_factory must return a valid ID") from error
    if candidate in reserved_ids or candidate in allocated_ids:
        raise ProcessPlannerError(f"id_factory returned a reused {kind} ID {candidate}")
    allocated_ids.add(candidate)
    return candidate
