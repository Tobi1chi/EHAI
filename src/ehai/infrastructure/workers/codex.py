"""Bounded local-process connector for the P1 Codex Worker."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ehai import ID, normalize_id
from ehai.application.workers import (
    CandidateArtifact,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
    WorkerTimedOutError,
)
from ehai.domain.artifacts import ArtifactKind
from ehai.infrastructure.workers.codex_protocol import (
    CodexProtocolError,
    build_codex_prompt,
    codex_output_schema_json,
    parse_codex_result,
)

_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_TRUNCATION_MARKER = b"\n...[truncated]...\n"
_STREAM_CHUNK_BYTES = 8192
_MAX_JSONL_LINE_BYTES = 65_536
_MAX_JSONL_EVENT_TYPES = 16
_DEFAULT_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "XDG_CONFIG_HOME",
    }
)
_SENSITIVE_ENVIRONMENT_NAME = re.compile(r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|BEARER)")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_QUOTED_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|secret)[\"']?"
    r"\s*[:=]\s*)([\"']).*?\2"
)
_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|secret)[\"']?"
    r"\s*[:=]\s*[\"']?)[^\s\"',;]+"
)
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]+")


@dataclass(slots=True)
class _CancellationSlot:
    requested: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    loop: asyncio.AbstractEventLoop | None = None
    signal: asyncio.Event | None = None
    process: asyncio.subprocess.Process | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop, cancel_signal: asyncio.Event) -> None:
        with self.lock:
            self.loop = loop
            self.signal = cancel_signal
            requested = self.requested.is_set()
        if requested:
            cancel_signal.set()

    def bind_process(self, process: asyncio.subprocess.Process) -> None:
        with self.lock:
            self.process = process

    def cancel(self) -> None:
        self.requested.set()
        with self.lock:
            loop = self.loop
            cancel_signal = self.signal
        if loop is not None and cancel_signal is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(cancel_signal.set)


@dataclass(slots=True)
class _BoundedCapture:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.limit

    def snapshot(self) -> bytes:
        return _truncate(bytes(self.data), self.limit, truncated=self.truncated)


@dataclass(slots=True)
class _JsonlObserver:
    pending: bytearray = field(default_factory=bytearray)
    dropping_line: bool = False
    malformed_lines: int = 0
    oversized_lines: int = 0
    event_types: list[str] = field(default_factory=list)

    def feed(self, chunk: bytes) -> None:
        position = 0
        while position < len(chunk):
            newline = chunk.find(b"\n", position)
            if newline < 0:
                self._append(chunk[position:])
                return
            self._append(chunk[position:newline])
            self._finish_line()
            position = newline + 1

    def finish(self) -> None:
        if self.pending or self.dropping_line:
            self._finish_line()

    def diagnostics(self) -> tuple[str, ...]:
        items = tuple(f"codex jsonl event: {item}" for item in self.event_types)
        if self.malformed_lines:
            items += (f"ignored {self.malformed_lines} malformed codex JSONL lines",)
        if self.oversized_lines:
            items += (f"ignored {self.oversized_lines} oversized codex JSONL lines",)
        return items

    def _append(self, segment: bytes) -> None:
        if self.dropping_line:
            return
        remaining = _MAX_JSONL_LINE_BYTES - len(self.pending)
        if len(segment) > remaining:
            self.pending.clear()
            self.dropping_line = True
            return
        self.pending.extend(segment)

    def _finish_line(self) -> None:
        if self.dropping_line:
            self.oversized_lines += 1
        else:
            line = bytes(self.pending).rstrip(b"\r")
            if line:
                self._observe(line)
        self.pending.clear()
        self.dropping_line = False

    def _observe(self, line: bytes) -> None:
        try:
            decoded = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.malformed_lines += 1
            return
        if not isinstance(decoded, dict):
            self.malformed_lines += 1
            return
        event_type = decoded.get("type")
        if isinstance(event_type, str) and event_type.strip():
            sanitized = _bounded_text(_redact_text(event_type.strip()), 100)
            if sanitized not in self.event_types and len(self.event_types) < _MAX_JSONL_EVENT_TYPES:
                self.event_types.append(sanitized)


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    return_code: int | None
    stdout: bytes
    stderr: bytes
    diagnostics: tuple[str, ...]
    timed_out: bool = False
    cancelled: bool = False
    communication_error: str | None = None


class CodexWorkerAdapter:
    """Execute one Codex CLI process per Worker Attempt with bounded capture."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        executable: str | Path | Sequence[str] = "codex",
        sandbox: str = "read-only",
        timeout_seconds: float = 300.0,
        cancel_grace_seconds: float = 2.0,
        max_output_bytes: int = 1_048_576,
        env_overrides: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace = _validated_workspace(workspace)
        self._command_prefix = _validated_command_prefix(executable)
        if sandbox not in _SANDBOX_MODES:
            raise ValueError(f"unsupported Codex sandbox mode: {sandbox}")
        self._timeout_seconds = _positive_finite(timeout_seconds, "timeout_seconds")
        self._cancel_grace_seconds = _positive_finite(cancel_grace_seconds, "cancel_grace_seconds")
        if type(max_output_bytes) is not int or max_output_bytes < len(_TRUNCATION_MARKER):
            raise ValueError(
                f"max_output_bytes must be an integer of at least {len(_TRUNCATION_MARKER)}"
            )
        self._sandbox = sandbox
        self._max_output_bytes = max_output_bytes
        self._environment, self._secret_values = _worker_environment(env_overrides)
        self._active: dict[ID, _CancellationSlot] = {}
        self._pending_cancellations: set[ID] = set()
        self._active_lock = threading.Lock()

    def execute(self, request: WorkerRequest) -> WorkerResult:
        """Run Codex once and return only candidate and diagnostic Artifacts."""
        if not isinstance(request, WorkerRequest):
            raise TypeError("request must be a WorkerRequest")
        slot = self._register(request)
        try:
            prompt = build_codex_prompt(request).encode("utf-8")
            with tempfile.TemporaryDirectory(prefix="ehai-codex-worker-") as temporary:
                temporary_path = Path(temporary)
                schema_path = temporary_path / "output-schema.json"
                last_message_path = temporary_path / "last-message.json"
                schema_path.write_text(codex_output_schema_json(), encoding="utf-8")
                if slot.requested.is_set():
                    raise self._error(
                        WorkerCancelledError,
                        request,
                        "Codex execution was cancelled before process spawn",
                        diagnostics=("cancellation requested before Codex process spawn",),
                    )
                command = self._command(schema_path, last_message_path)
                try:
                    outcome = asyncio.run(self._execute_async(command, prompt, slot))
                except OSError as error:
                    raise self._error(
                        WorkerExecutionError,
                        request,
                        "Codex process could not start: "
                        f"{type(error).__name__}: "
                        f"{_redact_text(str(error), self._secret_values)}",
                    ) from error
                if slot.requested.is_set() and not outcome.timed_out:
                    outcome = _ProcessOutcome(
                        return_code=outcome.return_code,
                        stdout=outcome.stdout,
                        stderr=outcome.stderr,
                        diagnostics=outcome.diagnostics,
                        cancelled=True,
                        communication_error=outcome.communication_error,
                    )
                return self._interpret(request, last_message_path, outcome)
        finally:
            self._unregister(request.attempt_id, slot)

    def cancel(self, attempt_id: ID) -> None:
        """Record cancellation and wake the active process loop, even before spawn."""
        normalized_id = normalize_id(attempt_id)
        with self._active_lock:
            slot = self._active.get(normalized_id)
            if slot is None:
                self._pending_cancellations.add(normalized_id)
                return
        slot.cancel()

    async def _execute_async(
        self,
        command: tuple[str, ...],
        prompt: bytes,
        slot: _CancellationSlot,
    ) -> _ProcessOutcome:
        loop = asyncio.get_running_loop()
        cancel_signal = asyncio.Event()
        slot.bind_loop(loop, cancel_signal)
        if cancel_signal.is_set():
            return _ProcessOutcome(None, b"", b"", (), cancelled=True)

        process = await self._spawn(command)
        slot.bind_process(process)
        if slot.requested.is_set():
            cancel_signal.set()

        stdout_capture = _BoundedCapture(self._max_output_bytes)
        stderr_capture = _BoundedCapture(self._max_output_bytes)
        jsonl_observer = _JsonlObserver()
        stdout_task = asyncio.create_task(
            _drain_stream(process.stdout, stdout_capture, jsonl_observer),
            name="ehai-codex-stdout",
        )
        stderr_task = asyncio.create_task(
            _drain_stream(process.stderr, stderr_capture),
            name="ehai-codex-stderr",
        )
        prompt_task = asyncio.create_task(
            _write_prompt(process.stdin, prompt),
            name="ehai-codex-stdin",
        )
        process_task = asyncio.create_task(process.wait(), name="ehai-codex-process")
        cancel_task = asyncio.create_task(cancel_signal.wait(), name="ehai-codex-cancel")
        timed_out = False
        cancelled = False
        termination_diagnostics: tuple[str, ...] = ()
        done, _ = await asyncio.wait(
            (process_task, cancel_task),
            timeout=self._timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancel_task.result():
            cancelled = True
            termination_diagnostics = await self._terminate_tree(process)
        elif process_task not in done:
            timed_out = True
            termination_diagnostics = await self._terminate_tree(process)

        return_code, wait_diagnostic = await self._settle_process(process, process_task)
        io_diagnostics = await _settle_io(
            (prompt_task, stdout_task, stderr_task),
            self._cancel_grace_seconds,
        )
        if not cancel_task.done():
            cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        jsonl_observer.finish()
        diagnostics = (
            *jsonl_observer.diagnostics(),
            *termination_diagnostics,
            *io_diagnostics,
        )
        if stdout_capture.truncated:
            diagnostics += ("codex stdout was truncated",)
        if stderr_capture.truncated:
            diagnostics += ("codex stderr was truncated",)
        return _ProcessOutcome(
            return_code=return_code,
            stdout=stdout_capture.snapshot(),
            stderr=stderr_capture.snapshot(),
            diagnostics=diagnostics,
            timed_out=timed_out,
            cancelled=cancelled,
            communication_error=wait_diagnostic,
        )

    async def _spawn(self, command: tuple[str, ...]) -> asyncio.subprocess.Process:
        options: dict[str, object] = {
            "cwd": str(self._workspace),
            "env": dict(self._environment),
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*command, **options)  # type: ignore[arg-type]

    async def _terminate_tree(
        self,
        process: asyncio.subprocess.Process,
    ) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if os.name == "nt":
            descendants, discovery_diagnostic = await self._windows_descendant_pids(process.pid)
            if discovery_diagnostic is not None:
                diagnostics.append(discovery_diagnostic)
            helper_diagnostic = await self._taskkill(process.pid, force=False)
            if helper_diagnostic is not None:
                diagnostics.append(helper_diagnostic)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=self._cancel_grace_seconds)
            for descendant_pid in reversed(descendants):
                descendant_diagnostic = await self._taskkill(descendant_pid, force=True)
                if descendant_diagnostic is not None:
                    diagnostics.append(descendant_diagnostic)
            if process.returncode is None:
                force_diagnostic = await self._taskkill(process.pid, force=True)
                if force_diagnostic is not None:
                    diagnostics.append(force_diagnostic)
        else:
            try:
                os.kill(-process.pid, signal.SIGTERM)
            except OSError as error:
                diagnostics.append(f"process-group TERM failed: {type(error).__name__}")
            await asyncio.sleep(self._cancel_grace_seconds)
            try:
                os.kill(-process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError as error:
                diagnostics.append(f"process-group KILL failed: {type(error).__name__}")
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        return tuple(diagnostics)

    async def _windows_descendant_pids(
        self,
        root_pid: int,
    ) -> tuple[tuple[int, ...], str | None]:
        script = (
            "param([int]$RootPidValue);"
            "$all=@(Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId);"
            "$frontier=@($RootPidValue);$result=@();"
            "while($frontier.Count -gt 0){$next=@();"
            "foreach($parent in $frontier){foreach($child in $all){"
            "if([int]$child.ParentProcessId -eq [int]$parent){"
            "$id=[int]$child.ProcessId;"
            "if($result -notcontains $id){$result+=$id;$next+=$id}}}};"
            "$frontier=$next};$result | ForEach-Object { Write-Output $_ }"
        )
        try:
            helper = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                str(root_pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(self._environment),
            )
        except OSError as error:
            return (), f"Windows descendant discovery failed: {type(error).__name__}"
        try:
            stdout, stderr = await asyncio.wait_for(
                helper.communicate(), timeout=max(self._cancel_grace_seconds, 1.0)
            )
        except TimeoutError:
            with suppress(ProcessLookupError):
                helper.kill()
            with suppress(TimeoutError):
                await asyncio.wait_for(helper.wait(), timeout=self._cancel_grace_seconds)
            return (), "Windows descendant discovery failed: TimeoutError"
        if helper.returncode != 0:
            detail = _bounded_text(
                _redact_bytes(stderr, self._secret_values).decode("utf-8").strip(),
                200,
            )
            suffix = f": {detail}" if detail else ""
            return (), f"Windows descendant discovery exited with {helper.returncode}{suffix}"
        descendants: list[int] = []
        for line in stdout.decode("ascii", errors="ignore").splitlines():
            candidate = line.strip()
            if candidate.isdigit():
                pid = int(candidate)
                if pid > 0 and pid not in {root_pid, os.getpid()}:
                    descendants.append(pid)
        return tuple(dict.fromkeys(descendants)), None

    async def _taskkill(self, pid: int, *, force: bool) -> str | None:
        if pid <= 0 or pid == os.getpid():
            return f"refused unsafe Windows process-tree target PID {pid}"
        helper_timeout = max(self._cancel_grace_seconds, 1.0)
        arguments = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            arguments.append("/F")
        try:
            helper = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(self._environment),
            )
        except OSError as error:
            return f"Windows process-tree kill failed to start: {type(error).__name__}"
        try:
            helper_stdout, helper_stderr = await asyncio.wait_for(
                helper.communicate(), timeout=helper_timeout
            )
        except TimeoutError:
            with suppress(ProcessLookupError):
                helper.kill()
            try:
                helper_stdout, helper_stderr = await asyncio.wait_for(
                    helper.communicate(), timeout=helper_timeout
                )
            except TimeoutError:
                return "Windows process-tree kill helper did not exit"
        return_code = helper.returncode
        if return_code == 0:
            return None
        helper_output = helper_stdout + b"\n" + helper_stderr
        detail = _bounded_text(
            _redact_bytes(helper_output, self._secret_values).decode("utf-8").strip(),
            200,
        )
        suffix = f": {detail}" if detail else ""
        mode = "force" if force else "graceful"
        return f"Windows {mode} process-tree stop exited with {return_code}{suffix}"

    async def _settle_process(
        self,
        process: asyncio.subprocess.Process,
        process_task: asyncio.Task[int],
    ) -> tuple[int | None, str | None]:
        try:
            return await asyncio.wait_for(
                asyncio.shield(process_task),
                timeout=self._cancel_grace_seconds,
            ), None
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            try:
                return await asyncio.wait_for(
                    asyncio.shield(process_task),
                    timeout=self._cancel_grace_seconds,
                ), "Codex process required direct kill after tree cleanup"
            except TimeoutError:
                process_task.cancel()
                await asyncio.gather(process_task, return_exceptions=True)
                return process.returncode, "Codex process did not exit after bounded cleanup"

    def _interpret(
        self,
        request: WorkerRequest,
        last_message_path: Path,
        outcome: _ProcessOutcome,
    ) -> WorkerResult:
        stdout = _sanitize_output(outcome.stdout, self._max_output_bytes, self._secret_values)
        stderr = _sanitize_output(outcome.stderr, self._max_output_bytes, self._secret_values)
        diagnostics = tuple(
            _bounded_text(_redact_text(item, self._secret_values), 500)
            for item in outcome.diagnostics
        )
        if outcome.cancelled:
            raise self._error(
                WorkerCancelledError,
                request,
                "Codex execution was cancelled",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        if outcome.timed_out:
            raise self._error(
                WorkerTimedOutError,
                request,
                f"Codex execution timed out after {self._timeout_seconds:g} seconds",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        if outcome.communication_error is not None:
            raise self._error(
                WorkerExecutionError,
                request,
                outcome.communication_error,
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        if outcome.return_code != 0:
            raise self._error(
                WorkerExecutionError,
                request,
                f"Codex exited with code {outcome.return_code}",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        try:
            final_text = _read_final(last_message_path, self._max_output_bytes)
        except (OSError, UnicodeError, ValueError) as error:
            raise self._error(
                WorkerExecutionError,
                request,
                str(error),
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            ) from error
        try:
            parsed = parse_codex_result(final_text).worker_result
        except CodexProtocolError as error:
            raise self._error(
                WorkerExecutionError,
                request,
                f"Codex returned an invalid structured result: {error}",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            ) from error
        return self._result(parsed, final_text, stdout, stderr, diagnostics)

    def _result(
        self,
        parsed: WorkerResult,
        final_text: str,
        stdout: bytes,
        stderr: bytes,
        diagnostics: tuple[str, ...],
    ) -> WorkerResult:
        candidates = tuple(
            _sanitize_candidate(artifact, self._secret_values) for artifact in parsed.artifacts
        )
        worker_output = _sanitize_output(
            final_text.encode("utf-8") + b"\n--- codex stdout jsonl ---\n" + stdout,
            self._max_output_bytes,
            self._secret_values,
        )
        output_artifact = CandidateArtifact(
            kind=ArtifactKind.WORKER_OUTPUT,
            name="codex-worker-output.txt",
            media_type="text/plain; charset=utf-8",
            content=worker_output,
        )
        log_artifacts = (
            (
                CandidateArtifact(
                    kind=ArtifactKind.LOG,
                    name="codex-stderr.log",
                    media_type="text/plain; charset=utf-8",
                    content=stderr,
                ),
            )
            if stderr
            else ()
        )
        return WorkerResult(
            artifacts=(*candidates, output_artifact, *log_artifacts),
            summary=_redact_text(parsed.summary, self._secret_values),
            raw_output=stdout,
            diagnostics=("codex exited with code 0", *diagnostics),
        )

    def _register(self, request: WorkerRequest) -> _CancellationSlot:
        slot = _CancellationSlot()
        with self._active_lock:
            if request.attempt_id in self._active:
                raise WorkerExecutionError(
                    request.run_id,
                    request.attempt_id,
                    request.plan_node_id,
                    "Codex process is already active for this Attempt",
                )
            if request.attempt_id in self._pending_cancellations:
                self._pending_cancellations.remove(request.attempt_id)
                slot.requested.set()
            self._active[request.attempt_id] = slot
        return slot

    def _unregister(self, attempt_id: ID, slot: _CancellationSlot) -> None:
        with self._active_lock:
            if self._active.get(attempt_id) is slot:
                del self._active[attempt_id]

    def _command(self, schema_path: Path, last_message_path: Path) -> tuple[str, ...]:
        return (
            *self._command_prefix,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--sandbox",
            self._sandbox,
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(last_message_path),
            "--cd",
            str(self._workspace),
            "-",
        )

    def _error(
        self,
        error_type: type[WorkerExecutionError],
        request: WorkerRequest,
        reason: str,
        *,
        raw_output: bytes = b"",
        log_output: bytes = b"",
        diagnostics: tuple[str, ...] = (),
    ) -> WorkerExecutionError:
        return error_type(
            request.run_id,
            request.attempt_id,
            request.plan_node_id,
            _redact_text(reason, self._secret_values),
            raw_output=raw_output,
            log_output=log_output,
            diagnostics=diagnostics,
        )


async def _drain_stream(
    reader: asyncio.StreamReader | None,
    capture: _BoundedCapture,
    observer: _JsonlObserver | None = None,
) -> str | None:
    if reader is None:
        return "Codex process stream was unavailable"
    try:
        while True:
            chunk = await reader.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                return None
            capture.feed(chunk)
            if observer is not None:
                observer.feed(chunk)
    except (OSError, asyncio.IncompleteReadError) as error:
        return f"Codex stream drain failed: {type(error).__name__}"


async def _write_prompt(writer: asyncio.StreamWriter | None, prompt: bytes) -> str | None:
    if writer is None:
        return "Codex stdin was unavailable"
    try:
        writer.write(prompt)
        await writer.drain()
        return None
    except (BrokenPipeError, ConnectionResetError, OSError) as error:
        return f"Codex stdin write failed: {type(error).__name__}"
    finally:
        writer.close()


async def _settle_io(
    tasks: tuple[asyncio.Task[str | None], ...],
    timeout: float,
) -> tuple[str, ...]:
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    diagnostics: list[str] = []
    for task in pending:
        task.cancel()
        diagnostics.append(f"{task.get_name()} did not finish within cleanup deadline")
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        try:
            diagnostic = task.result()
        except Exception as error:
            diagnostics.append(f"{task.get_name()} failed: {type(error).__name__}")
        else:
            if diagnostic is not None:
                diagnostics.append(diagnostic)
    return tuple(diagnostics)


def _read_final(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError as error:
        raise ValueError("Codex produced no final structured result") from error
    if size == 0:
        raise ValueError("Codex produced no final structured result")
    if size > limit:
        raise ValueError("Codex final structured result exceeded output limit")
    return path.read_text(encoding="utf-8")


def _validated_workspace(value: str | Path) -> Path:
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, TypeError) as error:
        raise ValueError(f"Codex workspace does not exist: {value}") from error
    if not path.is_dir():
        raise ValueError(f"Codex workspace is not a directory: {path}")
    return path


def _validated_command_prefix(value: str | Path | Sequence[str]) -> tuple[str, ...]:
    raw = (str(value),) if isinstance(value, (str, Path)) else tuple(value)
    if not raw or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise ValueError("Codex executable command must contain non-blank strings")
    return raw


def _positive_finite(value: float, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be positive and finite")
    return float(value)


def _worker_environment(
    overrides: Mapping[str, str] | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _DEFAULT_ENVIRONMENT_NAMES
        and _SENSITIVE_ENVIRONMENT_NAME.search(key) is None
    }
    secret_values: set[str] = set()
    for key, value in environment.items():
        if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY"}:
            secret_values.add(value)
            parsed = urlsplit(value)
            if parsed.username:
                secret_values.add(unquote(parsed.username))
            if parsed.password:
                secret_values.add(unquote(parsed.password))
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise TypeError("env_overrides must be a string mapping")
        for key, value in overrides.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\x00" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ValueError("env_overrides must contain valid string environment entries")
            environment[key] = value
            if len(value) >= 4:
                secret_values.add(value)
    bounded_secrets = tuple(
        sorted((value for value in secret_values if value), key=len, reverse=True)
    )
    return environment, bounded_secrets


def _sanitize_candidate(
    artifact: CandidateArtifact,
    secret_values: tuple[str, ...] = (),
) -> CandidateArtifact:
    return CandidateArtifact(
        kind=artifact.kind,
        name=_redact_text(artifact.name, secret_values),
        media_type=artifact.media_type,
        content=_redact_bytes(artifact.content, secret_values),
    )


def _sanitize_output(
    value: bytes,
    limit: int,
    secret_values: tuple[str, ...] = (),
) -> bytes:
    return _truncate(
        _redact_bytes(value, secret_values),
        limit,
        truncated=len(value) > limit,
    )


def _redact_bytes(value: bytes, secret_values: tuple[str, ...] = ()) -> bytes:
    return _redact_text(value.decode("utf-8", errors="replace"), secret_values).encode("utf-8")


def _redact_text(value: str, secret_values: tuple[str, ...] = ()) -> str:
    redacted = value
    for secret in secret_values:
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = _QUOTED_SECRET_PATTERN.sub(r"\1\2[REDACTED]\2", redacted)
    redacted = _SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    return _KEY_PATTERN.sub("[REDACTED]", redacted)


def _truncate(value: bytes, limit: int, *, truncated: bool | None = None) -> bytes:
    was_truncated = len(value) > limit if truncated is None else truncated or len(value) > limit
    if not was_truncated:
        return value
    return value[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 16] + "...[truncated]"
