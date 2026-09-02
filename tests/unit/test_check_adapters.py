import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from ehai import ID, new_id
from ehai.application.checks import (
    CheckAdapter,
    CheckContext,
    CheckOutcome,
    CheckRegistry,
    CheckRunner,
)
from ehai.application.ports import ArtifactStore
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, CheckRunStatus, CheckSpec
from ehai.domain.execution import Attempt, Run
from ehai.domain.planning import PlanNode
from ehai.infrastructure.checks import (
    ArtifactCheckAdapter,
    ArtifactCheckRule,
    CommandCheckAdapter,
    SemanticCheckAdapter,
    SemanticEvaluation,
    SemanticRubric,
)

NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)


class MemoryArtifactStore:
    def __init__(self, entries: dict[ID, tuple[Artifact, bytes]] | None = None) -> None:
        self.entries = {} if entries is None else dict(entries)

    def put(self, artifact: Artifact, content: bytes) -> None:
        self.entries[artifact.artifact_id] = (artifact, bytes(content))

    def get(self, artifact_id: ID) -> Artifact | None:
        entry = self.entries.get(artifact_id)
        return None if entry is None else entry[0]

    def read(self, artifact_id: ID) -> bytes:
        try:
            return self.entries[artifact_id][1]
        except KeyError as error:
            raise FileNotFoundError(str(artifact_id)) from error

    def list_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        return tuple(artifact for artifact, _ in self.entries.values() if artifact.run_id == run_id)


def make_context(
    tmp_path: Path,
    *,
    check_id: ID,
    contents: tuple[bytes, ...] = (b"candidate evidence",),
    stored: bool = True,
) -> tuple[CheckContext, MemoryArtifactStore]:
    run = Run(new_id(), new_id(), created_at=NOW).start(at=NOW)
    node = PlanNode(
        new_id(),
        "verify",
        "verify candidate",
        required_check_ids=(check_id,),
    )
    verifying = node.mark_ready().start().submit_candidate().begin_verification()
    artifact_ids = tuple(new_id() for _ in contents)
    attempt = Attempt(
        run.run_id,
        verifying.plan_node_id,
        1,
        created_at=NOW,
    ).start(at=NOW)
    attempt = attempt.succeed(artifact_ids, at=NOW)
    artifacts = tuple(
        Artifact(
            artifact_id=artifact_id,
            kind=ArtifactKind.CANDIDATE,
            name=f"candidate-{index}.txt",
            media_type="text/plain",
            size_bytes=len(content),
            sha256=sha256(content).hexdigest(),
            relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
            created_at=NOW,
            run_id=run.run_id,
            plan_node_id=verifying.plan_node_id,
            attempt_id=attempt.attempt_id,
        )
        for index, (artifact_id, content) in enumerate(zip(artifact_ids, contents, strict=True))
    )
    entries = (
        {
            artifact.artifact_id: (artifact, content)
            for artifact, content in zip(artifacts, contents, strict=True)
        }
        if stored
        else {}
    )
    store = MemoryArtifactStore(entries)
    assert isinstance(store, ArtifactStore)
    return (
        CheckContext(
            run=run,
            attempt=attempt,
            plan_node=verifying,
            artifacts=artifacts,
            workspace=tmp_path,
        ),
        store,
    )


def run_check(spec: CheckSpec, context: CheckContext, adapter: CheckAdapter):
    runner = CheckRunner(CheckRegistry({spec.kind: adapter}), clock=lambda: NOW)
    return runner.run(spec, context)


def test_command_check_passes_with_evidence_and_captures_output(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "command must pass")
    context, _ = make_context(tmp_path, check_id=spec.check_id)
    adapter = CommandCheckAdapter(
        {spec.check_id: (sys.executable, "-c", "print('controlled output')")}
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.status is CheckRunStatus.COMPLETED
    assert check_run.result is not None and check_run.result.passed
    output = json.loads(check_run.result.output or "")
    assert output["exit_code"] == 0
    assert output["stdout"].splitlines() == ["controlled output"]
    assert check_run.result.evidence_artifact_ids == context.attempt.artifact_ids


def test_command_check_materializes_artifacts_in_check_temp_dir(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "command must inspect candidate")
    context, store = make_context(tmp_path, check_id=spec.check_id, contents=(b"candidate",))
    adapter = CommandCheckAdapter(
        {
            spec.check_id: (
                sys.executable,
                "-c",
                "import pathlib; assert pathlib.Path('candidate-0.txt').read_text() == 'candidate'",
            )
        },
        store=store,
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.result is not None and check_run.result.passed
    output = json.loads(check_run.result.output or "")
    assert output["materialized_artifacts"] == [
        {
            "artifact_id": context.artifacts[0].artifact_id,
            "name": "candidate-0.txt",
            "size_bytes": len(b"candidate"),
            "sha256": context.artifacts[0].sha256,
        }
    ]
    assert not (tmp_path / "candidate-0.txt").exists()


def test_command_check_rejects_materialized_path_escape(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "command must inspect candidate")
    context, store = make_context(tmp_path, check_id=spec.check_id)
    unsafe = replace(context.artifacts[0], name="../escape.txt")
    store.entries[unsafe.artifact_id] = (unsafe, b"candidate evidence")
    context = replace(context, artifacts=(unsafe,))
    adapter = CommandCheckAdapter(
        {spec.check_id: (sys.executable, "-c", "raise SystemExit(0)")},
        store=store,
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.status is CheckRunStatus.FAILED
    assert check_run.failure_reason is not None and "unsafe" in check_run.failure_reason


def test_command_check_nonzero_is_a_failed_verdict_with_output(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "command must pass")
    context, _ = make_context(tmp_path, check_id=spec.check_id)
    adapter = CommandCheckAdapter(
        {spec.check_id: (sys.executable, "-c", "import sys; print('bad'); sys.exit(3)")}
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.status is CheckRunStatus.COMPLETED
    assert check_run.result is not None and not check_run.result.passed
    assert check_run.result.failure_reason == "command exited with code 3"
    assert json.loads(check_run.result.output or "")["stdout"].splitlines() == ["bad"]


def test_command_check_timeout_marks_check_run_timed_out(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "command must finish")
    context, _ = make_context(tmp_path, check_id=spec.check_id)
    adapter = CommandCheckAdapter(
        {spec.check_id: (sys.executable, "-c", "import time; time.sleep(5)")},
        timeout_seconds=0.05,
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.status is CheckRunStatus.TIMED_OUT
    assert check_run.result is None
    assert check_run.failure_reason is not None and "timed out" in check_run.failure_reason


def test_command_output_is_truncated_without_using_a_shell(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "command output is bounded")
    context, _ = make_context(tmp_path, check_id=spec.check_id)
    adapter = CommandCheckAdapter(
        {spec.check_id: (sys.executable, "-c", "print('x' * 100)")},
        max_output_bytes=8,
    )

    check_run = run_check(spec, context, adapter)
    assert check_run.result is not None
    output = json.loads(check_run.result.output or "")
    assert output["stdout"] == "xxxxxxxx"
    assert output["stdout_truncated"] is True


def test_artifact_check_validates_media_non_empty_and_count(tmp_path: Path) -> None:
    spec = CheckSpec("artifact", CheckKind.ARTIFACT, "candidate must exist")
    context, store = make_context(tmp_path, check_id=spec.check_id)
    adapter = ArtifactCheckAdapter(
        store,
        {
            spec.check_id: ArtifactCheckRule(
                allowed_media_types=("text/plain",),
                require_non_empty=True,
                minimum_count=1,
                maximum_count=1,
            )
        },
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.result is not None and check_run.result.passed
    assert check_run.result.evidence_artifact_ids == context.attempt.artifact_ids


@pytest.mark.parametrize("case", ["missing", "empty", "wrong_media", "too_many"])
def test_artifact_check_fails_closed(case: str, tmp_path: Path) -> None:
    spec = CheckSpec("artifact", CheckKind.ARTIFACT, "candidate must be valid")
    contents = (
        (b"",) if case == "empty" else ((b"one", b"two") if case == "too_many" else (b"one",))
    )
    context, store = make_context(
        tmp_path,
        check_id=spec.check_id,
        contents=contents,
        stored=case != "missing",
    )
    if case == "wrong_media":
        artifact = replace(context.artifacts[0], media_type="application/json")
        store.entries[artifact.artifact_id] = (artifact, b"one")
        context = replace(context, artifacts=(artifact,))
    adapter = ArtifactCheckAdapter(
        store,
        {
            spec.check_id: ArtifactCheckRule(
                allowed_media_types=("text/plain",),
                maximum_count=1,
            )
        },
    )

    check_run = run_check(spec, context, adapter)

    assert check_run.status is CheckRunStatus.COMPLETED
    assert check_run.result is not None and not check_run.result.passed
    assert check_run.result.failure_reason


def test_semantic_check_saves_rubric_score_explanation_and_evidence(tmp_path: Path) -> None:
    spec = CheckSpec("semantic", CheckKind.SEMANTIC, "required concepts are present")
    context, store = make_context(
        tmp_path,
        check_id=spec.check_id,
        contents=(b"PlanGraph remains separate from ExecutionTrace",),
    )
    rubric = SemanticRubric(
        description="both architecture terms must appear",
        required_terms=("PlanGraph", "ExecutionTrace"),
        minimum_score=1.0,
    )
    adapter = SemanticCheckAdapter(store, {spec.check_id: rubric})

    check_run = run_check(spec, context, adapter)

    assert check_run.result is not None and check_run.result.passed
    output = json.loads(check_run.result.output or "")
    assert output["rubric"]["description"] == rubric.description
    assert output["score"] == 1.0
    assert output["explanation"]
    assert output["evidence_artifact_ids"] == list(context.attempt.artifact_ids)


def test_semantic_check_low_score_and_missing_evidence_fail_closed(tmp_path: Path) -> None:
    spec = CheckSpec("semantic", CheckKind.SEMANTIC, "required concepts are present")
    context, store = make_context(tmp_path, check_id=spec.check_id, contents=(b"only PlanGraph",))
    adapter = SemanticCheckAdapter(
        store,
        {
            spec.check_id: SemanticRubric(
                description="both terms",
                required_terms=("PlanGraph", "ExecutionTrace"),
                minimum_score=1.0,
            )
        },
    )

    low_score = run_check(spec, context, adapter)
    assert low_score.result is not None and not low_score.result.passed
    assert "below" in (low_score.result.failure_reason or "")

    store.entries.clear()
    missing = run_check(spec, context, adapter)
    assert missing.result is not None and not missing.result.passed
    output = json.loads(missing.result.output or "")
    assert output["score"] == 0.0
    assert output["explanation"]
    assert missing.result.evidence_artifact_ids == ()


def test_semantic_check_supports_injected_deterministic_evaluator(tmp_path: Path) -> None:
    spec = CheckSpec("semantic", CheckKind.SEMANTIC, "custom deterministic evaluation")
    context, store = make_context(tmp_path, check_id=spec.check_id)

    def evaluator(*_args: object) -> SemanticEvaluation:
        return SemanticEvaluation(0.75, "three of four explicit rubric points met")

    adapter = SemanticCheckAdapter(
        store,
        {
            spec.check_id: SemanticRubric(
                description="four explicit points",
                minimum_score=0.75,
            )
        },
        evaluator=evaluator,
    )

    check_run = run_check(spec, context, adapter)
    assert check_run.result is not None and check_run.result.passed
    assert json.loads(check_run.result.output or "")["score"] == 0.75


def test_runner_rejects_adapter_evidence_outside_context(tmp_path: Path) -> None:
    spec = CheckSpec("command", CheckKind.COMMAND, "must not forge evidence")
    context, _ = make_context(tmp_path, check_id=spec.check_id)

    class ForgedAdapter:
        def evaluate(self, spec: CheckSpec, context: CheckContext) -> CheckOutcome:
            del spec, context
            return CheckOutcome(True, "forged", (new_id(),))

    check_run = run_check(spec, context, ForgedAdapter())
    assert check_run.status is CheckRunStatus.FAILED
    assert check_run.failure_reason is not None and "outside" in check_run.failure_reason
