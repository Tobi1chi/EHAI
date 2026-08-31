"""P1 Worker request/result boundary owned by the application layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode, PlanNodeStatus

_MAX_ERROR_OUTPUT_BYTES = 1_048_576
_MAX_ERROR_REASON_CHARACTERS = 2_000
_MAX_ERROR_DIAGNOSTICS = 32
_MAX_DIAGNOSTIC_CHARACTERS = 500
_ERROR_TRUNCATION_MARKER = b"\n...[truncated]...\n"


class WorkerExecutionError(RuntimeError):
    """A Worker failure tied to one Run, Attempt, and PlanNode."""

    def __init__(
        self,
        run_id: ID,
        attempt_id: ID,
        plan_node_id: ID,
        reason: str,
        *,
        raw_output: bytes = b"",
        log_output: bytes = b"",
        diagnostics: tuple[str, ...] = (),
    ) -> None:
        self.run_id = _validated_id(run_id, "run_id")
        self.attempt_id = _validated_id(attempt_id, "attempt_id")
        self.plan_node_id = _validated_id(plan_node_id, "plan_node_id")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Worker failure reason must not be blank")
        self.reason = _bounded_text(reason, _MAX_ERROR_REASON_CHARACTERS)
        self.raw_output = _bounded_bytes(raw_output, "raw_output")
        self.log_output = _bounded_bytes(log_output, "log_output")
        normalized_diagnostics = tuple(diagnostics)
        if any(not isinstance(item, str) or not item.strip() for item in normalized_diagnostics):
            raise ValueError("Worker failure diagnostics must contain non-blank strings")
        self.diagnostics = tuple(
            _bounded_text(item, _MAX_DIAGNOSTIC_CHARACTERS)
            for item in normalized_diagnostics[:_MAX_ERROR_DIAGNOSTICS]
        )
        super().__init__(
            f"worker failed for run {self.run_id}, attempt {self.attempt_id}, "
            f"node {self.plan_node_id}: {self.reason}"
        )


class WorkerTimedOutError(WorkerExecutionError):
    """A Worker exceeded its application wall-clock deadline."""


class WorkerCancelledError(WorkerExecutionError):
    """A Worker stopped because cancellation was requested."""


@dataclass(frozen=True, slots=True, init=False)
class WorkerRequest:
    """A validated, immutable input snapshot for one Worker invocation."""

    run: Run
    attempt: Attempt
    plan_node: PlanNode
    completion_contract: CompletionContract
    artifact_inputs: tuple[Artifact, ...]
    _context_json: str = field(repr=False)

    def __init__(
        self,
        *,
        run: Run,
        attempt: Attempt,
        plan_node: PlanNode,
        completion_contract: CompletionContract,
        context: Mapping[str, JsonValue],
        artifact_inputs: tuple[Artifact, ...] = (),
    ) -> None:
        if not isinstance(run, Run):
            raise ValueError("WorkerRequest run must be a Run")
        if not isinstance(attempt, Attempt):
            raise ValueError("WorkerRequest attempt must be an Attempt")
        if not isinstance(plan_node, PlanNode):
            raise ValueError("WorkerRequest plan_node must be a PlanNode")
        if not isinstance(completion_contract, CompletionContract):
            raise ValueError("WorkerRequest completion_contract must be a CompletionContract")
        if not isinstance(context, Mapping):
            raise ValueError("WorkerRequest context must be a JSON object")

        artifacts = tuple(artifact_inputs)
        if not all(isinstance(artifact, Artifact) for artifact in artifacts):
            raise ValueError("WorkerRequest artifact_inputs must contain only Artifacts")
        if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise ValueError("WorkerRequest artifact_inputs must not contain duplicate IDs")

        owner = f"WorkerRequest for Attempt {attempt.attempt_id}"
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"{owner} requires a running Run")
        if attempt.status is not AttemptStatus.RUNNING:
            raise ValueError(f"{owner} requires a running Attempt")
        if plan_node.status is not PlanNodeStatus.RUNNING:
            raise ValueError(f"{owner} requires a running PlanNode")
        if attempt.run_id != run.run_id:
            raise ValueError(f"{owner} Attempt belongs to another Run")
        if attempt.plan_node_id != plan_node.plan_node_id:
            raise ValueError(f"{owner} Attempt belongs to another PlanNode")
        if not completion_contract.is_confirmed:
            raise ValueError(f"{owner} requires a confirmed CompletionContract")
        if completion_contract.goal_id != run.goal_id:
            raise ValueError(f"{owner} CompletionContract belongs to another Goal")
        foreign_artifacts = tuple(
            artifact.artifact_id
            for artifact in artifacts
            if artifact.run_id is not None and artifact.run_id != run.run_id
        )
        if foreign_artifacts:
            raise ValueError(f"{owner} contains Artifacts from another Run: {foreign_artifacts}")

        object.__setattr__(self, "run", run)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "plan_node", plan_node)
        object.__setattr__(self, "completion_contract", completion_contract)
        object.__setattr__(self, "artifact_inputs", artifacts)
        object.__setattr__(self, "_context_json", json_dumps(dict(context)))

    @property
    def run_id(self) -> ID:
        """Return the invocation Run ID."""
        return self.run.run_id

    @property
    def attempt_id(self) -> ID:
        """Return the invocation Attempt ID."""
        return self.attempt.attempt_id

    @property
    def plan_node_id(self) -> ID:
        """Return the invocation PlanNode ID."""
        return self.plan_node.plan_node_id

    @property
    def context(self) -> dict[str, JsonValue]:
        """Return an isolated JSON-compatible copy of Worker context."""
        decoded = json_loads(self._context_json)
        if not isinstance(decoded, dict):  # pragma: no cover - guarded by construction
            raise RuntimeError("stored WorkerRequest context is not a JSON object")
        return decoded


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    """Candidate bytes returned by a Worker before Artifact persistence or checks."""

    kind: ArtifactKind
    name: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ArtifactKind(self.kind))
        if not isinstance(self.name, str) or not self.name.strip() or "\x00" in self.name:
            raise ValueError("CandidateArtifact name must not be blank or contain NUL")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type.strip()
            or "/" not in self.media_type
            or any(character in self.media_type for character in "\r\n\x00")
        ):
            raise ValueError("CandidateArtifact media_type is invalid")
        if not isinstance(self.content, (bytes, bytearray, memoryview)):
            raise TypeError("CandidateArtifact content must be bytes-like")
        object.__setattr__(self, "content", bytes(self.content))


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """A successful Worker return containing candidates, never completion state."""

    artifacts: tuple[CandidateArtifact, ...]
    summary: str
    raw_output: bytes = b""
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        artifacts = tuple(self.artifacts)
        diagnostics = tuple(self.diagnostics)
        if not artifacts or not all(
            isinstance(artifact, CandidateArtifact) for artifact in artifacts
        ):
            raise ValueError("WorkerResult requires at least one CandidateArtifact")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("WorkerResult summary must not be blank")
        if not isinstance(self.raw_output, (bytes, bytearray, memoryview)):
            raise TypeError("WorkerResult raw_output must be bytes-like")
        if any(not isinstance(item, str) or not item.strip() for item in diagnostics):
            raise ValueError("WorkerResult diagnostics must contain non-blank strings")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "raw_output", bytes(self.raw_output))
        object.__setattr__(self, "diagnostics", diagnostics)


@runtime_checkable
class WorkerAdapter(Protocol):
    """P1 boundary for executing and cancelling one Worker Attempt."""

    def execute(self, request: WorkerRequest) -> WorkerResult:
        """Execute one request and return candidates without completing domain state."""
        ...

    def cancel(self, attempt_id: ID) -> None:
        """Request cancellation of one Attempt by ID."""
        ...


def _validated_id(value: ID, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"Worker has invalid {field_name}: {error}") from error


def _bounded_bytes(value: bytes, field_name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"Worker failure {field_name} must be bytes-like")
    snapshot = bytes(value)
    if len(snapshot) <= _MAX_ERROR_OUTPUT_BYTES:
        return snapshot
    retained = _MAX_ERROR_OUTPUT_BYTES - len(_ERROR_TRUNCATION_MARKER)
    return snapshot[:retained] + _ERROR_TRUNCATION_MARKER


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 16] + "...[truncated]"
