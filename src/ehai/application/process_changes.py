"""Gate-structure checks used when retaining a process revision.

These checks do not prove natural-language requirements, interface semantics,
obligation coverage, or artifact provenance. They are not an authorization to
publish a Planner proposal on their own.
"""

from __future__ import annotations

from collections.abc import Mapping

from ehai.application.process_gates import (
    analyze_gate_preconditions,
    family_implies,
)
from ehai.application.process_obligations import (
    ProcessObligationMapping,
    validate_process_obligations,
)
from ehai.domain.planning import PlanRevision
from ehai.domain.process import ProcessRevision


class ProcessBoundaryError(ValueError):
    """The proposal changes or cannot retain an approved Gate boundary."""


def validate_process_gate_preservation(
    approved: PlanRevision,
    previous: ProcessRevision,
    proposed: ProcessRevision,
    *,
    obligation_mapping: ProcessObligationMapping,
    source_documents: Mapping[str, str],
) -> None:
    """Check unchanged Gate identities/scopes and their successful prerequisites."""
    approved_nodes = {node.plan_node_id: node for node in approved.nodes}
    original_owners = {key for key, node in approved_nodes.items() if node.required_check_ids}
    if set(proposed.gate_owners) != original_owners or set(previous.gate_owners) != original_owners:
        raise ProcessBoundaryError(
            "Process must map every original Gate without adding or deleting one"
        )
    proposed_nodes = {node.plan_node_id: node for node in proposed.graph.nodes}
    for original, target in proposed.gate_owners.items():
        if proposed_nodes[target].required_check_ids != approved_nodes[original].required_check_ids:
            raise ProcessBoundaryError("An approved Gate's Check grouping or conditions changed")

    original_phases = {phase.phase_id: phase for phase in approved.phases}
    next_phases = {phase.phase_id: phase for phase in proposed.graph.phases}
    if tuple(original_phases) != tuple(next_phases):
        raise ProcessBoundaryError("Approved phase Gate boundaries or their order changed")
    for phase_id, phase in original_phases.items():
        target_phase = next_phases[phase_id]
        if (
            target_phase.gate_node_id != proposed.gate_owners[phase.gate_node_id]
            or target_phase.reviewer_node_id != target_phase.gate_node_id
        ):
            raise ProcessBoundaryError("A phase no longer uses its original Reviewer Gate")
    original_phase_by_node = {
        node_id: phase.phase_id for phase in approved.phases for node_id in phase.node_ids
    }
    next_phase_by_node = {
        node_id: phase.phase_id for phase in proposed.graph.phases for node_id in phase.node_ids
    }
    for original, target in proposed.gate_owners.items():
        if original_phase_by_node.get(original) != next_phase_by_node.get(target):
            raise ProcessBoundaryError("An approved Gate moved to another phase scope")

    validate_process_obligations(approved, proposed, obligation_mapping, source_documents)

    baseline = analyze_gate_preconditions(approved, {owner: owner for owner in original_owners})
    candidate = analyze_gate_preconditions(
        proposed.graph, {current: original for original, current in proposed.gate_owners.items()}
    )
    if not baseline.final_success or not candidate.final_success:
        raise ProcessBoundaryError("Process has no proven successful completion route")
    if not (
        family_implies(candidate.final_success, baseline.final_success)
        and family_implies(baseline.final_success, candidate.final_success)
    ):
        raise ProcessBoundaryError("Final completion changed the required Gate alternatives")
    for original, target in proposed.gate_owners.items():
        if not candidate.ready[target] or not baseline.ready[original]:
            raise ProcessBoundaryError("An approved Gate has no proven reachable execution route")
        if not family_implies(candidate.ready[target], baseline.ready[original]):
            raise ProcessBoundaryError("An approved Gate can run before an original prerequisite")
