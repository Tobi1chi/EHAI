"""Codex App Server stdio connector with per-Thread/Turn event isolation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from ehai import ID, JsonValue, json_dumps, json_loads, new_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.execution_policy import EndpointHealthStatus
from ehai.application.workers import WorkerResult
from ehai.domain.workers import AttemptActivity
from ehai.infrastructure.workers.codex_protocol import (
    CodexProtocolError,
    build_codex_prompt,
    codex_output_schema_json,
    parse_codex_result,
)

type JsonObject = dict[str, JsonValue]
type RequestId = int | str

_WAITING_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
        "item/permissions/requestApproval",
    }
)
_TERMINAL_EVENT_TYPES = frozenset({WorkerEventType.COMPLETED, WorkerEventType.FAILED})
_APPROVAL_POLICIES = frozenset({"untrusted", "on-request", "never"})
_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


class CodexAppServerProtocolError(RuntimeError):
    """An invalid or failed Codex App Server JSON-RPC exchange."""


@dataclass(frozen=True, slots=True, init=False)
class CodexAppServerPendingRequest:
    """A server request that must remain pending for an explicit EHAI decision."""

    request_id: RequestId
    method: str
    thread_id: str
    turn_id: str
    item_id: str
    _params_json: str = field(repr=False)

    def __init__(
        self,
        *,
        request_id: RequestId,
        method: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        params: Mapping[str, JsonValue],
    ) -> None:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            raise ValueError("App Server request_id must be an integer or string")
        for name, value in (
            ("method", method),
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("item_id", item_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"App Server pending request {name} must not be blank")
        if method not in _WAITING_METHODS:
            raise ValueError(f"unsupported App Server waiting method: {method}")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "_params_json", json_dumps(dict(params)))

    @property
    def params(self) -> JsonObject:
        """Return an isolated copy of the provider request parameters."""
        decoded = json_loads(self._params_json)
        if not isinstance(decoded, dict):  # pragma: no cover - guarded by construction
            raise RuntimeError("pending App Server request params are not an object")
        return decoded


class AppServerTransport(Protocol):
    """One bidirectional JSONL connection to a single App Server process."""

    async def open(self) -> None: ...

    async def send(self, message: JsonObject) -> None: ...

    async def receive(self) -> JsonObject | None: ...

    async def close(self) -> None: ...


class StdioAppServerTransport:
    """Own one ``codex app-server --listen stdio://`` child process."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        executable: str | Path | Sequence[str] = "codex",
        env_overrides: Mapping[str, str] | None = None,
        close_grace_seconds: float = 2.0,
    ) -> None:
        self._workspace = Path(workspace).resolve(strict=True)
        self._command = _normalize_command(executable)
        self._environment = dict(os.environ)
        if env_overrides is not None:
            if any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in env_overrides.items()
            ):
                raise TypeError("App Server environment overrides must contain strings")
            self._environment.update(env_overrides)
        if not isinstance(close_grace_seconds, (int, float)) or close_grace_seconds <= 0:
            raise ValueError("close_grace_seconds must be positive")
        self._close_grace_seconds = float(close_grace_seconds)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            "app-server",
            "--listen",
            "stdio://",
            cwd=self._workspace,
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def send(self, message: JsonObject) -> None:
        process = self._required_process()
        if process.stdin is None:
            raise CodexAppServerProtocolError("App Server stdin is unavailable")
        process.stdin.write((json_dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def receive(self) -> JsonObject | None:
        process = self._required_process()
        if process.stdout is None:
            raise CodexAppServerProtocolError("App Server stdout is unavailable")
        line = await process.stdout.readline()
        if not line:
            return None
        try:
            decoded = json_loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise CodexAppServerProtocolError("App Server emitted invalid JSONL") from error
        if not isinstance(decoded, dict):
            raise CodexAppServerProtocolError("App Server message must be a JSON object")
        return decoded

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self._close_grace_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
            self._stderr_task = None

    def _required_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is None or process.returncode is not None:
            raise CodexAppServerProtocolError("App Server transport is not open")
        return process

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while await process.stderr.read(65_536):
            pass


@dataclass(slots=True)
class _TurnState:
    execution: ConnectorExecution
    queue: asyncio.Queue[WorkerEvent] = field(default_factory=asyncio.Queue)
    candidate_emitted: bool = False
    candidate_error: str | None = None
    terminal: bool = False


class CodexAppServerConnector:
    """Multiplex Attempts over one stdio App Server Endpoint connection."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        model: str,
        executable: str | Path | Sequence[str] = "codex",
        approval_policy: str = "on-request",
        sandbox: str = "workspace-write",
        reasoning_effort: str | None = None,
        env_overrides: Mapping[str, str] | None = None,
        transport_factory: Callable[[], AppServerTransport] | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._workspace = Path(workspace).resolve(strict=True)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Codex App Server model must not be blank")
        self._model = model.strip()
        if approval_policy not in _APPROVAL_POLICIES:
            raise ValueError("unsupported Codex App Server approval policy")
        self._approval_policy = approval_policy
        if sandbox not in _SANDBOX_MODES:
            raise ValueError("unsupported Codex App Server sandbox mode")
        self._sandbox = sandbox
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be non-blank when provided")
        self._reasoning_effort = reasoning_effort
        if not isinstance(request_timeout_seconds, (int, float)) or request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._transport_factory = transport_factory or (
            lambda: StdioAppServerTransport(
                workspace=self._workspace,
                executable=executable,
                env_overrides=env_overrides,
            )
        )
        self._transport: AppServerTransport | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_request_id = 1
        self._pending_responses: dict[RequestId, asyncio.Future[JsonObject]] = {}
        self._pending_requests: dict[RequestId, CodexAppServerPendingRequest] = {}
        self._attempt_executions: dict[ID, ConnectorExecution] = {}
        self._turns: dict[tuple[str, str], _TurnState] = {}
        self._active_turns: dict[str, str] = {}
        self._orphan_messages: dict[tuple[str, str], list[JsonObject]] = {}
        self._connection_token = str(new_id())
        self._connection_generation = 0
        self._event_sequence = 0

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        """Start one new Thread and Turn, idempotent within this Endpoint process."""
        if not isinstance(request, ConnectorStartRequest):
            raise TypeError("request must be a ConnectorStartRequest")
        async with self._start_lock:
            existing = self._attempt_executions.get(request.attempt_id)
            if existing is not None:
                return existing
            thread = await self.start_thread()
            thread_id = _required_text(thread.get("id"), "thread.id")
            execution = await self._start_turn(thread_id, request)
            self._attempt_executions[request.attempt_id] = execution
            return execution

    def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        """Stream only the events scoped to the requested Thread and Turn."""
        return self._events(execution, after_cursor=after_cursor)

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        """Inspect non-terminal activity without changing the Attempt lifecycle."""
        state = self._turns.get(_execution_key(execution))
        if state is None or self._reader_task is None or self._reader_task.done():
            return AttemptActivity.STALLED
        if any(
            request.thread_id == execution.provider_session_id
            and request.turn_id == execution.provider_execution_id
            for request in self._pending_requests.values()
        ):
            return AttemptActivity.WAITING
        if state.terminal:
            return AttemptActivity.STALLED
        return AttemptActivity.RUNNING

    async def health(self) -> EndpointHealthStatus:
        """Report only this Endpoint connection health, never Session progress."""
        if self._reader_task is None:
            return EndpointHealthStatus.UNKNOWN
        if self._reader_task.done() or self._transport is None:
            return EndpointHealthStatus.UNHEALTHY
        return EndpointHealthStatus.HEALTHY

    async def cancel(self, execution: ConnectorExecution) -> None:
        """Interrupt only the exact active Turn named by the execution handle."""
        try:
            await self._rpc(
                "turn/interrupt",
                {
                    "threadId": execution.provider_session_id,
                    "turnId": execution.provider_execution_id,
                },
            )
        except CodexAppServerProtocolError as error:
            if "no active turn to interrupt" not in str(error):
                raise

    async def recover(
        self,
        request: ConnectorRecoveryRequest,
    ) -> ConnectorExecution | None:
        """Read and resume the referenced Thread; never create a replacement Turn."""
        if not isinstance(request, ConnectorRecoveryRequest):
            raise TypeError("request must be a ConnectorRecoveryRequest")
        thread = await self.read_thread(request.provider_session_id, include_turns=True)
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None
        turn = next(
            (
                item
                for item in turns
                if isinstance(item, dict) and item.get("id") == request.provider_execution_id
            ),
            None,
        )
        if not isinstance(turn, dict):
            return None
        execution = ConnectorExecution(
            request.attempt_id,
            request.provider_session_id,
            request.provider_execution_id,
            True,
        )
        state = self._turns.setdefault(_execution_key(execution), _TurnState(execution))
        self._attempt_executions[request.attempt_id] = execution
        status = turn.get("status")
        if status == "inProgress":
            active = self._active_turns.get(request.provider_session_id)
            if active is not None and active != request.provider_execution_id:
                raise CodexAppServerProtocolError(
                    f"Thread {request.provider_session_id} already has active Turn {active}"
                )
            self._active_turns[request.provider_session_id] = request.provider_execution_id
        await self.resume_thread(request.provider_session_id)
        self._replay_orphans(execution)
        if status != "inProgress" and not state.terminal:
            self._recover_terminal_turn(state, turn)
        return execution

    async def start_thread(self) -> JsonObject:
        """Create a new App Server Thread for a new Agent Session."""
        result = await self._rpc(
            "thread/start",
            {
                "cwd": str(self._workspace),
                "model": self._model,
                "approvalPolicy": self._approval_policy,
                "sandbox": self._sandbox,
            },
        )
        return _required_object(result.get("thread"), "thread/start result.thread")

    async def resume_thread(self, thread_id: str) -> JsonObject:
        """Resume an existing Thread and subscribe this connection to its events."""
        result = await self._rpc(
            "thread/resume",
            {
                "threadId": _required_text(thread_id, "thread_id"),
                "cwd": str(self._workspace),
                "model": self._model,
                "approvalPolicy": self._approval_policy,
                "sandbox": self._sandbox,
            },
        )
        return _required_object(result.get("thread"), "thread/resume result.thread")

    async def fork_thread(self, thread_id: str, *, last_turn_id: str | None = None) -> JsonObject:
        """Fork one persisted Thread into a distinct Agent Session."""
        params: JsonObject = {
            "threadId": _required_text(thread_id, "thread_id"),
            "cwd": str(self._workspace),
            "model": self._model,
            "approvalPolicy": self._approval_policy,
            "sandbox": self._sandbox,
        }
        if last_turn_id is not None:
            params["lastTurnId"] = _required_text(last_turn_id, "last_turn_id")
        result = await self._rpc("thread/fork", params)
        return _required_object(result.get("thread"), "thread/fork result.thread")

    async def read_thread(self, thread_id: str, *, include_turns: bool = False) -> JsonObject:
        """Read a persisted Thread without creating or replacing provider work."""
        result = await self._rpc(
            "thread/read",
            {
                "threadId": _required_text(thread_id, "thread_id"),
                "includeTurns": include_turns,
            },
        )
        return _required_object(result.get("thread"), "thread/read result.thread")

    def pending_requests(
        self,
        execution: ConnectorExecution | None = None,
    ) -> tuple[CodexAppServerPendingRequest, ...]:
        """List unresolved approval/input requests, optionally for one Turn."""
        requests = tuple(self._pending_requests.values())
        if execution is None:
            return requests
        return tuple(
            request
            for request in requests
            if request.thread_id == execution.provider_session_id
            and request.turn_id == execution.provider_execution_id
        )

    async def resolve_request(
        self,
        request_id: RequestId,
        result: Mapping[str, JsonValue],
    ) -> None:
        """Send only an explicit caller decision for a pending server request."""
        if request_id not in self._pending_requests:
            raise KeyError(f"App Server request {request_id!r} is not pending")
        response: JsonObject = {"id": request_id, "result": dict(result)}
        await self._send(response)
        del self._pending_requests[request_id]

    async def close(self) -> None:
        """Close the Endpoint connection and its owned App Server process."""
        reader = self._reader_task
        transport = self._transport
        if transport is not None:
            await transport.close()
        if reader is not None:
            try:
                await asyncio.wait_for(reader, 2.0)
            except TimeoutError:
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader
        self._reader_task = None
        self._transport = None
        self._fail_pending_responses("App Server connection closed")
        self._pending_requests.clear()

    async def _events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None,
    ) -> AsyncIterator[WorkerEvent]:
        state = self._turns.get(_execution_key(execution))
        if state is None:
            raise CodexAppServerProtocolError("App Server execution is not loaded")
        while True:
            event = await state.queue.get()
            if _cursor_after(event.cursor, after_cursor):
                yield event
            if event.type in _TERMINAL_EVENT_TYPES:
                return

    async def _start_turn(
        self,
        thread_id: str,
        request: ConnectorStartRequest,
    ) -> ConnectorExecution:
        if thread_id in self._active_turns:
            raise CodexAppServerProtocolError(f"Thread {thread_id} already has an active Turn")
        params: JsonObject = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": build_codex_prompt(request.request)}],
            "cwd": str(self._workspace),
            "model": self._model,
            "approvalPolicy": self._approval_policy,
            "sandboxPolicy": self._sandbox_policy(),
            "outputSchema": json_loads(codex_output_schema_json()),
        }
        if self._reasoning_effort is not None:
            params["effort"] = self._reasoning_effort
        result = await self._rpc("turn/start", params)
        turn = _required_object(result.get("turn"), "turn/start result.turn")
        turn_id = _required_text(turn.get("id"), "turn.id")
        execution = ConnectorExecution(request.attempt_id, thread_id, turn_id, True)
        self._turns[_execution_key(execution)] = _TurnState(execution)
        self._active_turns[thread_id] = turn_id
        self._replay_orphans(execution)
        return execution

    async def _ensure_connected(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            return
        async with self._connect_lock:
            if self._reader_task is not None and not self._reader_task.done():
                return
            transport = self._transport_factory()
            await transport.open()
            self._transport = transport
            self._connection_generation += 1
            self._reader_task = asyncio.create_task(self._reader_loop(transport))
            result = await self._request_on_open_connection(
                "initialize",
                {
                    "clientInfo": {
                        "name": "ehai",
                        "title": "EHAI",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            _required_text(result.get("userAgent"), "initialize result.userAgent")
            await transport.send({"method": "initialized", "params": {}})

    async def _rpc(self, method: str, params: JsonObject) -> JsonObject:
        await self._ensure_connected()
        return await self._request_on_open_connection(method, params)

    async def _request_on_open_connection(self, method: str, params: JsonObject) -> JsonObject:
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[request_id] = future
        try:
            await self._send({"method": method, "id": request_id, "params": params})
            return await asyncio.wait_for(future, self._request_timeout_seconds)
        except TimeoutError as error:
            raise CodexAppServerProtocolError(f"App Server request {method} timed out") from error
        finally:
            self._pending_responses.pop(request_id, None)

    async def _send(self, message: JsonObject) -> None:
        transport = self._transport
        if transport is None:
            raise CodexAppServerProtocolError("App Server connection is not open")
        async with self._send_lock:
            await transport.send(message)

    async def _reader_loop(self, transport: AppServerTransport) -> None:
        failure = "App Server connection closed"
        try:
            while True:
                message = await transport.receive()
                if message is None:
                    break
                self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = f"App Server connection failed: {error}"
        finally:
            if self._transport is transport:
                self._transport = None
            self._pending_requests.clear()
            self._fail_pending_responses(failure)

    def _dispatch_message(self, message: JsonObject) -> None:
        if "id" in message and ("result" in message or "error" in message):
            self._dispatch_response(message)
            return
        if "id" in message and "method" in message:
            self._dispatch_server_request(message)
            return
        if "method" in message:
            self._dispatch_notification(message)
            return
        raise CodexAppServerProtocolError(
            "App Server message has no request, response, or notification shape"
        )

    def _dispatch_response(self, message: JsonObject) -> None:
        request_id = _request_id(message.get("id"))
        future = self._pending_responses.get(request_id)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            detail = error.get("message")
            future.set_exception(CodexAppServerProtocolError(f"App Server error {code}: {detail}"))
            return
        future.set_result(_required_object(message.get("result"), "App Server response.result"))

    def _dispatch_server_request(self, message: JsonObject) -> None:
        method = _required_text(message.get("method"), "server request method")
        if method not in _WAITING_METHODS:
            return
        params = _required_object(message.get("params"), "server request params")
        request = CodexAppServerPendingRequest(
            request_id=_request_id(message.get("id")),
            method=method,
            thread_id=_required_text(params.get("threadId"), "server request threadId"),
            turn_id=_required_text(params.get("turnId"), "server request turnId"),
            item_id=_required_text(params.get("itemId"), "server request itemId"),
            params=params,
        )
        self._pending_requests[request.request_id] = request
        key = (request.thread_id, request.turn_id)
        state = self._turns.get(key)
        if state is None:
            self._orphan_messages.setdefault(key, []).append(message)
            return
        self._emit(state, WorkerEventType.WAITING, reason=method)

    def _dispatch_notification(self, message: JsonObject) -> None:
        method = _required_text(message.get("method"), "notification method")
        params = _required_object(message.get("params"), "notification params")
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
                self._pending_requests.pop(request_id, None)
            return
        key = _notification_execution_key(params)
        if key is None:
            return
        state = self._turns.get(key)
        if state is None:
            self._orphan_messages.setdefault(key, []).append(message)
            return
        self._apply_notification(state, method, params)

    def _apply_notification(self, state: _TurnState, method: str, params: JsonObject) -> None:
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                phase = item.get("phase")
                if phase in (None, "final_answer"):
                    text = item.get("text")
                    if isinstance(text, str):
                        try:
                            result = parse_codex_result(text).result
                        except CodexProtocolError as error:
                            state.candidate_error = str(error)
                        else:
                            state.candidate_emitted = True
                            self._emit(state, WorkerEventType.CANDIDATE, result=result)
                            return
        if method == "turn/completed":
            turn = _required_object(params.get("turn"), "turn/completed params.turn")
            status = turn.get("status")
            state.terminal = True
            self._active_turns.pop(state.execution.provider_session_id, None)
            if status == "completed" and state.candidate_emitted:
                self._emit(state, WorkerEventType.COMPLETED)
            else:
                reason = state.candidate_error or _turn_failure_reason(turn, status)
                self._emit(state, WorkerEventType.FAILED, reason=reason)
            return
        self._emit(state, WorkerEventType.PROGRESS, reason=method)

    def _recover_terminal_turn(self, state: _TurnState, turn: JsonObject) -> None:
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "agentMessage":
                    continue
                phase = item.get("phase")
                text = item.get("text")
                if phase in (None, "final_answer") and isinstance(text, str):
                    try:
                        result = parse_codex_result(text).result
                    except CodexProtocolError as error:
                        state.candidate_error = str(error)
                    else:
                        state.candidate_emitted = True
                        self._emit(state, WorkerEventType.CANDIDATE, result=result)
        state.terminal = True
        status = turn.get("status")
        if status == "completed" and state.candidate_emitted:
            self._emit(state, WorkerEventType.COMPLETED)
        else:
            self._emit(
                state,
                WorkerEventType.FAILED,
                reason=state.candidate_error or _turn_failure_reason(turn, status),
            )

    def _replay_orphans(self, execution: ConnectorExecution) -> None:
        key = _execution_key(execution)
        messages = self._orphan_messages.pop(key, ())
        for message in messages:
            if "id" in message:
                self._dispatch_server_request(message)
            else:
                self._dispatch_notification(message)

    def _emit(
        self,
        state: _TurnState,
        event_type: WorkerEventType,
        *,
        result: WorkerResult | None = None,
        reason: str | None = None,
    ) -> None:
        self._event_sequence += 1
        cursor = f"{self._connection_token}:{self._connection_generation}:{self._event_sequence}"
        state.queue.put_nowait(
            WorkerEvent(
                worker_event_id=f"codex-app-server:{cursor}",
                attempt_id=state.execution.attempt_id,
                type=event_type,
                cursor=cursor,
                result=result if event_type is WorkerEventType.CANDIDATE else None,
                reason=reason,
            )
        )

    def _sandbox_policy(self) -> JsonObject:
        if self._sandbox == "read-only":
            return {"type": "readOnly", "access": {"type": "fullAccess"}}
        if self._sandbox == "workspace-write":
            return {
                "type": "workspaceWrite",
                "writableRoots": [str(self._workspace)],
                "readOnlyAccess": {"type": "fullAccess"},
                "networkAccess": False,
            }
        return {"type": "dangerFullAccess"}

    def _fail_pending_responses(self, reason: str) -> None:
        for future in tuple(self._pending_responses.values()):
            if not future.done():
                future.set_exception(CodexAppServerProtocolError(reason))


def _normalize_command(executable: str | Path | Sequence[str]) -> tuple[str, ...]:
    command: tuple[str, ...]
    if isinstance(executable, (str, Path)):
        command = (str(executable),)
    else:
        command = tuple(str(item) for item in executable)
    if not command or any(not item.strip() for item in command):
        raise ValueError("App Server executable command must not be empty")
    return command


def _execution_key(execution: ConnectorExecution) -> tuple[str, str]:
    return execution.provider_session_id, execution.provider_execution_id


def _notification_execution_key(params: JsonObject) -> tuple[str, str] | None:
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    turn = params.get("turn")
    if turn_id is None and isinstance(turn, dict):
        turn_id = turn.get("id")
    if isinstance(thread_id, str) and isinstance(turn_id, str):
        return thread_id, turn_id
    return None


def _required_object(value: JsonValue | object, name: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CodexAppServerProtocolError(f"{name} must be an object")
    return cast(JsonObject, value)


def _required_text(value: JsonValue | object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexAppServerProtocolError(f"{name} must be non-blank text")
    return value


def _request_id(value: JsonValue | object) -> RequestId:
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        raise CodexAppServerProtocolError("App Server id must be an integer or string")
    return value


def _turn_failure_reason(turn: JsonObject, status: JsonValue | object) -> str:
    error = turn.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message
    return f"Codex App Server Turn ended with status {status}"


def _cursor_after(cursor: str, after_cursor: str | None) -> bool:
    if after_cursor is None:
        return True
    try:
        cursor_prefix, cursor_index = cursor.rsplit(":", 1)
        after_prefix, after_index = after_cursor.rsplit(":", 1)
        if cursor_prefix != after_prefix:
            return True
        return int(cursor_index) > int(after_index)
    except ValueError:
        return cursor != after_cursor
