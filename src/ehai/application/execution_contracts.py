"""P2 execution contracts fixed by I0 without implementing the runtime."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

OPENAI_CREDENTIAL_REF = "env:OPENAI_API_KEY"
"""The only credential reference allowed for the P2 built-in OpenAI worker."""


class AttemptExecutionKind(StrEnum):
    """The two execution forms to which one Attempt may bind."""

    BUILTIN_TURN = "builtin_turn"
    EXTERNAL_EXECUTION = "external_execution"


class SessionPolicy(StrEnum):
    """How a Dispatcher must obtain the Agent Session for an Attempt."""

    NEW = "new"
    REUSE = "reuse"
    FORK = "fork"


class AttemptActivity(StrEnum):
    """Live observation of non-terminal work, separate from Attempt lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    STALLED = "stalled"


_CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


def normalize_capabilities(capabilities: Iterable[str]) -> frozenset[str]:
    """Validate opaque capability names without teaching core code provider details."""
    normalized = frozenset(capabilities)
    if any(
        not isinstance(capability, str) or _CAPABILITY_NAME.fullmatch(capability) is None
        for capability in normalized
    ):
        raise ValueError("capabilities must use lower-case dotted, dashed, or underscored names")
    return normalized


def supports_capabilities(*, offered: Iterable[str], required: Iterable[str]) -> bool:
    """Return whether every required capability is explicitly offered.

    Capability names are intentionally open. An unrecognized requirement therefore
    remains representable but cannot accidentally match a Worker that did not declare it.
    """
    return normalize_capabilities(required).issubset(normalize_capabilities(offered))


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
