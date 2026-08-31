from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from ehai import ID, JsonValue, new_id
from ehai.application.workers import (
    CandidateArtifact,
    WorkerAdapter,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
)
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode
from ehai.infrastructure.workers import FakeWorker

NOW = datetime(2026, 8, 31, 14, tzinfo=UTC)


def make_request(*, artifact_inputs: tuple[Artifact, ...] = ()) -> WorkerRequest:
    goal_id = new_id()
    run = Run(goal_id, new_id(), created_at=NOW).start(at=NOW)
    contract = CompletionContract.draft(
        goal_id,
        ("candidate is supported by evidence",),
        (new_id(),),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    node = PlanNode(new_id(), "produce candidate", "return deterministic candidate").mark_ready()
    node = node.start()
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=NOW).start(at=NOW)
    return WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        context={"prior": ["fact"]},
        artifact_inputs=artifact_inputs,
    )


def candidate(content: bytes = b"configured") -> CandidateArtifact:
    return CandidateArtifact(
        kind=ArtifactKind.CANDIDATE,
        name="candidate.txt",
        media_type="text/plain",
        content=content,
    )


def result(content: bytes = b"configured") -> WorkerResult:
    return WorkerResult(
        artifacts=(candidate(content),),
        summary="configured result",
        raw_output=content,
        diagnostics=("configured",),
    )


def input_artifact(run_id: ID | None = None) -> Artifact:
    artifact_id = new_id()
    return Artifact(
        artifact_id=artifact_id,
        kind=ArtifactKind.EVIDENCE,
        name="input.txt",
        media_type="text/plain",
        size_bytes=0,
        sha256=sha256(b"").hexdigest(),
        relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
        created_at=NOW,
        run_id=run_id,
    )


def test_worker_request_snapshots_context_and_exposes_validated_scope() -> None:
    context: dict[str, JsonValue] = {"prior": ["fact"]}
    request = make_request()
    request = WorkerRequest(
        run=request.run,
        attempt=request.attempt,
        plan_node=request.plan_node,
        completion_contract=request.completion_contract,
        context=context,
        artifact_inputs=(input_artifact(),),
    )
    prior = context["prior"]
    assert isinstance(prior, list)
    prior.append("later")
    returned = request.context["prior"]
    assert isinstance(returned, list)
    returned.append("also later")

    assert request.context == {"prior": ["fact"]}
    assert request.run_id == request.run.run_id
    assert request.attempt_id == request.attempt.attempt_id
    assert request.plan_node_id == request.plan_node.plan_node_id
    with pytest.raises(FrozenInstanceError):
        request.run = request.run  # type: ignore[misc]


def test_worker_request_rejects_cross_scope_attempt_contract_and_artifacts() -> None:
    request = make_request()
    other = make_request()

    with pytest.raises(ValueError, match="another Run"):
        WorkerRequest(
            run=request.run,
            attempt=other.attempt,
            plan_node=other.plan_node,
            completion_contract=request.completion_contract,
            context={},
        )
    with pytest.raises(ValueError, match="another Goal"):
        WorkerRequest(
            run=request.run,
            attempt=request.attempt,
            plan_node=request.plan_node,
            completion_contract=other.completion_contract,
            context={},
        )
    with pytest.raises(ValueError, match="Artifacts from another Run"):
        WorkerRequest(
            run=request.run,
            attempt=request.attempt,
            plan_node=request.plan_node,
            completion_contract=request.completion_contract,
            context={},
            artifact_inputs=(input_artifact(other.run_id),),
        )


def test_candidate_and_result_snapshot_bytes_and_containers() -> None:
    mutable_content = bytearray(b"candidate")
    artifact = CandidateArtifact(
        kind=ArtifactKind.CANDIDATE,
        name="candidate.bin",
        media_type="application/octet-stream",
        content=mutable_content,  # type: ignore[arg-type]
    )
    mutable_artifacts = [artifact]
    mutable_diagnostics = ["first"]
    worker_result = WorkerResult(
        artifacts=mutable_artifacts,  # type: ignore[arg-type]
        summary="candidate returned",
        raw_output=mutable_content,  # type: ignore[arg-type]
        diagnostics=mutable_diagnostics,  # type: ignore[arg-type]
    )
    mutable_content[:] = b"tampered!"
    mutable_artifacts.clear()
    mutable_diagnostics.clear()

    assert artifact.content == b"candidate"
    assert worker_result.artifacts == (artifact,)
    assert worker_result.raw_output == b"candidate"
    assert worker_result.diagnostics == ("first",)


def test_fake_worker_default_is_deterministic_and_records_calls() -> None:
    request = make_request()
    worker = FakeWorker()

    first = worker.execute(request)
    second = worker.execute(request)

    assert isinstance(worker, WorkerAdapter)
    assert first == second
    assert len(first.artifacts) == 1
    assert first.artifacts[0].kind is ArtifactKind.CANDIDATE
    assert request.attempt_id.encode() in first.artifacts[0].content
    assert worker.calls == (request, request)


def test_fake_worker_attempt_result_overrides_node_result() -> None:
    request = make_request()
    node_result = result(b"node")
    attempt_result = result(b"attempt")
    worker = FakeWorker(
        results_by_node={request.plan_node_id: node_result},
        results_by_attempt={request.attempt_id: attempt_result},
    )

    assert worker.execute(request) is attempt_result


def test_fake_worker_attempt_result_overrides_node_failure() -> None:
    request = make_request()
    attempt_result = result(b"attempt")
    worker = FakeWorker(
        results_by_attempt={request.attempt_id: attempt_result},
        failures_by_node={request.plan_node_id: "node failure"},
    )

    assert worker.execute(request) is attempt_result


@pytest.mark.parametrize("scope", ["node", "attempt"])
def test_fake_worker_raises_scoped_configured_failure(scope: str) -> None:
    request = make_request()
    options = (
        {"failures_by_node": {request.plan_node_id: "configured failure"}}
        if scope == "node"
        else {"failures_by_attempt": {request.attempt_id: "configured failure"}}
    )
    worker = FakeWorker(**options)  # type: ignore[arg-type]

    with pytest.raises(WorkerExecutionError) as exc_info:
        worker.execute(request)

    error = exc_info.value
    assert error.run_id == request.run_id
    assert error.attempt_id == request.attempt_id
    assert error.plan_node_id == request.plan_node_id
    assert error.reason == "configured failure"
    assert worker.calls == (request,)


def test_fake_worker_cancel_records_and_prevents_later_execution() -> None:
    request = make_request()
    worker = FakeWorker()

    worker.cancel(request.attempt_id)
    with pytest.raises(WorkerExecutionError, match="attempt was cancelled"):
        worker.execute(request)

    assert worker.cancel_calls == (request.attempt_id,)
    assert worker.calls == (request,)


def test_worker_result_has_no_completion_authority() -> None:
    worker_result = result()

    assert not hasattr(worker_result, "status")
    assert not hasattr(worker_result, "completed")
    assert not hasattr(worker_result, "plan_node_status")
    assert not hasattr(worker_result, "goal_status")
