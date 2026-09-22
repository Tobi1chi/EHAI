"""Serialize delivery/acknowledgement with the existing append-only SQLite Event log."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

from ehai import ID, format_utc_datetime, new_id, normalize_id, parse_utc_datetime
from ehai.application.event_consumers import (
    EventConsumer,
    EventConsumerAcknowledgement,
    EventConsumerBatch,
    EventConsumerNotFoundError,
)
from ehai.application.ports import StateConflictError
from ehai.infrastructure.sqlite.database import SQLiteDatabase
from ehai.infrastructure.sqlite.repository import SQLiteEventLog


class SQLiteEventConsumerRepository:
    """Persist a single outstanding batch and every ack receipt per named consumer."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def register(self, consumer_id: str) -> EventConsumer:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO event_consumers(consumer_id, created_at) VALUES (?, ?) "
                "ON CONFLICT(consumer_id) DO NOTHING",
                (consumer_id, format_utc_datetime(datetime.now(UTC))),
            )
            return _consumer(connection, consumer_id)

    def get(self, consumer_id: str) -> EventConsumer:
        with self._transaction(write=False) as connection:
            return _consumer(connection, consumer_id)

    def read(self, consumer_id: str, *, limit: int) -> EventConsumerBatch:
        with self._transaction() as connection:
            consumer = _consumer(connection, consumer_id)
            pending = connection.execute(
                "SELECT * FROM event_consumer_batches WHERE consumer_id = ? AND acknowledged = 0",
                (consumer_id,),
            ).fetchone()
            event_log = SQLiteEventLog(connection)
            if pending is not None:
                events = event_log.list_events(
                    after_event_id=_optional_id(pending["after_event_id"]),
                    limit=cast(int, pending["event_count"]),
                )
                through_offset = cast(int, pending["through_offset"])
                if not events or events[-1].offset != through_offset:
                    raise StateConflictError("Retained event batch no longer matches the event log")
                return EventConsumerBatch(
                    consumer_id=consumer_id,
                    batch_token=normalize_id(cast(str, pending["batch_token"])),
                    after_offset=cast(int, pending["after_offset"]),
                    through_offset=through_offset,
                    events=events,
                )
            events = event_log.list_events(
                after_event_id=consumer.acknowledged_event_id, limit=limit
            )
            if not events:
                return EventConsumerBatch(
                    consumer_id,
                    None,
                    consumer.acknowledged_offset,
                    consumer.acknowledged_offset,
                    (),
                )
            token = new_id()
            last = events[-1]
            connection.execute(
                """
                INSERT INTO event_consumer_batches (
                    batch_token, consumer_id, after_offset, through_offset,
                    after_event_id, through_event_id, event_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    consumer_id,
                    consumer.acknowledged_offset,
                    last.offset,
                    consumer.acknowledged_event_id,
                    last.event.id,
                    len(events),
                ),
            )
            return EventConsumerBatch(
                consumer_id, token, consumer.acknowledged_offset, last.offset, events
            )

    def acknowledge(self, consumer_id: str, *, batch_token: ID) -> EventConsumerAcknowledgement:
        with self._transaction() as connection:
            consumer = _consumer(connection, consumer_id)
            batch = connection.execute(
                "SELECT * FROM event_consumer_batches WHERE consumer_id = ? AND batch_token = ?",
                (consumer_id, batch_token),
            ).fetchone()
            if batch is None:
                raise StateConflictError("Batch token was not delivered to this consumer")
            through_offset = cast(int, batch["through_offset"])
            through_event_id = normalize_id(cast(str, batch["through_event_id"]))
            if not batch["acknowledged"]:
                if consumer.acknowledged_offset != batch["after_offset"]:
                    raise StateConflictError(
                        "Consumer position no longer matches the pending batch"
                    )
                connection.execute(
                    "UPDATE event_consumers SET acknowledged_offset = ?, "
                    "acknowledged_event_id = ? WHERE consumer_id = ?",
                    (through_offset, through_event_id, consumer_id),
                )
                connection.execute(
                    "UPDATE event_consumer_batches SET acknowledged = 1 WHERE batch_token = ?",
                    (batch_token,),
                )
            return EventConsumerAcknowledgement(
                consumer_id, batch_token, through_offset, through_event_id
            )


def _optional_id(value: object) -> ID | None:
    return None if value is None else normalize_id(cast(str, value))


def _consumer(connection: sqlite3.Connection, consumer_id: str) -> EventConsumer:
    row = connection.execute(
        """
        SELECT c.*, b.batch_token FROM event_consumers c
        LEFT JOIN event_consumer_batches b ON b.consumer_id = c.consumer_id AND b.acknowledged = 0
        WHERE c.consumer_id = ?
        """,
        (consumer_id,),
    ).fetchone()
    if row is None:
        raise EventConsumerNotFoundError(f"Event consumer {consumer_id} was not found")
    return EventConsumer(
        consumer_id=consumer_id,
        acknowledged_offset=cast(int, row["acknowledged_offset"]),
        acknowledged_event_id=_optional_id(row["acknowledged_event_id"]),
        pending_batch_token=_optional_id(row["batch_token"]),
        created_at=parse_utc_datetime(cast(str, row["created_at"])),
    )
