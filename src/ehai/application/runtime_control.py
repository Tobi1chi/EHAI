"""P2 commands for active Attempts and explicit Worker interaction requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ehai import ID, JsonValue, format_utc_datetime, new_id, normalize_id, utc_now
from ehai.application.async_runtime import ConnectorExecution, RuntimeConnector
from ehai.application.orchestrator import Orchestrator
from ehai.application.ports import UnitOfWork
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run


class WorkerRequestKind(StrEnum):
    COMMAND_APPROVAL = "command_approval"
    FILE_CHANGE_APPROVAL = "file_change_approval"
    USER_INPUT = "user_input"
    PERMISSION_APPROVAL = "permission_approval"


class WorkerRequestStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class WaitingWorkerRequestView:
    """Sanitized request metadata with no provider transcript or internal path."""

    worker_request_id: ID
    attempt_id: ID
    kind: WorkerRequestKind
    summary: str
    status: WorkerRequestStatus = WorkerRequestStatus.PENDING


class RuntimeControlError(RuntimeError):
    """A P2 Runtime command could not be applied to current execution state."""


class _PendingProviderRequest(Protocol):
    request_id: int | str
    method: str
    thread_id: str
    turn_id: str
    item_id: str

    @property
    def params(self) -> dict[str, JsonValue]: ...


@runtime_checkable
class InteractiveRuntimeConnector(Protocol):
    def pending_requests(
        self,
        execution: ConnectorExecution | None = None,
    ) -> tuple[_PendingProviderRequest, ...]: ...

    async def resolve_request(
        self,
        request_id: int | str,
        result: Mapping[str, JsonValue],
    ) -> None: ...


class ControllableRuntime(Protocol):
    @property
    def orchestrator(self) -> Orchestrator: ...

    def connector_for(self, worker_endpoint_id: ID) -> RuntimeConnector | None: ...

    async def cancel_attempt(self, attempt_id: ID) -> Run: ...


class RuntimeControlService:
    """Coordinate explicit P2 commands with one single-process Runtime registry."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        orchestrator: Orchestrator,
        runtimes: Mapping[ID, ControllableRuntime],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._orchestrator = orchestrator
        self._runtimes = {normalize_id(key): value for key, value in runtimes.items()}
        self._clock = clock
        self._public_ids: dict[tuple[ID, str], ID] = {}
        self._bindings: dict[ID, tuple[InteractiveRuntimeConnector, int | str]] = {}
        self._views: dict[ID, WaitingWorkerRequestView] = {}
        self._command_results: dict[str, tuple[str, object]] = {}

    def list_waiting_requests(self, attempt_id: ID) -> tuple[WaitingWorkerRequestView, ...]:
        attempt = self._attempt(attempt_id)
        if attempt.status is not AttemptStatus.RUNNING or attempt.worker_endpoint_id is None:
            return ()
        runtime = self._runtimes.get(attempt.worker_endpoint_id)
        if runtime is None:
            return ()
        connector = runtime.connector_for(attempt.worker_endpoint_id)
        if connector is None:
            return ()
        if not isinstance(connector, InteractiveRuntimeConnector):
            return ()
        execution = self._execution(attempt)
        views: list[WaitingWorkerRequestView] = []
        for request in connector.pending_requests(execution):
            key = (attempt.attempt_id, str(request.request_id))
            public_id = self._public_ids.setdefault(key, new_id())
            view = WaitingWorkerRequestView(
                public_id,
                attempt.attempt_id,
                _request_kind(request.method),
                _request_summary(request),
            )
            self._bindings[public_id] = (connector, request.request_id)
            self._views[public_id] = view
            views.append(view)
        return tuple(views)

    async def resolve_worker_request(
        self,
        worker_request_id: ID,
        resolution: Mapping[str, JsonValue],
        *,
        idempotency_key: str | None = None,
    ) -> WaitingWorkerRequestView:
        cached = self._cached(idempotency_key, "ResolveWorkerRequest", WaitingWorkerRequestView)
        if cached is not None:
            return cached
        request_id = normalize_id(worker_request_id)
        binding = self._bindings.get(request_id)
        view = self._views.get(request_id)
        if binding is None or view is None:
            raise RuntimeControlError(f"Worker request {request_id} is not pending")
        connector, provider_request_id = binding
        await connector.resolve_request(provider_request_id, resolution)
        resolved = replace(view, status=WorkerRequestStatus.RESOLVED)
        self._views[request_id] = resolved
        del self._bindings[request_id]
        self._remember(idempotency_key, "ResolveWorkerRequest", resolved)
        return resolved

    async def decline_worker_request(
        self,
        worker_request_id: ID,
        *,
        idempotency_key: str | None = None,
    ) -> WaitingWorkerRequestView:
        cached = self._cached(idempotency_key, "DeclineWorkerRequest", WaitingWorkerRequestView)
        if cached is not None:
            return cached
        request_id = normalize_id(worker_request_id)
        binding = self._bindings.get(request_id)
        view = self._views.get(request_id)
        if binding is None or view is None:
            raise RuntimeControlError(f"Worker request {request_id} is not pending")
        connector, provider_request_id = binding
        result = _decline_result(view.kind)
        await connector.resolve_request(provider_request_id, result)
        declined = replace(view, status=WorkerRequestStatus.DECLINED)
        self._views[request_id] = declined
        del self._bindings[request_id]
        self._remember(idempotency_key, "DeclineWorkerRequest", declined)
        return declined

    def extend_deadline(
        self,
        attempt_id: ID,
        deadline_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> Attempt:
        cached = self._cached(idempotency_key, "ExtendAttemptDeadline", Attempt)
        if cached is not None:
            return cached
        normalized_id = normalize_id(attempt_id)
        with self._uow_factory() as uow:
            attempt = uow.states.get_attempt(normalized_id)
            if attempt is None:
                raise RuntimeControlError(f"Attempt {normalized_id} was not found")
            extended = attempt.extend_deadline(deadline_at)
            assert extended.deadline_at is not None
            uow.states.put_attempt(extended)
            uow.events.append(
                Event(
                    type=EventType.ATTEMPT_DEADLINE_EXTENDED,
                    correlation_id=attempt.attempt_id,
                    run_id=attempt.run_id,
                    payload={
                        "attempt_id": attempt.attempt_id,
                        "deadline_at": format_utc_datetime(extended.deadline_at),
                    },
                    occurred_at=self._clock(),
                )
            )
            uow.commit()
            self._remember(idempotency_key, "ExtendAttemptDeadline", extended)
            return extended

    async def cancel_attempt(
        self,
        attempt_id: ID,
        *,
        idempotency_key: str | None = None,
    ) -> Run:
        cached = self._cached(idempotency_key, "CancelAttempt", Run)
        if cached is not None:
            return cached
        attempt = self._attempt(attempt_id)
        if attempt.status is AttemptStatus.PENDING:
            result = self._orchestrator.cancel_queued_attempt(attempt.attempt_id)
            self._remember(idempotency_key, "CancelAttempt", result)
            return result
        if attempt.status is not AttemptStatus.RUNNING:
            with self._uow_factory() as uow:
                run = uow.states.get_run(attempt.run_id)
            if run is None:
                raise RuntimeControlError(f"Run {attempt.run_id} was not found")
            self._remember(idempotency_key, "CancelAttempt", run)
            return run
        if attempt.worker_endpoint_id is None:
            raise RuntimeControlError(f"Attempt {attempt.attempt_id} has no Worker Endpoint")
        runtime = self._runtimes.get(attempt.worker_endpoint_id)
        if runtime is None:
            raise RuntimeControlError(
                f"Worker Endpoint {attempt.worker_endpoint_id} has no active Runtime"
            )
        result = await runtime.cancel_attempt(attempt.attempt_id)
        self._remember(idempotency_key, "CancelAttempt", result)
        return result

    def _attempt(self, attempt_id: ID) -> Attempt:
        normalized_id = normalize_id(attempt_id)
        with self._uow_factory() as uow:
            attempt = uow.states.get_attempt(normalized_id)
        if attempt is None:
            raise RuntimeControlError(f"Attempt {normalized_id} was not found")
        return attempt

    def _execution(self, attempt: Attempt) -> ConnectorExecution:
        if attempt.agent_session_ref_id is None or attempt.execution_handle is None:
            raise RuntimeControlError(f"Attempt {attempt.attempt_id} has no execution binding")
        with self._uow_factory() as uow:
            session = uow.states.get_agent_session_ref(attempt.agent_session_ref_id)
        if session is None:
            raise RuntimeControlError(f"Attempt {attempt.attempt_id} has no Agent Session")
        return ConnectorExecution(
            attempt.attempt_id,
            session.provider_session_id,
            attempt.execution_handle.provider_execution_id,
            session.recoverable,
        )

    def _cached[T](
        self,
        key: str | None,
        command_name: str,
        expected_type: type[T],
    ) -> T | None:
        if key is None:
            return None
        stored = self._command_results.get(key)
        if stored is None:
            return None
        stored_command, result = stored
        if stored_command != command_name:
            raise RuntimeControlError(
                f"idempotency key {key!r} was already used for {stored_command}"
            )
        if not isinstance(result, expected_type):
            raise RuntimeError("Runtime Control idempotency result type is corrupted")
        return result

    def _remember(self, key: str | None, command_name: str, result: object) -> None:
        if key is not None:
            self._command_results[key] = (command_name, result)


def _request_kind(method: str) -> WorkerRequestKind:
    return {
        "item/commandExecution/requestApproval": WorkerRequestKind.COMMAND_APPROVAL,
        "item/fileChange/requestApproval": WorkerRequestKind.FILE_CHANGE_APPROVAL,
        "item/tool/requestUserInput": WorkerRequestKind.USER_INPUT,
        "item/permissions/requestApproval": WorkerRequestKind.PERMISSION_APPROVAL,
    }[method]


def _request_summary(request: _PendingProviderRequest) -> str:
    if request.method == "item/tool/requestUserInput":
        questions = request.params.get("questions")
        if isinstance(questions, list):
            text = " ".join(
                str(question.get("question", ""))
                for question in questions
                if isinstance(question, dict)
            ).strip()
            if text:
                return text[:500]
    return {
        "item/commandExecution/requestApproval": "command execution approval required",
        "item/fileChange/requestApproval": "file change approval required",
        "item/tool/requestUserInput": "user input required",
        "item/permissions/requestApproval": "additional permissions approval required",
    }[request.method]


def _decline_result(kind: WorkerRequestKind) -> dict[str, JsonValue]:
    if kind in {WorkerRequestKind.COMMAND_APPROVAL, WorkerRequestKind.FILE_CHANGE_APPROVAL}:
        return {"decision": "decline"}
    if kind is WorkerRequestKind.PERMISSION_APPROVAL:
        return {"permissions": {}}
    return {"answers": {}}
