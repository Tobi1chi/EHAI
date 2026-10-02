"""SQLite repositories: current state (split by aggregate into ``state``), events and receipts."""

from __future__ import annotations

import sqlite3

from ehai import ID, format_utc_datetime, json_dumps, json_loads, parse_utc_datetime
from ehai.application.ports import COMMAND_RECEIPT_SCOPE, CommandReceipt, StoredEvent
from ehai.domain.events import Event
from ehai.infrastructure.sqlite.state.adoptions import AdoptionStateMixin
from ehai.infrastructure.sqlite.state.checks import CheckStateMixin
from ehai.infrastructure.sqlite.state.common import (
    DuplicateEventError,
    IdempotencyConflictError,
    PersistenceConflictError,
    SQLiteWorkerRegistry,
    UnknownEventCursorError,
    _row_index_integer,
    _row_index_string,
    _row_string,
)
from ehai.infrastructure.sqlite.state.planning import PlanningStateMixin
from ehai.infrastructure.sqlite.state.process import ProcessStateMixin
from ehai.infrastructure.sqlite.state.runs import RunStateMixin

__all__ = [
    "DuplicateEventError",
    "IdempotencyConflictError",
    "PersistenceConflictError",
    "SQLiteCommandReceiptStore",
    "SQLiteCurrentStateRepository",
    "SQLiteEventLog",
    "SQLiteWorkerRegistry",
    "UnknownEventCursorError",
]


class SQLiteEventLog:
    """Append-only Event Log sharing its caller's SQLite transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def append(self, event: Event) -> StoredEvent:
        try:
            cursor = self._connection.execute(
                """
                INSERT INTO event_log(event_id, run_id, event_type, occurred_at, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.run_id,
                    event.type.value,
                    format_utc_datetime(event.occurred_at),
                    event.to_json(),
                ),
            )
        except sqlite3.IntegrityError as error:
            if self._connection.execute(
                "SELECT 1 FROM event_log WHERE event_id = ?", (event.id,)
            ).fetchone():
                raise DuplicateEventError(f"Event {event.id} was already appended") from error
            raise
        offset = cursor.lastrowid
        if offset is None:  # pragma: no cover - SQLite always assigns the integer primary key
            raise RuntimeError("SQLite did not assign an Event offset")
        return StoredEvent(offset=offset, event=event)

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Event limit must be a non-negative integer")
        after_offset = 0
        if after_event_id is not None:
            row = self._connection.execute(
                "SELECT event_offset FROM event_log WHERE event_id = ?",
                (after_event_id,),
            ).fetchone()
            if row is None:
                raise UnknownEventCursorError(f"unknown Event cursor {after_event_id}")
            after_offset = _row_index_integer(row, 0)

        sql = """
            SELECT event_offset, event_json FROM event_log
            WHERE event_offset > ? ORDER BY event_offset
        """
        parameters: tuple[object, ...] = (after_offset,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (after_offset, limit)
        return tuple(
            StoredEvent(
                offset=_row_index_integer(row, 0),
                event=Event.from_json(_row_index_string(row, 1)),
            )
            for row in self._connection.execute(sql, parameters).fetchall()
        )

    def latest_offset(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(event_offset), 0) FROM event_log"
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate always returns one row
            return 0
        return _row_index_integer(row, 0)


class SQLiteCommandReceiptStore:
    """Durable idempotency receipts of every scope, sharing the caller's transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def put(self, receipt: CommandReceipt) -> None:
        existing = self.get(receipt.idempotency_key, scope=receipt.scope)
        if existing is not None:
            if (
                existing.command_name != receipt.command_name
                or existing.command_fingerprint != receipt.command_fingerprint
                or existing.result != receipt.result
            ):
                raise IdempotencyConflictError(
                    receipt.idempotency_key,
                    existing.command_fingerprint,
                    receipt.command_fingerprint,
                )
            return
        self._connection.execute(
            """
            INSERT INTO command_receipts(
                scope, idempotency_key, command_name, command_fingerprint,
                result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.scope,
                receipt.idempotency_key,
                receipt.command_name,
                receipt.command_fingerprint,
                json_dumps(receipt.result),
                None if receipt.created_at is None else format_utc_datetime(receipt.created_at),
            ),
        )

    def get(
        self, idempotency_key: str, *, scope: str = COMMAND_RECEIPT_SCOPE
    ) -> CommandReceipt | None:
        row = self._connection.execute(
            """
            SELECT command_name, command_fingerprint, result_json, created_at
            FROM command_receipts WHERE scope = ? AND idempotency_key = ?
            """,
            (scope, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        result = json_loads(_row_string(row, "result_json"))
        if not isinstance(result, dict):  # pragma: no cover - SQL CHECK and writer guarantee this
            raise RuntimeError("stored Command receipt result is not an object")
        return CommandReceipt(
            scope=scope,
            idempotency_key=idempotency_key,
            command_name=_row_string(row, "command_name"),
            command_fingerprint=_row_string(row, "command_fingerprint"),
            result=result,
            created_at=(
                None
                if row["created_at"] is None
                else parse_utc_datetime(_row_string(row, "created_at"))
            ),
        )


class SQLiteCurrentStateRepository(
    PlanningStateMixin, ProcessStateMixin, AdoptionStateMixin, RunStateMixin, CheckStateMixin
):
    """Current-state snapshots and retained version history in one SQLite transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _snapshot(self, table: str, id_column: str, entity_id: ID) -> str | None:
        row = self._connection.execute(
            f"SELECT snapshot_json FROM {table} WHERE {id_column} = ?",
            (entity_id,),
        ).fetchone()
        return None if row is None else _row_string(row, "snapshot_json")

    def _snapshots(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> tuple[str, ...]:
        return tuple(
            _row_string(row, "snapshot_json")
            for row in self._connection.execute(sql, parameters).fetchall()
        )

    def _strings(self, sql: str, parameters: tuple[object, ...]) -> tuple[str, ...]:
        rows = self._connection.execute(sql, parameters).fetchall()
        return tuple(_row_index_string(row, 0) for row in rows)
