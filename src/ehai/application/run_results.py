"""Pure projection of durable execution facts into the Run result view.

This module deliberately receives an already materialized :class:`ExecutionTraceView`.
It does not open a persistence session, read artifact bytes, or know about an
interface serializer.  The interface layer can therefore use the same immutable
projection for the CLI and HTTP result documents once a QueryService has supplied
one trace snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ehai import ID
from ehai.domain.events import Event, EventType

if TYPE_CHECKING:
    from ehai.application.queries import (
        CheckRunView,
        ExecutionTraceView,
        RunView,
    )


_CODE_DELIVERY_FIELDS = (
    "workspace",
    "base_commit",
    "commit",
    "diff_path",
    "attempt_id",
    "run_id",
)


class ConflictingExecutionConfigError(RuntimeError):
    """The durable Run contains incompatible execution configuration facts."""


@dataclass(frozen=True, slots=True)
class RunResultCheckResult:
    """The CheckRun evidence selected for the delivered Attempt."""

    passed: bool | None
    check_run_ids: tuple[ID, ...]
    checks: tuple[CheckRunView, ...]


@dataclass(frozen=True, slots=True)
class RunResult:
    """Durable code-delivery fields and their associated CheckRun evidence."""

    workspace: str | None
    base_commit: str | None
    commit: str | None
    diff_path: str | None
    attempt_id: ID | None
    run_id: ID
    diff_artifact_ids: tuple[ID, ...]
    check_result: RunResultCheckResult | None
    configured_workspace: str | None
    artifact_root: str


@dataclass(frozen=True, slots=True)
class RunResultTraceIds:
    """Ordered durable identifiers supporting the result projection."""

    attempt_ids: tuple[ID, ...]
    artifact_ids: tuple[ID, ...]
    check_run_ids: tuple[ID, ...]
    checkpoint_ids: tuple[ID, ...]
    event_ids: tuple[ID, ...]


@dataclass(frozen=True, slots=True)
class RunResultDocument:
    """Complete result document shared by the CLI and HTTP boundary."""

    run: RunView
    result: RunResult
    trace_ids: RunResultTraceIds


# QueryService returns the document itself; the alias keeps the endpoint-facing
# terminology distinct from the inner ``RunResult`` payload.
RunResultView = RunResultDocument


@dataclass(frozen=True, slots=True)
class RunResultResponse:
    """The standard data envelope for a Run result document."""

    data: RunResultDocument


def project_run_result(
    trace: ExecutionTraceView,
    *,
    artifact_root: str,
) -> RunResultView:
    """Project one materialized execution trace into its durable result view.

    Code-delivery facts are folded in stored-event order, matching the existing
    foreground result semantics.  CheckRuns are selected only after the final
    delivery Attempt is known: a delivered Attempt with no CheckRuns means a
    nullable ``check_result``; when no delivery exists, the latest Attempt with
    Checks is used as the historical fallback (including an empty, unknown
    result when no such Attempt exists).
    """
    run_id = trace.run.run_id
    delivery = _code_delivery(trace.events, run_id)
    workspace = configured_workspace(trace.events, run_id)
    checks = _select_checks(trace.attempts, trace.check_runs, delivery["attempt_id"])

    result = RunResult(
        workspace=cast(str | None, delivery["workspace"]),
        base_commit=cast(str | None, delivery["base_commit"]),
        commit=cast(str | None, delivery["commit"]),
        diff_path=cast(str | None, delivery["diff_path"]),
        attempt_id=cast(ID | None, delivery["attempt_id"]),
        run_id=cast(ID, delivery["run_id"]),
        diff_artifact_ids=cast(tuple[ID, ...], delivery["diff_artifact_ids"]),
        check_result=checks,
        configured_workspace=workspace,
        artifact_root=artifact_root,
    )

    return RunResultDocument(
        run=trace.run,
        result=result,
        trace_ids=RunResultTraceIds(
            attempt_ids=tuple(attempt.attempt_id for attempt in trace.attempts),
            artifact_ids=tuple(artifact.artifact_id for artifact in trace.artifacts),
            check_run_ids=tuple(check.check_run_id for check in trace.check_runs),
            checkpoint_ids=tuple(checkpoint.checkpoint_id for checkpoint in trace.checkpoints),
            event_ids=tuple(stored.event.id for stored in trace.events),
        ),
    )


def _code_delivery(events: Sequence[object], run_id: ID) -> dict[str, object]:
    """Fold valid code-delivery fields without inventing a producer."""
    delivery: dict[str, object] = {
        "workspace": None,
        "base_commit": None,
        "commit": None,
        "diff_path": None,
        "attempt_id": None,
        "run_id": run_id,
        "diff_artifact_ids": (),
    }
    for stored in events:
        event = getattr(stored, "event", None)
        if not isinstance(event, Event):
            continue
        payload = event.payload
        document = payload.get("code_delivery")
        if not isinstance(document, Mapping):
            continue
        for key in _CODE_DELIVERY_FIELDS:
            value = document.get(key)
            if isinstance(value, str):
                delivery[key] = value
        diff_artifact_ids = document.get("diff_artifact_ids")
        if isinstance(diff_artifact_ids, list) and all(
            isinstance(artifact_id, str) for artifact_id in diff_artifact_ids
        ):
            delivery["diff_artifact_ids"] = tuple(
                cast(ID, artifact_id) for artifact_id in diff_artifact_ids
            )
    return delivery


def configured_workspace(events: Sequence[object], run_id: ID) -> str | None:
    """Return the one retained execution workspace, rejecting conflicts."""
    documents: list[dict[str, object]] = []
    for stored in events:
        event = getattr(stored, "event", None)
        if (
            not isinstance(event, Event)
            or event.run_id != run_id
            or event.type is not EventType.RUN_STARTED
        ):
            continue
        document = event.payload.get("execution_config")
        if isinstance(document, dict):
            documents.append(cast(dict[str, object], document))

    if not documents:
        return None
    first = documents[0]
    if any(document != first for document in documents[1:]):
        raise ConflictingExecutionConfigError(
            f"Run {run_id} has conflicting execution config events"
        )
    workspace = first.get("workspace")
    return workspace if isinstance(workspace, str) else None


def _select_checks(
    attempts: Sequence[object],
    check_runs: Sequence[CheckRunView],
    delivery_attempt_id: object,
) -> RunResultCheckResult | None:
    """Select CheckRuns using the persisted delivery/fallback rules."""
    checks_by_attempt: dict[str, list[CheckRunView]] = {}
    for check in check_runs:
        checks_by_attempt.setdefault(str(check.attempt_id), []).append(check)

    if isinstance(delivery_attempt_id, str):
        selected = checks_by_attempt.get(delivery_attempt_id, [])
        if not selected:
            return None
    else:
        latest_attempt_id = _latest_attempt_with_checks(attempts, checks_by_attempt)
        selected = (
            checks_by_attempt.get(latest_attempt_id, []) if latest_attempt_id is not None else []
        )

    selected_checks = tuple(selected)
    return RunResultCheckResult(
        passed=_checks_passed(selected_checks),
        check_run_ids=tuple(check.check_run_id for check in selected_checks),
        checks=selected_checks,
    )


def _latest_attempt_with_checks(
    attempts: Sequence[object],
    checks_by_attempt: Mapping[str, Sequence[CheckRunView]],
) -> str | None:
    """Find the latest trace Attempt that has at least one CheckRun."""
    for attempt in reversed(attempts):
        attempt_id = getattr(attempt, "attempt_id", None)
        if isinstance(attempt_id, str) and attempt_id in checks_by_attempt:
            return attempt_id
    return None


def _checks_passed(checks: Sequence[CheckRunView]) -> bool | None:
    """Fold CheckRun verdicts: false wins, then unknown, then true."""
    has_unknown = False
    for check in checks:
        check_result = check.result
        passed = check_result.passed if check_result is not None else None
        if passed is False:
            return False
        if passed is not True:
            has_unknown = True
    return None if has_unknown else (True if checks else None)
