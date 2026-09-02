"""SQLite factories for explicit write transactions and short read snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from ehai.infrastructure.sqlite.migrations import migrate
from ehai.infrastructure.sqlite.repository import (
    SQLiteCommandReceiptStore,
    SQLiteCurrentStateRepository,
    SQLiteEventLog,
    SQLiteWorkerRegistry,
)


class SQLiteDatabase:
    """Own SQLite configuration and create isolated transactional units of work."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise ValueError("use a temporary SQLite file instead of isolated :memory: connections")
        connection = self.connect()
        try:
            migrate(connection)
        finally:
            connection.close()

    def connect(self) -> sqlite3.Connection:
        """Open one configured connection; the caller owns its lifetime."""
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def unit_of_work(self) -> SQLiteUnitOfWork:
        """Create a fresh Unit of Work; entering it begins the write transaction."""
        return SQLiteUnitOfWork(self)

    def read_session(self) -> SQLiteReadSession:
        """Create a short-lived, query-only session with a deferred snapshot."""
        return SQLiteReadSession(self)

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA query_only = ON")
        return connection


class SQLiteReadSession:
    """One query-only SQLite snapshot that never takes a write transaction."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._connection: sqlite3.Connection | None = None
        self._states: SQLiteCurrentStateRepository | None = None
        self._events: SQLiteEventLog | None = None
        self._worker_registry: SQLiteWorkerRegistry | None = None

    @property
    def states(self) -> SQLiteCurrentStateRepository:
        """Return current-state readers bound to this snapshot."""
        if self._states is None or self._connection is None:
            raise RuntimeError("Read session is not active")
        return self._states

    @property
    def events(self) -> SQLiteEventLog:
        """Return the Event reader bound to this snapshot."""
        if self._events is None or self._connection is None:
            raise RuntimeError("Read session is not active")
        return self._events

    @property
    def worker_registry(self) -> SQLiteWorkerRegistry:
        """Return configured Worker routing entries in this snapshot."""
        if self._worker_registry is None or self._connection is None:
            raise RuntimeError("Read session is not active")
        return self._worker_registry

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("Read session cannot be entered more than once")
        connection = self._database._connect_read_only()
        try:
            connection.execute("BEGIN DEFERRED")
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._states = SQLiteCurrentStateRepository(connection)
        self._events = SQLiteEventLog(connection)
        self._worker_registry = SQLiteWorkerRegistry(connection)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        connection = self._connection
        if connection is None:
            return
        try:
            connection.rollback()
        finally:
            connection.close()
            self._connection = None
            self._states = None
            self._events = None
            self._worker_registry = None


class SQLiteUnitOfWork:
    """One explicit transaction across current state, Events, and receipts."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._connection: sqlite3.Connection | None = None
        self._states: SQLiteCurrentStateRepository | None = None
        self._events: SQLiteEventLog | None = None
        self._command_receipts: SQLiteCommandReceiptStore | None = None
        self._worker_registry: SQLiteWorkerRegistry | None = None
        self._finished = False

    @property
    def states(self) -> SQLiteCurrentStateRepository:
        """Return current-state repositories bound to this transaction."""
        if self._states is None or self._finished:
            raise RuntimeError("Unit of Work is not active")
        return self._states

    @property
    def events(self) -> SQLiteEventLog:
        """Return the append-only Event Log bound to this transaction."""
        if self._events is None or self._finished:
            raise RuntimeError("Unit of Work is not active")
        return self._events

    @property
    def command_receipts(self) -> SQLiteCommandReceiptStore:
        """Return the Command receipt store bound to this transaction."""
        if self._command_receipts is None or self._finished:
            raise RuntimeError("Unit of Work is not active")
        return self._command_receipts

    @property
    def worker_registry(self) -> SQLiteWorkerRegistry:
        """Return the Worker registry bound to this transaction."""
        if self._worker_registry is None or self._finished:
            raise RuntimeError("Unit of Work is not active")
        return self._worker_registry

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("Unit of Work cannot be entered more than once")
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._states = SQLiteCurrentStateRepository(connection)
        self._events = SQLiteEventLog(connection)
        self._command_receipts = SQLiteCommandReceiptStore(connection)
        self._worker_registry = SQLiteWorkerRegistry(connection)
        return self

    def commit(self) -> None:
        """Atomically commit every write made through this Unit of Work."""
        connection = self._active_connection()
        connection.commit()
        self._finished = True

    def rollback(self) -> None:
        """Discard every write made through this Unit of Work."""
        connection = self._active_connection()
        connection.rollback()
        self._finished = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        connection = self._connection
        if connection is None:
            return
        try:
            if not self._finished:
                connection.rollback()
        finally:
            connection.close()

    def _active_connection(self) -> sqlite3.Connection:
        if self._connection is None or self._finished:
            raise RuntimeError("Unit of Work is not active")
        return self._connection
