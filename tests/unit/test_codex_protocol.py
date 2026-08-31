from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from ehai import json_dumps, json_loads, new_id
from ehai.application.workers import WorkerRequest, WorkerResult
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode
from ehai.infrastructure.workers.codex_protocol import (
    CodexProtocolError,
    ParsedCodexResult,
    build_codex_prompt,
    codex_output_schema_json,
    parse_codex_result,
)

NOW = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)


def _request(*, artifact_inputs: tuple[Artifact, ...] = ()) -> WorkerRequest:
    goal_id = new_id()
    run = Run(goal_id, new_id(), run_id=new_id(), created_at=NOW).start(at=NOW)
    check_id = new_id()
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
        context={"z": 2, "a": "context"},
        artifact_inputs=artifact_inputs,
    )


def _input_artifact() -> Artifact:
    return Artifact(
        artifact_id=new_id(),
        kind=ArtifactKind.EVIDENCE,
        name="input.txt",
        media_type="text/plain",
        size_bytes=19,
        sha256="a" * 64,
        relative_path="objects/input.metadata-only",
        created_at=NOW,
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


def test_prompt_is_deterministic_separated_and_metadata_only(
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
    assert "--- INPUT_ARTIFACT_METADATA_JSON ---" in prompt
    assert artifact.artifact_id in prompt
    assert artifact.sha256 in prompt
    assert "Return candidate artifacts only" in prompt
    assert "must not mark the PlanNode, Run, or Goal completed" in prompt
    assert "must-not-leak" not in prompt
    assert "unavailable Artifact body sentinel" not in prompt


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
