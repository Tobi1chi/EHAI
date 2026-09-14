"""Approved obligation mappings for retained process proposals.

The structures in this module are intentionally narrow: they establish that
each approved obligation has a cited source and a structurally reachable
implementation alternative for every retained Gate scope.  They do not prove
the meaning of a quote, Artifact provenance, interface semantics, or release
authorization.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ehai import ID, normalize_id
from ehai.application.process_gates import (
    NodePreconditions,
    ProcessProofUnavailable,
    analyze_node_preconditions,
)
from ehai.domain.planning import PlanNodeKind, PlanRevision, PlanRevisionStatus
from ehai.domain.process import ProcessRevision


class ProcessObligationError(ValueError):
    """Raised when an approved obligation mapping cannot be checked safely."""


@dataclass(frozen=True, slots=True)
class ApprovalSourceRef:
    """One exact quote from a named approved source document."""

    source_key: str
    quote: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, str) or not self.source_key.strip():
            raise ProcessObligationError("ApprovalSourceRef source_key must be non-blank text")
        if not isinstance(self.quote, str) or not self.quote.strip():
            raise ProcessObligationError("ApprovalSourceRef quote must be non-blank text")
        object.__setattr__(self, "source_key", self.source_key.strip())


@dataclass(frozen=True, slots=True)
class ApprovedObligation:
    """A reviewer's interpretation of an original obligation, with source citations.

    This record does not create or replace user approval. Its interpretation
    and completeness remain part of the independent semantic review.
    """

    key: str
    description: str
    source_refs: tuple[ApprovalSourceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ProcessObligationError("ApprovedObligation key must be non-blank text")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ProcessObligationError(
                f"ApprovedObligation {self.key!r} description must be non-blank text"
            )
        refs = tuple(self.source_refs)
        if not refs or not all(isinstance(ref, ApprovalSourceRef) for ref in refs):
            raise ProcessObligationError(
                f"ApprovedObligation {self.key!r} requires source references"
            )
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "source_refs", refs)


@dataclass(frozen=True, slots=True)
class ProcessObligationMapping:
    """Immutable mapping from approved obligations to proposed node alternatives."""

    obligations: tuple[ApprovedObligation, ...]
    implementations: Mapping[str, tuple[frozenset[ID], ...]]
    gate_scopes: Mapping[ID, tuple[str, ...]]

    def __post_init__(self) -> None:
        obligations = tuple(self.obligations)
        if not all(isinstance(item, ApprovedObligation) for item in obligations):
            raise ProcessObligationError("ProcessObligationMapping obligations are invalid")
        keys = tuple(item.key for item in obligations)
        if len(set(keys)) != len(keys):
            raise ProcessObligationError("ProcessObligationMapping obligation keys must be unique")

        if not isinstance(self.implementations, Mapping):
            raise ProcessObligationError(
                "ProcessObligationMapping implementations must be a mapping"
            )
        implementations: dict[str, tuple[frozenset[ID], ...]] = {}
        for raw_key, raw_alternatives in self.implementations.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ProcessObligationError(
                    "ProcessObligationMapping implementation keys must be non-blank text"
                )
            key = raw_key.strip()
            if key in implementations:
                raise ProcessObligationError(
                    f"ProcessObligationMapping contains duplicate implementation {key!r}"
                )
            try:
                alternatives = tuple(
                    frozenset(normalize_id(node_id) for node_id in alternative)
                    for alternative in raw_alternatives
                )
            except (TypeError, ValueError) as error:
                raise ProcessObligationError(
                    f"ProcessObligationMapping implementation {key!r} has invalid node IDs"
                ) from error
            if not alternatives or any(not alternative for alternative in alternatives):
                raise ProcessObligationError(
                    f"ProcessObligationMapping implementation {key!r} requires "
                    "non-empty alternatives"
                )
            implementations[key] = alternatives

        if not isinstance(self.gate_scopes, Mapping):
            raise ProcessObligationError("ProcessObligationMapping gate_scopes must be a mapping")
        gate_scopes: dict[ID, tuple[str, ...]] = {}
        for raw_gate_id, raw_scope in self.gate_scopes.items():
            try:
                gate_id = normalize_id(raw_gate_id)
            except (TypeError, ValueError) as error:
                raise ProcessObligationError(
                    "ProcessObligationMapping gate scope IDs must be valid IDs"
                ) from error
            if gate_id in gate_scopes:
                raise ProcessObligationError(
                    f"ProcessObligationMapping contains duplicate Gate scope {gate_id}"
                )
            scope = tuple(item.strip() if isinstance(item, str) else "" for item in raw_scope)
            if not scope or any(not item for item in scope):
                raise ProcessObligationError(
                    f"ProcessObligationMapping Gate scope {gate_id} must be non-empty"
                )
            gate_scopes[gate_id] = scope

        object.__setattr__(self, "obligations", obligations)
        object.__setattr__(self, "implementations", MappingProxyType(implementations))
        object.__setattr__(self, "gate_scopes", MappingProxyType(gate_scopes))


def validate_process_obligations(
    approved: PlanRevision,
    proposed: ProcessRevision,
    mapping: ProcessObligationMapping,
    source_documents: Mapping[str, str],
) -> None:
    """Validate exact citations and structural coverage of a process proposal."""
    if not isinstance(approved, PlanRevision):
        raise TypeError("approved must be a PlanRevision")
    if approved.status is not PlanRevisionStatus.APPROVED:
        raise ProcessObligationError("approved must be an approved PlanRevision")
    if not isinstance(proposed, ProcessRevision):
        raise TypeError("proposed must be a ProcessRevision")
    if not isinstance(mapping, ProcessObligationMapping):
        raise TypeError("mapping must be a ProcessObligationMapping")
    if not isinstance(source_documents, Mapping):
        raise TypeError("source_documents must be a mapping")

    obligation_keys = {item.key for item in mapping.obligations}
    implementation_keys = set(mapping.implementations)
    if implementation_keys != obligation_keys:
        missing = sorted(obligation_keys - implementation_keys)
        extra = sorted(implementation_keys - obligation_keys)
        raise ProcessObligationError(
            f"implementations must exactly cover obligations; missing={missing}, extra={extra}"
        )

    for obligation in mapping.obligations:
        for source_ref in obligation.source_refs:
            document = source_documents.get(source_ref.source_key)
            if not isinstance(document, str):
                raise ProcessObligationError(
                    f"source document {source_ref.source_key!r} is missing"
                )
            if not source_ref.quote.strip() or source_ref.quote not in document:
                raise ProcessObligationError(
                    f"obligation {obligation.key!r} has a quote not found in "
                    f"source document {source_ref.source_key!r}"
                )

    approved_gate_ids = {node.plan_node_id for node in approved.nodes if node.required_check_ids}
    if set(mapping.gate_scopes) != approved_gate_ids:
        missing = sorted(str(item) for item in approved_gate_ids - set(mapping.gate_scopes))
        extra = sorted(str(item) for item in set(mapping.gate_scopes) - approved_gate_ids)
        raise ProcessObligationError(
            f"gate_scopes must exactly cover approved Gate IDs; missing={missing}, extra={extra}"
        )
    if set(proposed.gate_owners) != approved_gate_ids:
        missing = sorted(str(item) for item in approved_gate_ids - set(proposed.gate_owners))
        extra = sorted(str(item) for item in set(proposed.gate_owners) - approved_gate_ids)
        raise ProcessObligationError(
            f"proposed Gate owners must exactly cover approved Gate IDs; "
            f"missing={missing}, extra={extra}"
        )

    for gate_id, scope in mapping.gate_scopes.items():
        if any(key not in obligation_keys for key in scope):
            unknown = sorted(key for key in scope if key not in obligation_keys)
            raise ProcessObligationError(
                f"Gate scope {gate_id} references unknown obligations {unknown}"
            )
    if set().union(*(set(scope) for scope in mapping.gate_scopes.values())) != obligation_keys:
        raise ProcessObligationError("gate_scopes must cover every approved obligation")

    proposed_nodes = {node.plan_node_id: node for node in proposed.graph.nodes}
    for obligation_key, alternatives in mapping.implementations.items():
        for alternative in alternatives:
            if any(node_id not in proposed_nodes for node_id in alternative):
                unknown = sorted(
                    str(node_id) for node_id in alternative if node_id not in proposed_nodes
                )
                raise ProcessObligationError(
                    f"obligation {obligation_key!r} references unknown proposed nodes {unknown}"
                )
            if not any(
                proposed_nodes[node_id].kind in {PlanNodeKind.WORK, PlanNodeKind.MERGE}
                for node_id in alternative
            ):
                raise ProcessObligationError(
                    f"obligation {obligation_key!r} has an alternative containing "
                    "only control nodes"
                )

    try:
        proof = analyze_node_preconditions(proposed.graph)
    except ProcessProofUnavailable as error:
        raise ProcessObligationError(f"node precondition proof unavailable: {error}") from error
    _validate_gate_entries(proposed, mapping, proof)


def _validate_gate_entries(
    proposed: ProcessRevision,
    mapping: ProcessObligationMapping,
    proof: NodePreconditions,
) -> None:
    proposed_nodes = {node.plan_node_id: node for node in proposed.graph.nodes}
    for gate_id, owner_id in proposed.gate_owners.items():
        owner = proposed_nodes.get(owner_id)
        if owner is None:
            raise ProcessObligationError(
                f"approved Gate {gate_id} maps to an unknown proposed owner {owner_id}"
            )
        if owner.kind in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            entry = proof.completed.get(owner_id)
        elif owner.kind is PlanNodeKind.REVIEWER:
            entry = proof.ready.get(owner_id)
        else:
            raise ProcessObligationError(
                f"approved Gate {gate_id} has unsupported proposed owner kind {owner.kind}"
            )
        if not entry:
            raise ProcessObligationError(
                f"approved Gate {gate_id} has no structural precondition entry"
            )
        for route in entry:
            for obligation_key in mapping.gate_scopes[gate_id]:
                alternatives = mapping.implementations[obligation_key]
                if not any(group.issubset(route) for group in alternatives):
                    raise ProcessObligationError(
                        f"Gate {gate_id} route cannot establish obligation {obligation_key!r}"
                    )
