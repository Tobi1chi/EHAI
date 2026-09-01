"""Strict prompt and structured-output protocol for the Codex Worker."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ehai import JsonValue, json_dumps
from ehai.application.workers import CandidateArtifact, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.planning import PlanNodeKind

_OUTPUT_KEYS = frozenset({"summary", "artifacts"})
_ARTIFACT_KEYS = frozenset({"kind", "name", "media_type", "content"})
_ALLOWED_KINDS = frozenset({ArtifactKind.CANDIDATE, ArtifactKind.PATCH, ArtifactKind.LOG})

_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "EHAI Codex candidate result",
    "description": "Candidate artifacts only; this result cannot complete a node or goal.",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "artifacts"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "artifacts": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name", "media_type", "content"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["candidate", "patch", "log"],
                    },
                    "name": {"type": "string", "minLength": 1},
                    "media_type": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class CodexProtocolError(ValueError):
    """Raised when a Codex structured result violates the P1 protocol."""


@dataclass(frozen=True, slots=True)
class ParsedCodexResult:
    """A validated Codex result ready for the WorkerAdapter boundary."""

    worker_result: WorkerResult

    def __post_init__(self) -> None:
        if not isinstance(self.worker_result, WorkerResult):
            raise TypeError("worker_result must be a WorkerResult")

    @property
    def result(self) -> WorkerResult:
        """Return the application WorkerResult for connector convenience."""
        return self.worker_result


def build_codex_prompt(request: WorkerRequest) -> str:
    """Build a deterministic prompt with bounded Artifact content snapshots."""
    if not isinstance(request, WorkerRequest):
        raise TypeError("request must be a WorkerRequest")

    contract: dict[str, JsonValue] = {
        "completion_contract_id": request.completion_contract.completion_contract_id,
        "criteria": list(request.completion_contract.criteria),
        "version": request.completion_contract.version,
    }
    node: dict[str, JsonValue] = {
        "instruction": request.plan_node.instruction,
        "plan_node_id": request.plan_node_id,
        "title": request.plan_node.title,
    }
    artifact_inputs: JsonValue = [artifact.to_prompt_dict() for artifact in request.artifact_inputs]
    context = request.context
    general_context = dict(context)
    role_sections: tuple[str, ...] = ()
    output_notes: tuple[str, ...] = ()
    if request.plan_node.kind is PlanNodeKind.EVALUATOR:
        candidate_branches = context.get("candidate_branches")
        if not isinstance(candidate_branches, list) or len(candidate_branches) < 2:
            raise CodexProtocolError("Evaluator request requires at least two Branch candidates")
        general_context.pop("candidate_branches", None)
        role_sections = (
            "Compare every active Branch candidate below using actual Artifact content.",
            "Select only a Branch whose viable field is true; failed Branches must be pruned.",
            "--- CANDIDATE_BRANCHES_JSON ---",
            json_dumps(candidate_branches),
        )
        output_notes = (
            "For an evaluator PlanNode, return one candidate Artifact whose content is "
            "a JSON object with exactly these keys: selected_branch_id, pruned_branch_ids, "
            "criterion, explanation, compared_artifact_ids, selected_artifact_ids.",
            "compared_artifact_ids must include all candidate Artifact IDs you compared; "
            "selected_artifact_ids must list the selected Branch Artifact IDs for Merge.",
        )
    if request.plan_node.kind is PlanNodeKind.MERGE:
        branch_selection = context.get("branch_selection")
        selected_artifacts = context.get("selected_artifacts")
        if (
            not isinstance(branch_selection, dict)
            or not isinstance(selected_artifacts, list)
            or not selected_artifacts
        ):
            raise CodexProtocolError("Merge request requires selected Branch content")
        general_context.pop("branch_selection", None)
        general_context.pop("selected_artifacts", None)
        role_sections = (
            "Merge only the selected Branch artifacts below; ignore every pruned Branch.",
            "--- SELECTED_BRANCH_JSON ---",
            json_dumps(branch_selection),
            "--- SELECTED_ARTIFACT_CONTENTS_JSON ---",
            json_dumps(selected_artifacts),
        )
    sections = (
        "EHAI CODEX WORKER PROTOCOL v1",
        "You are executing one Worker Attempt.",
        "Return candidate artifacts only. You must not mark the PlanNode, Run, or Goal completed.",
        "Checks and Gates outside this process are the only completion authority.",
        "--- PLAN_NODE_JSON ---",
        json_dumps(node),
        "--- CONTEXT_JSON ---",
        json_dumps(general_context),
        "--- CONFIRMED_COMPLETION_CONTRACT_JSON ---",
        json_dumps(contract),
        "--- INPUT_ARTIFACTS_JSON ---",
        json_dumps(artifact_inputs),
        *role_sections,
        "--- OUTPUT_REQUIREMENTS ---",
        "Return exactly one JSON object matching the supplied output schema.",
        "Artifact content must be UTF-8 text; do not add fields outside the schema.",
        *output_notes,
    )
    return "\n".join(sections)


def codex_output_schema_json() -> str:
    """Return the canonical strict JSON Schema accepted by Codex and the parser."""
    return json_dumps(_OUTPUT_SCHEMA)


def parse_codex_result(document: str) -> ParsedCodexResult:
    """Fail closed on Codex output and convert it to a WorkerResult."""
    if not isinstance(document, str):
        raise CodexProtocolError("Codex result must be JSON text")
    decoded = _strict_json_decode(document)
    if not isinstance(decoded, dict):
        raise CodexProtocolError("Codex result must be a JSON object")
    _require_exact_keys(decoded, _OUTPUT_KEYS, "Codex result")

    summary = _required_text(decoded["summary"], "summary")
    artifact_documents = decoded["artifacts"]
    if not isinstance(artifact_documents, list):
        raise CodexProtocolError("artifacts must be an array")
    if not 1 <= len(artifact_documents) <= 3:
        raise CodexProtocolError("artifacts must contain between 1 and 3 items")

    artifacts: list[CandidateArtifact] = []
    names: set[str] = set()
    for index, value in enumerate(artifact_documents):
        owner = f"artifact[{index}]"
        if not isinstance(value, dict):
            raise CodexProtocolError(f"{owner} must be an object")
        _require_exact_keys(value, _ARTIFACT_KEYS, owner)
        kind_value = _required_text(value["kind"], f"{owner}.kind")
        try:
            kind = ArtifactKind(kind_value)
        except ValueError as error:
            raise CodexProtocolError(f"{owner}.kind must be candidate, patch, or log") from error
        if kind not in _ALLOWED_KINDS:
            raise CodexProtocolError(f"{owner}.kind must be candidate, patch, or log")

        name = _required_text(value["name"], f"{owner}.name")
        if name in names:
            raise CodexProtocolError(f"duplicate Artifact name: {name!r}")
        names.add(name)
        media_type = _required_text(value["media_type"], f"{owner}.media_type")
        content = _required_text(value["content"], f"{owner}.content", allow_whitespace=False)
        try:
            artifact = CandidateArtifact(
                kind=kind,
                name=name,
                media_type=media_type,
                content=content.encode("utf-8"),
            )
        except (TypeError, ValueError) as error:
            raise CodexProtocolError(f"invalid {owner}: {error}") from error
        artifacts.append(artifact)

    try:
        result = WorkerResult(
            artifacts=tuple(artifacts),
            summary=summary,
            raw_output=document.encode("utf-8"),
        )
    except (TypeError, ValueError) as error:  # pragma: no cover - guarded above
        raise CodexProtocolError(f"invalid Codex WorkerResult: {error}") from error
    return ParsedCodexResult(worker_result=result)


def _strict_json_decode(document: str) -> object:
    def reject_constant(value: str) -> None:
        raise CodexProtocolError(f"non-standard JSON constant is not allowed: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CodexProtocolError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            document,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except CodexProtocolError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CodexProtocolError(f"invalid Codex JSON: {error}") from error


def _require_exact_keys(value: dict[str, object], expected: frozenset[str], owner: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CodexProtocolError(f"{owner} keys are invalid; missing={missing}, extra={extra}")


def _required_text(value: object, field_name: str, *, allow_whitespace: bool = True) -> str:
    if not isinstance(value, str) or not value:
        raise CodexProtocolError(f"{field_name} must be non-empty text")
    if not allow_whitespace and not value.strip():
        raise CodexProtocolError(f"{field_name} must not be whitespace-only")
    return value
