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
    claim_owner: str | None = None
    lease_expires_at: datetime | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "run_id", normalize_id(self.run_id))
        object.__setattr__(self, "dispatch_work_id", normalize_id(self.dispatch_work_id))
        object.__setattr__(self, "status", DispatchWorkStatus(self.status))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "claimed_at", _optional_utc(self.claimed_at))
        object.__setattr__(self, "completed_at", _optional_utc(self.completed_at))
        object.__setattr__(self, "lease_expires_at", _optional_utc(self.lease_expires_at))
        if self.claim_owner is not None and not self.claim_owner.strip():
            raise ValueError("DispatchWork claim_owner must not be blank")
        if self.status is not DispatchWorkStatus.PENDING and _rehydrate_token is not _REHYDRATE:
            raise ValueError("non-pending DispatchWork must use a transition or rehydrate")
        if self.status is DispatchWorkStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.claimed_at,
                    self.completed_at,
                    self.claim_owner,
                    self.lease_expires_at,
                )
            ):
                raise ValueError("pending DispatchWork cannot have transition timestamps")
        elif self.status is DispatchWorkStatus.CLAIMED:
            if (
                self.claimed_at is None
                or self.claim_owner is None
                or self.lease_expires_at is None
                or self.completed_at is not None
            ):
                raise ValueError("claimed DispatchWork requires owner and lease")
        elif (
            self.claimed_at is None
            or self.completed_at is None
            or self.claim_owner is None
            or self.lease_expires_at is None
        ):
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
        claim_owner: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> Self:
        return cls(
            run_id=run_id,
            dispatch_work_id=dispatch_work_id,
            status=status,
            created_at=created_at,
            claimed_at=claimed_at,
            completed_at=completed_at,
            claim_owner=claim_owner,
            lease_expires_at=lease_expires_at,
            _rehydrate_token=_REHYDRATE,
        )

    def claim(
        self,
        owner: str,
        lease_expires_at: datetime,
        *,
        at: datetime | None = None,
    ) -> Self:
        if self.status is not DispatchWorkStatus.PENDING:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} is not pending")
        if not owner.strip():
            raise ValueError("DispatchWork claim owner must not be blank")
        return replace(
            self,
            status=DispatchWorkStatus.CLAIMED,
            claimed_at=_utc(at or utc_now()),
            claim_owner=owner,
            lease_expires_at=_utc(lease_expires_at),
            _rehydrate_token=_REHYDRATE,
        )

    def reclaim(
        self,
        owner: str,
        lease_expires_at: datetime,
        *,
        at: datetime | None = None,
    ) -> Self:
        """Take over only an expired claim after a Runtime crash."""
        timestamp = _utc(at or utc_now())
        if self.status is not DispatchWorkStatus.CLAIMED:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} is not claimed")
        if self.lease_expires_at is None or self.lease_expires_at > timestamp:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} claim lease has not expired")
        if not owner.strip():
            raise ValueError("DispatchWork claim owner must not be blank")
        return replace(
            self,
            claimed_at=timestamp,
            claim_owner=owner,
            lease_expires_at=_utc(lease_expires_at),
            _rehydrate_token=_REHYDRATE,
        )

    def release(self, owner: str) -> Self:
        """Return a quiesced host's claim to the queue without changing its identity."""
        if self.status is not DispatchWorkStatus.CLAIMED or self.claim_owner != owner:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} is not claimed by {owner}")
        return replace(
            self,
            status=DispatchWorkStatus.PENDING,
            claimed_at=None,
            completed_at=None,
            claim_owner=None,
            lease_expires_at=None,
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

    def requeue(self) -> Self:
        """Reuse a settled dispatch request when its Run is explicitly resumed."""
        if self.status is not DispatchWorkStatus.COMPLETED:
            raise ValueError(f"DispatchWork {self.dispatch_work_id} is not completed")
        return replace(
            self,
            status=DispatchWorkStatus.PENDING,
            claimed_at=None,
            completed_at=None,
            claim_owner=None,
            lease_expires_at=None,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("DispatchWork timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)
