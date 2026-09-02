"""SQLite persistence Adapter for the P1 execution plane."""

from ehai.infrastructure.sqlite.database import SQLiteDatabase, SQLiteUnitOfWork
from ehai.infrastructure.sqlite.migrations import (
    LATEST_SCHEMA_VERSION,
    P1_SCHEMA_VERSION,
    SchemaVersionError,
)
from ehai.infrastructure.sqlite.repository import (
    DuplicateEventError,
    IdempotencyConflictError,
    PersistenceConflictError,
    SQLiteCommandReceiptStore,
    SQLiteCurrentStateRepository,
    SQLiteEventLog,
    SQLiteWorkerRegistry,
    UnknownEventCursorError,
)

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "P1_SCHEMA_VERSION",
    "DuplicateEventError",
    "IdempotencyConflictError",
    "PersistenceConflictError",
    "SQLiteCommandReceiptStore",
    "SQLiteCurrentStateRepository",
    "SQLiteDatabase",
    "SQLiteEventLog",
    "SQLiteUnitOfWork",
    "SQLiteWorkerRegistry",
    "SchemaVersionError",
    "UnknownEventCursorError",
]
