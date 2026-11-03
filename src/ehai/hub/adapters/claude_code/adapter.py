"""Claude Code compatibility layer: the unmodified ``claude`` CLI in headless stream-json mode.

EHAI tools reach Claude Code only through this session's Hub MCP endpoint. Every built-in
tool is switched off (``--tools ""``), only the session's MCP server is loaded
(``--strict-mcp-config``) and permission mode ``dontAsk`` denies whatever is not
pre-approved, so the harness can call nothing but the session's tools. ``--bare`` skips
hooks, plugins, CLAUDE.md discovery and keychain/OAuth reads: credentials come only from
the environment variables the settings name.

Each state key gets its own configuration directory (``CLAUDE_CONFIG_DIR`` and ``HOME``)
and a fixed working directory. Claude Code finds a session to resume by working
directory, and EHAI tools run in the core against the Attempt's workspace, so a fixed
directory lets a native session continue across Attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import uuid
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from ehai import JsonValue, json_dumps, json_loads
from ehai.hub.adapters import HarnessFailure, HarnessRequestError, McpEndpoint, ToolBatch
from ehai.hub.protocol import ResolvedHarness, StartSession, ToolResult

KIND = "claude-code"
# Major series of the stream-json control protocol this layer speaks; the exact CLI
# version is part of the settings and their fingerprint.
VERSION = "2"
_MINIMUM = (2, 1, 0)
_SERVER = "ehai"
_PREFIX = f"mcp__{_SERVER}__"
_SETTINGS = frozenset({"executable", "environment_names", "base_url", "state_dir", "cli_version"})
_EFFORTS = frozenset({"low", "medium", "high", "max"})
_BLOCKED_ENVIRONMENT = frozenset(
    {"PATH", "HOME", "USERPROFILE", "NODE_OPTIONS", "NODE_PATH", "COMSPEC", "ANTHROPIC_BASE_URL"}
)
_CAPABILITIES: dict[str, JsonValue] = {
    "session.resume": True,
    "session.fork": False,
    "requests.interactive": False,
    "tools.native.restrictable": True,
    "tools.mcp": True,
    "usage.reported": True,
    "cancel.confirmed": False,
    "schema.unique_items": False,
}
_CONTROL_TIMEOUT_SECONDS = 30.0
_CORRELATION_TIMEOUT_SECONDS = 10.0
# Claude Code names the tool_use block behind every MCP call in the request metadata.
_TOOL_USE_META = "claudecode/toolUseId"
# Claude Code starts a message's tools only after writing the whole model message out, so
# its later tool_use blocks are already in the pipe; this lets the reader take them.
_BATCH_SETTLE_SECONDS = 0.05
_TOOL_PROMPT = (
    "\n\nThe tools named in these instructions are available with the prefix "
    f"`{_PREFIX}` (for example `{_PREFIX}<name>`)."
)


def _version(executable: Path) -> str:
    try:
        with TemporaryDirectory(prefix="ehai-claude-version-") as directory:
            completed = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                timeout=30.0,
                env=_base_environment(executable, Path(directory)),
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Claude Code did not report its version") from error
    match = re.match(rb"(\d+)\.(\d+)\.(\d+) \(Claude Code\)", completed.stdout.strip())
    if completed.returncode != 0 or match is None:
        raise ValueError("Claude Code did not report its version")
    version = tuple(int(part) for part in match.groups())
    if not _MINIMUM <= version < (_MINIMUM[0] + 1, 0, 0):
        raise ValueError(f"This layer needs Claude Code 2.x from {'.'.join(map(str, _MINIMUM))}")
    return ".".join(str(part) for part in version)


def _settings(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Validate settings on this machine and return their canonical form."""
    unknown = set(value) - _SETTINGS
    if unknown or not {"executable", "environment_names", "state_dir"}.issubset(value):
        raise ValueError(
            "Claude Code settings take executable, environment_names, state_dir and "
            "optionally base_url and cli_version"
        )
    paths: list[Path] = []
    for name in ("executable", "state_dir"):
        entry = value[name]
        if not isinstance(entry, str) or not Path(entry).is_absolute():
            raise ValueError(f"Claude Code {name} must be an absolute path")
        paths.append(Path(entry).resolve(strict=True))
    executable, state_dir = paths
    if not executable.is_file() or not state_dir.is_dir():
        raise ValueError("Claude Code executable/state_dir paths have the wrong kind")
    if executable.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("Claude Code executable must be the native binary, not a shell shim")
    names = value["environment_names"]
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(n, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", n) for n in names)
    ):
        raise ValueError("Claude Code environment_names must be uppercase variable names")
    if any(n in _BLOCKED_ENVIRONMENT or str(n).startswith("CLAUDE_") for n in names):
        raise ValueError("Claude Code environment_names cannot replace process controls")
    if len(set(cast(list[str], names))) != len(names):
        raise ValueError("Claude Code environment_names must be unique")
    base_url = value.get("base_url")
    if base_url is not None and (
        not isinstance(base_url, str) or not re.fullmatch(r"https?://[^\s]+", base_url)
    ):
        raise ValueError("Claude Code base_url must be an http(s) URL or null")
    version = _version(executable)
    if value.get("cli_version") not in (None, version):
        raise ValueError(f"Claude Code changed since authorization (now {version})")
    return {
        "executable": str(executable),
        "environment_names": list(cast(list[str], names)),
        "base_url": base_url,
        "state_dir": str(state_dir),
        "cli_version": version,
    }


def _fingerprint(settings: dict[str, JsonValue]) -> str:
    return hashlib.sha256(json_dumps(settings).encode("utf-8")).hexdigest()


def _base_environment(executable: Path, home: Path) -> dict[str, str]:
    # Nothing from the Hub's environment except locale and Windows process basics:
    # no provider keys, proxies, Node options or the user's Claude configuration.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}
    }
    system = (
        [os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")]
        if sys.platform == "win32"
        else ["/usr/bin", "/bin"]
    )
    environment.update(
        {
            "PATH": os.pathsep.join([str(executable.parent), *system]),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CLAUDE_CONFIG_DIR": str(home),
            "TMPDIR": str(home),
            "TEMP": str(home),
            "TMP": str(home),
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_COLOR": "1",
        }
    )
    return environment


def _command(executable: Path, *extra: str) -> list[str]:
    return [
        str(executable),
        "--bare",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--replay-user-messages",
        "--tools",
        "",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        *extra,
    ]


def _sessions(home: Path, session_id: str) -> list[Path]:
    return list((home / "projects").glob(f"*/{session_id}.jsonl"))


class _StreamProcess:
    """One owned ``claude -p`` process: JSONL in and out, correlated control requests."""

    def __init__(self, command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
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

    async def start(self) -> None:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._cwd,
                env=self._environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16 * 1024 * 1024,
                creationflags=flags,
                start_new_session=sys.platform != "win32",
            )
        except OSError as error:
            raise HarnessFailure("Claude Code process could not be started") from error
        self._readers = [asyncio.create_task(self._read())]

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while raw := await self._process.stdout.readline():
                document = json_loads(raw.decode("utf-8"))
                if not isinstance(document, dict) or not isinstance(document.get("type"), str):
                    raise ValueError("record is not an object with a type")
                if document["type"] == "control_response":
                    self._accept(document)
                elif document["type"] != "control_request":
                    self._messages.put_nowait(document)
            self._fail("Claude Code output closed; in-flight outcomes may be unknown")
        except asyncio.CancelledError:
            raise
        except (ValueError, UnicodeError, OSError):
            # Never copy malformed native output into errors; it may hold provider data.
            self._fail("Claude Code output failed validation or disconnected")

    def _accept(self, document: dict[str, JsonValue]) -> None:
        response = document.get("response")
        request_id = response.get("request_id") if isinstance(response, dict) else None
        future = self._pending.get(request_id) if isinstance(request_id, str) else None
        if future is None or future.done():
            return
        assert isinstance(response, dict)
        if response.get("subtype") == "success":
            body = response.get("response")
            future.set_result(body if isinstance(body, dict) else {})
        else:
            future.set_exception(HarnessFailure("Claude Code rejected a control request"))

    def _fail(self, reason: str) -> None:
        if self._failure is None:
            self._failure = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HarnessFailure(reason))
        self._messages.put_nowait(None)

    async def write(self, document: dict[str, JsonValue]) -> None:
        if self._failure is not None:
            raise HarnessFailure(self._failure)
        process = self._process
        if self._closed or process is None or process.stdin is None:
            raise HarnessFailure("Claude Code process is not running")
        try:
            async with self._write_lock:
                process.stdin.write(json_dumps(document).encode("utf-8") + b"\n")
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as error:
            self._fail("Claude Code input closed; the outcome is unknown")
            raise HarnessFailure("Claude Code input closed; the outcome is unknown") from error

    async def control(self, request: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, JsonValue]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.write(
                {"type": "control_request", "request_id": request_id, "request": request}
            )
            async with asyncio.timeout(_CONTROL_TIMEOUT_SECONDS):
                return await future
        except TimeoutError as error:
            raise HarnessFailure("Claude Code did not answer a control request") from error
        finally:
            self._pending.pop(request_id, None)

    async def next(self) -> dict[str, JsonValue]:
        message = await self._messages.get()
        if message is None:
            self._messages.put_nowait(None)
            raise HarnessFailure(self._failure or "Claude Code process ended")
        return message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        try:
            if process is not None and process.returncode is None:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    async with asyncio.timeout(3.0):
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


def _usage(value: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    names = {
        "input_tokens": "input",
        "output_tokens": "output",
        "cache_read_input_tokens": "cacheRead",
        "cache_creation_input_tokens": "cacheWrite",
    }
    return {
        names[key]: item
        for key, item in value.items()
        if key in names and type(item) is int and item >= 0
    }


class ClaudeCodeSession:
    def __init__(
        self, process: _StreamProcess, session_id: str, tools: set[str], files: list[Path]
    ) -> None:
        self._process = process
        self._session_id = session_id
        self._tools = tools
        self._files = files
        self._out: deque[dict[str, JsonValue]] = deque()
        # The model message being read: its id, text so far and whether the Hub has it.
        self._message: str | None = None
        self._text: list[str] = []
        self._usage: dict[str, JsonValue] | None = None
        self._announced = False
        # Tool uses of the current message, in order; shared with each of its uses below.
        self._batch: list[str] = []
        # Tool uses by id whose model message the Hub has logged, and those still waiting.
        self._published: dict[str, tuple[str, str, list[str]]] = {}
        self._unpublished: list[tuple[str, tuple[str, str, list[str]]]] = []
        self._announcement_returned = False

    @property
    def native_session(self) -> JsonValue:
        return {"session_id": self._session_id}

    async def next_event(self) -> dict[str, JsonValue]:
        # The Hub logs each returned event before asking for the next one. Once it has the
        # current model message, MCP calls that message requested may go ahead.
        self._publish()
        while not self._out:
            self._normalize(await self._process.next())
            self._publish()
        event = self._out.popleft()
        if event["type"] == "assistant_message":
            self._announcement_returned = True
        return event

    def _publish(self) -> None:
        if self._announcement_returned and self._unpublished:
            self._published.update(self._unpublished)
            self._unpublished.clear()

    def _normalize(self, event: dict[str, JsonValue]) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            tools = event.get("tools")
            servers = event.get("mcp_servers")
            if (
                not isinstance(tools, list)
                or set(tools) != self._tools
                or event.get("permissionMode") != "dontAsk"
                or servers != [{"name": _SERVER, "status": "connected"}]
            ):
                raise HarnessFailure("Claude Code exposed tools outside the EHAI session")
        elif kind == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                # Our own input replayed: Claude Code is about to send it to the model.
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                self._out.append({"type": "input_prepared", "input_hashes": [digest]})
        elif kind == "assistant":
            self._assistant(event.get("message"))
        elif kind == "result":
            self._end_message()
            if event.get("subtype") == "success" and event.get("is_error") is False:
                self._out.append({"type": "settled"})
            else:
                subtype = event.get("subtype")
                reason = subtype if isinstance(subtype, str) else "unknown"
                self._out.append(
                    {"type": "failed", "reason": f"Claude Code ended the turn: {reason}"}
                )

    def _assistant(self, message: JsonValue) -> None:
        if not isinstance(message, dict) or not isinstance(message.get("id"), str):
            raise HarnessFailure("Claude Code reported a model message without identity")
        if message["id"] != self._message:
            self._end_message()
            self._message = str(message["id"])
            self._usage = _usage(message.get("usage"))
        blocks = message.get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                self._text.append(str(block["text"]))
            elif block.get("type") == "tool_use":
                use_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(name, str) or name not in self._tools:
                    raise HarnessFailure("Claude Code requested a tool outside the session")
                if not isinstance(use_id, str) or not use_id:
                    raise HarnessFailure("Claude Code requested a tool without identity")
                # The Hub needs the model message before its MCP calls run.
                self._batch.append(use_id)
                use = (name.removeprefix(_PREFIX), json_dumps(arguments), self._batch)
                self._unpublished.append((use_id, use))
                self._announce("toolUse")

    def _announce(self, stop_reason: str) -> None:
        if self._announced or self._message is None:
            return
        self._announced = True
        self._out.append(
            {
                "type": "assistant_message",
                "text": "\n".join(self._text),
                "stop_reason": stop_reason,
                "usage": self._usage,
            }
        )

    def _end_message(self) -> None:
        if self._message is None:
            return
        self._announce("stop")
        self._out.append({"type": "lifecycle", "name": "turn_end"})
        self._message, self._text, self._usage, self._announced = None, [], None, False
        self._batch = []
        self._announcement_returned = False

    async def await_tool_call(
        self, name: str, arguments: dict[str, JsonValue], meta: Mapping[str, JsonValue]
    ) -> ToolBatch:
        use_id = meta.get(_TOOL_USE_META)
        if not isinstance(use_id, str):
            raise HarnessFailure("Claude Code made an MCP call without its tool use identity")
        # Bounded: a call never waits forever for output Claude Code did not write.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CORRELATION_TIMEOUT_SECONDS
        while use_id not in self._published and loop.time() < deadline:
            await asyncio.sleep(0.02)
        use = self._published.pop(use_id, None)
        if use is None:
            raise HarnessFailure("Claude Code made an MCP call its output never requested")
        if use[:2] != (name, json_dumps(arguments)):
            raise HarnessFailure("Claude Code made an MCP call that differs from its request")
        await asyncio.sleep(_BATCH_SETTLE_SECONDS)
        return ToolBatch(use_id, tuple(use[2]))

    async def _user(self, text: str) -> None:
        await self._process.write(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": self._session_id,
            }
        )

    async def steer(self, messages: list[str]) -> None:
        # Claude Code adds input that arrives during a turn after the running tool returns.
        for message in messages:
            await self._user(message)

    async def prompt(self, message: str) -> None:
        await self._user(message)

    async def tool_result(self, result: ToolResult) -> None:
        del result
        raise HarnessFailure("Claude Code receives EHAI tool results over MCP")

    async def cancel(self) -> None:
        await self._process.control({"subtype": "interrupt"})

    async def close(self, *, abort: bool) -> None:
        async def stop() -> None:
            if abort:
                with suppress(HarnessFailure):
                    await self._process.control({"subtype": "interrupt"})
            await self._process.close()
            for path in self._files:
                with suppress(OSError):
                    path.unlink()

        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


class ClaudeCodeAdapter:
    kind = KIND
    version = VERSION

    @property
    def capabilities(self) -> dict[str, JsonValue]:
        return dict(_CAPABILITIES)

    def resolve(self, settings: dict[str, JsonValue], pin_directory: str | None) -> ResolvedHarness:
        # Claude Code has no native configuration files to pin: everything is in settings.
        del pin_directory
        try:
            canonical = _settings(settings)
        except (OSError, ValueError) as error:
            raise HarnessRequestError(str(error)) from error
        return ResolvedHarness(
            kind=self.kind,
            version=self.version,
            settings=canonical,
            fingerprint=_fingerprint(canonical),
            environment_names=list(cast(list[str], canonical["environment_names"])),
            protected_paths=[str(canonical["executable"]), str(canonical["state_dir"])],
            capabilities=self.capabilities,
        )

    async def inspect(self, settings: dict[str, JsonValue]) -> dict[str, JsonValue]:
        executable = settings.get("executable")
        if set(settings) != {"executable"} or not isinstance(executable, str):
            raise HarnessRequestError("Claude Code inspection takes exactly executable")
        try:
            path = Path(executable).resolve(strict=True)
            version = await asyncio.to_thread(_version, path)
        except (OSError, ValueError) as error:
            raise HarnessRequestError(str(error)) from error
        with TemporaryDirectory(prefix="ehai-claude-inspect-") as directory:
            home = Path(directory)
            process = _StreamProcess(
                _command(path, "--no-session-persistence", "--model", "inspect"),
                cwd=home,
                environment=_base_environment(path, home),
            )
            await process.start()
            try:
                initialized = await process.control({"subtype": "initialize", "hooks": None})
                status = await process.control({"subtype": "mcp_status"})
            finally:
                await process.close()
        return {
            "backend": self.kind,
            "version": version,
            "protocol": "stream-json",
            "control_channel_verified": "commands" in initialized,
            "mcp_servers": len(cast(list[JsonValue], status.get("mcpServers") or [])),
            "model_execution_verified": False,
            "model_requests": 0,
            "credentials_used": False,
        }

    async def launch(self, request: StartSession, mcp: McpEndpoint) -> ClaudeCodeSession:
        settings = dict(request.harness.settings)
        state_root = settings.pop("state_root", None)
        try:
            canonical = await asyncio.to_thread(_settings, settings)
            if state_root is None:
                state_root = canonical["state_dir"]
            if not isinstance(state_root, str) or not Path(state_root).is_absolute():
                raise ValueError("Claude Code settings require an absolute state_root")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", request.state_key):
                raise ValueError("State key must be a plain directory name")
            session_id = str(uuid.UUID(request.native_session_id))
            if request.reasoning_effort not in (None, *_EFFORTS):
                raise ValueError("Claude Code effort is low, medium, high or max")
            executable = Path(str(canonical["executable"]))
            home = Path(state_root).resolve() / request.state_key
            work = home / "work"
            work.mkdir(parents=True, exist_ok=True)
            existing = _sessions(home, session_id)
            if request.fresh and existing:
                raise ValueError("Untracked native history cannot become a fresh EHAI Session")
            if not request.fresh and not existing:
                raise ValueError("Native Claude Code history is missing; refusing a fresh one")
            environment = _base_environment(executable, home)
            if canonical["base_url"] is not None:
                environment["ANTHROPIC_BASE_URL"] = str(canonical["base_url"])
            for name in cast(list[str], canonical["environment_names"]):
                value = os.environ.get(name)
                if not value:
                    raise ValueError(f"Claude Code requires configured environment variable {name}")
                environment[name] = value
            servers = home / f"mcp-{request.invocation_id}.json"
            prompt = home / f"prompt-{request.invocation_id}.txt"
            # The MCP token stays out of the process list: it is passed in a private file.
            servers.write_text(
                json_dumps(
                    {
                        "mcpServers": {
                            _SERVER: {
                                "type": "http",
                                "url": mcp.url,
                                "headers": {"Authorization": f"Bearer {mcp.token}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            servers.chmod(0o600)
            prompt.write_text(request.system_prompt + _TOOL_PROMPT, encoding="utf-8")
        except (OSError, ValueError) as error:
            raise HarnessRequestError(str(error)) from error
        extra = [
            "--mcp-config",
            str(servers),
            "--allowedTools",
            f"mcp__{_SERVER}",
            "--model",
            request.model,
            "--system-prompt-file",
            str(prompt),
            "--session-id" if request.fresh else "--resume",
            session_id,
        ]
        if request.reasoning_effort is not None:
            extra.extend(("--effort", request.reasoning_effort))
        process = _StreamProcess(_command(executable, *extra), cwd=work, environment=environment)
        await process.start()
        tools = {_PREFIX + tool.name for tool in request.tools}
        try:
            await process.control({"subtype": "initialize", "hooks": None})
            await self._await_tools(process, {tool.name for tool in request.tools})
        except BaseException:
            await process.close()
            for path in (servers, prompt):
                with suppress(OSError):
                    path.unlink()
            raise
        return ClaudeCodeSession(process, session_id, tools, [servers, prompt])

    async def _await_tools(self, process: _StreamProcess, expected: set[str]) -> None:
        # Claude Code connects to MCP servers in the background; wait until it has ours.
        try:
            async with asyncio.timeout(_CONTROL_TIMEOUT_SECONDS):
                while True:
                    status = await process.control({"subtype": "mcp_status"})
                    servers = status.get("mcpServers")
                    if not isinstance(servers, list) or len(servers) != 1:
                        raise HarnessFailure("Claude Code loaded MCP servers outside the session")
                    server = servers[0]
                    if not isinstance(server, dict) or server.get("name") != _SERVER:
                        raise HarnessFailure("Claude Code loaded MCP servers outside the session")
                    if server.get("status") == "connected":
                        listed = server.get("tools")
                        names = {
                            item.get("name")
                            for item in (listed if isinstance(listed, list) else [])
                            if isinstance(item, dict)
                        }
                        if names != expected:
                            raise HarnessFailure("Claude Code did not list the session's tools")
                        return
                    if server.get("status") not in ("pending", "connecting"):
                        raise HarnessFailure("Claude Code could not reach the session's tools")
                    await asyncio.sleep(0.2)
        except TimeoutError as error:
            raise HarnessFailure("Claude Code did not connect to the session's tools") from error
