"""Deterministic, in-memory Worker used by P1 tests."""

from __future__ import annotations

from collections.abc import Mapping

from ehai import ID, normalize_id
from ehai.application.workers import (
    CandidateArtifact,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
)
from ehai.domain.artifacts import ArtifactKind


class FakeWorker:
    """Return configured candidates or scoped failures without external effects."""

    def __init__(
        self,
        *,
        results_by_node: Mapping[ID, WorkerResult] | None = None,
        results_by_attempt: Mapping[ID, WorkerResult] | None = None,
        failures_by_node: Mapping[ID, str] | None = None,
        failures_by_attempt: Mapping[ID, str] | None = None,
    ) -> None:
        self._results_by_node = _result_mapping(results_by_node, "results_by_node")
        self._results_by_attempt = _result_mapping(results_by_attempt, "results_by_attempt")
        self._failures_by_node = _failure_mapping(failures_by_node, "failures_by_node")
        self._failures_by_attempt = _failure_mapping(failures_by_attempt, "failures_by_attempt")
        _reject_ambiguous_outcomes(
            self._results_by_node,
            self._failures_by_node,
            "node",
        )
        _reject_ambiguous_outcomes(
            self._results_by_attempt,
            self._failures_by_attempt,
            "attempt",
        )
        self._calls: list[WorkerRequest] = []
        self._cancel_calls: list[ID] = []
        self._cancelled_attempt_ids: set[ID] = set()

    @property
    def calls(self) -> tuple[WorkerRequest, ...]:
        """Return an immutable snapshot of execution calls in arrival order."""
        return tuple(self._calls)

    @property
    def cancel_calls(self) -> tuple[ID, ...]:
        """Return an immutable snapshot of cancellation calls."""
        return tuple(self._cancel_calls)

    def execute(self, request: WorkerRequest) -> WorkerResult:
        """Return an attempt override, node override, or deterministic default."""
        if not isinstance(request, WorkerRequest):
            raise TypeError("FakeWorker request must be a WorkerRequest")
        self._calls.append(request)

        if request.attempt_id in self._cancelled_attempt_ids:
            raise _execution_error(request, "attempt was cancelled")
        attempt_failure = self._failures_by_attempt.get(request.attempt_id)
        if attempt_failure is not None:
            raise _execution_error(request, attempt_failure)
        attempt_result = self._results_by_attempt.get(request.attempt_id)
        if attempt_result is not None:
            return attempt_result

        node_failure = self._failures_by_node.get(request.plan_node_id)
        if node_failure is not None:
            raise _execution_error(request, node_failure)
        node_result = self._results_by_node.get(request.plan_node_id)
        return self._default_result(request) if node_result is None else node_result

    def cancel(self, attempt_id: ID) -> None:
        """Record cancellation without touching a process or filesystem."""
        normalized_id = normalize_id(attempt_id)
        self._cancel_calls.append(normalized_id)
        self._cancelled_attempt_ids.add(normalized_id)

    @staticmethod
    def _default_result(request: WorkerRequest) -> WorkerResult:
        content = (
            f"fake candidate for run={request.run_id} attempt={request.attempt_id} "
            f"node={request.plan_node_id}"
        ).encode()
        return WorkerResult(
            artifacts=(
                CandidateArtifact(
                    kind=ArtifactKind.CANDIDATE,
                    name=f"{request.plan_node_id}.txt",
                    media_type="text/plain; charset=utf-8",
                    content=content,
                ),
            ),
            summary=f"fake worker returned a candidate for {request.plan_node.title}",
            raw_output=content,
            diagnostics=("deterministic fake worker result",),
        )


def _execution_error(request: WorkerRequest, reason: str) -> WorkerExecutionError:
    return WorkerExecutionError(
        request.run_id,
        request.attempt_id,
        request.plan_node_id,
        reason,
    )


def _result_mapping(
    values: Mapping[ID, WorkerResult] | None,
    field_name: str,
) -> dict[ID, WorkerResult]:
    normalized: dict[ID, WorkerResult] = {}
    for key, result in ({} if values is None else values).items():
        normalized_id = normalize_id(key)
        if normalized_id in normalized:
            raise ValueError(f"{field_name} contains duplicate normalized IDs")
        if not isinstance(result, WorkerResult):
            raise TypeError(f"{field_name} values must be WorkerResults")
        normalized[normalized_id] = result
    return normalized


def _failure_mapping(
    values: Mapping[ID, str] | None,
    field_name: str,
) -> dict[ID, str]:
    normalized: dict[ID, str] = {}
    for key, reason in ({} if values is None else values).items():
        normalized_id = normalize_id(key)
        if normalized_id in normalized:
            raise ValueError(f"{field_name} contains duplicate normalized IDs")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{field_name} reasons must not be blank")
        normalized[normalized_id] = reason
    return normalized


def _reject_ambiguous_outcomes(
    results: Mapping[ID, WorkerResult],
    failures: Mapping[ID, str],
    scope: str,
) -> None:
    overlap = set(results).intersection(failures)
    if overlap:
        raise ValueError(f"FakeWorker has ambiguous {scope} outcomes for {sorted(overlap)}")
