"""Reusable Starlette SSE endpoint for durable P1 Event replay."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise
from typing import Protocol

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, Router

from ehai import ID, normalize_id
from ehai.application.ports import StoredEvent
from ehai.interfaces.public_events import public_event_json


class EventBatchReader(Protocol):
    """Read one bounded, ordered batch using a short-lived query session."""

    def __call__(
        self,
        *,
        after_event_id: ID | None,
        limit: int,
    ) -> tuple[StoredEvent, ...]: ...


EventStreamEndpoint = Callable[[Request], Awaitable[Response]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def create_event_stream_endpoint(
    read_events: EventBatchReader,
    *,
    batch_size: int = 100,
    poll_interval_seconds: float = 0.25,
    heartbeat_interval_seconds: float = 15.0,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> EventStreamEndpoint:
    """Create an SSE endpoint without binding it to SQLite or an app composition root."""
    if not callable(read_events):
        raise TypeError("read_events must be callable")
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    poll_interval = _positive_finite(poll_interval_seconds, "poll_interval_seconds")
    heartbeat_interval = _positive_finite(
        heartbeat_interval_seconds,
        "heartbeat_interval_seconds",
    )
    if not callable(sleep) or not callable(clock):
        raise TypeError("sleep and clock must be callable")

    async def endpoint(request: Request) -> Response:
        try:
            cursor = _resolve_cursor(
                request.headers.get("last-event-id"),
                request.query_params.get("after_event_id"),
            )
        except ValueError as error:
            return _error_response(400, "InvalidEventCursor", str(error))

        try:
            initial_batch = await _read_batch(read_events, cursor, batch_size)
        except LookupError as error:
            return _error_response(404, "UnknownEventCursor", str(error))

        async def stream() -> AsyncIterator[bytes]:
            current_cursor = cursor
            batch = initial_batch
            last_output_at = clock()
            while True:
                if await request.is_disconnected():
                    return
                if batch:
                    for stored in batch:
                        if await request.is_disconnected():
                            return
                        yield _frame(stored)
                        current_cursor = stored.event.id
                        last_output_at = clock()
                    try:
                        batch = await _read_batch(read_events, current_cursor, batch_size)
                    except LookupError:
                        return
                    continue

                now = clock()
                if now - last_output_at >= heartbeat_interval:
                    yield b": heartbeat\n\n"
                    last_output_at = now
                    continue

                remaining = heartbeat_interval - (now - last_output_at)
                await sleep(min(poll_interval, remaining))
                if await request.is_disconnected():
                    return
                try:
                    batch = await _read_batch(read_events, current_cursor, batch_size)
                except LookupError:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return endpoint


def create_event_stream_router(
    read_events: EventBatchReader,
    *,
    path: str = "/api/v1/events/stream",
    batch_size: int = 100,
    poll_interval_seconds: float = 0.25,
    heartbeat_interval_seconds: float = 15.0,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> Router:
    """Create a mountable Starlette Router for the P1 Event stream."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("SSE route path must be absolute")
    endpoint = create_event_stream_endpoint(
        read_events,
        batch_size=batch_size,
        poll_interval_seconds=poll_interval_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        sleep=sleep,
        clock=clock,
    )
    return Router(routes=[Route(path, endpoint, methods=["GET"], name="event-stream")])


async def _read_batch(
    read_events: EventBatchReader,
    after_event_id: ID | None,
    limit: int,
) -> tuple[StoredEvent, ...]:
    values = await run_in_threadpool(
        read_events,
        after_event_id=after_event_id,
        limit=limit,
    )
    batch = tuple(values)
    if len(batch) > limit:
        raise RuntimeError("Event reader exceeded its bounded batch limit")
    if not all(isinstance(item, StoredEvent) for item in batch):
        raise TypeError("Event reader must return StoredEvent values")
    offsets = tuple(item.offset for item in batch)
    if any(current <= previous for previous, current in pairwise(offsets)):
        raise RuntimeError("Event reader returned Events outside strict offset order")
    if after_event_id is not None and any(item.event.id == after_event_id for item in batch):
        raise RuntimeError("Event reader did not apply strict-after cursor semantics")
    return batch


def _resolve_cursor(header_value: str | None, query_value: str | None) -> ID | None:
    header_cursor = _optional_cursor(header_value, "Last-Event-ID")
    query_cursor = _optional_cursor(query_value, "after_event_id")
    if header_cursor is not None and query_cursor is not None and header_cursor != query_cursor:
        raise ValueError("Last-Event-ID and after_event_id cursors conflict")
    return header_cursor if header_cursor is not None else query_cursor


def _optional_cursor(value: str | None, field_name: str) -> ID | None:
    if value is None:
        return None
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an Event UUID") from error


def _frame(stored: StoredEvent) -> bytes:
    event = stored.event
    return (
        f"id: {event.id}\nevent: {event.type.value}\ndata: {public_event_json(event)}\n\n"
    ).encode()


def _error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": _error_code(error_type), "message": message}},
        status_code=status_code,
    )


def _error_code(error_type: str) -> str:
    characters = (
        f"_{character.lower()}" if character.isupper() else character for character in error_type
    )
    return "".join(characters).lstrip("_")


def _positive_finite(value: float, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be positive and finite")
    return float(value)
