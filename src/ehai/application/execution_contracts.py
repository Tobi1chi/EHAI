"""P2 execution contracts fixed by I0 without implementing the runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar, runtime_checkable

from ehai.domain.workers import (
    AttemptActivity,
    AttemptExecutionKind,
    SessionPolicy,
    normalize_capabilities,
    supports_capabilities,
)

OPENAI_CREDENTIAL_REF = "env:OPENAI_API_KEY"
"""The only credential reference allowed for the P2 built-in OpenAI worker."""

__all__ = [
    "OPENAI_CREDENTIAL_REF",
    "AttemptActivity",
    "AttemptExecutionKind",
    "Connector",
    "SessionPolicy",
    "normalize_capabilities",
    "supports_capabilities",
]


StartRequestT = TypeVar("StartRequestT", contravariant=True)
RecoveryRequestT = TypeVar("RecoveryRequestT", contravariant=True)
ExecutionT = TypeVar("ExecutionT")
WorkerEventT = TypeVar("WorkerEventT", covariant=True)
InspectionT = TypeVar("InspectionT", covariant=True)


@runtime_checkable
class Connector(Protocol[StartRequestT, RecoveryRequestT, ExecutionT, WorkerEventT, InspectionT]):
    """Provider-neutral asynchronous port for one external or built-in Worker."""

    async def start(self, request: StartRequestT) -> ExecutionT:
        """Start or resolve the idempotent execution described by the request."""
        ...

    def events(
        self,
        execution: ExecutionT,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEventT]:
        """Stream normalized events after an optional provider cursor."""
        ...

    async def inspect(self, execution: ExecutionT) -> InspectionT:
        """Inspect current provider activity without changing domain lifecycle."""
        ...

    async def cancel(self, execution: ExecutionT) -> None:
        """Request cancellation without deciding Attempt or PlanNode completion."""
        ...

    async def recover(self, request: RecoveryRequestT) -> ExecutionT:
        """Recover the referenced execution without starting a replacement."""
        ...
