"""EHAI-owned Git worktree lifecycle and persisted Workspace leases.

Host Git commands do not consume stdin. In particular, they must not inherit a
managed child's parent-monitor pipe, which can block Git startup on Windows.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ehai import (
    ID,
    JsonValue,
    json_dumps,
    json_loads,
    new_id,
    normalize_id,
    parse_utc_datetime,
    utc_now,
)
from ehai.application.scheduler import WorkspaceAllocationPort
from ehai.domain.events import Event, EventType
from ehai.domain.workers import AgentSessionRef, SessionPolicy
from ehai.domain.workspaces import (
    WorkspaceKind,
    WorkspaceLease,
    WorkspaceLeaseStatus,
    WorkspaceRef,
)
from ehai.infrastructure.sqlite.database import SQLiteDatabase
from ehai.infrastructure.sqlite.repository import SQLiteEventLog


class WorkspaceBusyError(RuntimeError):
    """Raised when a non-Git write Workspace already has an active writer."""


@dataclass(frozen=True, slots=True)
class WorkspaceAllocation:
    reference: WorkspaceRef
    lease: WorkspaceLease


class WorkspaceManager:
    """Create and clean only path/token-matched EHAI-owned Git worktrees."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        base_workspace: Path,
        owned_root: Path,
        clock: Callable[[], datetime] = utc_now,
        preserve_completed: bool = False,
    ) -> None:
        self.database = database
        self.base_workspace = base_workspace.resolve()
        self.owned_root = owned_root.resolve()
        self.clock = clock
        self.preserve_completed = preserve_completed

    def allocate(
        self,
        *,
        run_id: ID,
        attempt_id: ID,
        write_capable: bool,
        isolate: bool,
    ) -> WorkspaceAllocation:
        git_workspace = self._is_git_workspace()
        if write_capable and isolate and git_workspace:
            self.owned_root.mkdir(parents=True, exist_ok=True)
            path = (self.owned_root / str(attempt_id)).resolve()
            self._require_owned_path(path)
            if path.exists():
                raise ValueError(f"owned worktree path {path} already exists")
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-C",
                    str(self.base_workspace),
                    "worktree",
                    "add",
                    "--detach",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            reference = WorkspaceRef(
                run_id,
                str(path),
                WorkspaceKind.GIT_WORKTREE,
                True,
                ownership_token=str(new_id()),
            )
        else:
            if write_capable and not git_workspace and self._has_active_writer():
                raise WorkspaceBusyError("non-Git Workspace already has an active writer")
            reference = WorkspaceRef(
                run_id,
                str(self.base_workspace),
                WorkspaceKind.DIRECTORY,
                False,
            )
        lease = WorkspaceLease(
            attempt_id,
            reference.workspace_ref_id,
            write_capable,
            created_at=self.clock(),
        )
        self._store(reference, lease)
        return WorkspaceAllocation(reference, lease)

    def can_isolate_writes(self) -> bool:
        """Return whether concurrent writers can receive Git worktrees."""
        return self._is_git_workspace()

    def allocation_for_attempt(self, attempt_id: ID) -> WorkspaceAllocation | None:
        """Load the latest persisted Workspace allocation for Runtime recovery."""
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT r.snapshot_json, l.snapshot_json
                FROM workspace_leases l
                JOIN workspace_refs r ON r.workspace_ref_id = l.workspace_ref_id
                WHERE l.attempt_id = ?
                ORDER BY l.created_at DESC
                LIMIT 1
                """,
                (normalize_id(attempt_id),),
            ).fetchone()
            if row is None:
                return None
            return WorkspaceAllocation(_decode_ref(row[0]), _decode_lease(row[1]))
        finally:
            connection.close()

    def cleanup(self, allocation: WorkspaceAllocationPort) -> WorkspaceLease:
        reference = allocation.reference
        lease = allocation.lease
        if not reference.ehai_owned:
            released = lease.release(at=self.clock())
            self._update_lease(released)
            return released
        path = Path(reference.path).resolve()
        self._require_owned_path(path)
        stored = self._load_ref(reference.workspace_ref_id)
        if stored != reference or stored.ownership_token != reference.ownership_token:
            raise ValueError("Workspace ownership record does not match cleanup request")
        if self.preserve_completed:
            preserved = lease.preserve(at=self.clock())
            self._update_lease(preserved, preserved_path=reference.path)
            return preserved
        dirty = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        ).stdout
        if dirty:
            preserved = lease.preserve(at=self.clock())
            self._update_lease(preserved, preserved_path=reference.path)
            return preserved
        subprocess.run(
            ["git", "-C", str(self.base_workspace), "worktree", "remove", str(path)],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        released = lease.release(at=self.clock())
        self._update_lease(released)
        return released

    def select_provider_session_id(
        self,
        policy: SessionPolicy,
        *,
        predecessor: AgentSessionRef | None,
        new_provider_session_id: str,
    ) -> str:
        """Apply new/reuse/fork without allowing implicit Session sharing."""
        if policy is SessionPolicy.REUSE:
            if predecessor is None or not predecessor.recoverable:
                raise ValueError("reuse requires a recoverable predecessor Session")
            return predecessor.provider_session_id
        if not new_provider_session_id.strip():
            raise ValueError("new provider Session ID must not be blank")
        if (
            policy is SessionPolicy.FORK
            and predecessor is not None
            and predecessor.provider_session_id == new_provider_session_id
        ):
            raise ValueError("fork must create a new provider Session ID")
        return new_provider_session_id

    def _is_git_workspace(self) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.base_workspace), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _has_active_writer(self) -> bool:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT 1 FROM workspace_leases l
                JOIN workspace_refs r ON r.workspace_ref_id = l.workspace_ref_id
                WHERE r.path = ? AND l.write_capable = 1 AND l.status = 'active'
                LIMIT 1
                """,
                (str(self.base_workspace),),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def _store(self, reference: WorkspaceRef, lease: WorkspaceLease) -> None:
        connection = self.database.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO workspace_refs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    reference.workspace_ref_id,
                    reference.run_id,
                    reference.path,
                    reference.kind.value,
                    int(reference.ehai_owned),
                    reference.ownership_token,
                    _encode_ref(reference),
                ),
            )
            connection.execute(
                "INSERT INTO workspace_leases VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    lease.workspace_lease_id,
                    lease.attempt_id,
                    lease.workspace_ref_id,
                    int(lease.write_capable),
                    lease.status.value,
                    lease.created_at.isoformat(),
                    _encode_lease(lease),
                ),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _update_lease(self, lease: WorkspaceLease, *, preserved_path: str | None = None) -> None:
        connection = self.database.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE workspace_leases SET status = ?, snapshot_json = ?
                WHERE workspace_lease_id = ?
                """,
                (lease.status.value, _encode_lease(lease), lease.workspace_lease_id),
            )
            if preserved_path is not None:
                reference = self._load_ref_with_connection(connection, lease.workspace_ref_id)
                SQLiteEventLog(connection).append(
                    Event(
                        type=EventType.WORKSPACE_PRESERVED,
                        run_id=reference.run_id,
                        correlation_id=lease.workspace_lease_id,
                        payload={
                            "workspace_lease_id": lease.workspace_lease_id,
                            "path": preserved_path,
                            "reason": "dirty EHAI-owned worktree",
                        },
                        occurred_at=self.clock(),
                    )
                )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _load_ref(self, workspace_ref_id: ID) -> WorkspaceRef:
        connection = self.database.connect()
        try:
            return self._load_ref_with_connection(connection, workspace_ref_id)
        finally:
            connection.close()

    @staticmethod
    def _load_ref_with_connection(
        connection: sqlite3.Connection,
        workspace_ref_id: ID,
    ) -> WorkspaceRef:
        row = connection.execute(
            "SELECT snapshot_json FROM workspace_refs WHERE workspace_ref_id = ?",
            (workspace_ref_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"WorkspaceRef {workspace_ref_id} is not persisted")
        return _decode_ref(row[0])

    def _require_owned_path(self, path: Path) -> None:
        if path == self.owned_root or not path.is_relative_to(self.owned_root):
            raise ValueError("owned worktree path escapes the configured EHAI root")


def _encode_ref(reference: WorkspaceRef) -> str:
    return json_dumps(
        {
            "workspace_ref_id": reference.workspace_ref_id,
            "run_id": reference.run_id,
            "path": reference.path,
            "kind": reference.kind.value,
            "ehai_owned": reference.ehai_owned,
            "ownership_token": reference.ownership_token,
        }
    )


def _decode_ref(snapshot: str) -> WorkspaceRef:
    value = json_loads(snapshot)
    if not isinstance(value, dict):
        raise ValueError("WorkspaceRef snapshot is not an object")
    ownership_value = value.get("ownership_token")
    ownership_token = ownership_value if isinstance(ownership_value, str) else None
    return WorkspaceRef(
        ID(_string(value, "run_id")),
        _string(value, "path"),
        WorkspaceKind(_string(value, "kind")),
        bool(value.get("ehai_owned")),
        ownership_token,
        ID(_string(value, "workspace_ref_id")),
    )


def _decode_lease(snapshot: str) -> WorkspaceLease:
    value = json_loads(snapshot)
    if not isinstance(value, dict):
        raise ValueError("WorkspaceLease snapshot is not an object")
    ended_value = value.get("ended_at")
    if ended_value is not None and not isinstance(ended_value, str):
        raise ValueError("WorkspaceLease snapshot ended_at must be text or null")
    write_capable = value.get("write_capable")
    if not isinstance(write_capable, bool):
        raise ValueError("WorkspaceLease snapshot write_capable must be a boolean")
    return WorkspaceLease.rehydrate(
        attempt_id=ID(_string(value, "attempt_id")),
        workspace_ref_id=ID(_string(value, "workspace_ref_id")),
        write_capable=write_capable,
        workspace_lease_id=ID(_string(value, "workspace_lease_id")),
        status=WorkspaceLeaseStatus(_string(value, "status")),
        created_at=parse_utc_datetime(_string(value, "created_at")),
        ended_at=None if ended_value is None else parse_utc_datetime(ended_value),
    )


def _encode_lease(lease: WorkspaceLease) -> str:
    return json_dumps(
        {
            "workspace_lease_id": lease.workspace_lease_id,
            "attempt_id": lease.attempt_id,
            "workspace_ref_id": lease.workspace_ref_id,
            "write_capable": lease.write_capable,
            "status": lease.status.value,
            "created_at": lease.created_at.isoformat(),
            "ended_at": None if lease.ended_at is None else lease.ended_at.isoformat(),
        }
    )


def _string(value: dict[str, JsonValue], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"Workspace snapshot {key} is not text")
    return item
