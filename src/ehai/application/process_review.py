"""Review evidence for a process draft, distinct from original user approval.

The host can validate references and graph coverage; a Reviewer must still judge
meaning against the complete original material. Source quotation equality is
not a semantic proof or a replacement approval boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from ehai import ID, JsonValue, json_dumps, normalize_id
from ehai.application.process_changes import validate_process_gate_preservation
from ehai.application.process_obligations import (
    ApprovalSourceRef,
    ApprovedObligation,
    ProcessObligationMapping,
)
from ehai.application.process_review_context import ProcessReviewContext
from ehai.domain.planning import PlanNodeKind


class BoundaryAspect(StrEnum):
    REQUIREMENTS = "requirements"
    INTERFACES = "interfaces"
    PERMISSIONS = "permissions"
    GATE_SCOPE = "gate_scope"
    RESULT_REUSE = "result_reuse"


class BoundaryJudgment(StrEnum):
    PRESERVED = "preserved"
    NOT_PRESERVED = "not_preserved"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class BoundaryAssessment:
    aspect: BoundaryAspect
    judgment: BoundaryJudgment
    explanation: str
    source_refs: tuple[ApprovalSourceRef, ...]
    candidate_node_ids: tuple[ID, ...]


@dataclass(frozen=True, slots=True)
class ProcessBoundaryReport:
    summary: str
    assessments: tuple[BoundaryAssessment, ...]
    obligation_mapping: ProcessObligationMapping | None
    evidence_artifact_ids: tuple[ID, ...]

    @property
    def preserves_boundary(self) -> bool:
        """Reviewer conclusion only; publication must also validate freshness and runtime state."""
        return (
            {item.aspect for item in self.assessments} == set(BoundaryAspect)
            and len(self.assessments) == len(BoundaryAspect)
            and all(item.judgment is BoundaryJudgment.PRESERVED for item in self.assessments)
        )


def process_boundary_report_document(report: ProcessBoundaryReport) -> dict[str, JsonValue]:
    """Serialize the validated report without changing its source or producer references."""

    def refs(items: tuple[ApprovalSourceRef, ...]) -> list[JsonValue]:
        return [{"source_key": item.source_key, "quote": item.quote} for item in items]

    mapping = report.obligation_mapping
    mapping_document: JsonValue = (
        None
        if mapping is None
        else {
            "obligations": [
                {
                    "key": item.key,
                    "description": item.description,
                    "source_refs": refs(item.source_refs),
                }
                for item in mapping.obligations
            ],
            "implementations": [
                {
                    "obligation_key": key,
                    "alternatives": [cast(JsonValue, sorted(group)) for group in groups],
                }
                for key, groups in mapping.implementations.items()
            ],
            "gate_scopes": [
                {"original_gate_id": gate_id, "obligation_keys": list(keys)}
                for gate_id, keys in mapping.gate_scopes.items()
            ],
        }
    )
    return {
        "summary": report.summary,
        "assessments": [
            {
                "aspect": item.aspect.value,
                "judgment": item.judgment.value,
                "explanation": item.explanation,
                "source_refs": refs(item.source_refs),
                "candidate_node_ids": list(item.candidate_node_ids),
            }
            for item in report.assessments
        ],
        "obligation_mapping": mapping_document,
        "evidence_artifact_ids": list(report.evidence_artifact_ids),
    }


def approved_source_documents(context: ProcessReviewContext) -> Mapping[str, str]:
    """Expose original facts, not the Planner's replacement explanation, as quote sources."""
    sources = {
        "goal": context.goal.objective,
        "contract": "\n".join(context.contract.criteria),
    }
    if context.approved.design_document is not None:
        sources["design"] = context.approved.design_document
    for node in context.approved.nodes:
        sources[f"node:{node.plan_node_id}"] = f"{node.title}\n{node.instruction}"
    for check in context.checks:
        sources[f"check:{check.check_id}"] = json_dumps(
            {
                "name": check.name,
                "description": check.description,
                "kind": check.kind.value,
                "required": check.required,
                "command_argv": list(check.command_argv),
                "semantic_required_terms": list(check.semantic_required_terms),
            }
        )
    for stored in context.authorization_events:
        sources[f"authorization:{stored.event.id}"] = json_dumps(stored.event.payload)
    return MappingProxyType(sources)


def parse_process_boundary_report(
    value: Mapping[str, JsonValue], context: ProcessReviewContext
) -> ProcessBoundaryReport:
    """Validate a Reviewer submission against its exact retained draft and source facts."""
    _keys(
        value, {"summary", "assessments", "obligation_mapping", "evidence_artifact_ids"}, "report"
    )
    summary = _text(value.get("summary"), "summary")
    candidate = context.draft.candidate
    if candidate is None:
        raise ValueError("Boundary review requires a generated candidate")
    candidate_ids = {node.plan_node_id for node in candidate.graph.nodes}
    sources = approved_source_documents(context)
    assessments: list[BoundaryAssessment] = []
    for raw in _array(value.get("assessments"), "assessments"):
        item = _object(raw, "assessment")
        _keys(
            item,
            {"aspect", "judgment", "explanation", "source_refs", "candidate_node_ids"},
            "assessment",
        )
        aspect = BoundaryAspect(_text(item.get("aspect"), "aspect"))
        judgment = BoundaryJudgment(_text(item.get("judgment"), "judgment"))
        refs = _source_refs(item.get("source_refs"), sources)
        if judgment is BoundaryJudgment.PRESERVED and not refs:
            raise ValueError("A preserved assessment must cite original approved material")
        node_ids = tuple(
            normalize_id(_text(node_id, "candidate_node_id"))
            for node_id in _array(item.get("candidate_node_ids"), "candidate_node_ids")
        )
        if len(set(node_ids)) != len(node_ids) or not set(node_ids).issubset(candidate_ids):
            raise ValueError("Assessment node references must name unique candidate nodes")
        assessments.append(
            BoundaryAssessment(
                aspect, judgment, _text(item.get("explanation"), "explanation"), refs, node_ids
            )
        )
    if len(assessments) != len(BoundaryAspect) or {item.aspect for item in assessments} != set(
        BoundaryAspect
    ):
        raise ValueError("Report must assess each approved-boundary aspect exactly once")
    mapping_value = value.get("obligation_mapping")
    mapping = None if mapping_value is None else _obligation_mapping(mapping_value, sources)
    evidence_ids = tuple(
        normalize_id(_text(item, "evidence Artifact ID"))
        for item in _array(value.get("evidence_artifact_ids"), "evidence_artifact_ids")
    )
    available_artifacts = {
        artifact.artifact_id: artifact
        for result in context.retained_results
        for artifact in result.artifacts
    }
    if len(set(evidence_ids)) != len(evidence_ids) or not set(evidence_ids).issubset(
        available_artifacts
    ):
        raise ValueError("Review evidence must reference unique retained result Artifacts")
    report = ProcessBoundaryReport(summary, tuple(assessments), mapping, evidence_ids)
    if report.preserves_boundary:
        if any(
            notice.get("kind") == "external_effects" and notice.get("status") == "open"
            for notice in context.interventions
        ):
            raise ValueError(
                "An unanswered external-effects intervention prevents process application; "
                "report uncertainty and obtain an explicit continuation reply before replanning"
            )
        if mapping is None:
            raise ValueError("A preserved boundary requires an explicit obligation mapping")
        covered = {node_id for item in assessments for node_id in item.candidate_node_ids}
        covered.update(
            node_id
            for groups in mapping.implementations.values()
            for group in groups
            for node_id in group
        )
        if covered != candidate_ids:
            raise ValueError("Preservation review must account for every proposed task")
        for retained in context.retained_results:
            if retained.node.kind in {PlanNodeKind.WORK, PlanNodeKind.MERGE} and (
                retained.attempt is None
                or not any(artifact.artifact_id in evidence_ids for artifact in retained.artifacts)
            ):
                raise ValueError(
                    "Every reused implementation task requires successful result evidence"
                )
        validate_process_gate_preservation(
            context.approved,
            context.previous,
            candidate,
            obligation_mapping=mapping,
            source_documents=sources,
        )
    return report


def _obligation_mapping(raw: JsonValue, sources: Mapping[str, str]) -> ProcessObligationMapping:
    value = _object(raw, "obligation_mapping")
    _keys(value, {"obligations", "implementations", "gate_scopes"}, "obligation_mapping")
    obligations: list[ApprovedObligation] = []
    for raw_obligation in _array(value.get("obligations"), "obligations"):
        item = _object(raw_obligation, "obligation")
        _keys(item, {"key", "description", "source_refs"}, "obligation")
        obligations.append(
            ApprovedObligation(
                key=_text(item.get("key"), "obligation key"),
                description=_text(item.get("description"), "obligation description"),
                source_refs=_source_refs(item.get("source_refs"), sources),
            )
        )
    implementations: dict[str, tuple[frozenset[ID], ...]] = {}
    for raw_implementation in _array(value.get("implementations"), "implementations"):
        item = _object(raw_implementation, "implementation")
        _keys(item, {"obligation_key", "alternatives"}, "implementation")
        key = _text(item.get("obligation_key"), "obligation_key")
        if key in implementations:
            raise ValueError("Each obligation must have exactly one implementation mapping")
        implementations[key] = tuple(
            frozenset(
                normalize_id(_text(node_id, "producer node"))
                for node_id in _array(group, "producer group")
            )
            for group in _array(item.get("alternatives"), "implementation alternatives")
        )
    scopes: dict[ID, tuple[str, ...]] = {}
    for raw_scope in _array(value.get("gate_scopes"), "gate_scopes"):
        item = _object(raw_scope, "gate scope")
        _keys(item, {"original_gate_id", "obligation_keys"}, "gate scope")
        gate_id = normalize_id(_text(item.get("original_gate_id"), "original_gate_id"))
        if gate_id in scopes:
            raise ValueError("Each original Gate must have exactly one obligation scope")
        scopes[gate_id] = tuple(
            _text(key, "scope obligation")
            for key in _array(item.get("obligation_keys"), "obligation_keys")
        )
    return ProcessObligationMapping(tuple(obligations), implementations, scopes)


def process_boundary_report_schema(context: ProcessReviewContext) -> dict[str, JsonValue]:
    """One strict tool schema, using arrays so model output needs no arbitrary object keys."""
    candidate = context.draft.candidate
    if candidate is None:
        raise ValueError("Boundary review requires a generated candidate")
    text_schema: dict[str, JsonValue] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 16000,
        "description": (
            "Non-whitespace text, at most 16000 UTF-8 bytes. The host also enforces "
            "this byte limit; non-ASCII characters may occupy multiple bytes."
        ),
    }
    node_schema: dict[str, JsonValue] = {
        "type": "string",
        "enum": [node.plan_node_id for node in candidate.graph.nodes],
    }
    source_ref = _object_schema(
        {
            "source_key": {"type": "string", "enum": list(approved_source_documents(context))},
            "quote": text_schema,
        }
    )
    assessment = _object_schema(
        {
            "aspect": {"type": "string", "enum": [item.value for item in BoundaryAspect]},
            "judgment": {"type": "string", "enum": [item.value for item in BoundaryJudgment]},
            "explanation": text_schema,
            "source_refs": _array_schema(source_ref),
            "candidate_node_ids": _array_schema(node_schema),
        }
    )
    obligation = _object_schema(
        {
            "key": text_schema,
            "description": text_schema,
            "source_refs": _array_schema(source_ref),
        }
    )
    implementation = _object_schema(
        {
            "obligation_key": text_schema,
            "alternatives": _array_schema(_array_schema(node_schema)),
        }
    )
    scope = _object_schema(
        {
            "original_gate_id": {
                "type": "string",
                "enum": list(candidate.gate_owners),
            },
            "obligation_keys": _array_schema(text_schema),
        }
    )
    mapping = _object_schema(
        {
            "obligations": _array_schema(obligation),
            "implementations": _array_schema(implementation),
            "gate_scopes": _array_schema(scope),
        }
    )
    return _object_schema(
        {
            "summary": text_schema,
            "assessments": _array_schema(assessment),
            "obligation_mapping": {"anyOf": [mapping, {"type": "null"}]},
            "evidence_artifact_ids": _array_schema({"type": "string"}),
        }
    )


def _object_schema(properties: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _array_schema(items: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"type": "array", "items": items, "maxItems": 256}


def _source_refs(raw: JsonValue, sources: Mapping[str, str]) -> tuple[ApprovalSourceRef, ...]:
    refs: list[ApprovalSourceRef] = []
    for raw_ref in _array(raw, "source_refs"):
        item = _object(raw_ref, "source reference")
        _keys(item, {"source_key", "quote"}, "source reference")
        key = _text(item.get("source_key"), "source_key")
        quote = _text(item.get("quote"), "quote")
        if key not in sources or quote not in sources[key]:
            raise ValueError("Source reference must quote the specified original material exactly")
        refs.append(ApprovalSourceRef(key, quote))
    return tuple(refs)


def _keys(value: Mapping[str, JsonValue], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} must contain exactly {sorted(expected)}")


def _object(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: JsonValue, name: str) -> list[JsonValue]:
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError(f"{name} must be an array with at most 256 entries")
    return value


def _text(value: JsonValue, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 16_000:
        raise ValueError(f"{name} must contain 1-16000 UTF-8 bytes")
    return value
