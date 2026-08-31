import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ehai import new_id
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.domain.events import Event, EventType
from ehai.domain.goal import Project
from ehai.infrastructure.sqlite import (
    LATEST_SCHEMA_VERSION,
    IdempotencyConflictError,
    SchemaVersionError,
    SQLiteDatabase,
    UnknownEventCursorError,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_database_applies_migration_and_connection_pragmas(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "state.sqlite3")

    connection = database.connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        connection.close()


def test_database_rejects_schema_from_the_future(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer than supported"):
        SQLiteDatabase(path)


def test_uow_rolls_back_uncommitted_state_and_event(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "rollback.sqlite3")
    project = Project.create("rollback", project_id=new_id(), created_at=NOW)
    event = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=project.project_id,
        payload={"project_id": project.project_id},
        occurred_at=NOW,
    )

    with database.unit_of_work() as uow:
        assert isinstance(uow, UnitOfWork)
        uow.states.put_project(project)
        uow.events.append(event)

    with database.unit_of_work() as uow:
        assert uow.states.get_project(project.project_id) is None
        assert uow.events.latest_offset() == 0


def test_state_event_and_receipt_commit_atomically_with_idempotency(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "commit.sqlite3")
    project = Project.create("committed", project_id=new_id(), created_at=NOW)
    event = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=project.project_id,
        payload={"project_id": project.project_id},
        occurred_at=NOW,
    )
    receipt = CommandReceipt(
        idempotency_key="create-project-1",
        command_name="CreateProject",
        command_fingerprint="sha256:one",
        result={"project_id": project.project_id},
        created_at=NOW,
    )

    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        stored = uow.events.append(event)
        uow.command_receipts.put(receipt)
        uow.command_receipts.put(receipt)
        uow.commit()
    assert stored.offset == 1

    with database.unit_of_work() as uow:
        assert uow.states.get_project(project.project_id) == project
        assert uow.events.list_events() == (stored,)
        restored_receipt = uow.command_receipts.get(receipt.idempotency_key)
        assert restored_receipt is not None
        assert restored_receipt.result == receipt.result
        with pytest.raises(IdempotencyConflictError):
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=receipt.idempotency_key,
                    command_name=receipt.command_name,
                    command_fingerprint="sha256:different",
                    result={},
                    created_at=NOW,
                )
            )
        with pytest.raises(IdempotencyConflictError):
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=receipt.idempotency_key,
                    command_name="AnotherCommand",
                    command_fingerprint=receipt.command_fingerprint,
                    result=receipt.result,
                    created_at=NOW,
                )
            )
        with pytest.raises(IdempotencyConflictError):
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=receipt.idempotency_key,
                    command_name=receipt.command_name,
                    command_fingerprint=receipt.command_fingerprint,
                    result={"project_id": new_id()},
                    created_at=NOW,
                )
            )
        uow.command_receipts.put(
            CommandReceipt(
                idempotency_key=receipt.idempotency_key,
                command_name=receipt.command_name,
                command_fingerprint=receipt.command_fingerprint,
                result=receipt.result,
                created_at=NOW + timedelta(seconds=1),
            )
        )


def test_event_offsets_are_monotonic_and_cursor_is_fail_closed(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "events.sqlite3")
    correlation_id = new_id()
    events = tuple(
        Event(
            type=EventType.PROJECT_CREATED,
            correlation_id=correlation_id,
            payload={"sequence": index},
            occurred_at=NOW + timedelta(seconds=index),
        )
        for index in range(3)
    )
    with database.unit_of_work() as uow:
        stored = tuple(uow.events.append(event) for event in events)
        uow.commit()

    assert tuple(item.offset for item in stored) == (1, 2, 3)
    with database.unit_of_work() as uow:
        assert uow.events.latest_offset() == 3
        assert uow.events.list_events(after_event_id=events[1].id) == (stored[2],)
        assert uow.events.list_events(limit=2) == stored[:2]
        with pytest.raises(UnknownEventCursorError):
            uow.events.list_events(after_event_id=new_id())
