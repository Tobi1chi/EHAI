"""Single-slot P2 dispatch work state used before the Scheduler Increment."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from ehai import ID, new_id, normalize_id, utc_now

_REHYDRATE = object()


class DispatchWorkStatus(StrEnum):
    """Durable state for one Run-level background dispatch request."""

    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class DispatchWork:
    """One idempotent request for the single-slot Runtime to advance a Run."""

    run_id: ID
    dispatch_work_id: ID = field(default_factory=new_id)
    status: DispatchWorkStatus = DispatchWorkStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "run_id", normalize_id(self.run_id))
        object.__setattr__(self, "dispatch_work_id", normalize_id(self.dispatch_work_id))
        object.__setattr__(self, "status", DispatchWorkStatus(self.status))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "claimed_at", _optional_utc(self.claimed_at))
        object.__setattr__(self, "completed_at", _optional_utc(self.completed_at))
        if self.status is not DispatchWorkStatus.PENDING and _rehydrate_token is not _REHYDRATE:
            raise ValueError("non-pending DispatchWork must use a transition or rehydrate")
        if self.status is DispatchWorkStatus.PENDING:
            if self.claimed_at is not None or self.completed_at is not None:
                raise ValueError("pending DispatchWork cannot have transition timestamps")
        elif self.status is DispatchWorkStatus.CLAIMED:
            if self.claimed_at is None or self.completed_at is not None:
                raise ValueError("claimed DispatchWork requires only claimed_at")
        elif self.claimed_at is None or self.completed_at is None:
            raise ValueError("completed DispatchWork requires claim and completion timestamps")

    @classmethod
    def rehydrate(
        cls,
        *,
        run_id: ID,
        dispatch_work_id: ID,
        status: DispatchWorkStatus,
        created_at: datetime,
        claimed_at: datetime | None,
        completed_at: datetime | None,
    ) -> Self:
        return cls(
            run_id=run_id,
            dispatch_work_id=dispatch_work_id,
            status=status,
            created_at=created_at,
            claimed_at=claimed_at,
            completed_at=completed_at,
            _rehydrate_token=_REHYDRATE,
        )

    def claim(self, *, at: datetime | None = None) -> Self:
        if self.status is not DispatchWorkStatus.PENDING:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} is not pending")
        return replace(
            self,
            status=DispatchWorkStatus.CLAIMED,
            claimed_at=_utc(at or utc_now()),
            _rehydrate_token=_REHYDRATE,
        )

    def complete(self, *, at: datetime | None = None) -> Self:
        if self.status is not DispatchWorkStatus.CLAIMED:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} is not claimed")
        return replace(
            self,
            status=DispatchWorkStatus.COMPLETED,
            completed_at=_utc(at or utc_now()),
            _rehydrate_token=_REHYDRATE,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("DispatchWork timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)
