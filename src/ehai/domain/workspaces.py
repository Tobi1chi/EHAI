"""P2 Workspace references and ownership-bound leases."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from ehai import ID, new_id, normalize_id, utc_now

_REHYDRATE = object()


class WorkspaceKind(StrEnum):
    DIRECTORY = "directory"
    GIT_WORKTREE = "git_worktree"


class WorkspaceLeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    PRESERVED = "preserved"


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """One concrete Workspace path with explicit EHAI ownership."""

    run_id: ID
    path: str
    kind: WorkspaceKind
    ehai_owned: bool
    ownership_token: str | None = None
    workspace_ref_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", normalize_id(self.run_id))
        object.__setattr__(self, "workspace_ref_id", normalize_id(self.workspace_ref_id))
        object.__setattr__(self, "kind", WorkspaceKind(self.kind))
        if not self.path.strip():
            raise ValueError("WorkspaceRef path must not be blank")
        if self.ehai_owned != (self.ownership_token is not None):
            raise ValueError("EHAI-owned WorkspaceRef requires exactly one ownership token")
        if self.ownership_token is not None and not self.ownership_token.strip():
            raise ValueError("WorkspaceRef ownership_token must not be blank")


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """One Attempt's read or write claim over a WorkspaceRef."""

    attempt_id: ID
    workspace_ref_id: ID
    write_capable: bool
    workspace_lease_id: ID = field(default_factory=new_id)
    status: WorkspaceLeaseStatus = WorkspaceLeaseStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))
        object.__setattr__(self, "workspace_ref_id", normalize_id(self.workspace_ref_id))
        object.__setattr__(self, "workspace_lease_id", normalize_id(self.workspace_lease_id))
        object.__setattr__(self, "status", WorkspaceLeaseStatus(self.status))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "ended_at", _optional_utc(self.ended_at))
        if not isinstance(self.write_capable, bool):
            raise ValueError("WorkspaceLease write_capable must be a boolean")
        if self.status is not WorkspaceLeaseStatus.ACTIVE and _rehydrate_token is not _REHYDRATE:
            raise ValueError("terminal WorkspaceLease must use transition or rehydrate")
        if self.status is WorkspaceLeaseStatus.ACTIVE and self.ended_at is not None:
            raise ValueError("active WorkspaceLease cannot have ended_at")
        if self.status is not WorkspaceLeaseStatus.ACTIVE and self.ended_at is None:
            raise ValueError("terminal WorkspaceLease requires ended_at")

    @classmethod
    def rehydrate(
        cls,
        *,
        attempt_id: ID,
        workspace_ref_id: ID,
        write_capable: bool,
        workspace_lease_id: ID,
        status: WorkspaceLeaseStatus,
        created_at: datetime,
        ended_at: datetime | None,
    ) -> Self:
        return cls(
            attempt_id=attempt_id,
            workspace_ref_id=workspace_ref_id,
            write_capable=write_capable,
            workspace_lease_id=workspace_lease_id,
            status=status,
            created_at=created_at,
            ended_at=ended_at,
            _rehydrate_token=_REHYDRATE,
        )

    def release(self, *, at: datetime | None = None) -> Self:
        return self._finish(WorkspaceLeaseStatus.RELEASED, at)

    def preserve(self, *, at: datetime | None = None) -> Self:
        return self._finish(WorkspaceLeaseStatus.PRESERVED, at)

    def _finish(self, status: WorkspaceLeaseStatus, at: datetime | None) -> Self:
        if self.status is not WorkspaceLeaseStatus.ACTIVE:
            raise ValueError(f"WorkspaceLease {self.workspace_lease_id} is not active")
        return replace(
            self,
            status=status,
            ended_at=_utc(at or utc_now()),
            _rehydrate_token=_REHYDRATE,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Workspace timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)
