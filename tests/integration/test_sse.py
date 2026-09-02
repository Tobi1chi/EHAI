from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from starlette.datastructures import Headers, QueryParams
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Router

from ehai import ID, new_id
from ehai.application.ports import StoredEvent
from ehai.domain.events import Event, EventType
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.sse import (
    EventBatchReader,
    create_event_stream_endpoint,
    create_event_stream_router,
)

NOW = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)


class _Request:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        disconnect_after: int | None = None,
    ) -> None:
        self.headers = Headers(headers or {})
        self.query_params = QueryParams(query or {})
        self._checks = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._disconnect_after is not None and self._checks > self._disconnect_after


def _event(index: int) -> Event:
    return Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=new_id(),
        payload={"sequence": index},
        occurred_at=NOW + timedelta(seconds=index),
    )


def _sqlite_reader(database: SQLiteDatabase) -> EventBatchReader:
    def read_events(
        *,
        after_event_id: ID | None,
        limit: int,
    ) -> tuple[StoredEvent, ...]:
        with database.unit_of_work() as uow:
            return uow.events.list_events(after_event_id=after_event_id, limit=limit)

    return read_events


def _call_endpoint(endpoint, request: _Request):
    return asyncio.run(endpoint(cast(object, request)))


async def _collect(response: StreamingResponse, count: int | None = None) -> list[bytes]:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        encoded = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        chunks.append(encoded)
        if count is not None and len(chunks) >= count:
            await response.body_iterator.aclose()
            break
    return chunks


def test_sse_strictly_replays_after_event_id_in_offset_order(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "events.sqlite3")
    events = tuple(_event(index) for index in range(3))
    with database.unit_of_work() as uow:
        stored = tuple(uow.events.append(event) for event in events)
        uow.commit()
    endpoint = create_event_stream_endpoint(_sqlite_reader(database), batch_size=1)
    request = _Request(query={"after_event_id": events[1].id}, disconnect_after=2)

    response = _call_endpoint(endpoint, request)
    assert isinstance(response, StreamingResponse)
    frames = asyncio.run(_collect(response))

    assert len(frames) == 1
    assert (
        frames[0]
        == (
            f"id: {events[2].id}\nevent: {events[2].type.value}\ndata: {events[2].to_json()}\n\n"
        ).encode()
    )
    assert stored[2].offset > stored[1].offset


def test_equal_header_and_query_cursor_are_accepted(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "equal.sqlite3")
    event = _event(1)
    with database.unit_of_work() as uow:
        uow.events.append(event)
        uow.commit()
    endpoint = create_event_stream_endpoint(_sqlite_reader(database))

    response = _call_endpoint(
        endpoint,
        _Request(
            headers={"Last-Event-ID": event.id.upper()},
            query={"after_event_id": event.id},
            disconnect_after=0,
        ),
    )

    assert isinstance(response, StreamingResponse)


@pytest.mark.parametrize(
    ("headers", "query", "message"),
    [
        ({"Last-Event-ID": str(new_id())}, {"after_event_id": str(new_id())}, "conflict"),
        ({"Last-Event-ID": " "}, {}, "blank"),
        ({}, {"after_event_id": "not-a-uuid"}, "Event UUID"),
    ],
)
def test_invalid_or_conflicting_cursor_returns_400_before_stream(
    headers: dict[str, str],
    query: dict[str, str],
    message: str,
) -> None:
    calls = 0

    def reader(*, after_event_id: ID | None, limit: int) -> tuple[StoredEvent, ...]:
        del after_event_id, limit
        nonlocal calls
        calls += 1
        return ()

    response = _call_endpoint(
        create_event_stream_endpoint(reader),
        _Request(headers=headers, query=query),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert message in json.loads(response.body)["error"]["message"]
    assert calls == 0


def test_unknown_cursor_returns_404_before_stream(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "unknown.sqlite3")
    response = _call_endpoint(
        create_event_stream_endpoint(_sqlite_reader(database)),
        _Request(query={"after_event_id": new_id()}),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
    assert json.loads(response.body)["error"]["code"] == "unknown_event_cursor"


def test_heartbeat_is_comment_only_and_does_not_write_event_log(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "heartbeat.sqlite3")
    now = [0.0]

    async def advance(delay: float) -> None:
        now[0] += delay

    endpoint = create_event_stream_endpoint(
        _sqlite_reader(database),
        poll_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
        sleep=advance,
        clock=lambda: now[0],
    )
    response = _call_endpoint(endpoint, _Request())
    assert isinstance(response, StreamingResponse)

    frames = asyncio.run(_collect(response, count=1))

    assert frames == [b": heartbeat\n\n"]
    with database.unit_of_work() as uow:
        assert uow.events.latest_offset() == 0


def test_disconnect_stops_without_another_query_session() -> None:
    calls = 0

    def reader(*, after_event_id: ID | None, limit: int) -> tuple[StoredEvent, ...]:
        del after_event_id, limit
        nonlocal calls
        calls += 1
        return ()

    response = _call_endpoint(
        create_event_stream_endpoint(reader),
        _Request(disconnect_after=0),
    )
    assert isinstance(response, StreamingResponse)

    assert asyncio.run(_collect(response)) == []
    assert calls == 1


def test_reader_session_is_closed_before_frame_is_yielded() -> None:
    active = False
    calls = 0
    event = _event(1)

    def reader(*, after_event_id: ID | None, limit: int) -> tuple[StoredEvent, ...]:
        del after_event_id, limit
        nonlocal active, calls
        active = True
        try:
            calls += 1
            return (StoredEvent(1, event),) if calls == 1 else ()
        finally:
            active = False

    endpoint = create_event_stream_endpoint(reader)
    response = _call_endpoint(endpoint, _Request(disconnect_after=2))
    assert isinstance(response, StreamingResponse)

    frames = asyncio.run(_collect(response))

    assert len(frames) == 1
    assert not active


def test_sse_redacts_internal_workspace_paths() -> None:
    event = Event(
        type=EventType.WORKSPACE_PRESERVED,
        correlation_id=new_id(),
        run_id=new_id(),
        payload={"path": "C:\\private\\workspace", "reason": "dirty worktree"},
        occurred_at=NOW,
    )
    calls = 0

    def reader(*, after_event_id: ID | None, limit: int) -> tuple[StoredEvent, ...]:
        del after_event_id, limit
        nonlocal calls
        calls += 1
        return (StoredEvent(1, event),) if calls == 1 else ()

    response = _call_endpoint(
        create_event_stream_endpoint(reader),
        _Request(disconnect_after=2),
    )
    assert isinstance(response, StreamingResponse)
    frame = asyncio.run(_collect(response))[0].decode()
    payload = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))

    assert "path" not in payload["payload"]
    assert payload["payload"]["reason"] == "dirty worktree"


def test_router_factory_is_mountable_and_configuration_is_bounded() -> None:
    def reader(*, after_event_id: ID | None, limit: int) -> tuple[StoredEvent, ...]:
        del after_event_id, limit
        return ()

    router = create_event_stream_router(reader)
    assert isinstance(router, Router)
    assert router.routes[0].path == "/api/v1/events/stream"

    with pytest.raises(ValueError, match="batch_size"):
        create_event_stream_endpoint(reader, batch_size=0)
    with pytest.raises(ValueError, match="positive and finite"):
        create_event_stream_endpoint(reader, poll_interval_seconds=float("nan"))
