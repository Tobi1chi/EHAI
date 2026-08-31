from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from ehai import ID, new_id
from ehai.application import (
    ArtifactStore,
    CommandReceipt,
    CommandReceiptStore,
    CurrentStateRepository,
    EventLog,
    StoredEvent,
    UnitOfWork,
)
from ehai.domain import Event, EventType

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _event() -> Event:
    return Event(
        type=EventType.RUN_STARTED,
        run_id=new_id(),
        correlation_id=new_id(),
        payload={"status": "running"},
        occurred_at=NOW,
    )


def test_stored_event_requires_a_positive_offset() -> None:
    event = _event()

    assert StoredEvent(offset=1, event=event).event is event
    with pytest.raises(ValueError, match="positive integer"):
        StoredEvent(offset=0, event=event)
    with pytest.raises(ValueError, match="positive integer"):
        StoredEvent(offset=True, event=event)


def test_command_receipt_snapshots_json_result_and_normalizes_time() -> None:
    result = {"run_id": str(new_id()), "status": "running"}
    receipt = CommandReceipt(
        idempotency_key="retry-key",
        command_name="StartRun",
        command_fingerprint="request-digest",
        result=result,
        created_at=NOW.astimezone(timezone(timedelta(hours=8))),
    )

    result["status"] = "tampered"
    first_read = receipt.result
    first_read["status"] = "also tampered"

    assert receipt.result["status"] == "running"
    assert receipt.created_at.tzinfo is UTC


@pytest.mark.parametrize("field", ["idempotency_key", "command_name", "command_fingerprint"])
def test_command_receipt_rejects_empty_identity_fields(field: str) -> None:
    arguments = {
        "idempotency_key": "retry-key",
        "command_name": "StartRun",
        "command_fingerprint": "request-digest",
    }
    arguments[field] = "   "

    with pytest.raises(ValueError, match=field):
        CommandReceipt(
            idempotency_key=arguments["idempotency_key"],
            command_name=arguments["command_name"],
            command_fingerprint=arguments["command_fingerprint"],
            result={},
            created_at=NOW,
        )


def test_command_receipt_rejects_naive_time_and_non_json_result() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        CommandReceipt(
            idempotency_key="retry-key",
            command_name="StartRun",
            command_fingerprint="request-digest",
            result={},
            created_at=datetime(2026, 8, 31),
        )

    with pytest.raises(ValueError, match="JSON"):
        CommandReceipt(
            idempotency_key="retry-key",
            command_name="StartRun",
            command_fingerprint="request-digest",
            result={"invalid": object()},  # type: ignore[dict-item]
            created_at=NOW,
        )


class _EventLog:
    def append(self, event: Event) -> StoredEvent:
        return StoredEvent(1, event)

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        del after_event_id, limit
        return ()

    def latest_offset(self) -> int:
        return 0


class _CommandReceipts:
    def put(self, receipt: CommandReceipt) -> None:
        del receipt

    def get(self, idempotency_key: str) -> CommandReceipt | None:
        del idempotency_key
        return None


class _UnitOfWork:
    def __init__(self) -> None:
        self.states = cast(CurrentStateRepository, object())
        self.events = _EventLog()
        self.command_receipts = _CommandReceipts()
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> _UnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if not self.committed:
            self.rollback()


def test_ports_are_runtime_checkable_and_unit_of_work_is_explicit() -> None:
    unit_of_work = _UnitOfWork()

    assert isinstance(unit_of_work.events, EventLog)
    assert isinstance(unit_of_work.command_receipts, CommandReceiptStore)
    assert isinstance(unit_of_work, UnitOfWork)
    assert not isinstance(object(), CurrentStateRepository)
    assert not isinstance(object(), ArtifactStore)

    with unit_of_work:
        pass
    assert unit_of_work.rolled_back
