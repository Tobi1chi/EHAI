"""Application boundary and runner for P1 Check adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ehai import ID, normalize_id, utc_now
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import CheckKind, CheckResult, CheckRun, CheckSpec
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanNode, PlanNodeStatus


class CheckAdapterError(RuntimeError):
    """An infrastructure failure while invoking a Check adapter."""


class CheckAdapterTimeout(CheckAdapterError):
    """A Check adapter exceeded its configured execution deadline."""


@dataclass(frozen=True, slots=True)
class CheckContext:
    """Immutable execution scope and Artifact metadata presented to a Check."""

    run: Run
    attempt: Attempt
    plan_node: PlanNode
    artifacts: tuple[Artifact, ...]
    workspace: Path
    code_workspace: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run, Run):
            raise ValueError("CheckContext run must be a Run")
        if not isinstance(self.attempt, Attempt):
            raise ValueError("CheckContext attempt must be an Attempt")
        if not isinstance(self.plan_node, PlanNode):
            raise ValueError("CheckContext plan_node must be a PlanNode")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(artifact, Artifact) for artifact in artifacts):
            raise ValueError("CheckContext artifacts must contain only Artifact metadata")
        if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise ValueError("CheckContext artifacts must not contain duplicate IDs")

        owner = f"CheckContext for Attempt {self.attempt.attempt_id}"
        if self.run.status is not RunStatus.RUNNING:
            raise ValueError(f"{owner} requires a running Run")
        if self.attempt.status is not AttemptStatus.SUCCEEDED:
            raise ValueError(f"{owner} requires a succeeded Attempt")
        if self.plan_node.status is not PlanNodeStatus.VERIFYING:
            raise ValueError(f"{owner} requires a verifying PlanNode")
        if self.attempt.run_id != self.run.run_id:
            raise ValueError(f"{owner} Attempt belongs to another Run")
        if self.attempt.plan_node_id != self.plan_node.plan_node_id:
            raise ValueError(f"{owner} Attempt belongs to another PlanNode")
        if {artifact.artifact_id for artifact in artifacts} != set(self.attempt.artifact_ids):
            raise ValueError(f"{owner} artifacts must match the Attempt Artifact snapshot")
        if any(
            artifact.run_id != self.run.run_id
            or artifact.plan_node_id != self.plan_node.plan_node_id
            or artifact.attempt_id != self.attempt.attempt_id
            for artifact in artifacts
        ):
            raise ValueError(f"{owner} contains Artifact metadata from another execution scope")

        workspace = Path(self.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError(f"{owner} workspace is not a directory")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "workspace", workspace)

    @property
    def run_id(self) -> ID:
        """Return the Check Run ID."""
        return self.run.run_id

    @property
    def attempt_id(self) -> ID:
        """Return the checked Attempt ID."""
        return self.attempt.attempt_id

    @property
    def plan_node_id(self) -> ID:
        """Return the checked PlanNode ID."""
        return self.plan_node.plan_node_id


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One adapter verdict before it is attached to a domain CheckRun."""

    passed: bool
    output: str
    evidence_artifact_ids: tuple[ID, ...]
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("CheckOutcome passed must be a boolean")
        if not isinstance(self.output, str) or not self.output.strip():
            raise ValueError("CheckOutcome output must not be blank")
        evidence = tuple(normalize_id(artifact_id) for artifact_id in self.evidence_artifact_ids)
        if len(set(evidence)) != len(evidence):
            raise ValueError("CheckOutcome evidence must not contain duplicate Artifact IDs")
        if self.passed:
            if not evidence:
                raise ValueError("passing CheckOutcome requires Artifact evidence")
            if self.failure_reason is not None:
                raise ValueError("passing CheckOutcome cannot have a failure reason")
        elif not isinstance(self.failure_reason, str) or not self.failure_reason.strip():
            raise ValueError("failing CheckOutcome requires a failure reason")
        object.__setattr__(self, "evidence_artifact_ids", evidence)


@runtime_checkable
class CheckAdapter(Protocol):
    """Evaluate one CheckSpec within a validated execution context."""

    def evaluate(self, spec: CheckSpec, context: CheckContext) -> CheckOutcome:
        """Return a deterministic verdict or raise a scoped adapter error."""
        ...


class CheckRegistry:
    """Immutable internal Check adapter registry created explicitly at startup."""

    def __init__(self, adapters: Mapping[CheckKind, CheckAdapter]) -> None:
        normalized: dict[CheckKind, CheckAdapter] = {}
        for kind, adapter in adapters.items():
            normalized_kind = CheckKind(kind)
            if normalized_kind in normalized:
                raise ValueError(f"duplicate Check adapter for {normalized_kind.value}")
            if not isinstance(adapter, CheckAdapter):
                raise TypeError(
                    f"adapter for {normalized_kind.value} does not implement CheckAdapter"
                )
            normalized[normalized_kind] = adapter
        self._adapters = MappingProxyType(normalized)

    def get(self, kind: CheckKind) -> CheckAdapter | None:
        """Return only a preconfigured adapter; no dynamic loading is performed."""
        return self._adapters.get(CheckKind(kind))


class CheckRunner:
    """Dispatch CheckSpecs and advance their domain CheckRun snapshots."""

    def __init__(
        self,
        adapters: Mapping[CheckKind, CheckAdapter] | CheckRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = (
            adapters if isinstance(adapters, CheckRegistry) else CheckRegistry(adapters)
        )
        self._clock = utc_now if clock is None else clock

    def run(self, spec: CheckSpec, context: CheckContext) -> CheckRun:
        """Create, start, and terminally advance a CheckRun."""
        if not isinstance(spec, CheckSpec):
            raise TypeError("spec must be a CheckSpec")
        if not isinstance(context, CheckContext):
            raise TypeError("context must be a CheckContext")
        if spec.check_id not in context.plan_node.required_check_ids:
            raise ValueError(
                f"Check {spec.check_id} is not required by PlanNode {context.plan_node_id}"
            )

        started_at = self._now()
        check_run = CheckRun(
            run_id=context.run_id,
            plan_node_id=context.plan_node_id,
            attempt_id=context.attempt_id,
            check_id=spec.check_id,
            created_at=started_at,
        ).start(at=started_at)
        adapter = self._registry.get(spec.kind)
        if adapter is None:
            return check_run.fail(
                f"no Check adapter configured for {spec.kind.value}",
                at=self._now(),
            )

        try:
            outcome = adapter.evaluate(spec, context)
            allowed_evidence = {artifact.artifact_id for artifact in context.artifacts}
            if not set(outcome.evidence_artifact_ids).issubset(allowed_evidence):
                raise CheckAdapterError(
                    f"Check {spec.check_id} returned evidence outside its execution context"
                )
        except CheckAdapterTimeout as error:
            return check_run.time_out(str(error), at=self._now())
        except CheckAdapterError as error:
            return check_run.fail(str(error), at=self._now())

        evaluated_at = self._now()
        result = CheckResult(
            check_id=spec.check_id,
            check_run_id=check_run.check_run_id,
            run_id=context.run_id,
            plan_node_id=context.plan_node_id,
            attempt_id=context.attempt_id,
            passed=outcome.passed,
            evaluated_at=evaluated_at,
            evidence_artifact_ids=outcome.evidence_artifact_ids,
            output=outcome.output,
            failure_reason=outcome.failure_reason,
        )
        return check_run.complete(result, at=evaluated_at)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("CheckRunner clock must return a datetime")
        return value
