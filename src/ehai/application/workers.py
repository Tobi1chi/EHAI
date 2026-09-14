"""P1 Worker request/result boundary owned by the application layer."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol, runtime_checkable

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckSpec
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision

_MAX_ERROR_OUTPUT_BYTES = 1_048_576
_MAX_ERROR_REASON_CHARACTERS = 2_000
_MAX_ERROR_DIAGNOSTICS = 32
_MAX_DIAGNOSTIC_CHARACTERS = 500
_ERROR_TRUNCATION_MARKER = b"\n...[truncated]...\n"
MAX_ARTIFACT_INPUT_BYTES = 256 * 1024
MAX_ARTIFACT_INPUT_TOTAL_BYTES = 1024 * 1024
MAX_WORKER_CONTEXT_BYTES = 1024 * 1024
MAX_CANDIDATE_ARTIFACT_BYTES = 1024 * 1024
MAX_CANDIDATE_TOTAL_BYTES = 3 * 1024 * 1024
MAX_WORKER_SUMMARY_BYTES = 64 * 1024
_ARTIFACT_INPUT_ENCODINGS = frozenset({"utf-8", "base64"})


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


class ArtifactInputBudgetExceeded(ValueError):
    """Raised when dependency Artifact content cannot be safely snapshotted."""


@dataclass(frozen=True, slots=True)
class ArtifactInputSnapshot:
    """A bounded immutable Artifact content snapshot for one Worker invocation."""

    artifact_id: ID
    plan_node_id: ID
    name: str
    media_type: str
    sha256: str
    encoding: str
    content: str
    kind: ArtifactKind
    size_bytes: int
    run_id: ID | None = None
    attempt_id: ID | None = None
    adoption: ResultAdoption | None = None

    @classmethod
    def from_artifact(
        cls,
        artifact: Artifact,
        content: bytes,
        adoption: ResultAdoption | None = None,
    ) -> ArtifactInputSnapshot:
        """Build and verify a content snapshot from trusted Artifact metadata and bytes."""
        if not isinstance(artifact, Artifact):
            raise TypeError("artifact must be an Artifact")
        if not isinstance(content, bytes):
            raise TypeError("Artifact input content must be bytes")
        if artifact.plan_node_id is None:
            raise ValueError(f"Artifact {artifact.artifact_id} has no PlanNode scope")
        if len(content) != artifact.size_bytes or sha256(content).hexdigest() != artifact.sha256:
            raise ValueError(f"Artifact {artifact.artifact_id} content does not match metadata")
        try:
            encoded_content = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoded_content = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        return cls(
            artifact_id=artifact.artifact_id,
            plan_node_id=artifact.plan_node_id,
            name=artifact.name,
            media_type=artifact.media_type,
            sha256=artifact.sha256,
            encoding=encoding,
            content=encoded_content,
            kind=artifact.kind,
            size_bytes=artifact.size_bytes,
            run_id=artifact.run_id,
            attempt_id=artifact.attempt_id,
            adoption=adoption,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", normalize_id(self.artifact_id))
        object.__setattr__(self, "plan_node_id", normalize_id(self.plan_node_id))
        object.__setattr__(
            self, "run_id", None if self.run_id is None else normalize_id(self.run_id)
        )
        object.__setattr__(
            self,
            "attempt_id",
            None if self.attempt_id is None else normalize_id(self.attempt_id),
        )
        object.__setattr__(self, "kind", ArtifactKind(self.kind))
        if not isinstance(self.name, str) or not self.name.strip() or "\x00" in self.name:
            raise ValueError("Artifact input name must not be blank or contain NUL")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type.strip()
            or "/" not in self.media_type
            or any(character in self.media_type for character in "\r\n\x00")
        ):
            raise ValueError("Artifact input media_type is invalid")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in self.sha256)
        ):
            raise ValueError("Artifact input sha256 must be 64 hexadecimal digits")
        object.__setattr__(self, "sha256", self.sha256.lower())
        if self.encoding not in _ARTIFACT_INPUT_ENCODINGS:
            raise ValueError("Artifact input encoding must be utf-8 or base64")
        if not isinstance(self.content, str):
            raise TypeError("Artifact input content must be text")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("Artifact input size_bytes must be non-negative")
        decoded = self._decoded_content()
        if len(decoded) != self.size_bytes:
            raise ValueError("Artifact input content size does not match size_bytes")
        if sha256(decoded).hexdigest() != self.sha256:
            raise ValueError("Artifact input content SHA-256 does not match metadata")
        adoption = self.adoption
        if adoption is not None:
            if not isinstance(adoption, ResultAdoption):
                raise TypeError("Artifact input adoption must be a ResultAdoption or None")
            if (
                self.run_id != adoption.source_run_id
                or self.plan_node_id != adoption.source_plan_node_id
                or self.attempt_id != adoption.source_attempt_id
            ):
                raise ValueError("Artifact input source identity does not match its adoption")
            evidence = next(
                (item for item in adoption.evidence if item.artifact_id == self.artifact_id),
                None,
            )
            if evidence is None or evidence.sha256 != self.sha256:
                raise ValueError("Artifact input is not the adopted evidence version")
            if self.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}:
                raise ValueError("Adopted Artifact input must be a candidate or patch")

    @property
    def effective_run_id(self) -> ID | None:
        """Return the target Run identity used for dependency mapping."""
        return self.run_id if self.adoption is None else self.adoption.target_run_id

    @property
    def effective_plan_node_id(self) -> ID:
        """Return the target PlanNode identity used for dependency mapping."""
        return self.plan_node_id if self.adoption is None else self.adoption.target_plan_node_id

    def to_prompt_dict(self) -> dict[str, JsonValue]:
        """Return the stable JSON form exposed to Workers."""
        payload: dict[str, JsonValue] = {
            "artifact_id": self.artifact_id,
            "plan_node_id": self.plan_node_id,
            "name": self.name,
            "kind": self.kind.value,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "encoding": self.encoding,
            "content": self.content,
        }
        if self.adoption is not None:
            payload["adoption"] = {
                "adoption_id": self.adoption.adoption_id,
                "target_run_id": self.adoption.target_run_id,
                "target_plan_node_id": self.adoption.target_plan_node_id,
                "source_run_id": self.adoption.source_run_id,
                "source_plan_node_id": self.adoption.source_plan_node_id,
                "source_attempt_id": self.adoption.source_attempt_id,
            }
        return payload

    def _decoded_content(self) -> bytes:
        if self.encoding == "utf-8":
            return self.content.encode("utf-8")
        try:
            return base64.b64decode(self.content.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as error:
            raise ValueError("Artifact input base64 content is invalid") from error


@dataclass(frozen=True, slots=True)
class AdoptedResultInputs:
    """Host input-applicability request, deliberately without a synthetic Attempt."""

    run: Run
    plan_revision: PlanRevision
    plan_node: PlanNode
    adoption: ResultAdoption
    artifact_inputs: tuple[ArtifactInputSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.adoption.target_run_id != self.run.run_id
            or self.adoption.target_plan_revision_id != self.run.plan_revision_id
            or self.adoption.target_plan_node_id != self.plan_node.plan_node_id
            or self.adoption.source_run_id != self.run.predecessor_run_id
            or self.plan_revision.plan_revision_id != self.run.plan_revision_id
            or self.plan_revision.goal_id != self.run.goal_id
            or self.plan_node not in self.plan_revision.nodes
        ):
            raise ValueError("Adopted input request does not match its target execution scope")
        object.__setattr__(self, "artifact_inputs", tuple(self.artifact_inputs))
        for artifact in self.artifact_inputs:
            if artifact.effective_run_id != self.run.run_id:
                raise ValueError("Adopted input request contains an unbound foreign Artifact")
            if artifact.adoption is not None and (
                artifact.adoption.target_plan_revision_id != self.run.plan_revision_id
                or artifact.adoption.source_run_id != self.run.predecessor_run_id
            ):
                raise ValueError("Adopted input request contains an unrelated adoption")

    @property
    def run_id(self) -> ID:
        return self.run.run_id

    @property
    def plan_node_id(self) -> ID:
        return self.plan_node.plan_node_id


@dataclass(frozen=True, slots=True, init=False)
class WorkerRequest:
    """A validated, immutable input snapshot for one Worker invocation."""

    run: Run
    attempt: Attempt
    plan_node: PlanNode
    completion_contract: CompletionContract
    required_check_specs: tuple[CheckSpec, ...]
    artifact_inputs: tuple[ArtifactInputSnapshot, ...]
    _context_json: str = field(repr=False)

    def __init__(
        self,
        *,
        run: Run,
        attempt: Attempt,
        plan_node: PlanNode,
        completion_contract: CompletionContract,
        required_check_specs: tuple[CheckSpec, ...],
        context: Mapping[str, JsonValue],
        artifact_inputs: tuple[ArtifactInputSnapshot, ...] = (),
    ) -> None:
        if not isinstance(run, Run):
            raise ValueError("WorkerRequest run must be a Run")
        if not isinstance(attempt, Attempt):
            raise ValueError("WorkerRequest attempt must be an Attempt")
        if not isinstance(plan_node, PlanNode):
            raise ValueError("WorkerRequest plan_node must be a PlanNode")
        if not isinstance(completion_contract, CompletionContract):
            raise ValueError("WorkerRequest completion_contract must be a CompletionContract")
        checks = tuple(required_check_specs)
        if not all(isinstance(check, CheckSpec) for check in checks):
            raise ValueError("WorkerRequest required_check_specs must contain CheckSpecs")
        if not isinstance(context, Mapping):
            raise ValueError("WorkerRequest context must be a JSON object")

        artifacts = tuple(artifact_inputs)
        if not all(isinstance(artifact, ArtifactInputSnapshot) for artifact in artifacts):
            raise ValueError(
                "WorkerRequest artifact_inputs must contain only ArtifactInputSnapshots"
            )
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
        check_ids = tuple(check.check_id for check in checks)
        if len(set(check_ids)) != len(check_ids):
            raise ValueError(f"{owner} required CheckSpecs must not contain duplicate IDs")
        if set(check_ids) != set(plan_node.required_check_ids):
            raise ValueError(f"{owner} required CheckSpecs do not match its PlanNode")
        if any(not check.required for check in checks):
            raise ValueError(f"{owner} required CheckSpecs cannot be optional")
        foreign_artifacts = tuple(
            artifact.artifact_id
            for artifact in artifacts
            if (
                artifact.adoption is None
                and artifact.run_id is not None
                and artifact.run_id != run.run_id
            )
            or (
                artifact.adoption is not None
                and (
                    artifact.effective_run_id != run.run_id
                    or artifact.adoption.target_plan_revision_id != run.plan_revision_id
                    or artifact.adoption.source_run_id != run.predecessor_run_id
                )
            )
        )
        if foreign_artifacts:
            raise ValueError(f"{owner} contains Artifacts from another Run: {foreign_artifacts}")

        object.__setattr__(self, "run", run)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "plan_node", plan_node)
        object.__setattr__(self, "completion_contract", completion_contract)
        object.__setattr__(self, "required_check_specs", checks)
        object.__setattr__(self, "artifact_inputs", artifacts)
        context_json = json_dumps(dict(context))
        if len(context_json.encode("utf-8")) > MAX_WORKER_CONTEXT_BYTES:
            raise ArtifactInputBudgetExceeded("Worker context exceeds the P2 bounded context limit")
        object.__setattr__(self, "_context_json", context_json)

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
        if len(self.summary.encode("utf-8")) > MAX_WORKER_SUMMARY_BYTES:
            raise ValueError("WorkerResult summary exceeds the P2 evidence limit")
        artifact_sizes = tuple(len(artifact.content) for artifact in artifacts)
        if any(size > MAX_CANDIDATE_ARTIFACT_BYTES for size in artifact_sizes):
            raise ValueError("WorkerResult Artifact exceeds the P2 evidence limit")
        if sum(artifact_sizes) > MAX_CANDIDATE_TOTAL_BYTES:
            raise ValueError("WorkerResult Artifacts exceed the P2 total evidence limit")
        if not isinstance(self.raw_output, (bytes, bytearray, memoryview)):
            raise TypeError("WorkerResult raw_output must be bytes-like")
        if any(not isinstance(item, str) or not item.strip() for item in diagnostics):
            raise ValueError("WorkerResult diagnostics must contain non-blank strings")
        if len(self.raw_output) > _MAX_ERROR_OUTPUT_BYTES:
            raise ValueError("WorkerResult raw_output exceeds the P2 log limit")
        if len(diagnostics) > _MAX_ERROR_DIAGNOSTICS or any(
            len(item) > _MAX_DIAGNOSTIC_CHARACTERS for item in diagnostics
        ):
            raise ValueError("WorkerResult diagnostics exceed the P2 log limit")
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
