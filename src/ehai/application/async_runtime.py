"""Single-slot P2 background Runtime without Scheduler or multi-Endpoint routing."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ehai import ID, new_id, normalize_id, utc_now
from ehai.application.execution_policy import (
    EndpointHealth,
    EndpointHealthStatus,
    ExecutionPolicy,
    ExecutionTimeoutKind,
    ExecutionWatchdog,
    RetrySafety,
    TimeoutDecision,
)
from ehai.application.orchestrator import Orchestrator, WorkerEventReceipt
from ehai.application.ports import UnitOfWork
from ehai.application.workers import WorkerRequest, WorkerResult
from ehai.domain.events import Event, EventType
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
    HEARTBEAT = "heartbeat"
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
    retry_safety: RetrySafety = RetrySafety.UNKNOWN
    provider_cost: float | None = None
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.worker_event_id, str) or not self.worker_event_id.strip():
            raise ValueError("WorkerEvent worker_event_id must not be blank")
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))
        object.__setattr__(self, "type", WorkerEventType(self.type))
        object.__setattr__(self, "retry_safety", RetrySafety(self.retry_safety))
        if not isinstance(self.cursor, str) or not self.cursor.strip():
            raise ValueError("WorkerEvent cursor must not be blank")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("WorkerEvent occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        if self.type is WorkerEventType.CANDIDATE and self.result is None:
            raise ValueError("candidate WorkerEvent requires WorkerResult")
        if self.type is WorkerEventType.FAILED and not self.reason:
            raise ValueError("failed WorkerEvent requires a reason")
        if self.provider_cost is not None and (
            not isinstance(self.provider_cost, (int, float))
            or isinstance(self.provider_cost, bool)
            or not math.isfinite(self.provider_cost)
            or self.provider_cost < 0
        ):
            raise ValueError("WorkerEvent provider_cost must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ConnectorStartRequest:
    """Start one execution using Attempt ID as the provider idempotency key."""

    request: WorkerRequest
    workspace: str | None = None

    def __post_init__(self) -> None:
        if self.workspace is not None and (
            not isinstance(self.workspace, str) or not self.workspace.strip()
        ):
            raise ValueError("ConnectorStartRequest workspace must not be blank")

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


@runtime_checkable
class HealthAwareConnector(Protocol):
    """Optional Endpoint health observation, separate from Session progress."""

    async def health(self) -> EndpointHealthStatus: ...


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
        policy: ExecutionPolicy | None = None,
        workspace: str | Path | None = None,
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
        self._policy = ExecutionPolicy() if policy is None else policy
        self._workspace = None if workspace is None else str(Path(workspace).resolve(strict=True))
        self._watchdog = ExecutionWatchdog(self._policy)
        self._clock = clock
        self._id_factory = id_factory
        self._claim_owner = f"runtime:{self._id_factory()}"
        self._endpoint_health = EndpointHealth(
            endpoint.worker_endpoint_id,
            EndpointHealthStatus.UNKNOWN,
            self._clock(),
        )

    @property
    def endpoint_health(self) -> EndpointHealth:
        """Return the latest connection/health-derived Endpoint observation."""
        return self._endpoint_health

    @property
    def connector(self) -> RuntimeConnector:
        """Return the Connector owned by this Endpoint Runtime."""
        return self._connector

    def connector_for(self, worker_endpoint_id: ID) -> RuntimeConnector | None:
        """Return this Runtime's Connector when the Endpoint identity matches."""
        return (
            self._connector
            if normalize_id(worker_endpoint_id) == self._endpoint.worker_endpoint_id
            else None
        )

    @property
    def orchestrator(self) -> Orchestrator:
        """Return the application Orchestrator used by Runtime Control."""
        return self._orchestrator

    async def run_once(self) -> Run | None:
        """Claim the oldest pending work and advance its Run to a terminal state."""
        with self._uow_factory() as uow:
            self._persist_registry(uow)
            claimed_at = self._clock()
            work = uow.states.claim_next_dispatch_work(
                owner=self._claim_owner,
                at=claimed_at,
                lease_expires_at=claimed_at + timedelta(minutes=5),
            )
            if work is not None:
                uow.events.append(
                    Event(
                        type=EventType.DISPATCH_WORK_CLAIMED,
                        correlation_id=work.dispatch_work_id,
                        run_id=work.run_id,
                        payload={
                            "dispatch_work_id": work.dispatch_work_id,
                            "claim_owner": self._claim_owner,
                        },
                        occurred_at=claimed_at,
                    )
                )
            uow.commit()
        if work is None:
            return None
        return await self._recover_work(work)

    async def recover_startup(self) -> tuple[Run, ...]:
        """Recover referenced work and retry only confirmed-missing executions."""
        works = self._claimed_for_recovery()
        return tuple([await self._recover_work(work) for work in works])

    def _claimed_for_recovery(self) -> tuple[DispatchWork, ...]:
        recovered: list[DispatchWork] = []
        with self._uow_factory() as uow:
            self._persist_registry(uow)
            now = self._clock()
            for work in uow.states.list_dispatch_work(DispatchWorkStatus.CLAIMED):
                if work.claim_owner == self._claim_owner:
                    recovered.append(work)
                    continue
                if work.lease_expires_at is not None and work.lease_expires_at <= now:
                    reclaimed = work.reclaim(
                        self._claim_owner,
                        now + timedelta(minutes=5),
                        at=now,
                    )
                    uow.states.put_dispatch_work(reclaimed)
                    uow.events.append(
                        Event(
                            type=EventType.DISPATCH_WORK_CLAIMED,
                            correlation_id=reclaimed.dispatch_work_id,
                            run_id=reclaimed.run_id,
                            payload={
                                "dispatch_work_id": reclaimed.dispatch_work_id,
                                "claim_owner": self._claim_owner,
                            },
                            occurred_at=now,
                        )
                    )
                    recovered.append(reclaimed)
            uow.commit()
        return tuple(recovered)

    async def _recover_work(self, work: DispatchWork) -> Run:
        with self._uow_factory() as uow:
            stored_run = uow.states.get_run(work.run_id)
            if stored_run is None:
                raise RuntimeError(f"Run {work.run_id} is not persisted")
            attempts = uow.states.list_attempts(work.run_id)
            running = tuple(
                attempt for attempt in attempts if attempt.status is AttemptStatus.RUNNING
            )
        if not running:
            if stored_run.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                self._complete_work(work)
                return stored_run
            return await self._process_work(work)
        attempt = running[-1]
        run = await self.recover_attempt(attempt)
        if run.status is RunStatus.RUNNING:
            return await self._process_work(work)
        self._complete_work(work)
        return run

    async def recover_attempt(self, attempt: Attempt) -> Run:
        """Recover one already-bound Attempt without scheduling sibling work."""
        execution = self._execution_from_attempt(attempt)
        try:
            recovered = await self._connector.recover(
                ConnectorRecoveryRequest(
                    attempt.attempt_id,
                    execution.provider_session_id,
                    execution.provider_execution_id,
                    attempt.event_cursor,
                )
            )
        except Exception as error:
            await self._set_endpoint_health(
                EndpointHealthStatus.UNHEALTHY,
                run_id=attempt.run_id,
                reason=f"recover failed: {type(error).__name__}",
            )
            run = self._orchestrator.interrupt_attempt(
                attempt.attempt_id,
                f"provider recovery state is unknown: {type(error).__name__}: {error}",
            )
            return run
        if recovered is None:
            return self._orchestrator.retry_attempt(
                attempt.attempt_id,
                "original provider execution was not found during Runtime recovery",
                retry_safety=RetrySafety.EXECUTION_NOT_FOUND,
            )
        await self._set_endpoint_health(
            EndpointHealthStatus.HEALTHY,
            run_id=attempt.run_id,
            reason=None,
        )
        return await self._consume_events(attempt, recovered)

    async def cancel_attempt(self, attempt_id: ID) -> Run:
        """Let a completed provider result win a cancel/completed race."""
        attempt, execution = await self.request_cancel(attempt_id)
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

    async def request_cancel(
        self,
        attempt_id: ID,
    ) -> tuple[Attempt, ConnectorExecution]:
        """Request provider cancellation without opening a second event consumer."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, normalize_id(attempt_id))
        execution = self._execution_from_attempt(attempt)
        await self._connector.cancel(execution)
        return attempt, execution

    async def execute_started_request(self, request: WorkerRequest) -> Run:
        """Execute one already-started queued Attempt through this Endpoint."""
        started = await self._start_request(request)
        if isinstance(started, Run):
            return started
        execution = started
        bound = self._bind_execution(request.attempt, execution)
        return await self._consume_events(bound, execution)

    def finish_dispatch_work(self, work: DispatchWork) -> None:
        """Complete claimed Run-level work after its Run reaches a terminal state."""
        self._complete_work(work)

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
            started = await self._start_request(request)
            if isinstance(started, Run):
                if started.status is not RunStatus.RUNNING:
                    self._complete_work(work)
                    return started
                continue
            execution = started
            bound_attempt = self._bind_execution(request.attempt, execution)
            run = await self._consume_events(bound_attempt, execution)
            if run.status is not RunStatus.RUNNING:
                self._complete_work(work)
                return run

    async def _start_request(self, request: WorkerRequest) -> ConnectorExecution | Run:
        self._record_dispatch(request)
        budget_failure = self._budget_failure(request.run_id)
        if budget_failure is not None:
            return self._orchestrator.fail_attempt(request.attempt_id, budget_failure)
        if isinstance(self._connector, HealthAwareConnector):
            try:
                reported_health = await self._connector.health()
            except Exception as error:
                await self._set_endpoint_health(
                    EndpointHealthStatus.UNHEALTHY,
                    run_id=request.run_id,
                    reason=f"Endpoint health check failed: {type(error).__name__}",
                )
            else:
                await self._set_endpoint_health(
                    reported_health,
                    run_id=request.run_id,
                    reason=None,
                )
        try:
            execution = await asyncio.wait_for(
                self._connector.start(ConnectorStartRequest(request, self._workspace)),
                self._policy.start_timeout.total_seconds(),
            )
            if execution.attempt_id != request.attempt_id:
                raise RuntimeError("Connector start returned another Attempt ID")
        except TimeoutError:
            await self._set_endpoint_health(
                EndpointHealthStatus.UNHEALTHY,
                run_id=request.run_id,
                reason="Connector start timed out",
            )
            return self._orchestrator.retry_attempt(
                request.attempt_id,
                "Connector start timed out before an ExecutionHandle was persisted",
                retry_safety=RetrySafety.NO_EXECUTION_HANDLE,
                timed_out=True,
            )
        except Exception as error:
            await self._set_endpoint_health(
                EndpointHealthStatus.UNHEALTHY,
                run_id=request.run_id,
                reason=f"Connector start failed: {type(error).__name__}",
            )
            return self._orchestrator.retry_attempt(
                request.attempt_id,
                f"Connector start failed before an ExecutionHandle was persisted: "
                f"{type(error).__name__}: {error}",
                retry_safety=RetrySafety.NO_EXECUTION_HANDLE,
            )
        await self._set_endpoint_health(
            EndpointHealthStatus.HEALTHY,
            run_id=request.run_id,
            reason=None,
        )
        return execution

    def _record_dispatch(self, request: WorkerRequest) -> None:
        with self._uow_factory() as uow:
            already_recorded = any(
                stored.event.type is EventType.ATTEMPT_DISPATCHED
                and stored.event.correlation_id == request.attempt_id
                for stored in uow.events.list_events()
            )
            if not already_recorded:
                uow.events.append(
                    Event(
                        type=EventType.ATTEMPT_DISPATCHED,
                        correlation_id=request.attempt_id,
                        run_id=request.run_id,
                        payload={"attempt_id": request.attempt_id},
                        occurred_at=self._clock(),
                    )
                )
            uow.commit()

    def _budget_failure(self, run_id: ID) -> str | None:
        now = self._clock()
        with self._uow_factory() as uow:
            run = uow.states.get_run(run_id)
            if run is None:
                raise RuntimeError(f"Run {run_id} is not persisted")
            events = uow.events.list_events()
        if run.started_at is not None and now >= run.started_at + self._policy.max_run_duration:
            return "Run time budget exhausted before Connector start"
        connector_calls = sum(
            event.event.type is EventType.ATTEMPT_BOUND
            for event in events
            if event.event.run_id == run_id
        )
        if connector_calls >= self._policy.max_connector_calls:
            return (
                "Connector call budget exhausted: "
                f"consumed={connector_calls}, limit={self._policy.max_connector_calls}"
            )
        if self._policy.max_provider_cost is not None:
            usage_events = tuple(
                event.event.payload
                for event in events
                if event.event.run_id == run_id
                and event.event.type is EventType.PROVIDER_USAGE_RECORDED
            )
            numeric_costs: list[float] = []
            for payload in usage_events:
                cost = payload.get("provider_cost")
                if type(cost) not in {int, float}:
                    numeric_costs.clear()
                    break
                assert isinstance(cost, (int, float))
                numeric_costs.append(float(cost))
            if usage_events and len(numeric_costs) == len(usage_events):
                total_cost = sum(numeric_costs)
                if total_cost >= self._policy.max_provider_cost:
                    return (
                        "provider cost budget exhausted: "
                        f"consumed={total_cost}, limit={self._policy.max_provider_cost}"
                    )
        return None

    def _bind_execution(
        self,
        attempt: Attempt,
        execution: ConnectorExecution,
    ) -> Attempt:
        with self._uow_factory() as uow:
            stored = _required_attempt(uow, attempt.attempt_id)
            run = uow.states.get_run(stored.run_id)
            if run is None or run.started_at is None:
                raise RuntimeError(f"Attempt {stored.attempt_id} has no running Run")
            observed_at = self._clock()
            attempt_deadline = (
                stored.started_at or observed_at
            ) + self._policy.absolute_attempt_timeout
            run_deadline = run.started_at + self._policy.max_run_duration
            deadline_at = min(attempt_deadline, run_deadline)
            lease_expires_at = observed_at + self._policy.heartbeat_lease
            session = AgentSessionRef(
                run_id=stored.run_id,
                worker_profile_id=self._profile.worker_profile_id,
                worker_endpoint_id=self._endpoint.worker_endpoint_id,
                provider_session_id=execution.provider_session_id,
                recoverable=execution.recoverable,
                agent_session_ref_id=self._id_factory(),
                created_at=observed_at,
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
            bound = (
                stored.assign(
                    profile=self._profile,
                    endpoint=self._endpoint,
                    session=session,
                    deadline_at=deadline_at,
                    lease_expires_at=lease_expires_at,
                )
                .bind_execution(handle)
                .observe(
                    AttemptActivity.RUNNING,
                    event_cursor=stored.event_cursor,
                    heartbeat_at=observed_at,
                    progress_at=stored.progress_at or stored.started_at or observed_at,
                    lease_expires_at=lease_expires_at,
                )
            )
            uow.states.put_attempt(bound)
            uow.events.append(
                Event(
                    type=EventType.ATTEMPT_BOUND,
                    correlation_id=bound.attempt_id,
                    run_id=bound.run_id,
                    payload={
                        "attempt_id": bound.attempt_id,
                        "worker_profile_id": self._profile.worker_profile_id,
                        "worker_endpoint_id": self._endpoint.worker_endpoint_id,
                        "agent_session_ref_id": session.agent_session_ref_id,
                        "provider_execution_id": execution.provider_execution_id,
                    },
                    occurred_at=observed_at,
                )
            )
            uow.commit()
            return bound

    async def _consume_events(
        self,
        attempt: Attempt,
        execution: ConnectorExecution,
    ) -> Run:
        candidate: WorkerResult | None = None
        pending_events: list[WorkerEvent] = []
        iterator = self._connector.events(
            execution,
            after_cursor=attempt.event_cursor,
        ).__aiter__()
        next_event: asyncio.Task[WorkerEvent] = asyncio.create_task(_next_worker_event(iterator))
        try:
            while True:
                if not next_event.done():
                    await asyncio.sleep(0)
                if next_event.done():
                    try:
                        event = next_event.result()
                    except StopAsyncIteration as error:
                        raise RuntimeError(
                            f"Connector event stream ended before Attempt {attempt.attempt_id}"
                        ) from error
                    next_event = asyncio.create_task(_next_worker_event(iterator))
                    terminal, candidate = self._apply_event(
                        attempt.attempt_id,
                        event,
                        candidate,
                        pending_events,
                    )
                    if terminal is not None:
                        return terminal
                stored = self._stored_attempt(attempt.attempt_id)
                now = self._clock()
                decision = self._watchdog.evaluate(stored, at=now)
                if decision is not None:
                    if decision.kind is ExecutionTimeoutKind.HEARTBEAT_LEASE:
                        try:
                            activity = await asyncio.wait_for(
                                self._connector.inspect(execution),
                                self._policy.cancel_grace.total_seconds(),
                            )
                        except Exception as error:
                            await self._set_endpoint_health(
                                EndpointHealthStatus.UNHEALTHY,
                                run_id=attempt.run_id,
                                reason=f"execution inspect failed: {type(error).__name__}",
                            )
                        else:
                            if activity in {AttemptActivity.RUNNING, AttemptActivity.WAITING}:
                                await self._set_endpoint_health(
                                    EndpointHealthStatus.HEALTHY,
                                    run_id=attempt.run_id,
                                    reason=None,
                                )
                                self._record_heartbeat(attempt.attempt_id, activity)
                                continue
                    return await self._cancel_timed_out(
                        attempt,
                        execution,
                        iterator,
                        next_event,
                        candidate,
                        pending_events,
                        decision,
                    )
                delay = max(
                    0.001,
                    (self._watchdog.next_check_at(stored) - now).total_seconds(),
                )
                done, _ = await asyncio.wait({next_event}, timeout=delay)
                if not done:
                    continue
        finally:
            if not next_event.done():
                next_event.cancel()

    def _apply_event(
        self,
        attempt_id: ID,
        event: WorkerEvent,
        candidate: WorkerResult | None,
        pending_events: list[WorkerEvent],
    ) -> tuple[Run | None, WorkerResult | None]:
        if event.attempt_id != attempt_id:
            raise RuntimeError("Connector emitted a WorkerEvent for another Attempt")
        if event.type is WorkerEventType.CANDIDATE:
            if not any(item.worker_event_id == event.worker_event_id for item in pending_events):
                pending_events.append(event)
                self._observe_deferred_event(attempt_id, event)
            return None, event.result
        if event.type is WorkerEventType.COMPLETED:
            if candidate is None:
                raise RuntimeError("Worker completed without a candidate")
            terminal = self._orchestrator.accept_worker_result(
                attempt_id,
                candidate,
                worker_event_receipts=_event_receipts((*pending_events, event)),
            )
            return terminal, candidate
        if event.type is WorkerEventType.FAILED:
            reason = event.reason or "Worker failed"
            if event.retry_safety.allows_retry:
                terminal = self._orchestrator.retry_attempt(
                    attempt_id,
                    reason,
                    retry_safety=event.retry_safety,
                    worker_event_receipts=_event_receipts((*pending_events, event)),
                )
            else:
                terminal = self._orchestrator.interrupt_attempt(
                    attempt_id,
                    reason,
                    worker_event_receipts=_event_receipts((*pending_events, event)),
                )
            return terminal, candidate
        if pending_events:
            if not any(item.worker_event_id == event.worker_event_id for item in pending_events):
                pending_events.append(event)
                self._observe_deferred_event(attempt_id, event)
            return None, candidate
        if not self._record_event(attempt_id, event):
            return None, candidate
        return None, candidate

    async def _cancel_timed_out(
        self,
        attempt: Attempt,
        execution: ConnectorExecution,
        iterator: AsyncIterator[WorkerEvent],
        next_event: asyncio.Task[WorkerEvent],
        candidate: WorkerResult | None,
        pending_events: list[WorkerEvent],
        decision: TimeoutDecision,
    ) -> Run:
        loop = asyncio.get_running_loop()
        grace_deadline = loop.time() + self._policy.cancel_grace.total_seconds()
        try:
            await asyncio.wait_for(
                self._connector.cancel(execution),
                self._policy.cancel_grace.total_seconds(),
            )
        except Exception as error:
            await self._set_endpoint_health(
                EndpointHealthStatus.UNHEALTHY,
                run_id=attempt.run_id,
                reason=f"cancel failed: {type(error).__name__}",
            )
        try:
            while (remaining := grace_deadline - loop.time()) > 0:
                done, _ = await asyncio.wait({next_event}, timeout=remaining)
                if not done:
                    break
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    break
                if event.attempt_id != attempt.attempt_id:
                    raise RuntimeError("Connector emitted a WorkerEvent for another Attempt")
                if event.type is WorkerEventType.CANDIDATE:
                    if not any(
                        item.worker_event_id == event.worker_event_id for item in pending_events
                    ):
                        pending_events.append(event)
                        self._observe_deferred_event(attempt.attempt_id, event)
                    candidate = event.result
                elif event.type is WorkerEventType.COMPLETED and candidate is not None:
                    terminal = self._orchestrator.accept_worker_result(
                        attempt.attempt_id,
                        candidate,
                        worker_event_receipts=_event_receipts((*pending_events, event)),
                    )
                    return terminal
                elif event.type is WorkerEventType.FAILED:
                    pending_events.append(event)
                    break
                elif pending_events:
                    pending_events.append(event)
                    self._observe_deferred_event(attempt.attempt_id, event)
                else:
                    self._record_event(attempt.attempt_id, event)
                next_event = asyncio.create_task(_next_worker_event(iterator))
            reason = f"{decision.reason}; cancellation grace expired"
            return self._orchestrator.time_out_attempt(
                attempt.attempt_id,
                reason,
                worker_event_receipts=_event_receipts(tuple(pending_events)),
            )
        finally:
            if not next_event.done():
                next_event.cancel()

    def _record_event(self, attempt_id: ID, event: WorkerEvent) -> bool:
        if event.type in {
            WorkerEventType.CANDIDATE,
            WorkerEventType.COMPLETED,
            WorkerEventType.FAILED,
        }:
            raise ValueError("candidate and terminal WorkerEvents require deferred recording")
        with self._uow_factory() as uow:
            is_new = uow.states.record_worker_event(
                attempt_id,
                event.worker_event_id,
                at=event.occurred_at,
            )
            if is_new:
                attempt = _required_attempt(uow, attempt_id)
                if event.type is WorkerEventType.WAITING:
                    activity = AttemptActivity.WAITING
                elif event.type is WorkerEventType.HEARTBEAT:
                    activity = attempt.activity or AttemptActivity.RUNNING
                else:
                    activity = AttemptActivity.RUNNING
                heartbeat_at = max(
                    value
                    for value in (attempt.heartbeat_at, event.occurred_at)
                    if value is not None
                )
                progress_at = (
                    attempt.progress_at
                    if event.type is WorkerEventType.HEARTBEAT
                    else max(
                        value
                        for value in (attempt.progress_at, event.occurred_at)
                        if value is not None
                    )
                )
                observed = attempt.observe(
                    activity,
                    event_cursor=event.cursor,
                    heartbeat_at=heartbeat_at,
                    progress_at=progress_at,
                    lease_expires_at=heartbeat_at + self._policy.heartbeat_lease,
                )
                uow.states.put_attempt(observed)
                if event.type is WorkerEventType.WAITING:
                    uow.events.append(
                        Event(
                            type=EventType.ATTEMPT_WAITING,
                            correlation_id=attempt.attempt_id,
                            run_id=attempt.run_id,
                            payload={
                                "attempt_id": attempt.attempt_id,
                                "reason": event.reason,
                            },
                            occurred_at=event.occurred_at,
                        )
                    )
                if event.type is WorkerEventType.HEARTBEAT:
                    uow.events.append(self._heartbeat_event(attempt, event.occurred_at))
            uow.commit()
            return is_new

    def _observe_deferred_event(self, attempt_id: ID, event: WorkerEvent) -> None:
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            if event.type is WorkerEventType.WAITING:
                activity = AttemptActivity.WAITING
            elif event.type is WorkerEventType.HEARTBEAT:
                activity = attempt.activity or AttemptActivity.RUNNING
            else:
                activity = AttemptActivity.RUNNING
            heartbeat_at = max(
                value for value in (attempt.heartbeat_at, event.occurred_at) if value is not None
            )
            progress_at = (
                attempt.progress_at
                if event.type is WorkerEventType.HEARTBEAT
                else max(
                    value for value in (attempt.progress_at, event.occurred_at) if value is not None
                )
            )
            uow.states.put_attempt(
                attempt.observe(
                    activity,
                    event_cursor=attempt.event_cursor,
                    heartbeat_at=heartbeat_at,
                    progress_at=progress_at,
                    lease_expires_at=heartbeat_at + self._policy.heartbeat_lease,
                )
            )
            uow.commit()

    def _record_heartbeat(self, attempt_id: ID, activity: AttemptActivity) -> None:
        observed_at = self._clock()
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            observed = attempt.observe(
                activity,
                event_cursor=attempt.event_cursor,
                heartbeat_at=observed_at,
                progress_at=attempt.progress_at,
                lease_expires_at=observed_at + self._policy.heartbeat_lease,
            )
            uow.states.put_attempt(observed)
            uow.events.append(self._heartbeat_event(attempt, observed_at))
            uow.commit()

    @staticmethod
    def _heartbeat_event(attempt: Attempt, occurred_at: datetime) -> Event:
        return Event(
            type=EventType.ATTEMPT_HEARTBEAT_OBSERVED,
            correlation_id=attempt.attempt_id,
            run_id=attempt.run_id,
            payload={"attempt_id": attempt.attempt_id},
            occurred_at=occurred_at,
        )

    def _stored_attempt(self, attempt_id: ID) -> Attempt:
        with self._uow_factory() as uow:
            return _required_attempt(uow, attempt_id)

    async def _set_endpoint_health(
        self,
        status: EndpointHealthStatus,
        *,
        run_id: ID,
        reason: str | None,
    ) -> None:
        observed = EndpointHealth(
            self._endpoint.worker_endpoint_id,
            status,
            self._clock(),
            reason,
        )
        changed = observed.status is not self._endpoint_health.status
        self._endpoint_health = observed
        if not changed:
            return
        with self._uow_factory() as uow:
            uow.events.append(
                Event(
                    type=EventType.ENDPOINT_HEALTH_CHANGED,
                    correlation_id=self._endpoint.worker_endpoint_id,
                    run_id=run_id,
                    payload={
                        "worker_endpoint_id": self._endpoint.worker_endpoint_id,
                        "status": observed.status.value,
                        "reason": reason,
                    },
                    occurred_at=observed.checked_at,
                )
            )
            uow.commit()

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


def _event_receipts(events: tuple[WorkerEvent, ...]) -> tuple[WorkerEventReceipt, ...]:
    return tuple(
        WorkerEventReceipt(
            event.worker_event_id,
            event.cursor,
            event.occurred_at,
            event.provider_cost,
            event.type in {WorkerEventType.COMPLETED, WorkerEventType.FAILED},
        )
        for event in events
    )


def _required_attempt(uow: UnitOfWork, attempt_id: ID) -> Attempt:
    attempt = uow.states.get_attempt(attempt_id)
    if attempt is None:
        raise RuntimeError(f"Attempt {attempt_id} is not persisted")
    return attempt


async def _next_worker_event(iterator: AsyncIterator[WorkerEvent]) -> WorkerEvent:
    return await anext(iterator)
