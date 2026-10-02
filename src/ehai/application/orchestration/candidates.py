"""Validate and persist Worker candidates, error artifacts and receipts."""

from __future__ import annotations

from hashlib import sha256

from ehai import ID, JsonValue, json_loads
from ehai.application.orchestration.common import (
    OrchestrationError,
    WorkerEventReceipt,
    _ExecutionContext,
    _replace_node,
    _required_attempt,
    _required_node,
    _required_plan,
    _required_run,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.ports import UnitOfWork
from ehai.application.review import validate_review_submission
from ehai.application.workers import (
    WorkerExecutionError,
    WorkerResult,
)
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run
from ehai.domain.planning import (
    PlanNodeKind,
)


class CandidatesMixin:
    """Validate and persist Worker candidates, error artifacts and receipts."""

    def _validate_reviewer_result(
        self: OrchestratorHost, context: _ExecutionContext, result: WorkerResult
    ) -> None:
        if context.plan_node.kind is not PlanNodeKind.REVIEWER:
            return
        candidates = tuple(
            artifact for artifact in result.artifacts if artifact.kind is ArtifactKind.CANDIDATE
        )
        if len(candidates) != 1:
            raise ValueError("Reviewer must submit exactly one candidate review.json Artifact")
        review = candidates[0]
        validate_review_submission(
            {
                "name": review.name,
                "media_type": review.media_type,
                "content": review.content.decode("utf-8"),
            },
            (artifact.artifact_id for artifact in self._worker_inputs(context)),
        )

    def _store_candidate_artifacts(
        self: OrchestratorHost,
        context: _ExecutionContext,
        result: WorkerResult,
    ) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        for candidate in result.artifacts:
            artifacts.append(
                self._store_artifact(
                    context,
                    kind=candidate.kind,
                    name=candidate.name,
                    media_type=candidate.media_type,
                    content=candidate.content,
                )
            )
        return tuple(artifacts)

    def _store_worker_error_artifacts(
        self: OrchestratorHost,
        context: _ExecutionContext,
        error: WorkerExecutionError,
    ) -> tuple[Artifact, ...]:
        payloads = (
            (
                ArtifactKind.WORKER_OUTPUT,
                "worker-error-output.txt",
                error.raw_output,
            ),
            (ArtifactKind.LOG, "worker-error-stderr.log", error.log_output),
            (
                ArtifactKind.LOG,
                "worker-error-diagnostics.log",
                "\n".join(error.diagnostics).encode("utf-8"),
            ),
        )
        return tuple(
            self._store_artifact(
                context,
                kind=kind,
                name=name,
                media_type="text/plain; charset=utf-8",
                content=content,
            )
            for kind, name, content in payloads
            if content
        )

    def _store_artifact(
        self: OrchestratorHost,
        context: _ExecutionContext,
        *,
        kind: ArtifactKind,
        name: str,
        media_type: str,
        content: bytes,
    ) -> Artifact:
        artifact_id = self._id_factory()
        artifact = Artifact(
            artifact_id=artifact_id,
            kind=kind,
            name=name,
            media_type=media_type,
            size_bytes=len(content),
            sha256=sha256(content).hexdigest(),
            relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
            created_at=self._clock(),
            run_id=context.run.run_id,
            plan_node_id=context.plan_node.plan_node_id,
            attempt_id=context.attempt.attempt_id,
        )
        self._artifact_store.put(artifact, content)
        return artifact

    def _record_candidate(
        self: OrchestratorHost,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        result: WorkerResult,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> None:
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            evidence_artifacts = tuple(
                artifact
                for artifact in artifacts
                if artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
            succeeded = self._apply_worker_event_receipts(
                uow,
                attempt.succeed(
                    tuple(artifact.artifact_id for artifact in evidence_artifacts),
                    at=self._clock(),
                ),
                worker_event_receipts,
            )
            candidate_node = node.submit_candidate()
            plan = _replace_node(plan, candidate_node)
            for artifact in artifacts:
                uow.states.put_artifact(artifact)
                uow.events.append(
                    self._event(
                        EventType.ARTIFACT_CREATED,
                        run,
                        artifact.artifact_id,
                        {
                            "artifact_id": artifact.artifact_id,
                            "attempt_id": attempt.attempt_id,
                        },
                    )
                )
            uow.states.put_attempt(succeeded)
            uow.states.put_execution_plan(run.run_id, plan)
            outcome: dict[str, JsonValue] = {
                "attempt_id": attempt.attempt_id,
                "summary": result.summary,
            }
            if self._execution_workspace is not None:
                for candidate in result.artifacts:
                    if candidate.media_type != "application/vnd.ehai.code-snapshot+json":
                        continue
                    delivery = json_loads(candidate.content.decode("utf-8"))
                    if not isinstance(delivery, dict):
                        raise OrchestrationError("Host code snapshot must be an object")
                    delivery["diff_artifact_ids"] = [
                        artifact.artifact_id
                        for artifact in artifacts
                        if artifact.name == "solution.patch"
                    ]
                    outcome["code_delivery"] = delivery
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_SUCCEEDED,
                    run,
                    attempt.attempt_id,
                    outcome,
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_CANDIDATE_SUBMITTED,
                    run,
                    candidate_node.plan_node_id,
                    {"plan_node_id": candidate_node.plan_node_id},
                )
            )
            uow.commit()

    def _apply_worker_event_receipts(
        self: OrchestratorHost,
        uow: UnitOfWork,
        attempt: Attempt,
        receipts: tuple[WorkerEventReceipt, ...],
    ) -> Attempt:
        if not receipts:
            return attempt
        for receipt in receipts:
            is_new = uow.states.record_worker_event(
                attempt.attempt_id,
                receipt.worker_event_id,
                at=receipt.occurred_at,
            )
            if is_new and receipt.records_usage:
                uow.events.append(
                    Event(
                        type=EventType.PROVIDER_USAGE_RECORDED,
                        correlation_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        payload={
                            "attempt_id": attempt.attempt_id,
                            "provider_cost": receipt.provider_cost,
                            "provider_cost_available": receipt.provider_cost is not None,
                        },
                        occurred_at=receipt.occurred_at,
                    )
                )
        return attempt.record_terminal_cursor(receipts[-1].cursor)

    def _record_artifacts(
        self: OrchestratorHost,
        uow: UnitOfWork,
        run: Run,
        attempt: Attempt,
        artifacts: tuple[Artifact, ...],
    ) -> None:
        for artifact in artifacts:
            uow.states.put_artifact(artifact)
            uow.events.append(
                self._event(
                    EventType.ARTIFACT_CREATED,
                    run,
                    artifact.artifact_id,
                    {
                        "artifact_id": artifact.artifact_id,
                        "attempt_id": attempt.attempt_id,
                    },
                )
            )

    def _record_artifact_failure(
        self: OrchestratorHost,
        attempt_id: ID,
        result: WorkerResult,
        error: Exception,
    ) -> None:
        reason = f"Artifact persistence failed: {type(error).__name__}: {error}"
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            succeeded_attempt = attempt.succeed((), at=self._clock())
            failed_node = node.fail()
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_attempt(succeeded_attempt)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, failed_node))
            uow.states.put_run(failed_run)
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_SUCCEEDED,
                    run,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "summary": result.summary},
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": reason},
                )
            )
            uow.events.append(
                self._event(
                    EventType.RUN_FAILED,
                    failed_run,
                    run.run_id,
                    {"run_id": run.run_id, "reason": reason},
                )
            )
            uow.commit()
