"""Core-side Hub client and the local sidecar Hub used when no remote Hub is configured."""

from __future__ import annotations

import asyncio
import atexit
import os
import queue
import secrets
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ehai.hub.protocol import (
    TOKEN_ENVIRONMENT,
    URL_ENVIRONMENT,
    ErrorBody,
    EventBatch,
    Health,
    PromptMessage,
    SessionStarted,
    SessionState,
    StartSession,
    SteerMessages,
    ToolResult,
)

_Model = TypeVar("_Model", bound=BaseModel)
_LISTENING_PREFIX = "EHAI_HUB_LISTENING "


class HubUnavailableError(RuntimeError):
    """The Hub or its harness did not confirm the request; the outcome is unknown."""


class HubRequestError(ValueError):
    """The Hub rejected the request before launching anything."""


@dataclass(frozen=True, slots=True)
class HubEndpoint:
    url: str
    token: str


class LocalHub:
    """Start ``ehai-hub`` as a loopback child process that exits with this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._endpoint: HubEndpoint | None = None

    def endpoint(self) -> HubEndpoint:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                assert self._endpoint is not None
                return self._endpoint
            token = secrets.token_urlsafe(32)
            process = subprocess.Popen(
                [sys.executable, "-m", "ehai.hub.server", "--port", "0", "--sidecar"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env={**os.environ, TOKEN_ENVIRONMENT: token},
            )
            lines: queue.Queue[bytes] = queue.Queue()

            def read() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    lines.put(line)
                lines.put(b"")

            threading.Thread(target=read, daemon=True).start()
            try:
                line = lines.get(timeout=60.0).decode("utf-8", "replace").strip()
            except queue.Empty:
                line = ""
            if not line.startswith(_LISTENING_PREFIX):
                process.kill()
                process.wait()
                raise HubUnavailableError("Local Hub did not start")
            port = int(line.removeprefix(_LISTENING_PREFIX))
            self._process = process
            self._endpoint = HubEndpoint(f"http://127.0.0.1:{port}", token)
            return self._endpoint

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        assert process.stdin is not None
        process.stdin.close()
        try:
            process.wait(timeout=20.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class HubConnection:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _call(
        self,
        method: str,
        path: str,
        response: type[_Model] | None = None,
        *,
        body: BaseModel | None = None,
        params: dict[str, str | int | float | bool] | None = None,
        timeout: float | None = None,
    ) -> _Model | None:
        try:
            reply = await self._client.request(
                method,
                path,
                content=None if body is None else body.model_dump_json(),
                headers=None if body is None else {"Content-Type": "application/json"},
                params=params,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as error:
            raise HubUnavailableError("Hub request did not complete; outcome is unknown") from error
        if reply.status_code >= 400:
            try:
                error_body = ErrorBody.model_validate_json(reply.content)
            except ValidationError:
                raise HubUnavailableError(f"Hub returned HTTP {reply.status_code}") from None
            if error_body.code == "invalid_request":
                raise HubRequestError(error_body.message)
            raise HubUnavailableError(error_body.message)
        if response is None:
            return None
        try:
            return response.model_validate_json(reply.content)
        except ValidationError as error:
            raise HubUnavailableError("Hub returned an invalid response") from error

    async def health(self) -> Health:
        result = await self._call("GET", "/v1/health", Health)
        assert result is not None
        return result

    async def start(self, request: StartSession) -> SessionStarted:
        result = await self._call("POST", "/v1/sessions", SessionStarted, body=request)
        assert result is not None
        return result

    async def inspect(self, session_id: str) -> SessionState:
        result = await self._call("GET", f"/v1/sessions/{session_id}", SessionState)
        assert result is not None
        return result

    async def events(self, session_id: str, *, after: int, wait: float) -> EventBatch:
        result = await self._call(
            "GET",
            f"/v1/sessions/{session_id}/events",
            EventBatch,
            params={"after": after, "wait": wait},
            timeout=wait + 30.0,
        )
        assert result is not None
        return result

    async def steer(self, session_id: str, messages: list[str]) -> None:
        await self._call(
            "POST", f"/v1/sessions/{session_id}/steer", body=SteerMessages(messages=messages)
        )

    async def prompt(self, session_id: str, message: str) -> None:
        await self._call(
            "POST", f"/v1/sessions/{session_id}/prompt", body=PromptMessage(message=message)
        )

    async def tool_result(self, session_id: str, result: ToolResult) -> None:
        await self._call("POST", f"/v1/sessions/{session_id}/tool-results", body=result)

    async def cancel(self, session_id: str) -> None:
        await self._call("POST", f"/v1/sessions/{session_id}/cancel")

    async def close(self, session_id: str, *, abort: bool) -> None:
        await self._call("DELETE", f"/v1/sessions/{session_id}", params={"abort": abort})


class HubClient:
    """Reach a remote Hub from ``EHAI_HUB_URL``, or a local sidecar Hub otherwise."""

    def __init__(self, endpoint: HubEndpoint | None = None) -> None:
        self._endpoint = endpoint
        self._local: LocalHub | None = None

    def _resolve(self) -> HubEndpoint:
        if self._endpoint is not None:
            return self._endpoint
        if self._local is None:
            self._local = LocalHub()
            atexit.register(self._local.stop)
        return self._local.endpoint()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[HubConnection]:
        # One HTTP client per call keeps the client usable from any event loop.
        endpoint = await asyncio.to_thread(self._resolve)
        async with httpx.AsyncClient(
            base_url=endpoint.url,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            timeout=httpx.Timeout(60.0),
            # Loopback sidecar traffic must not go through a configured proxy.
            trust_env=self._endpoint is not None,
        ) as client:
            yield HubConnection(client)


_default: HubClient | None = None
_default_lock = threading.Lock()


def default_hub() -> HubClient:
    """The process-wide Hub client, configured once from the environment."""
    global _default
    with _default_lock:
        if _default is None:
            url = os.environ.get(URL_ENVIRONMENT)
            if url:
                token = os.environ.get(TOKEN_ENVIRONMENT, "")
                if not token:
                    raise ValueError(f"{URL_ENVIRONMENT} requires {TOKEN_ENVIRONMENT}")
                _default = HubClient(HubEndpoint(url.rstrip("/"), token))
            else:
                _default = HubClient()
        return _default
