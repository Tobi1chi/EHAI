from __future__ import annotations

from base64 import b64encode
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from ehai import JsonValue, json_dumps, json_loads, new_id
from ehai.application.workers import ArtifactInputSnapshot, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode, PlanNodeKind
from ehai.infrastructure.workers.codex_protocol import (
    CodexProtocolError,
    ParsedCodexResult,
    build_codex_prompt,
    codex_output_schema_json,
    parse_codex_result,
)

NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)


def _request(
    *,
    artifact_inputs: tuple[ArtifactInputSnapshot, ...] = (),
    context: dict[str, JsonValue] | None = None,
    node_kind: PlanNodeKind = PlanNodeKind.WORK,
) -> WorkerRequest:
    goal_id = new_id()
    run = Run(goal_id, new_id(), run_id=new_id(), created_at=NOW).start(at=NOW)
    check_id = new_id()
    check_spec = CheckSpec(
        "completion-artifact",
        CheckKind.ARTIFACT,
        "artifact:non-empty",
        check_id=check_id,
    )
    contract = CompletionContract.draft(
        goal_id,
        ("artifact:non-empty",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    node = (
        PlanNode(
            new_id(),
            "produce candidate",
            "Write a concise candidate result.",
            kind=node_kind,
            required_check_ids=(check_id,),
        )
        .mark_ready()
        .start()
    )
    attempt = Attempt(
        run.run_id,
        node.plan_node_id,
        1,
        attempt_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    return WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        required_check_specs=(check_spec,),
        context={"z": 2, "a": "context"} if context is None else context,
        artifact_inputs=artifact_inputs,
    )


def _input_artifact(content: bytes = b"available input body") -> ArtifactInputSnapshot:
    artifact_id = new_id()
    try:
        encoded_content = content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoded_content = b64encode(content).decode("ascii")
        encoding = "base64"
    return ArtifactInputSnapshot(
        artifact_id=artifact_id,
        kind=ArtifactKind.EVIDENCE,
        name="input.txt",
        media_type="text/plain",
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
        plan_node_id=new_id(),
        encoding=encoding,
        content=encoded_content,
    )


def _valid_document(*, artifacts: list[dict[str, str]] | None = None) -> str:
    return json_dumps(
        {
            "summary": "candidate prepared",
            "artifacts": artifacts
            or [
                {
                    "kind": "candidate",
                    "name": "result.txt",
                    "media_type": "text/plain",
                    "content": "hello",
                }
            ],
        }
    )


def test_prompt_is_deterministic_separated_and_contains_snapshotted_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EHAI_TEST_SECRET", "must-not-leak")
    artifact = _input_artifact()
    request = _request(artifact_inputs=(artifact,))

    prompt = build_codex_prompt(request)

    assert prompt == build_codex_prompt(request)
    assert "--- PLAN_NODE_JSON ---" in prompt
    assert '"instruction":"Write a concise candidate result."' in prompt
    assert '--- CONTEXT_JSON ---\n{"a":"context","z":2}' in prompt
    assert "--- CONFIRMED_COMPLETION_CONTRACT_JSON ---" in prompt
    assert '"criteria":["artifact:non-empty"]' in prompt
    assert '"required_check_ids":["' in prompt
    assert "--- REQUIRED_CHECKS_JSON ---" in prompt
    assert '"kind":"artifact"' in prompt
    assert "--- INPUT_ARTIFACTS_JSON ---" in prompt
    assert artifact.artifact_id in prompt
    assert artifact.sha256 in prompt
    assert "available input body" in prompt
    assert "relative_path" not in prompt
    assert "Return candidate artifacts only" in prompt
    assert "must not mark the PlanNode, Run, or Goal completed" in prompt
    assert "must-not-leak" not in prompt
    assert "unavailable Artifact body sentinel" not in prompt


def test_evaluator_prompt_contains_branch_candidate_contents() -> None:
    first = _input_artifact(b"branch A candidate")
    second = _input_artifact(b"branch B candidate")
    request = _request(
        node_kind=PlanNodeKind.EVALUATOR,
        artifact_inputs=(first, second),
        context={
            "candidate_branches": [
                {
                    "branch_id": new_id(),
                    "label": "approach-a",
                    "node_ids": [first.plan_node_id],
                    "artifacts": [first.to_prompt_dict()],
                },
                {
                    "branch_id": new_id(),
                    "label": "approach-b",
                    "node_ids": [second.plan_node_id],
                    "artifacts": [second.to_prompt_dict()],
                },
            ]
        },
    )

    prompt = build_codex_prompt(request)

    assert "--- CANDIDATE_BRANCHES_JSON ---" in prompt
    assert "branch A candidate" in prompt
    assert "branch B candidate" in prompt
    assert "selected_branch_id" in prompt
    assert "compared_artifact_ids" in prompt


def test_merge_prompt_explicitly_contains_selected_candidate_content_only() -> None:
    selected_token = "SELECTED_BRANCH_CANDIDATE"
    pruned_token = "PRUNED_BRANCH_CANDIDATE"
    request = _request(
        node_kind=PlanNodeKind.MERGE,
        context={
            "branch_selection": {
                "selected_branch_id": new_id(),
                "fork_node_id": new_id(),
                "criterion": "first viable branch",
                "explanation": "selected evidence passed",
                "evidence_artifact_ids": [new_id()],
            },
            "selected_artifacts": [
                {
                    "artifact_id": new_id(),
                    "sha256": "a" * 64,
                    "media_type": "text/plain",
                    "encoding": "utf-8",
                    "content": selected_token,
                }
            ],
        },
    )

    prompt = build_codex_prompt(request)

    assert "--- SELECTED_BRANCH_JSON ---" in prompt
    assert "--- SELECTED_ARTIFACT_CONTENTS_JSON ---" in prompt
    assert selected_token in prompt
    assert pruned_token not in prompt


def test_output_schema_is_canonical_and_strict() -> None:
    encoded = codex_output_schema_json()
    schema = json_loads(encoded)

    assert encoded == codex_output_schema_json()
    assert isinstance(schema, dict)
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, dict)
    artifacts = properties["artifacts"]
    assert isinstance(artifacts, dict)
    assert artifacts["minItems"] == 1
    assert artifacts["maxItems"] == 3
    item = artifacts["items"]
    assert isinstance(item, dict)
    assert item["additionalProperties"] is False
    item_properties = item["properties"]
    assert isinstance(item_properties, dict)
    kind = item_properties["kind"]
    assert isinstance(kind, dict)
    assert kind["enum"] == ["candidate", "patch", "log"]


def test_parse_result_builds_candidate_artifacts_and_worker_result() -> None:
    document = _valid_document(
        artifacts=[
            {
                "kind": "candidate",
                "name": "candidate.txt",
                "media_type": "text/plain",
                "content": "候选",
            },
            {
                "kind": "patch",
                "name": "change.diff",
                "media_type": "text/x-diff",
                "content": "+ change",
            },
            {
                "kind": "log",
                "name": "worker.log",
                "media_type": "text/plain",
                "content": "done",
            },
        ]
    )

    parsed = parse_codex_result(document)

    assert isinstance(parsed, ParsedCodexResult)
    assert isinstance(parsed.result, WorkerResult)
    assert parsed.result.summary == "candidate prepared"
    assert tuple(artifact.kind for artifact in parsed.result.artifacts) == (
        ArtifactKind.CANDIDATE,
        ArtifactKind.PATCH,
        ArtifactKind.LOG,
    )
    assert parsed.result.artifacts[0].content == "候选".encode()
    assert parsed.result.raw_output == document.encode()
    with pytest.raises(FrozenInstanceError):
        parsed.worker_result = parsed.worker_result  # type: ignore[misc]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("not json", "invalid Codex JSON"),
        ("[]", "JSON object"),
        ('{"summary":"ok"}', "missing"),
        ('{"summary":"ok","artifacts":[],"extra":true}', "extra"),
        ('{"summary":"","artifacts":[]}', "summary"),
        ('{"summary":"ok","artifacts":[]}', "between 1 and 3"),
        (
            json_dumps(
                {
                    "summary": "ok",
                    "artifacts": [
                        {
                            "kind": "candidate",
                            "name": "a.txt",
                            "media_type": "text/plain",
                            "content": "a",
                        }
                    ]
                    * 4,
                }
            ),
            "between 1 and 3",
        ),
        (
            '{"summary":"ok","summary":"again","artifacts":[]}',
            "duplicate JSON key",
        ),
    ],
)
def test_parse_result_rejects_invalid_top_level(document: str, message: str) -> None:
    with pytest.raises(CodexProtocolError, match=message):
        parse_codex_result(document)


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (
            {"kind": "candidate", "name": "a", "media_type": "text/plain"},
            "missing",
        ),
        (
            {
                "kind": "candidate",
                "name": "a",
                "media_type": "text/plain",
                "content": "x",
                "extra": "no",
            },
            "extra",
        ),
        (
            {"kind": "evidence", "name": "a", "media_type": "text/plain", "content": "x"},
            "candidate, patch, or log",
        ),
        (
            {"kind": "candidate", "name": "a", "media_type": "text/plain", "content": ""},
            "non-empty text",
        ),
        (
            {"kind": "candidate", "name": "a", "media_type": "text/plain", "content": "  "},
            "whitespace-only",
        ),
        (
            {"kind": "candidate", "name": "a", "media_type": "invalid", "content": "x"},
            "invalid artifact",
        ),
    ],
)
def test_parse_result_rejects_invalid_artifact(
    artifact: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(CodexProtocolError, match=message):
        parse_codex_result(_valid_document(artifacts=[artifact]))


def test_parse_result_rejects_duplicate_artifact_names() -> None:
    artifact = {
        "kind": "candidate",
        "name": "same.txt",
        "media_type": "text/plain",
        "content": "x",
    }
    with pytest.raises(CodexProtocolError, match="duplicate Artifact name"):
        parse_codex_result(_valid_document(artifacts=[artifact, dict(artifact)]))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_parse_result_rejects_non_standard_json_constants(value: str) -> None:
    with pytest.raises(CodexProtocolError, match="non-standard JSON constant"):
        parse_codex_result(f'{{"summary":{value},"artifacts":[]}}')


def test_parse_result_requires_text_input() -> None:
    with pytest.raises(CodexProtocolError, match="JSON text"):
        parse_codex_result(b"{}")  # type: ignore[arg-type]
