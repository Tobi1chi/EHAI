"""Single-slot P2 background Runtime without Scheduler or multi-Endpoint routing."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ehai import ID, new_id, normalize_id, utc_now
from ehai.application.orchestrator import Orchestrator
from ehai.application.ports import UnitOfWork
from ehai.application.workers import WorkerRequest, WorkerResult
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import (
    AgentSessionRef,
    AttemptActivity,
    BuiltinExecutionRef,
    ExecutionHandle,
    ExternalExecutionRef,
    WorkerEndpoint,
    WorkerKind,
    WorkerProfile,
)

UnitOfWorkFactory = Callable[[], UnitOfWork]


class WorkerEventType(StrEnum):
    """Normalized live Connector events consumed by the Runtime."""

    PROGRESS = "progress"
    WAITING = "waiting"
    CANDIDATE = "candidate"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    """One deduplicated live event for a specific Attempt execution."""

    worker_event_id: str
    attempt_id: ID
    type: WorkerEventType
    cursor: str
    result: WorkerResult | None = None
    reason: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.worker_event_id, str) or not self.worker_event_id.strip():
            raise ValueError("WorkerEvent worker_event_id must not be blank")
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))
        object.__setattr__(self, "type", WorkerEventType(self.type))
        if not isinstance(self.cursor, str) or not self.cursor.strip():
            raise ValueError("WorkerEvent cursor must not be blank")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("WorkerEvent occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        if self.type is WorkerEventType.CANDIDATE and self.result is None:
            raise ValueError("candidate WorkerEvent requires WorkerResult")
        if self.type is WorkerEventType.FAILED and not self.reason:
            raise ValueError("failed WorkerEvent requires a reason")


@dataclass(frozen=True, slots=True)
class ConnectorStartRequest:
    """Start one execution using Attempt ID as the provider idempotency key."""

    request: WorkerRequest

    @property
    def attempt_id(self) -> ID:
        return self.request.attempt_id


@dataclass(frozen=True, slots=True)
class ConnectorExecution:
    """Provider IDs returned by Connector start or recover."""

    attempt_id: ID
    provider_session_id: str
    provider_execution_id: str
    recoverable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))
        for name in ("provider_session_id", "provider_execution_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ConnectorExecution {name} must not be blank")
        if not isinstance(self.recoverable, bool):
            raise ValueError("ConnectorExecution recoverable must be a boolean")


@dataclass(frozen=True, slots=True)
class ConnectorRecoveryRequest:
    """Persisted execution facts used to recover without replacement start."""

    attempt_id: ID
    provider_session_id: str
    provider_execution_id: str
    event_cursor: str | None


@runtime_checkable
class RuntimeConnector(Protocol):
    """Concrete I4 Connector Port over normalized Runtime DTOs."""

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution: ...

    def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]: ...

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity: ...

    async def cancel(self, execution: ConnectorExecution) -> None: ...

    async def recover(
        self,
        request: ConnectorRecoveryRequest,
    ) -> ConnectorExecution | None: ...


class SingleSlotRuntime:
    """Claim and execute one Run at a time through one configured Endpoint."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        orchestrator: Orchestrator,
        connector: RuntimeConnector,
        profile: WorkerProfile,
        endpoint: WorkerEndpoint,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], ID] = new_id,
    ) -> None:
        if endpoint.capacity != 1:
            raise ValueError("SingleSlotRuntime requires Endpoint capacity=1")
        if profile.kind != endpoint.worker_kind:
            raise ValueError("SingleSlotRuntime Worker and Endpoint kinds must match")
        self._uow_factory = uow_factory
        self._orchestrator = orchestrator
        self._connector = connector
        self._profile = profile
        self._endpoint = endpoint
        self._clock = clock
        self._id_factory = id_factory

    async def run_once(self) -> Run | None:
        """Claim the oldest pending work and advance its Run to a terminal state."""
        with self._uow_factory() as uow:
            self._persist_registry(uow)
            work = uow.states.claim_next_dispatch_work(at=self._clock())
            uow.commit()
        if work is None:
            return None
        return await self._process_work(work)

    async def recover_startup(self) -> tuple[Run, ...]:
        """Recover claimed work without creating replacement provider executions."""
        with self._uow_factory() as uow:
            works = uow.states.list_dispatch_work(DispatchWorkStatus.CLAIMED)
        recovered_runs: list[Run] = []
        for work in works:
            with self._uow_factory() as uow:
                attempts = uow.states.list_attempts(work.run_id)
                running = tuple(
                    attempt for attempt in attempts if attempt.status is AttemptStatus.RUNNING
                )
            if not running:
                recovered_runs.append(await self._process_work(work))
                continue
            attempt = running[-1]
            execution = self._execution_from_attempt(attempt)
            recovered = await self._connector.recover(
                ConnectorRecoveryRequest(
                    attempt.attempt_id,
                    execution.provider_session_id,
                    execution.provider_execution_id,
                    attempt.event_cursor,
                )
            )
            if recovered is None:
                run = self._orchestrator.interrupt_attempt(
                    attempt.attempt_id,
                    "original provider execution was not found during Runtime recovery",
                )
                self._complete_work(work)
                recovered_runs.append(run)
                continue
            recovered_runs.append(await self._process_work(work, recovered=(attempt, recovered)))
        return tuple(recovered_runs)

    async def cancel_attempt(self, attempt_id: ID) -> Run:
        """Let a completed provider result win a cancel/completed race."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, normalize_id(attempt_id))
        execution = self._execution_from_attempt(attempt)
        await self._connector.cancel(execution)
        run = await self._consume_events(attempt, execution)
        if run.status is not RunStatus.RUNNING:
            with self._uow_factory() as uow:
                works = tuple(
                    work
                    for work in uow.states.list_dispatch_work(DispatchWorkStatus.CLAIMED)
                    if work.run_id == run.run_id
                )
            if works:
                self._complete_work(works[0])
        return run

    async def _process_work(
        self,
        work: DispatchWork,
        *,
        recovered: tuple[Attempt, ConnectorExecution] | None = None,
    ) -> Run:
        if recovered is not None:
            run = await self._consume_events(*recovered)
            if run.status is not RunStatus.RUNNING:
                self._complete_work(work)
                return run
        while True:
            request = self._orchestrator.prepare_worker_request(work.run_id)
            execution = await self._connector.start(ConnectorStartRequest(request))
            if execution.attempt_id != request.attempt_id:
                raise RuntimeError("Connector start returned another Attempt ID")
            bound_attempt = self._bind_execution(request.attempt, execution)
            run = await self._consume_events(bound_attempt, execution)
            if run.status is not RunStatus.RUNNING:
                self._complete_work(work)
                return run

    def _bind_execution(
        self,
        attempt: Attempt,
        execution: ConnectorExecution,
    ) -> Attempt:
        with self._uow_factory() as uow:
            stored = _required_attempt(uow, attempt.attempt_id)
            session = AgentSessionRef(
                run_id=stored.run_id,
                worker_profile_id=self._profile.worker_profile_id,
                worker_endpoint_id=self._endpoint.worker_endpoint_id,
                provider_session_id=execution.provider_session_id,
                recoverable=execution.recoverable,
                agent_session_ref_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_agent_session_ref(session)
            if self._profile.kind is WorkerKind.BUILTIN:
                reference = BuiltinExecutionRef(
                    stored.attempt_id,
                    session.agent_session_ref_id,
                    builtin_execution_id=normalize_id(execution.provider_execution_id),
                    builtin_execution_ref_id=self._id_factory(),
                )
                uow.states.put_builtin_execution_ref(reference)
                handle = ExecutionHandle(builtin=reference)
            else:
                external = ExternalExecutionRef(
                    stored.attempt_id,
                    session.agent_session_ref_id,
                    execution.provider_execution_id,
                    external_execution_ref_id=self._id_factory(),
                )
                uow.states.put_external_execution_ref(external)
                handle = ExecutionHandle(external=external)
            bound = stored.assign(
                profile=self._profile,
                endpoint=self._endpoint,
                session=session,
            ).bind_execution(handle)
            uow.states.put_attempt(bound)
            uow.commit()
            return bound

    async def _consume_events(
        self,
        attempt: Attempt,
        execution: ConnectorExecution,
    ) -> Run:
        candidate: WorkerResult | None = None
        after_cursor = attempt.event_cursor
        async for event in self._connector.events(execution, after_cursor=after_cursor):
            if event.attempt_id != attempt.attempt_id:
                raise RuntimeError("Connector emitted a WorkerEvent for another Attempt")
            if not self._record_event(attempt.attempt_id, event):
                continue
            after_cursor = event.cursor
            if event.type is WorkerEventType.CANDIDATE:
                candidate = event.result
            elif event.type is WorkerEventType.COMPLETED:
                if candidate is None:
                    raise RuntimeError("Worker completed without a candidate")
                return self._orchestrator.accept_worker_result(attempt.attempt_id, candidate)
            elif event.type is WorkerEventType.FAILED:
                return self._orchestrator.interrupt_attempt(
                    attempt.attempt_id,
                    event.reason or "Worker failed",
                )
        raise RuntimeError(f"Connector event stream ended before Attempt {attempt.attempt_id}")

    def _record_event(self, attempt_id: ID, event: WorkerEvent) -> bool:
        with self._uow_factory() as uow:
            is_new = uow.states.record_worker_event(
                attempt_id,
                event.worker_event_id,
                at=event.occurred_at,
            )
            if is_new:
                attempt = _required_attempt(uow, attempt_id)
                activity = (
                    AttemptActivity.WAITING
                    if event.type is WorkerEventType.WAITING
                    else AttemptActivity.RUNNING
                )
                observed = attempt.observe(
                    activity,
                    event_cursor=event.cursor,
                    heartbeat_at=event.occurred_at,
                    progress_at=event.occurred_at,
                    lease_expires_at=attempt.lease_expires_at,
                )
                uow.states.put_attempt(observed)
            uow.commit()
            return is_new

    def _execution_from_attempt(self, attempt: Attempt) -> ConnectorExecution:
        if attempt.execution_handle is None or attempt.agent_session_ref_id is None:
            raise RuntimeError(f"Attempt {attempt.attempt_id} has no ExecutionHandle")
        with self._uow_factory() as uow:
            session = uow.states.get_agent_session_ref(attempt.agent_session_ref_id)
        if session is None:
            raise RuntimeError(f"Attempt {attempt.attempt_id} has no AgentSessionRef")
        return ConnectorExecution(
            attempt.attempt_id,
            session.provider_session_id,
            attempt.execution_handle.provider_execution_id,
            session.recoverable,
        )

    def _persist_registry(self, uow: UnitOfWork) -> None:
        uow.worker_registry.put_worker_profile(self._profile)
        uow.worker_registry.put_worker_endpoint(self._endpoint)

    def _complete_work(self, work: DispatchWork) -> None:
        with self._uow_factory() as uow:
            stored = uow.states.get_dispatch_work(work.dispatch_work_id)
            if stored is None:
                raise RuntimeError(f"DispatchWork {work.dispatch_work_id} disappeared")
            if stored.status is DispatchWorkStatus.CLAIMED:
                uow.states.put_dispatch_work(stored.complete(at=self._clock()))
            uow.commit()


def _required_attempt(uow: UnitOfWork, attempt_id: ID) -> Attempt:
    attempt = uow.states.get_attempt(attempt_id)
    if attempt is None:
        raise RuntimeError(f"Attempt {attempt_id} is not persisted")
    return attempt
