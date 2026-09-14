"""Checks, the P1 all-required Gate, and recoverable Checkpoints."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Self

from ehai import ID, JsonValue, json_dumps, new_id, normalize_id, utc_now
from ehai.domain.execution import Run, RunStatus
from ehai.domain.planning import BranchStatus, PlanNodeKind, PlanRevision, PlanRevisionStatus

_REHYDRATE = object()
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class CheckKind(StrEnum):
    """Checker implementations supported by P1."""

    COMMAND = "command"
    ARTIFACT = "artifact"
    SEMANTIC = "semantic"
    HUMAN = "human"


class CheckRunStatus(StrEnum):
    """Execution status of a Checker invocation, separate from its verdict."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class GatePolicy(StrEnum):
    """Gate policies available in P1."""

    ALL_REQUIRED = "all-required"


class InvalidCheckRunTransition(ValueError):
    """Raised when a CheckRun is asked to perform an illegal transition."""

    def __init__(
        self,
        check_run_id: ID,
        run_id: ID,
        plan_node_id: ID,
        source: CheckRunStatus,
        target: CheckRunStatus,
    ) -> None:
        self.check_run_id = check_run_id
        self.run_id = run_id
        self.plan_node_id = plan_node_id
        self.source = source
        self.target = target
        super().__init__(
            f"check run {check_run_id} for run {run_id} and node {plan_node_id}: "
            f"cannot transition from {source.value} to {target.value}"
        )


_CHECK_RUN_TRANSITIONS: dict[CheckRunStatus, frozenset[CheckRunStatus]] = {
    CheckRunStatus.PENDING: frozenset({CheckRunStatus.RUNNING, CheckRunStatus.CANCELLED}),
    CheckRunStatus.RUNNING: frozenset(
        {
            CheckRunStatus.COMPLETED,
            CheckRunStatus.FAILED,
            CheckRunStatus.TIMED_OUT,
            CheckRunStatus.CANCELLED,
            CheckRunStatus.INTERRUPTED,
        }
    ),
    CheckRunStatus.COMPLETED: frozenset(),
    CheckRunStatus.FAILED: frozenset(),
    CheckRunStatus.TIMED_OUT: frozenset(),
    CheckRunStatus.CANCELLED: frozenset(),
    CheckRunStatus.INTERRUPTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """A versioned completion condition evaluated by a Checker Adapter."""

    name: str
    kind: CheckKind
    description: str
    required: bool = True
    check_id: ID = field(default_factory=new_id)
    command_argv: tuple[str, ...] = ()
    semantic_required_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _validated_id(self.check_id, "check_id"))
        object.__setattr__(self, "kind", CheckKind(self.kind))
        if not self.name.strip():
            raise ValueError(f"check {self.check_id}: name must not be blank")
        if not self.description.strip():
            raise ValueError(f"check {self.check_id}: description must not be blank")
        if isinstance(self.command_argv, str):
            raise ValueError(
                f"check {self.check_id}: command_argv must be an argv sequence, not shell text"
            )
        argv = tuple(self.command_argv)
        if any(
            not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
        ):
            raise ValueError(f"check {self.check_id}: command_argv contains an invalid argument")
        if isinstance(self.semantic_required_terms, str):
            raise ValueError(
                f"check {self.check_id}: semantic_required_terms must be a term sequence"
            )
        terms = tuple(self.semantic_required_terms)
        if any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError(
                f"check {self.check_id}: semantic_required_terms contains a blank term"
            )
        normalized_terms = tuple(term.strip().casefold() for term in terms)
        if len(set(normalized_terms)) != len(normalized_terms):
            raise ValueError(f"check {self.check_id}: semantic_required_terms contains duplicates")
        if argv and self.kind is not CheckKind.COMMAND:
            raise ValueError(f"check {self.check_id}: command_argv requires a Command Check")
        if normalized_terms and self.kind is not CheckKind.SEMANTIC:
            raise ValueError(
                f"check {self.check_id}: semantic_required_terms requires a Semantic Check"
            )
        object.__setattr__(self, "command_argv", argv)
        object.__setattr__(self, "semantic_required_terms", normalized_terms)


@dataclass(frozen=True, slots=True)
class HumanCheckEvidence:
    """One immutable artifact version presented for a human Check."""

    artifact_id: ID
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _validated_id(self.artifact_id, "artifact_id"))
        if not isinstance(self.sha256, str):
            raise ValueError("sha256 must be a string")
        canonical_hash = self.sha256.lower()
        if _SHA256_PATTERN.fullmatch(canonical_hash) is None:
            raise ValueError("sha256 must be 64 hexadecimal digits")
        object.__setattr__(self, "sha256", canonical_hash)


@dataclass(frozen=True, slots=True)
class HumanCheckRequest:
    """The approved-version and artifact snapshot awaiting human judgment."""

    plan_revision_id: ID
    completion_contract_id: ID
    completion_contract_version: int
    question: str
    evidence: tuple[HumanCheckEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_revision_id",
            _validated_id(self.plan_revision_id, "plan_revision_id"),
        )
        object.__setattr__(
            self,
            "completion_contract_id",
            _validated_id(self.completion_contract_id, "completion_contract_id"),
        )
        if (
            type(self.completion_contract_version) is not int
            or self.completion_contract_version < 1
        ):
            raise ValueError("completion_contract_version must be a positive integer")
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("question must not be blank")
        object.__setattr__(self, "question", self.question.strip())
        evidence = tuple(self.evidence)
        if not evidence:
            raise ValueError("evidence must not be empty")
        if any(not isinstance(item, HumanCheckEvidence) for item in evidence):
            raise TypeError("evidence must contain HumanCheckEvidence values")
        if len({item.artifact_id for item in evidence}) != len(evidence):
            raise ValueError("evidence must not contain duplicate artifact IDs")
        object.__setattr__(self, "evidence", evidence)

    @property
    def request_token(self) -> str:
        """Return a stable token for the normalized request snapshot."""
        document: dict[str, JsonValue] = {
            "plan_revision_id": self.plan_revision_id,
            "completion_contract_id": self.completion_contract_id,
            "completion_contract_version": self.completion_contract_version,
            "question": self.question,
            "evidence": [
                {"artifact_id": item.artifact_id, "sha256": item.sha256} for item in self.evidence
            ],
        }
        return sha256(json_dumps(document).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HumanCheckDecision:
    """Auditable actor and rationale recorded when a human Check is decided."""

    actor: str
    comment: str
    decided_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("actor must not be blank")
        if not isinstance(self.comment, str) or not self.comment.strip():
            raise ValueError("comment must not be blank")
        object.__setattr__(self, "actor", self.actor.strip())
        object.__setattr__(self, "comment", self.comment.strip())
        object.__setattr__(self, "decided_at", _utc(self.decided_at, "decided_at"))


@dataclass(frozen=True, slots=True)
class CheckResult:
    """A Checker's verdict plus the evidence supporting that verdict."""

    check_id: ID
    check_run_id: ID
    run_id: ID
    plan_node_id: ID
    attempt_id: ID
    passed: bool
    evaluated_at: datetime
    evidence_artifact_ids: tuple[ID, ...] = ()
    output: str | None = None
    failure_reason: str | None = None
    adoption_id: ID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _validated_id(self.check_id, "check_id"))
        object.__setattr__(self, "check_run_id", _validated_id(self.check_run_id, "check_run_id"))
        object.__setattr__(self, "run_id", _validated_id(self.run_id, "run_id"))
        object.__setattr__(self, "plan_node_id", _validated_id(self.plan_node_id, "plan_node_id"))
        object.__setattr__(self, "attempt_id", _validated_id(self.attempt_id, "attempt_id"))
        if self.adoption_id is not None:
            object.__setattr__(self, "adoption_id", _validated_id(self.adoption_id, "adoption_id"))
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            _validated_ids(self.evidence_artifact_ids, "evidence_artifact_ids"),
        )
        _validate_optional_text(self.output, "output")
        _validate_optional_text(self.failure_reason, "failure_reason")
        if self.passed and self.failure_reason is not None:
            raise ValueError(
                f"check result {self.check_run_id}: passing result cannot have failure_reason"
            )
        if not self.passed and self.failure_reason is None:
            raise ValueError(
                f"check result {self.check_run_id}: failing result requires failure_reason"
            )


@dataclass(frozen=True, slots=True)
class CheckRun:
    """An immutable snapshot of one Checker execution."""

    run_id: ID
    plan_node_id: ID
    attempt_id: ID
    check_id: ID
    check_run_id: ID = field(default_factory=new_id)
    status: CheckRunStatus = CheckRunStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    result: CheckResult | None = None
    failure_reason: str | None = None
    human_request: HumanCheckRequest | None = None
    human_decision: HumanCheckDecision | None = None
    adoption_id: ID | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        if self.adoption_id is not None:
            object.__setattr__(self, "adoption_id", _validated_id(self.adoption_id, "adoption_id"))
        for field_name in ("run_id", "plan_node_id", "attempt_id", "check_id", "check_run_id"):
            object.__setattr__(
                self,
                field_name,
                _validated_id(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "started_at", _optional_utc(self.started_at, "started_at"))
        object.__setattr__(self, "ended_at", _optional_utc(self.ended_at, "ended_at"))
        _validate_optional_text(self.failure_reason, "failure_reason")
        if self.human_request is not None and not isinstance(self.human_request, HumanCheckRequest):
            raise TypeError("human_request must be a HumanCheckRequest or None")
        if self.human_decision is not None and not isinstance(
            self.human_decision, HumanCheckDecision
        ):
            raise TypeError("human_decision must be a HumanCheckDecision or None")
        if self.human_decision is not None and self.human_request is None:
            raise ValueError("human_decision requires a human_request")
        if self.status is not CheckRunStatus.PENDING and _rehydrate_token is not _REHYDRATE:
            raise ValueError(
                f"check run {self.check_run_id}: non-pending state must use a transition or "
                "rehydrate()"
            )
        self._validate_lifecycle()

    @classmethod
    def rehydrate(
        cls,
        *,
        run_id: ID,
        plan_node_id: ID,
        attempt_id: ID,
        check_id: ID,
        check_run_id: ID,
        status: CheckRunStatus,
        created_at: datetime,
        started_at: datetime | None,
        ended_at: datetime | None,
        result: CheckResult | None = None,
        failure_reason: str | None = None,
        human_request: HumanCheckRequest | None = None,
        human_decision: HumanCheckDecision | None = None,
        adoption_id: ID | None = None,
    ) -> Self:
        """Restore a persisted CheckRun through an explicit validation boundary."""
        return cls(
            run_id=run_id,
            plan_node_id=plan_node_id,
            attempt_id=attempt_id,
            check_id=check_id,
            check_run_id=check_run_id,
            status=status,
            created_at=created_at,
            started_at=started_at,
            ended_at=ended_at,
            result=result,
            failure_reason=failure_reason,
            human_request=human_request,
            human_decision=human_decision,
            adoption_id=adoption_id,
            _rehydrate_token=_REHYDRATE,
        )

    def start(self, *, at: datetime | None = None) -> Self:
        """Start a pending CheckRun."""
        self._ensure_transition(CheckRunStatus.RUNNING)
        return replace(
            self,
            status=CheckRunStatus.RUNNING,
            started_at=_utc(at or utc_now(), "at"),
            ended_at=None,
            failure_reason=None,
            _rehydrate_token=_REHYDRATE,
        )

    def complete(self, result: CheckResult, *, at: datetime | None = None) -> Self:
        """Record a successfully executed Checker and its pass/fail verdict."""
        if self.human_request is not None and self.human_decision is None:
            raise ValueError(f"check run {self.check_run_id}: human Check requires decide_human()")
        return self._complete(result, at=at)

    def request_human(self, request: HumanCheckRequest) -> Self:
        """Attach an immutable human-review snapshot to an open CheckRun."""
        if not isinstance(request, HumanCheckRequest):
            raise TypeError("request must be a HumanCheckRequest")
        if self.status is not CheckRunStatus.RUNNING:
            raise InvalidCheckRunTransition(
                self.check_run_id,
                self.run_id,
                self.plan_node_id,
                self.status,
                CheckRunStatus.RUNNING,
            )
        if self.human_request is None:
            if self.human_decision is not None:
                raise ValueError(
                    f"check run {self.check_run_id}: human decision exists without request"
                )
            return replace(
                self,
                human_request=request,
                _rehydrate_token=_REHYDRATE,
            )
        if self.human_request != request:
            raise ValueError(f"check run {self.check_run_id}: human request snapshot changed")
        return self

    def decide_human(
        self,
        result: CheckResult,
        *,
        actor: str,
        comment: str,
        at: datetime | None = None,
    ) -> Self:
        """Complete one open human CheckRun and record its auditable decision."""
        if self.human_request is None:
            raise ValueError(f"check run {self.check_run_id}: no human request is open")
        if self.status is not CheckRunStatus.RUNNING or self.human_decision is not None:
            raise ValueError(f"check run {self.check_run_id}: human request is not open")
        if not set(result.evidence_artifact_ids).issubset(
            {item.artifact_id for item in self.human_request.evidence}
        ):
            raise ValueError(
                f"check run {self.check_run_id}: decision evidence is outside the human request"
            )
        decided_at = _utc(at or utc_now(), "at")
        decision = HumanCheckDecision(actor, comment, decided_at)
        return self._complete(result, at=decided_at, human_decision=decision)

    def _complete(
        self,
        result: CheckResult,
        *,
        at: datetime | None = None,
        human_decision: HumanCheckDecision | None = None,
    ) -> Self:
        """Complete a CheckRun after its human guard, when present, is satisfied."""
        expected_owner = (
            self.check_id,
            self.check_run_id,
            self.run_id,
            self.plan_node_id,
            self.attempt_id,
            self.adoption_id,
        )
        actual_owner = (
            result.check_id,
            result.check_run_id,
            result.run_id,
            result.plan_node_id,
            result.attempt_id,
            result.adoption_id,
        )
        if actual_owner != expected_owner:
            raise ValueError(
                f"check run {self.check_run_id} for run {self.run_id}, node "
                f"{self.plan_node_id}, attempt {self.attempt_id}: result ownership does not match"
            )
        self._ensure_transition(CheckRunStatus.COMPLETED)
        return replace(
            self,
            status=CheckRunStatus.COMPLETED,
            result=result,
            ended_at=_utc(at or utc_now(), "at"),
            failure_reason=None,
            human_decision=(
                self.human_decision if self.human_decision is not None else human_decision
            ),
            _rehydrate_token=_REHYDRATE,
        )

    def fail(self, reason: str, *, at: datetime | None = None) -> Self:
        """Record an infrastructure or Checker execution failure."""
        return self._terminal_failure(CheckRunStatus.FAILED, reason, at)

    def time_out(self, reason: str, *, at: datetime | None = None) -> Self:
        """Record a Checker timeout."""
        return self._terminal_failure(CheckRunStatus.TIMED_OUT, reason, at)

    def cancel(self, reason: str | None = None, *, at: datetime | None = None) -> Self:
        """Cancel a pending or running CheckRun."""
        _validate_optional_text(reason, "reason")
        self._ensure_transition(CheckRunStatus.CANCELLED)
        return replace(
            self,
            status=CheckRunStatus.CANCELLED,
            ended_at=_utc(at or utc_now(), "at"),
            failure_reason=reason,
            human_decision=None,
            _rehydrate_token=_REHYDRATE,
        )

    def interrupt(self, reason: str, *, at: datetime | None = None) -> Self:
        """Mark a running CheckRun interrupted during recovery."""
        return self._terminal_failure(CheckRunStatus.INTERRUPTED, reason, at)

    def _terminal_failure(
        self,
        status: CheckRunStatus,
        reason: str,
        at: datetime | None,
    ) -> Self:
        _require_text(reason, "reason")
        self._ensure_transition(status)
        return replace(
            self,
            status=status,
            ended_at=_utc(at or utc_now(), "at"),
            failure_reason=reason,
            human_decision=None,
            _rehydrate_token=_REHYDRATE,
        )

    def _ensure_transition(self, target: CheckRunStatus) -> None:
        if target not in _CHECK_RUN_TRANSITIONS[self.status]:
            raise InvalidCheckRunTransition(
                self.check_run_id,
                self.run_id,
                self.plan_node_id,
                self.status,
                target,
            )

    def _validate_lifecycle(self) -> None:
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError(f"check run {self.check_run_id}: started_at precedes created_at")
        if self.ended_at is not None:
            lower_bound = self.started_at or self.created_at
            if self.ended_at < lower_bound:
                raise ValueError(
                    f"check run {self.check_run_id}: ended_at precedes lifecycle start"
                )

        if self.status is CheckRunStatus.PENDING:
            if self.started_at is not None or self.ended_at is not None:
                raise ValueError(
                    f"check run {self.check_run_id}: pending run cannot have lifecycle timestamps"
                )
        elif self.status is CheckRunStatus.RUNNING:
            if self.started_at is None or self.ended_at is not None:
                raise ValueError(
                    f"check run {self.check_run_id}: running check requires only started_at"
                )
        elif self.status is CheckRunStatus.CANCELLED and self.started_at is None:
            if self.ended_at is None:
                raise ValueError(f"check run {self.check_run_id}: cancellation requires ended_at")
        elif self.started_at is None or self.ended_at is None:
            raise ValueError(
                f"check run {self.check_run_id}: {self.status.value} requires timestamps"
            )

        if self.status is CheckRunStatus.COMPLETED:
            if self.result is None or self.failure_reason is not None:
                raise ValueError(
                    f"check run {self.check_run_id}: completed run requires only a result"
                )
            if (
                self.result.check_id != self.check_id
                or self.result.check_run_id != self.check_run_id
                or self.result.run_id != self.run_id
                or self.result.plan_node_id != self.plan_node_id
                or self.result.attempt_id != self.attempt_id
                or self.result.adoption_id != self.adoption_id
            ):
                raise ValueError(
                    f"check run {self.check_run_id}: persisted result ownership does not match"
                )
            assert self.started_at is not None
            assert self.ended_at is not None
            if not self.started_at <= self.result.evaluated_at <= self.ended_at:
                raise ValueError(
                    f"check run {self.check_run_id}: result timestamp is outside its execution"
                )
            if self.human_request is not None and self.human_decision is None:
                raise ValueError(
                    f"check run {self.check_run_id}: completed human Check lacks a decision"
                )
        elif self.result is not None:
            raise ValueError(f"check run {self.check_run_id}: only completed run can have a result")

        if self.human_decision is not None and self.status is not CheckRunStatus.COMPLETED:
            raise ValueError(
                f"check run {self.check_run_id}: human decision requires a completed CheckRun"
            )

        failure_statuses = {
            CheckRunStatus.FAILED,
            CheckRunStatus.TIMED_OUT,
            CheckRunStatus.INTERRUPTED,
        }
        if self.status in failure_statuses:
            _require_text(self.failure_reason, "failure_reason")
        elif self.status is not CheckRunStatus.CANCELLED and self.failure_reason is not None:
            raise ValueError(
                f"check run {self.check_run_id}: {self.status.value} cannot have failure_reason"
            )


@dataclass(frozen=True, slots=True)
class GateDecision:
    """An auditable all-required Gate outcome."""

    gate_id: ID
    run_id: ID
    plan_node_id: ID
    attempt_id: ID
    passed: bool
    evaluated_at: datetime
    required_check_ids: tuple[ID, ...]
    failed_check_ids: tuple[ID, ...]
    evidence_artifact_ids: tuple[ID, ...]
    reason: str | None = None
    adoption_id: ID | None = None

    def __post_init__(self) -> None:
        if self.adoption_id is not None:
            object.__setattr__(self, "adoption_id", _validated_id(self.adoption_id, "adoption_id"))
        object.__setattr__(self, "gate_id", _validated_id(self.gate_id, "gate_id"))
        object.__setattr__(self, "run_id", _validated_id(self.run_id, "run_id"))
        object.__setattr__(self, "plan_node_id", _validated_id(self.plan_node_id, "plan_node_id"))
        object.__setattr__(self, "attempt_id", _validated_id(self.attempt_id, "attempt_id"))
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        object.__setattr__(
            self,
            "required_check_ids",
            _validated_ids(self.required_check_ids, "required_check_ids", allow_empty=False),
        )
        object.__setattr__(
            self,
            "failed_check_ids",
            _validated_ids(self.failed_check_ids, "failed_check_ids"),
        )
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            _validated_ids(self.evidence_artifact_ids, "evidence_artifact_ids"),
        )
        _validate_optional_text(self.reason, "reason")
        if not set(self.failed_check_ids).issubset(self.required_check_ids):
            raise ValueError(f"gate {self.gate_id}: failed checks must be required checks")
        if self.passed:
            if self.failed_check_ids:
                raise ValueError(f"gate {self.gate_id}: passing decision cannot have failed checks")
            if not self.evidence_artifact_ids:
                raise ValueError(f"gate {self.gate_id}: passing decision requires evidence")
            if self.reason is not None:
                raise ValueError(f"gate {self.gate_id}: passing decision cannot have a reason")
        elif not self.failed_check_ids or self.reason is None:
            raise ValueError(
                f"gate {self.gate_id}: failing decision requires failed checks and a reason"
            )


@dataclass(frozen=True, slots=True)
class Gate:
    """The fail-closed, all-required Gate supported by P1."""

    required_check_ids: tuple[ID, ...]
    gate_id: ID = field(default_factory=new_id)
    policy: GatePolicy = GatePolicy.ALL_REQUIRED

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_id", _validated_id(self.gate_id, "gate_id"))
        object.__setattr__(
            self,
            "required_check_ids",
            _validated_ids(self.required_check_ids, "required_check_ids", allow_empty=False),
        )
        if self.policy is not GatePolicy.ALL_REQUIRED:
            raise ValueError(f"gate {self.gate_id}: unsupported policy {self.policy}")

    @classmethod
    def from_specs(cls, specs: Iterable[CheckSpec], *, gate_id: ID | None = None) -> Self:
        """Build a Gate from the required subset of immutable CheckSpecs."""
        required = tuple(spec.check_id for spec in specs if spec.required)
        return cls(required_check_ids=required, gate_id=gate_id or new_id())

    def evaluate(
        self,
        results: Iterable[CheckResult],
        *,
        run_id: ID,
        plan_node_id: ID,
        attempt_id: ID,
        at: datetime | None = None,
        adoption_id: ID | None = None,
    ) -> GateDecision:
        """Evaluate all required checks and fail closed on missing or weak evidence."""
        evaluated_run_id = _validated_id(run_id, "run_id")
        evaluated_node_id = _validated_id(plan_node_id, "plan_node_id")
        evaluated_attempt_id = _validated_id(attempt_id, "attempt_id")
        evaluated_adoption_id = (
            None if adoption_id is None else _validated_id(adoption_id, "adoption_id")
        )
        grouped: dict[ID, list[CheckResult]] = {}
        for result in results:
            if result.check_id in self.required_check_ids:
                grouped.setdefault(result.check_id, []).append(result)

        failed: list[ID] = []
        evidence: list[ID] = []
        problems: list[str] = []
        for check_id in self.required_check_ids:
            matches = grouped.get(check_id, [])
            if not matches:
                failed.append(check_id)
                problems.append(f"missing result for {check_id}")
                continue
            if len(matches) > 1:
                failed.append(check_id)
                problems.append(f"multiple results for {check_id}")
                continue

            result = matches[0]
            if (
                result.run_id != evaluated_run_id
                or result.plan_node_id != evaluated_node_id
                or result.attempt_id != evaluated_attempt_id
                or result.adoption_id != evaluated_adoption_id
            ):
                failed.append(check_id)
                problems.append(f"check {check_id} result belongs to another execution scope")
                continue
            if not result.passed:
                failed.append(check_id)
                problems.append(f"check {check_id} failed")
                evidence.extend(result.evidence_artifact_ids)
                continue
            if not result.evidence_artifact_ids:
                failed.append(check_id)
                problems.append(f"check {check_id} has no evidence")
                continue
            evidence.extend(result.evidence_artifact_ids)

        unique_evidence = tuple(dict.fromkeys(evidence))
        if failed:
            return GateDecision(
                gate_id=self.gate_id,
                run_id=evaluated_run_id,
                plan_node_id=evaluated_node_id,
                attempt_id=evaluated_attempt_id,
                adoption_id=evaluated_adoption_id,
                passed=False,
                evaluated_at=at or utc_now(),
                required_check_ids=self.required_check_ids,
                failed_check_ids=tuple(failed),
                evidence_artifact_ids=unique_evidence,
                reason="; ".join(problems),
            )
        return GateDecision(
            gate_id=self.gate_id,
            run_id=evaluated_run_id,
            plan_node_id=evaluated_node_id,
            attempt_id=evaluated_attempt_id,
            adoption_id=evaluated_adoption_id,
            passed=True,
            evaluated_at=at or utc_now(),
            required_check_ids=self.required_check_ids,
            failed_check_ids=(),
            evidence_artifact_ids=unique_evidence,
        )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A Gate-approved, internally consistent recovery snapshot."""

    plan_revision: PlanRevision
    run: Run
    event_offset: int
    gate_decision: GateDecision
    branch_selections: Mapping[ID, ID] = field(default_factory=dict)
    artifact_refs: tuple[ID, ...] = ()
    checkpoint_id: ID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    process_revision_id: ID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "checkpoint_id", _validated_id(self.checkpoint_id, "checkpoint_id")
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.process_revision_id is not None:
            object.__setattr__(
                self,
                "process_revision_id",
                _validated_id(self.process_revision_id, "process_revision_id"),
            )
        if self.event_offset < 1:
            raise ValueError(f"checkpoint {self.checkpoint_id}: event_offset must be positive")
        if self.plan_revision.status is not PlanRevisionStatus.APPROVED:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: PlanRevision "
                f"{self.plan_revision.plan_revision_id} is not approved"
            )
        if self.run.goal_id != self.plan_revision.goal_id:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: run {self.run.run_id} belongs to Goal "
                f"{self.run.goal_id}, not {self.plan_revision.goal_id}"
            )
        if self.run.plan_revision_id != self.plan_revision.plan_revision_id:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: run {self.run.run_id} references plan revision "
                f"{self.run.plan_revision_id}, not {self.plan_revision.plan_revision_id}"
            )
        if self.run.status not in {RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.COMPLETED}:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: run {self.run.run_id} is {self.run.status.value}"
            )
        if not self.gate_decision.passed:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: gate {self.gate_decision.gate_id} did not pass"
            )
        if self.gate_decision.run_id != self.run.run_id:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: Gate {self.gate_decision.gate_id} belongs "
                f"to run {self.gate_decision.run_id}, not {self.run.run_id}"
            )
        plan_node_ids = {node.plan_node_id for node in self.plan_revision.nodes}
        if self.gate_decision.plan_node_id not in plan_node_ids:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: Gate node "
                f"{self.gate_decision.plan_node_id} is not in PlanRevision "
                f"{self.plan_revision.plan_revision_id}"
            )

        selections = _validated_id_mapping(self.branch_selections, "branch_selections")
        self._validate_branch_selections(selections)
        artifacts = _validated_ids(self.artifact_refs, "artifact_refs")
        missing_evidence = set(self.gate_decision.evidence_artifact_ids).difference(artifacts)
        if missing_evidence:
            missing = ", ".join(sorted(missing_evidence))
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: gate evidence missing from "
                f"artifact refs: {missing}"
            )
        object.__setattr__(self, "branch_selections", selections)
        object.__setattr__(self, "artifact_refs", artifacts)

    @property
    def run_id(self) -> ID:
        """Return the ID of the immutable Run snapshot referenced by this checkpoint."""
        return self.run.run_id

    @property
    def plan_revision_id(self) -> ID:
        """Return the ID of the immutable PlanRevision referenced by this checkpoint."""
        return self.plan_revision.plan_revision_id

    def _validate_branch_selections(self, selections: Mapping[ID, ID]) -> None:
        node_by_id = {node.plan_node_id: node for node in self.plan_revision.nodes}
        branch_by_id = {branch.branch_id: branch for branch in self.plan_revision.branches}
        selected_by_fork: dict[ID, ID] = {}
        for branch in self.plan_revision.branches:
            if branch.status is not BranchStatus.SELECTED:
                continue
            if branch.fork_node_id in selected_by_fork:
                raise ValueError(
                    f"checkpoint {self.checkpoint_id}: multiple selected Branches for fork "
                    f"{branch.fork_node_id}"
                )
            selected_by_fork[branch.fork_node_id] = branch.branch_id

        for fork_id, branch_id in selections.items():
            fork = node_by_id.get(fork_id)
            selected_branch = branch_by_id.get(branch_id)
            if fork is None or fork.kind is not PlanNodeKind.FORK:
                raise ValueError(
                    f"checkpoint {self.checkpoint_id}: unknown fork {fork_id} in branch selection"
                )
            if selected_branch is None:
                raise ValueError(
                    f"checkpoint {self.checkpoint_id}: unknown Branch {branch_id} in selection"
                )
            if selected_branch.fork_node_id != fork_id:
                raise ValueError(
                    f"checkpoint {self.checkpoint_id}: Branch {branch_id} does not belong to "
                    f"fork {fork_id}"
                )
            if selected_branch.status is not BranchStatus.SELECTED:
                raise ValueError(
                    f"checkpoint {self.checkpoint_id}: Branch {branch_id} is not selected"
                )
            if any(
                sibling.branch_id != branch_id
                and sibling.fork_node_id == fork_id
                and sibling.merge_node_id == selected_branch.merge_node_id
                and sibling.status is not BranchStatus.PRUNED
                for sibling in self.plan_revision.branches
            ):
                raise ValueError(
                    f"checkpoint {self.checkpoint_id}: selected Branch {branch_id} has an "
                    "unpruned sibling"
                )

        if dict(selections) != selected_by_fork:
            raise ValueError(
                f"checkpoint {self.checkpoint_id}: branch selections do not match "
                f"PlanRevision {self.plan_revision.plan_revision_id} state"
            )


def _validated_id(value: ID, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{field_name}: {error}") from error


def _validated_ids(
    values: tuple[ID, ...],
    field_name: str,
    *,
    allow_empty: bool = True,
) -> tuple[ID, ...]:
    normalized = tuple(_validated_id(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _validated_id_mapping(values: Mapping[ID, ID], field_name: str) -> Mapping[ID, ID]:
    normalized: dict[ID, ID] = {}
    for decision_id, branch_id in values.items():
        normalized_decision_id = _validated_id(decision_id, field_name)
        normalized_branch_id = _validated_id(branch_id, field_name)
        if normalized_decision_id in normalized:
            raise ValueError(f"{field_name} contains duplicate normalized fork IDs")
        normalized[normalized_decision_id] = normalized_branch_id
    return MappingProxyType(normalized)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None, field_name: str) -> datetime | None:
    return None if value is None else _utc(value, field_name)


def _validate_optional_text(value: str | None, field_name: str) -> None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_text(value: str | None, field_name: str) -> None:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
