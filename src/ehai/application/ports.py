"""Application-layer persistence ports for the P1 execution plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from ehai import ID, JsonValue, json_dumps, json_loads
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import Checkpoint, CheckRun, CheckSpec
from ehai.domain.events import Event
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanRevision


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An Event paired with its durable, monotonically increasing offset."""

    offset: int
    event: Event

    def __post_init__(self) -> None:
        if type(self.offset) is not int or self.offset < 1:
            raise ValueError("stored event offset must be a positive integer")
        if not isinstance(self.event, Event):
            raise ValueError("stored event must contain an Event")


@dataclass(frozen=True, slots=True, init=False)
class CommandReceipt:
    """The durable result of one idempotent application Command."""

    idempotency_key: str
    command_name: str
    command_fingerprint: str
    created_at: datetime
    _result_json: str = field(repr=False)

    def __init__(
        self,
        *,
        idempotency_key: str,
        command_name: str,
        command_fingerprint: str,
        result: Mapping[str, JsonValue],
        created_at: datetime,
    ) -> None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if not isinstance(command_name, str) or not command_name.strip():
            raise ValueError("command_name must not be empty")
        if not isinstance(command_fingerprint, str) or not command_fingerprint.strip():
            raise ValueError("command_fingerprint must not be empty")
        if not isinstance(created_at, datetime):
            raise ValueError("CommandReceipt created_at must be a datetime")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("CommandReceipt created_at must include a UTC offset")
        if not isinstance(result, Mapping):
            raise ValueError("CommandReceipt result must be a JSON object")

        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "command_name", command_name)
        object.__setattr__(self, "command_fingerprint", command_fingerprint)
        object.__setattr__(self, "created_at", created_at.astimezone(UTC))
        object.__setattr__(self, "_result_json", json_dumps(dict(result)))

    @property
    def result(self) -> dict[str, JsonValue]:
        """Return an isolated copy of the recorded Command result."""
        decoded = json_loads(self._result_json)
        if not isinstance(decoded, dict):  # pragma: no cover - guarded by construction
            raise RuntimeError("stored CommandReceipt result is not a JSON object")
        return decoded


@runtime_checkable
class CurrentStateReader(Protocol):
    """Read-only access to P1 current state and retained version history.

    ``list_*`` methods must return deterministic oldest-first order: creation
    order for entities, version order for contracts/plans, sequence order for
    Attempts, and event-offset order for Checkpoints.
    """

    def get_project(self, project_id: ID) -> Project | None:
        """Return a Project by ID."""
        ...

    def list_projects(self) -> tuple[Project, ...]:
        """List Projects in deterministic creation order."""
        ...

    def get_goal(self, goal_id: ID) -> Goal | None:
        """Return a Goal by ID."""
        ...

    def list_goals(self, project_id: ID) -> tuple[Goal, ...]:
        """List Goals owned by one Project."""
        ...

    def get_completion_contract(self, completion_contract_id: ID) -> CompletionContract | None:
        """Return a CompletionContract by ID."""
        ...

    def list_completion_contracts(self, goal_id: ID) -> tuple[CompletionContract, ...]:
        """List all retained CompletionContract versions for a Goal."""
        ...

    def get_plan_revision(self, plan_revision_id: ID) -> PlanRevision | None:
        """Return a PlanRevision by ID."""
        ...

    def list_plan_revisions(self, goal_id: ID) -> tuple[PlanRevision, ...]:
        """List all retained PlanRevision versions for a Goal."""
        ...

    def get_run(self, run_id: ID) -> Run | None:
        """Return a Run by ID."""
        ...

    def list_runs(self, goal_id: ID) -> tuple[Run, ...]:
        """List Runs for one Goal."""
        ...

    def get_attempt(self, attempt_id: ID) -> Attempt | None:
        """Return an Attempt by ID."""
        ...

    def list_attempts(self, run_id: ID) -> tuple[Attempt, ...]:
        """List Attempts for one Run in sequence order."""
        ...

    def get_check_spec(self, check_id: ID) -> CheckSpec | None:
        """Return one CheckSpec by ID."""
        ...

    def list_check_specs(self, plan_revision_id: ID) -> tuple[CheckSpec, ...]:
        """List CheckSpecs for one PlanRevision in insertion order."""
        ...

    def get_check_run(self, check_run_id: ID) -> CheckRun | None:
        """Return a CheckRun by ID."""
        ...

    def list_check_runs(self, run_id: ID) -> tuple[CheckRun, ...]:
        """List CheckRuns for one Run."""
        ...

    def get_checkpoint(self, checkpoint_id: ID) -> Checkpoint | None:
        """Return a Checkpoint by ID."""
        ...

    def list_checkpoints(self, run_id: ID) -> tuple[Checkpoint, ...]:
        """List Checkpoints for one Run in event-offset order."""
        ...

    def get_artifact(self, artifact_id: ID) -> Artifact | None:
        """Return Artifact metadata by ID."""
        ...

    def list_artifacts_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        """List Artifact metadata for one Run."""
        ...


@runtime_checkable
class CurrentStateRepository(CurrentStateReader, Protocol):
    """Mutable state storage used only inside an explicit write transaction."""

    def put_project(self, project: Project) -> None:
        """Insert or replace a Project snapshot by ID."""
        ...

    def put_goal(self, goal: Goal) -> None:
        """Insert or replace a Goal snapshot by ID."""
        ...

    def put_completion_contract(self, contract: CompletionContract) -> None:
        """Persist an immutable CompletionContract version."""
        ...

    def put_plan_revision(self, plan_revision: PlanRevision) -> None:
        """Insert or replace a PlanRevision snapshot by ID."""
        ...

    def put_run(self, run: Run) -> None:
        """Insert or replace a Run snapshot by ID."""
        ...

    def put_attempt(self, attempt: Attempt) -> None:
        """Insert or replace an Attempt snapshot by ID."""
        ...

    def put_check_spec(self, plan_revision_id: ID, check_spec: CheckSpec) -> None:
        """Persist one immutable CheckSpec owned by a PlanRevision."""
        ...

    def put_check_run(self, check_run: CheckRun) -> None:
        """Insert or replace a CheckRun snapshot by ID."""
        ...

    def put_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Persist an immutable Checkpoint."""
        ...

    def restore_checkpoint_state(self, checkpoint: Checkpoint, restored_run: Run) -> None:
        """Restore only a persisted Checkpoint through the explicit recovery boundary."""
        ...

    def put_artifact(self, artifact: Artifact) -> None:
        """Persist immutable Artifact metadata and its relative path."""
        ...


@runtime_checkable
class EventReader(Protocol):
    """Read-only access to the durable Event Log."""

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        """List Events strictly after an optional Event ID cursor."""
        ...

    def latest_offset(self) -> int:
        """Return the latest committed offset, or zero for an empty log."""
        ...


@runtime_checkable
class EventLog(EventReader, Protocol):
    """Append-only Event storage sharing a transaction with current state."""

    def append(self, event: Event) -> StoredEvent:
        """Append one Event and return its assigned durable offset."""
        ...

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        """List Events strictly after an optional Event ID cursor."""
        ...

    def latest_offset(self) -> int:
        """Return the latest committed offset, or zero for an empty log."""
        ...


@runtime_checkable
class ReadSession(Protocol):
    """One short-lived, consistent, read-only persistence snapshot."""

    @property
    def states(self) -> CurrentStateReader:
        """Return current-state readers bound to this snapshot."""
        ...

    @property
    def events(self) -> EventReader:
        """Return the Event reader bound to this snapshot."""
        ...

    def __enter__(self) -> Self:
        """Open the read-only snapshot."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Close the snapshot and its database connection."""
        ...


@runtime_checkable
class CommandReceiptStore(Protocol):
    """Persistence for idempotent Command results."""

    def put(self, receipt: CommandReceipt) -> None:
        """Persist a receipt, rejecting a key with a different fingerprint."""
        ...

    def get(self, idempotency_key: str) -> CommandReceipt | None:
        """Return the previously committed receipt for an idempotency key."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """One explicit transaction over state, Events, and Command receipts."""

    @property
    def states(self) -> CurrentStateRepository:
        """Return repositories bound to this transaction."""
        ...

    @property
    def events(self) -> EventLog:
        """Return the append-only Event log bound to this transaction."""
        ...

    @property
    def command_receipts(self) -> CommandReceiptStore:
        """Return the idempotency store bound to this transaction."""
        ...

    def commit(self) -> None:
        """Atomically commit current state, Events, and receipts."""
        ...

    def rollback(self) -> None:
        """Discard every uncommitted change in this transaction."""
        ...

    def __enter__(self) -> Self:
        """Enter a transaction scope."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Roll back an uncommitted or failed transaction scope."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Immutable Artifact bytes storage outside the relational database."""

    def put(self, artifact: Artifact, content: bytes) -> None:
        """Store bytes under an Artifact ID without permitting overwrite."""
        ...

    def get(self, artifact_id: ID) -> Artifact | None:
        """Return the filesystem metadata record for an Artifact, if present."""
        ...

    def read(self, artifact_id: ID) -> bytes:
        """Read immutable Artifact bytes, raising FileNotFoundError when absent."""
        ...

    def list_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        """List filesystem Artifact records for one Run."""
        ...
