"""Nonblocking, process-local admission for every planning operation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Concatenate, Protocol


class PlannerCapacityExceeded(RuntimeError):
    """No model request or planning intent was admitted for this operation."""

    def __init__(self, capacity: int, in_use: int) -> None:
        self.capacity = capacity
        self.in_use = in_use
        super().__init__("Planner capacity is full; retry after an active operation finishes")


@dataclass(slots=True)
class _Lease:
    references: int = 1


class PlannerCapacity:
    """Share one slot across nested calls, including asyncio.to_thread descendants.

    References keep the slot occupied if an HTTP coroutine is cancelled while its
    synchronous planning child is still running. Separate request contexts get
    separate leases; a copied but expired context cannot resurrect an old lease.
    This is admission only, not Worker capacity or execution authorization.
    """

    def __init__(self, capacity: int = 1) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("planner_capacity must be a positive integer")
        self.capacity = capacity
        self._lock = Lock()
        self._in_use = 0
        self._lease: ContextVar[_Lease | None] = ContextVar("planner_capacity_lease", default=None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "in_use": self._in_use,
                "available": self.capacity - self._in_use,
            }

    @contextmanager
    def slot(self) -> Iterator[None]:
        inherited = self._lease.get()
        with self._lock:
            if inherited is not None and inherited.references > 0:
                lease = inherited
                lease.references += 1
            else:
                if self._in_use >= self.capacity:
                    raise PlannerCapacityExceeded(self.capacity, self._in_use)
                self._in_use += 1
                lease = _Lease()
        token = self._lease.set(lease)
        try:
            yield
        finally:
            self._lease.reset(token)
            with self._lock:
                lease.references -= 1
                if lease.references == 0:
                    self._in_use -= 1


class _CapacityOwner(Protocol):
    @property
    def planner_capacity(self) -> PlannerCapacity: ...


def planning_operation[Owner: _CapacityOwner, **Parameters, Result](
    function: Callable[Concatenate[Owner, Parameters], Result],
) -> Callable[Concatenate[Owner, Parameters], Result]:
    """Reserve before the service reads/writes a planning command's intent."""

    @wraps(function)
    def admitted(self: Owner, /, *args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        with self.planner_capacity.slot():
            return function(self, *args, **kwargs)

    return admitted


def async_planning_operation[Owner: _CapacityOwner, **Parameters, Result](
    function: Callable[Concatenate[Owner, Parameters], Awaitable[Result]],
) -> Callable[Concatenate[Owner, Parameters], Awaitable[Result]]:
    """Hold the admission lease for the complete async planning lifecycle."""

    @wraps(function)
    async def admitted(
        self: Owner, /, *args: Parameters.args, **kwargs: Parameters.kwargs
    ) -> Result:
        with self.planner_capacity.slot():
            return await function(self, *args, **kwargs)

    return admitted
