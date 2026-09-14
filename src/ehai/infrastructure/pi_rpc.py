"""Public Pi stdio RPC transport, not an Agent loop or a model API adapter."""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from ehai import JsonValue, json_dumps, json_loads
from ehai.application.sanitization import bounded_redacted_text


class PiRpcError(RuntimeError):
    """A failed control channel; callers must not blindly replay a task."""


class PiRpcRejectedError(PiRpcError):
    """Pi explicitly rejected a correlated command."""


class PiRpcProcess:
    """One caller-owned process, with a single reader and correlated requests.

    Request IDs correlate acknowledgements only; they are not idempotency keys.
    Events remain opaque here. In particular, neither an accepted prompt nor
    agent_end is converted to EHAI task completion by this transport.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        environment: Mapping[str, str],
        command_timeout_seconds: float = 15.0,
    ) -> None:
        self.command = tuple(command)
        if not self.command or any(not item or "\x00" in item for item in self.command):
            raise ValueError("Pi command must be a non-empty argv without NUL")
        executable = Path(self.command[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("Pi executable must be an existing absolute file path")
        if executable.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError("Use a native Node executable and the Pi CLI file, not a shell shim")
        if (
            isinstance(command_timeout_seconds, bool)
            or not math.isfinite(command_timeout_seconds)
            or command_timeout_seconds <= 0
        ):
            raise ValueError("Pi control timeout must be finite and positive")
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Pi workspace must be a directory")
        self.environment = dict(environment)
        self.command_timeout_seconds = command_timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task[None]] = []
        self._pending: dict[str, tuple[str, asyncio.Future[dict[str, JsonValue]]]] = {}
        self._events: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue(maxsize=256)
        self._write_lock = asyncio.Lock()
        self._failure: PiRpcError | None = None
        self._closed = False
        self.stderr_bytes = 0

    async def __aenter__(self) -> PiRpcProcess:
        if self._process is not None or self._closed:
            raise PiRpcError("Pi RPC process cannot be reopened")
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.workspace,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._drain_stderr()),
        ]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Do not leave a live child behind when the caller is cancelled.
        async def stop() -> None:
            if exc_type is not None and self._failure is None:
                # Acknowledgements are not a safe-pause claim. Bound graceful shutdown,
                # then close the owned process even if the native loop does not settle.
                try:
                    async with asyncio.timeout(5.0):
                        await self.request("clear_queue")
                        await self.request("abort")
                except (PiRpcError, TimeoutError):
                    pass
            await self.close()

        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    async def request(
        self, command: str, parameters: Mapping[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        """Wait only for command acknowledgement, never for Agent completion."""
        if not command.strip() or "\x00" in command:
            raise ValueError("Pi command name must not be blank or contain NUL")
        arguments = dict(parameters or {})
        if {"id", "type"}.intersection(arguments):
            raise ValueError("Pi command parameters cannot override id or type")
        self._require_connection()
        request_id = str(uuid4())
        payload = json_dumps({"id": request_id, "type": command, **arguments})
        future: asyncio.Future[dict[str, JsonValue]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = command, future
        try:
            async with asyncio.timeout(self.command_timeout_seconds):
                async with self._write_lock:
                    process = self._require_connection()
                    assert process.stdin is not None
                    process.stdin.write(payload.encode("utf-8") + b"\n")
                    await process.stdin.drain()
                return await future
        except TimeoutError as error:
            failure = PiRpcError(
                "Pi RPC acknowledgement timed out; command outcome is unknown and was not retried"
            )
            self._fail(failure)
            raise failure from error
        except (BrokenPipeError, ConnectionError) as error:
            failure = PiRpcError("Pi RPC write failed; command outcome is unknown")
            self._fail(failure)
            raise failure from error
        except asyncio.CancelledError:
            self._fail(PiRpcError("Pi RPC request cancelled; command outcome may be unknown"))
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Consume an exception installed by _fail during a failed write.
                future.exception()

    async def next_event(self) -> dict[str, JsonValue]:
        """Return one native event without interpreting or rewriting its contents."""
        while self._events.empty():
            self._require_connection()
            try:
                async with asyncio.timeout(0.1):
                    return await self._events.get()
            except TimeoutError:
                continue
        return self._events.get_nowait()

    def _require_connection(self) -> asyncio.subprocess.Process:
        if self._failure is not None:
            raise self._failure
        process = self._process
        if self._closed or process is None or process.returncode is not None:
            raise PiRpcError("Pi RPC process is not running")
        return process

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while raw := await self._process.stdout.readline():
                if not raw.endswith(b"\n"):
                    raise PiRpcError("Pi RPC ended with an incomplete JSONL record")
                # LF is the ONLY delimiter; Unicode line separators belong to JSON strings.
                document = json_loads(raw.decode("utf-8"))
                if not isinstance(document, dict) or not isinstance(document.get("type"), str):
                    raise PiRpcError("Pi RPC record must be an object with a string type")
                if document["type"] == "response":
                    self._accept_response(document)
                else:
                    try:
                        self._events.put_nowait(document)
                    except asyncio.QueueFull as error:
                        raise PiRpcError(
                            "Pi event consumer fell behind; events were not dropped"
                        ) from error
            if not self._closed:
                self._fail(PiRpcError("Pi RPC stdout closed; in-flight outcomes may be unknown"))
        except asyncio.CancelledError:
            raise
        except (ValueError, UnicodeError, PiRpcError, OSError):
            # Never copy malformed native records or opaque provider content into errors.
            self._fail(PiRpcError("Pi RPC stream failed validation or disconnected"))

    def _accept_response(self, document: dict[str, JsonValue]) -> None:
        request_id = document.get("id")
        if not isinstance(request_id, str) or request_id not in self._pending:
            raise PiRpcError("Pi RPC response has no matching request")
        command, future = self._pending[request_id]
        if document.get("command") != command or type(document.get("success")) is not bool:
            raise PiRpcError("Pi RPC response does not match the command contract")
        if future.done():
            raise PiRpcError("Pi RPC returned a duplicate acknowledgement")
        if document["success"]:
            future.set_result(document)
        else:
            detail = document.get("error")
            reason = bounded_redacted_text(detail if isinstance(detail, str) else None)
            future.set_exception(PiRpcRejectedError(reason or "Pi rejected the command"))

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(8192):
            # Native stderr may contain credentials or provider data. Count, don't publish it.
            self.stderr_bytes += len(chunk)

    def _fail(self, error: PiRpcError) -> None:
        if self._failure is None:
            self._failure = error
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(self._failure)

    async def close(self) -> None:
        """Close the owned control process; not a successful task pause or rollback.

        The first consumer is a tool-free offline probe. A writing Worker must
        perform its own quiescence/effect audit before using this cleanup method.
        """
        if self._closed:
            return
        self._closed = True
        self._fail(PiRpcError("Pi RPC process closed"))
        process = self._process
        try:
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    async with asyncio.timeout(2.0):
                        await process.wait()
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        async with asyncio.timeout(3.0):
                            await process.wait()
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
        finally:
            for reader in self._readers:
                reader.cancel()
            await asyncio.gather(*self._readers, return_exceptions=True)
