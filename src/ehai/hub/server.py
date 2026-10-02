"""Hub service (``ehai-hub``): run harness sessions for one EHAI core over HTTP.

The Hub keeps only a process-local table of running sessions. It persists no EHAI facts;
when it restarts, the core sees unknown outcomes and decides what to do from its own records.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import socket
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from ehai import JsonValue
from ehai.hub.adapters import (
    HarnessAdapter,
    HarnessFailure,
    HarnessRequestError,
    HarnessSession,
    McpEndpoint,
)
from ehai.hub.adapters.pi.adapter import PiAdapter
from ehai.hub.mcp_endpoint import SessionToolEndpoint, new_session_token
from ehai.hub.protocol import (
    MIN_SESSION_IDLE_SECONDS,
    TOKEN_ENVIRONMENT,
    ErrorBody,
    EventBatch,
    HarnessInfo,
    Health,
    HubEvent,
    PromptMessage,
    SessionStarted,
    SessionState,
    StartSession,
    SteerMessages,
    ToolResult,
    ToolSpec,
)

_EVENT = TypeAdapter[HubEvent](HubEvent)
_MAX_WAIT_SECONDS = 30.0
LISTENING_PREFIX = "EHAI_HUB_LISTENING "


class HubError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = ErrorBody.model_validate({"code": code, "message": message})


@dataclass
class _Session:
    session_id: str
    harness: str
    native: HarnessSession
    events: list[HubEvent] = field(default_factory=list)
    state: Literal["running", "failed", "closed"] = "running"
    touched: float = field(default_factory=time.monotonic)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    pump: asyncio.Task[None] | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    mcp_token: str = ""
    mcp_pending: dict[str, asyncio.Future[ToolResult]] = field(default_factory=dict)

    async def call_tool_via_mcp(self, name: str, arguments: dict[str, JsonValue]) -> ToolResult:
        """Hand an MCP tool call to the core as a tool_call event and wait for its result."""
        if self.state != "running":
            raise RuntimeError("EHAI tool session is no longer running")
        call_id = "mcp-" + uuid.uuid4().hex
        result: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self.mcp_pending[call_id] = result
        try:
            self.touched = time.monotonic()
            await self.append(
                {
                    "type": "tool_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "batch_call_ids": [call_id],
                }
            )
            return await result
        finally:
            self.mcp_pending.pop(call_id, None)

    def fail_pending(self, reason: str) -> None:
        for pending in self.mcp_pending.values():
            if not pending.done():
                pending.set_exception(RuntimeError(reason))

    async def append(self, body: Mapping[str, JsonValue]) -> None:
        event = _EVENT.validate_python({**body, "seq": len(self.events) + 1})
        async with self.changed:
            self.events.append(event)
            if event.type == "failed":
                self.state = "failed"
            self.changed.notify_all()

    async def run_pump(self) -> None:
        try:
            while True:
                await self.append(await self.native.next_event())
        except HarnessFailure as error:
            await self.append({"type": "failed", "reason": str(error) or "Harness failed"})


class Hub:
    def __init__(self, adapters: Mapping[str, HarnessAdapter], *, idle_seconds: float) -> None:
        self.adapters = dict(adapters)
        self.idle_seconds = idle_seconds
        self.sessions: dict[str, _Session] = {}
        self.by_mcp_token: dict[str, str] = {}
        # Set once the server is listening; harnesses reach the MCP endpoint through it.
        self.public_url = ""

    def session_for_mcp_token(self, token: str) -> _Session | None:
        session_id = self.by_mcp_token.get(token) if token else None
        session = None if session_id is None else self.sessions.get(session_id)
        return session if session is not None and session.state == "running" else None

    def require(self, session_id: str) -> _Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise HubError(404, "not_found", "Hub session is not running here")
        session.touched = time.monotonic()
        return session

    async def start(self, request: StartSession) -> SessionStarted:
        adapter = self.adapters.get(request.harness.kind)
        if adapter is None:
            raise HubError(422, "invalid_request", "Harness kind is not installed in this Hub")
        token = new_session_token()
        endpoint = McpEndpoint(url=f"{self.public_url}/v1/mcp/", token=token)
        try:
            native = await adapter.launch(request, endpoint)
        except HarnessRequestError as error:
            raise HubError(422, "invalid_request", str(error)) from error
        except HarnessFailure as error:
            raise HubError(502, "harness_failed", str(error)) from error
        session = _Session(
            uuid.uuid4().hex, adapter.kind, native, tools=list(request.tools), mcp_token=token
        )
        session.pump = asyncio.create_task(session.run_pump())
        self.sessions[session.session_id] = session
        self.by_mcp_token[token] = session.session_id
        return SessionStarted(session_id=session.session_id, native_session=native.native_session)

    async def control(self, session: _Session, action: str, value: object = None) -> None:
        if session.state != "running":
            raise HubError(502, "harness_failed", "Hub session is no longer running")
        try:
            if action == "steer":
                assert isinstance(value, SteerMessages)
                await session.native.steer(value.messages)
            elif action == "prompt":
                assert isinstance(value, PromptMessage)
                await session.native.prompt(value.message)
            elif action == "tool_result":
                assert isinstance(value, ToolResult)
                pending = session.mcp_pending.get(value.call_id)
                if pending is not None:
                    if not pending.done():
                        pending.set_result(value)
                else:
                    await session.native.tool_result(value)
            else:
                await session.native.cancel()
        except HarnessFailure as error:
            raise HubError(502, "harness_failed", str(error)) from error

    async def events(self, session: _Session, after: int, wait: float) -> EventBatch:
        async with session.changed:
            if len(session.events) <= after and session.state == "running":
                with suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        await session.changed.wait_for(
                            lambda: len(session.events) > after or session.state != "running"
                        )
            return EventBatch(events=session.events[after:], state=session.state)

    async def close(self, session_id: str, *, abort: bool) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        self.by_mcp_token.pop(session.mcp_token, None)
        session.fail_pending("EHAI tool session closed")
        async with session.changed:
            session.state = "closed"
            session.changed.notify_all()
        if session.pump is not None:
            session.pump.cancel()
            await asyncio.gather(session.pump, return_exceptions=True)
        await session.native.close(abort=abort)

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(min(10.0, self.idle_seconds))
            now = time.monotonic()
            for session in list(self.sessions.values()):
                if now - session.touched > self.idle_seconds:
                    # The core stopped talking to this session; never leave the harness running.
                    await self.close(session.session_id, abort=True)

    async def close_all(self) -> None:
        await asyncio.gather(
            *(self.close(session_id, abort=True) for session_id in list(self.sessions)),
            return_exceptions=True,
        )


def create_app(
    token: str,
    *,
    idle_seconds: float = 600.0,
    adapters: Mapping[str, HarnessAdapter] | None = None,
) -> FastAPI:
    if len(token) < 32:
        raise ValueError("Hub token must have at least 32 characters")
    if not idle_seconds >= MIN_SESSION_IDLE_SECONDS:
        raise ValueError(
            f"Session idle timeout must be at least {MIN_SESSION_IDLE_SECONDS:g} seconds"
        )
    hub = Hub(adapters or {"pi": PiAdapter()}, idle_seconds=idle_seconds)
    tool_endpoint = SessionToolEndpoint(hub.session_for_mcp_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        reaper = asyncio.create_task(hub.reap())
        try:
            async with tool_endpoint.manager.run():
                yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await hub.close_all()

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HubError(401, "unauthorized", "Hub token is missing or wrong")

    app = FastAPI(
        title="EHAI Hub", lifespan=lifespan, dependencies=[Depends(authorize)], docs_url=None
    )
    app.state.hub = hub
    # Harnesses authenticate with their session token, never with the Hub token.
    app.mount("/v1/mcp", tool_endpoint)

    @app.exception_handler(HubError)
    async def hub_error(_: Request, error: HubError) -> JSONResponse:
        return JSONResponse(error.body.model_dump(), status_code=error.status)

    @app.get("/v1/health")
    async def health() -> Health:
        return Health(
            harnesses=[
                HarnessInfo(kind=adapter.kind, version=adapter.version)
                for adapter in hub.adapters.values()
            ]
        )

    @app.post("/v1/sessions")
    async def start(request: StartSession) -> SessionStarted:
        return await hub.start(request)

    @app.get("/v1/sessions/{session_id}")
    async def inspect(session_id: str) -> SessionState:
        session = hub.require(session_id)
        return SessionState(
            session_id=session.session_id,
            harness=session.harness,
            state=session.state,
            last_seq=len(session.events),
        )

    @app.get("/v1/sessions/{session_id}/events")
    async def events(
        session_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        wait: Annotated[float, Query(ge=0, le=_MAX_WAIT_SECONDS)] = 0,
    ) -> EventBatch:
        return await hub.events(hub.require(session_id), after, wait)

    @app.post("/v1/sessions/{session_id}/steer", status_code=204)
    async def steer(session_id: str, body: SteerMessages) -> None:
        await hub.control(hub.require(session_id), "steer", body)

    @app.post("/v1/sessions/{session_id}/prompt", status_code=204)
    async def prompt(session_id: str, body: PromptMessage) -> None:
        await hub.control(hub.require(session_id), "prompt", body)

    @app.post("/v1/sessions/{session_id}/tool-results", status_code=204)
    async def tool_result(session_id: str, body: ToolResult) -> None:
        await hub.control(hub.require(session_id), "tool_result", body)

    @app.post("/v1/sessions/{session_id}/heartbeat", status_code=204)
    async def heartbeat(session_id: str) -> None:
        # Every request renews the session lease; this one exists only to renew it.
        hub.require(session_id)

    @app.post("/v1/sessions/{session_id}/cancel", status_code=204)
    async def cancel(session_id: str) -> None:
        await hub.control(hub.require(session_id), "cancel")

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def close(session_id: str, abort: bool = False) -> None:
        await hub.close(session_id, abort=abort)

    return app


class _HubServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, hub: Hub) -> None:
        super().__init__(config)
        self.hub = hub

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Stop harnesses first: this also releases waiting event polls, which would
        # otherwise hold the graceful shutdown open.
        await self.hub.close_all()
        await super().shutdown(sockets)


def _watch_stdin(server: uvicorn.Server) -> None:
    # A sidecar Hub stops with the core that started it: the core holds our stdin open.
    while sys.stdin.buffer.read(65536):
        pass
    server.should_exit = True
    time.sleep(15.0)
    os._exit(1)


async def _serve(server: _HubServer, *, announce: bool, public_url: str | None, host: str) -> None:
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if server.started:
        port = server.servers[0].sockets[0].getsockname()[1]
        local = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        server.hub.public_url = (public_url or f"http://{local}:{port}").rstrip("/")
        if announce:
            print(f"{LISTENING_PREFIX}{port}", flush=True)
    await task


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ehai-hub", description="Run the EHAI harness Hub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--session-idle-seconds", type=float, default=600.0)
    parser.add_argument(
        "--public-url",
        help="base URL at which harnesses reach this Hub's MCP endpoint (default: listen address)",
    )
    parser.add_argument("--sidecar", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENVIRONMENT, "")
    if not token:
        parser.error(f"{TOKEN_ENVIRONMENT} must hold the shared Hub token")
    if not args.session_idle_seconds >= MIN_SESSION_IDLE_SECONDS:
        parser.error(
            f"--session-idle-seconds must be at least {MIN_SESSION_IDLE_SECONDS:g} "
            f"(three core heartbeat intervals)"
        )
    app = create_app(token, idle_seconds=args.session_idle_seconds)
    server = _HubServer(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", access_log=False),
        app.state.hub,
    )
    if args.sidecar:
        threading.Thread(target=_watch_stdin, args=(server,), daemon=True).start()
    asyncio.run(_serve(server, announce=args.sidecar, public_url=args.public_url, host=args.host))


if __name__ == "__main__":
    main()
