"""Read-only roster of completed node outputs for drafting a process revision."""

from __future__ import annotations

from ehai import ID, JsonValue
from ehai.application.ports import UnitOfWork
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckRun, CheckRunStatus, CheckSpec
from ehai.domain.execution import AttemptStatus
from ehai.domain.planning import PlanNodeStatus, PlanRevision

MAX_COMPLETED_OUTPUT_NODES = 100
MAX_OUTPUT_ARTIFACTS_PER_NODE = 20
_TEXT_LIMIT = 2_000


def completed_output_context(
    uow: UnitOfWork, run_id: ID, current: PlanRevision, checks: tuple[CheckSpec, ...]
) -> dict[str, JsonValue]:
    """List completed nodes with their retained output Artifacts and Check runs.

    Metadata only: the Planner reads Artifact bytes through a roster-bound tool.
    """
    attempts = sorted(uow.states.list_attempts(run_id), key=lambda item: item.sequence)
    latest = {
        attempt.plan_node_id: attempt.attempt_id
        for attempt in attempts
        if attempt.status is AttemptStatus.SUCCEEDED
    }
    artifacts = uow.states.list_artifacts_for_run(run_id)
    check_names = {spec.check_id: spec.name for spec in checks}
    check_runs = uow.states.list_check_runs(run_id)
    completed = [node for node in current.nodes if node.status is PlanNodeStatus.COMPLETED]
    entries: list[JsonValue] = []
    for node in completed[:MAX_COMPLETED_OUTPUT_NODES]:
        attempt_id = latest.get(node.plan_node_id)
        outputs = [
            artifact
            for artifact in artifacts
            if attempt_id is not None
            and artifact.plan_node_id == node.plan_node_id
            and artifact.attempt_id == attempt_id
            and artifact.kind is not ArtifactKind.CHECK_OUTPUT
        ]
        entry: dict[str, JsonValue] = {
            "plan_node_id": node.plan_node_id,
            "title": node.title,
            "kind": node.kind.value,
            "attempt_id": attempt_id,
            "reused_result": attempt_id is None,
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "name": artifact.name,
                    "kind": artifact.kind.value,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                }
                for artifact in outputs[:MAX_OUTPUT_ARTIFACTS_PER_NODE]
            ],
            "artifacts_truncated": len(outputs) > MAX_OUTPUT_ARTIFACTS_PER_NODE,
        }
        entries.append(entry)
    return {
        "completed_nodes": entries,
        "completed_nodes_truncated": len(completed) > MAX_COMPLETED_OUTPUT_NODES,
        "check_runs": [
            _check_entry(check, check_names.get(check.check_id)) for check in check_runs
        ],
    }


def _check_entry(check: CheckRun, name: str | None) -> dict[str, JsonValue]:
    awaiting_human = (
        check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
        and check.human_request is not None
        and check.human_decision is None
    )
    reason = check.failure_reason if check.result is None else check.result.failure_reason
    return {
        "check_run_id": check.check_run_id,
        "check_id": check.check_id,
        "name": name,
        "plan_node_id": check.plan_node_id,
        "status": "awaiting_human" if awaiting_human else check.status.value,
        "passed": None if check.result is None else check.result.passed,
        "failure_reason": None if reason is None else reason[:_TEXT_LIMIT],
        "human_comment": (
            None if check.human_decision is None else check.human_decision.comment[:_TEXT_LIMIT]
        ),
    }
