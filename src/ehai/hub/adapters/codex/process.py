"""Owned stdio app-server, using JSONRPC{Request,Response,Notification,Error}.json.

The pinned wire protocol omits ``jsonrpc``. Responses are read independently of the
Hub event consumer, so an approval or a long-running tool cannot block control replies.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from contextlib import suppress
from pathlib import Path

from ehai import JsonValue, json_dumps, json_loads
from ehai.hub.adapters import HarnessFailure

TIMEOUT = 30.0


class AppServer:
    def __init__(self, command: list[str], cwd: Path, environment: dict[str, str]) -> None:
        self._command = command
        self._cwd = cwd
        self._environment = environment
        self._process: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task[None]] = []
        self._pending: dict[str, asyncio.Future[dict[str, JsonValue]]] = {}
        self._messages: asyncio.Queue[dict[str, JsonValue] | None] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._failure: str | None = None
        self._closed = False
        self._commands_started = False

    async def start(self) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._cwd,
                env=self._environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=sys.platform != "win32",
                limit=16 * 1024 * 1024,
            )
        except OSError as error:
            raise HarnessFailure("Could not start Codex app-server") from error
        self._readers = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr())]

    async def _stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        # Drain without retaining native diagnostics, which may contain account data.
        while await self._process.stderr.read(65536):
            pass

    def _fail(self, reason: str) -> None:
        if self._failure is not None:
            return
        self._failure = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HarnessFailure(reason))
        self._messages.put_nowait(None)

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while raw := await self._process.stdout.readline():
                message = json_loads(raw.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("Expected an app-server object")
                if "method" in message:
                    params = message.get("params")
                    item = params.get("item") if isinstance(params, dict) else None
                    if isinstance(item, dict) and item.get("type") == "commandExecution":
                        self._commands_started = True
                    if "id" in message:
                        await self._server_request(message)
                    else:
                        self._messages.put_nowait(message)
                else:
                    request_id = message.get("id")
                    future = self._pending.get(request_id) if isinstance(request_id, str) else None
                    if future is None or future.done():
                        continue
                    result = message.get("result")
                    if isinstance(result, dict):
                        future.set_result(result)
                    else:
                        error = message.get("error")
                        detail = error.get("message") if isinstance(error, dict) else None
                        future.set_exception(HarnessFailure(f"Codex rejected a request: {detail}"))
            self._fail("Codex output closed; in-flight outcomes are unknown")
        except (ValueError, UnicodeError, OSError, HarnessFailure):
            self._fail("Codex control channel failed; in-flight outcomes are unknown")

    async def _server_request(self, message: dict[str, JsonValue]) -> None:
        method = str(message["method"])
        params = message.get("params")
        if method == "mcpServer/elicitation/request":
            # McpServerElicitationRequest{Params,Response}.json. Only EHAI tool approval,
            # never forms, OAuth or approval of another server, is automatic.
            params = params if isinstance(params, dict) else {}
            meta = params.get("_meta")
            accepted = (
                params.get("serverName") == "ehai"
                and isinstance(meta, dict)
                and meta.get("codex_approval_kind") == "mcp_tool_call"
            )
            await self.write(
                {
                    "id": message["id"],
                    "result": {"action": "accept", "content": {}}
                    if accepted
                    else {"action": "decline"},
                }
            )
        elif method.endswith("Approval"):
            # ServerRequest.json also names legacy *Approval requests. Keep them
            # pending as `other` until the core decides.
            self._messages.put_nowait(message)
        else:
            await self.unsupported(message["id"])
            self._messages.put_nowait(
                {"method": "ehai/unsupportedRequest", "params": {"name": method}}
            )

    async def unsupported(self, request_id: JsonValue) -> None:
        await self.write(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "EHAI does not support this server request"},
            }
        )

    async def write(self, message: dict[str, JsonValue]) -> None:
        if self._failure is not None:
            raise HarnessFailure(self._failure)
        process = self._process
        if self._closed or process is None or process.stdin is None:
            raise HarnessFailure("Codex app-server is not running")
        try:
            async with self._write_lock:
                process.stdin.write(json_dumps(message).encode("utf-8") + b"\n")
                await process.stdin.drain()
        except (OSError, ConnectionError) as error:
            self._fail("Codex input closed; in-flight outcomes are unknown")
            raise HarnessFailure(self._failure) from error

    async def call(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, JsonValue]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(TIMEOUT):
                await self.write({"id": request_id, "method": method, "params": params})
                return await future
        except TimeoutError as error:
            reason = f"Codex timed out answering {method}; outcome unknown, do not replay"
            self._fail(reason)
            raise HarnessFailure(reason) from error
        finally:
            self._pending.pop(request_id, None)

    async def initialize(self) -> None:
        # v1/Initialize{Params,Response}.json and ClientNotification.json.
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "ehai", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.write({"method": "initialized"})

    async def next(self) -> dict[str, JsonValue]:
        message = await self._messages.get()
        if message is None:
            self._messages.put_nowait(None)
            raise HarnessFailure(self._failure or "Codex app-server ended")
        return message

    async def _descendants(self, pid: int) -> list[int]:
        # PTY children can create their own process groups. Read only PID/PPID,
        # never command arguments or environments (which can contain credentials).
        try:
            listing = await asyncio.create_subprocess_exec(
                "/bin/ps",
                "-e",
                "-o",
                "pid=,ppid=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise HarnessFailure(
                "Cannot enumerate Codex descendants; cleanup is unconfirmed"
            ) from error
        output, _ = await listing.communicate()
        if listing.returncode != 0:
            raise HarnessFailure("Cannot enumerate Codex descendants; cleanup is unconfirmed")
        parents = [
            (int(child), int(parent))
            for child, parent in (line.split() for line in output.decode("ascii").splitlines())
        ]
        owned = [pid]
        for parent in owned:
            owned.extend(child for child, ppid in parents if ppid == parent)
        return owned[1:]

    async def _kill_tree(self, descendants: list[int]) -> None:
        process = self._process
        if process is None:
            return
        if sys.platform == "win32":
            if process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
        else:
            # Also kills descendants that outlived the app-server leader.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            for pid in reversed(descendants):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        await process.wait()

    async def close(self, *, abort: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            process = self._process
            if process is not None:
                descendants: list[int] = []
                try:
                    if self._commands_started and sys.platform != "win32":
                        descendants = await self._descendants(process.pid)
                    if not abort and not self._commands_started and process.returncode is None:
                        assert process.stdin is not None
                        process.stdin.close()
                        with suppress(TimeoutError):
                            async with asyncio.timeout(3.0):
                                await process.wait()
                finally:
                    await self._kill_tree(descendants)
        finally:
            self._fail("Codex app-server closed; in-flight outcomes are unknown")
            for reader in self._readers:
                reader.cancel()
            await asyncio.gather(*self._readers, return_exceptions=True)
