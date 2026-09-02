"""Runtime Connector for existing process-per-Attempt WorkerAdapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ehai import ID, new_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.execution_policy import RetrySafety
from ehai.application.workers import WorkerAdapter, WorkerExecutionError, WorkerRequest
from ehai.domain.workers import AttemptActivity


class WorkerAdapterConnector:
    """Keep Codex CLI process-per-Attempt semantics behind the P2 Connector Port."""

    def __init__(self, adapter: WorkerAdapter) -> None:
        if not isinstance(adapter, WorkerAdapter):
            raise TypeError("adapter must implement WorkerAdapter")
        self._adapter = adapter
        self._requests: dict[ID, WorkerRequest] = {}
        self._executions: dict[ID, ConnectorExecution] = {}
        self._active: set[ID] = set()

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        existing = self._executions.get(request.attempt_id)
        if existing is not None:
            return existing
        execution = ConnectorExecution(
            request.attempt_id,
            f"process-attempt:{request.attempt_id}",
            str(new_id()),
            False,
        )
        self._requests[request.attempt_id] = request.request
        self._executions[request.attempt_id] = execution
        return execution

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        request = self._requests[execution.attempt_id]
        self._active.add(execution.attempt_id)
        try:
            result = await asyncio.to_thread(self._adapter.execute, request)
        except WorkerExecutionError as error:
            yield WorkerEvent(
                f"{execution.attempt_id}:failed",
                execution.attempt_id,
                WorkerEventType.FAILED,
                "1",
                reason=str(error),
                retry_safety=RetrySafety.UNKNOWN,
            )
        else:
            yield WorkerEvent(
                f"{execution.attempt_id}:candidate",
                execution.attempt_id,
                WorkerEventType.CANDIDATE,
                "1",
                result=result,
            )
            yield WorkerEvent(
                f"{execution.attempt_id}:completed",
                execution.attempt_id,
                WorkerEventType.COMPLETED,
                "2",
            )
        finally:
            self._active.discard(execution.attempt_id)

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        return (
            AttemptActivity.RUNNING
            if execution.attempt_id in self._active
            else AttemptActivity.STALLED
        )

    async def cancel(self, execution: ConnectorExecution) -> None:
        self._adapter.cancel(execution.attempt_id)

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        del request
        return None
