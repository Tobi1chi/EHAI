"""Fixed, bounded Tool runtime for the P2 Built-in Agent."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast

from ehai import ID, JsonValue, json_dumps, new_id, normalize_id
from ehai.application.builtin_agent import (
    CancellationToken,
    RecoverableToolError,
    ToolDefinition,
    ToolExecutor,
    ToolHandler,
    ToolSet,
)
from ehai.application.builtin_runtime import ToolRegistry
from ehai.application.ports import ArtifactStore
from ehai.application.session_mailbox import SessionMailboxToolProvider
from ehai.infrastructure.codex_transport import redact_codex_bytes
from ehai.infrastructure.mcp_tools import MCPToolProvider
from ehai.infrastructure.skill_loader import SkillToolProvider
from ehai.infrastructure.web_tools import WebToolProvider

_MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
_MAX_SEARCH_MATCHES = 100
_MAX_LIST_ENTRIES = 1_000
_MAX_SEARCH_ENTRIES = 10_000
_MAX_SEARCH_FILES = 2_000
_MAX_SEARCH_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_SEARCH_LINE_BYTES = 4 * 1024
_SEARCH_YIELD_INTERVAL = 32
_STREAM_CHUNK_BYTES = 8192
_COMMAND_CANCEL_GRACE_SECONDS = 0.5
_WINDOWS_HELPER_TIMEOUT_SECONDS = 5.0
_GIT_READ_COMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "cat-file", "describe"}
)
_GIT_LOCAL_WRITE_COMMANDS = frozenset({"add", "commit", "rm", "mv", "tag"})
_GIT_REMOTE_WRITE_COMMANDS = frozenset({"push", "fetch", "pull", "clone", "submodule"})
_GIT_DANGEROUS_COMMANDS = frozenset(
    {"reset", "clean", "checkout", "switch", "merge", "rebase", "cherry-pick", "worktree"}
)
_COMMAND_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
)
_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?i)(?:AUTH|BEARER|COOKIE|CREDENTIAL|KEY|PASSWORD|SECRET|TOKEN)"
)
SubmitCandidateValidator = Callable[[Mapping[str, JsonValue]], None]


class _CommandOutputBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.consumed = 0
        self.exceeded = asyncio.Event()

    def capture(self, chunk: bytes) -> bytes:
        remaining = max(self.limit - self.consumed, 0)
        self.consumed += len(chunk)
        if self.consumed > self.limit:
            self.exceeded.set()
        return chunk[:remaining]


class BuiltinToolRuntime:
    """Own the seven fixed P2 Tools for one bounded Workspace."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        workspace: Path,
        allowed_commands: tuple[tuple[str, ...], ...] = (),
        command_timeout_seconds: float = 120.0,
        allow_workspace_write: bool = True,
        available_shells: tuple[str, ...] = (),
        git_permissions: frozenset[str] = frozenset(),
        web_provider: WebToolProvider | None = None,
        mcp_providers: tuple[MCPToolProvider, ...] = (),
        skill_provider: SkillToolProvider | None = None,
        session_provider: SessionMailboxToolProvider | None = None,
        submit_candidate_validator: SubmitCandidateValidator | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.workspace = workspace.resolve()
        self._command_policies = _resolve_allowed_commands(allowed_commands, self.workspace)
        self.allowed_commands = frozenset(allowed_commands)
        if not isinstance(allow_workspace_write, bool):
            raise ValueError("allow_workspace_write must be a boolean")
        self.allow_workspace_write = allow_workspace_write
        self._shell_paths = {
            name: _resolve_trusted_executable(
                _command_name(name, "available shell"), self.workspace
            )
            for name in available_shells
        }
        allowed_git_permissions = {
            "git.read",
            "git.local_write",
            "git.remote_write",
            "git.dangerous",
        }
        if not set(git_permissions).issubset(allowed_git_permissions):
            raise ValueError("git_permissions contains an unknown permission")
        self.git_permissions = frozenset(git_permissions)
        self._git_path = (
            _resolve_trusted_executable("git", self.workspace) if self.git_permissions else None
        )
        self._shell_processes: dict[ID, tuple[asyncio.subprocess.Process, Mapping[str, str]]] = {}
        self._mcp_providers = tuple(mcp_providers)
        self._submit_candidate_validator = submit_candidate_validator
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self.command_timeout_seconds = command_timeout_seconds
        definitions: list[ToolDefinition] = [
            _definition("artifact_read", "Read one immutable Artifact", "artifact_id"),
            _definition("workspace_list", "List one Workspace directory", "path"),
            _definition(
                "workspace_search",
                "Search UTF-8 Workspace files",
                "path",
                "query",
            ),
            _definition("workspace_read", "Read one UTF-8 Workspace file", "path"),
        ]
        handlers: dict[str, ToolHandler] = {
            "artifact_read": self._artifact_read,
            "workspace_list": self._workspace_list,
            "workspace_search": self._workspace_search,
            "workspace_read": self._workspace_read,
        }
        if allow_workspace_write:
            definitions.extend(_workspace_write_definitions())
            handlers.update(
                {
                    "workspace_write": self._workspace_write,
                    "workspace_patch": self._workspace_patch,
                    "workspace_apply_patch": self._workspace_apply_patch,
                    "workspace_delete": self._workspace_delete,
                    "workspace_move": self._workspace_move,
                    "workspace_mkdir": self._workspace_mkdir,
                }
            )
        if allowed_commands:
            definitions.append(
                _array_definition(
                    "command",
                    "Run one exact host-approved argv command without a shell",
                    allowed_commands,
                )
            )
            handlers["command"] = self._command
        if self._shell_paths:
            definitions.extend(_shell_definitions(tuple(self._shell_paths)))
            handlers.update(
                {
                    "shell_exec": self._shell_exec,
                    "shell_write": self._shell_write,
                    "shell_terminate": self._shell_terminate,
                }
            )
        if self._git_path is not None:
            definitions.append(_git_definition())
            handlers["git"] = self._git
        for provider in tuple(
            item
            for item in (web_provider, *self._mcp_providers, skill_provider, session_provider)
            if item is not None
        ):
            for definition in provider.definitions:
                if definition.name in handlers:
                    raise ValueError(f"duplicate Tool provider name {definition.name!r}")
                definitions.append(definition)
                handlers[definition.name] = provider.handlers[definition.name]
        definitions.append(
            _definition(
                "submit_candidate",
                "Submit the final candidate Artifact content",
                "name",
                "media_type",
                "content",
                ends_turn=True,
                allow_empty_fields=frozenset({"content"}),
            )
        )
        handlers["submit_candidate"] = self._submit_candidate
        self.registry = ToolRegistry(tuple(definitions), handlers)
        self.tool_set = ToolSet(tuple(definitions))
        self.executor = ToolExecutor(self.tool_set, handlers)

    async def aclose(self) -> None:
        for process, environment in tuple(self._shell_processes.values()):
            await _terminate_process_tree(process, environment)
        self._shell_processes.clear()
        for provider in self._mcp_providers:
            await provider.aclose()
        await self.executor.aclose()

    async def _artifact_read(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        try:
            artifact_id = normalize_id(ID(_required_string(arguments, "artifact_id")))
        except ValueError as error:
            raise RecoverableToolError(
                "invalid_artifact_id", "artifact_id is not a valid identifier"
            ) from error
        artifact = self.artifact_store.get(artifact_id)
        if artifact is None:
            raise RecoverableToolError("not_found", "Artifact does not exist")
        content = self.artifact_store.read(artifact_id)
        if len(content) > _MAX_TOOL_OUTPUT_BYTES:
            raise RecoverableToolError("output_limit", "Artifact exceeds the Tool output limit")
        try:
            encoded = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoded = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        return {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "media_type": artifact.media_type,
            "encoding": encoding,
            "content": encoded,
        }

    async def _workspace_list(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        target = self._resolve(_required_string(arguments, "path"))
        if not target.is_dir():
            raise RecoverableToolError("not_directory", "Workspace path is not a directory")
        entries: list[dict[str, JsonValue]] = []
        truncated = False
        try:
            with os.scandir(target) as iterator:
                for index, child in enumerate(iterator, start=1):
                    if index > _MAX_LIST_ENTRIES:
                        truncated = True
                        break
                    if index % _SEARCH_YIELD_INTERVAL == 0:
                        await asyncio.sleep(0)
                        cancellation.raise_if_cancelled()
                    try:
                        entries.append(
                            {
                                "name": child.name,
                                "is_directory": child.is_dir(follow_symlinks=False),
                            }
                        )
                    except OSError:
                        continue
        except OSError as error:
            raise RecoverableToolError(
                "read_failed", "Workspace directory could not be listed"
            ) from error
        ordered_entries = sorted(entries, key=_workspace_entry_name)
        return {
            "entries": [cast(JsonValue, entry) for entry in ordered_entries],
            "truncated": truncated,
        }

    async def _workspace_search(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        root = self._resolve(_required_string(arguments, "path"))
        if not root.exists():
            raise RecoverableToolError("not_found", "Workspace path does not exist")
        query = _tool_non_empty_text(_required_string(arguments, "query"), "query")
        candidates, traversal_limit = await _bounded_workspace_files(
            root,
            self.workspace,
            cancellation,
        )
        matches: list[JsonValue] = []
        searched_bytes = 0
        truncated_reason = traversal_limit
        for file_index, path in enumerate(candidates, start=1):
            cancellation.raise_if_cancelled()
            if file_index % _SEARCH_YIELD_INTERVAL == 0:
                await asyncio.sleep(0)
                cancellation.raise_if_cancelled()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_TOOL_OUTPUT_BYTES:
                truncated_reason = truncated_reason or "file_size_limit"
                continue
            if searched_bytes + size > _MAX_SEARCH_TOTAL_BYTES:
                return _search_result(matches, "byte_limit")
            searched_bytes += size
            start_index = len(matches)
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        if line_number % _SEARCH_YIELD_INTERVAL == 0:
                            await asyncio.sleep(0)
                            cancellation.raise_if_cancelled()
                        if query not in line:
                            continue
                        text, text_truncated = _bounded_utf8_text(
                            line.rstrip("\r\n"),
                            _MAX_SEARCH_LINE_BYTES,
                        )
                        match: dict[str, JsonValue] = {
                            "path": path.relative_to(self.workspace).as_posix(),
                            "line": line_number,
                            "text": text,
                            "text_truncated": text_truncated,
                        }
                        if _json_size(matches) + _json_size([match]) > _MAX_TOOL_OUTPUT_BYTES:
                            return _search_result(matches, "output_limit")
                        matches.append(match)
                        if len(matches) >= _MAX_SEARCH_MATCHES:
                            return _search_result(matches, "match_limit")
            except UnicodeDecodeError:
                del matches[start_index:]
                continue
            except OSError:
                del matches[start_index:]
                continue
        return _search_result(matches, truncated_reason)

    async def _workspace_read(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        if not path.is_file():
            raise RecoverableToolError("not_found", "Workspace file does not exist")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise RecoverableToolError("read_failed", "Workspace file could not be read") from error
        if size > _MAX_TOOL_OUTPUT_BYTES:
            raise RecoverableToolError(
                "output_limit", "Workspace file exceeds the Tool output limit"
            )
        content = bytearray()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(
                    min(_STREAM_CHUNK_BYTES, _MAX_TOOL_OUTPUT_BYTES + 1 - len(content))
                ):
                    content.extend(chunk)
                    if len(content) > _MAX_TOOL_OUTPUT_BYTES:
                        raise RecoverableToolError(
                            "output_limit", "Workspace file exceeds the Tool output limit"
                        )
                    await asyncio.sleep(0)
                    cancellation.raise_if_cancelled()
        except RecoverableToolError:
            raise
        except OSError as error:
            raise RecoverableToolError("read_failed", "Workspace file could not be read") from error
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RecoverableToolError("not_utf8", "Workspace file is not UTF-8 text") from error
        return {"path": path.relative_to(self.workspace).as_posix(), "content": decoded}

    async def _workspace_patch(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        expected = _required_string(arguments, "expected")
        replacement = _required_string(arguments, "replacement", allow_empty=True)
        if not path.is_file():
            raise RecoverableToolError("not_found", "Workspace file does not exist")
        try:
            if path.stat().st_size > _MAX_TOOL_OUTPUT_BYTES:
                raise RecoverableToolError(
                    "output_limit", "Workspace file exceeds the Tool input limit"
                )
            content = path.read_text(encoding="utf-8")
        except RecoverableToolError:
            raise
        except (OSError, UnicodeDecodeError) as error:
            raise RecoverableToolError(
                "read_failed", "Workspace file could not be read as UTF-8 text"
            ) from error
        if content.count(expected) != 1:
            raise RecoverableToolError(
                "precondition_failed",
                "workspace_patch expected text must occur exactly once",
            )
        updated = content.replace(expected, replacement, 1)
        if len(updated.encode("utf-8")) > _MAX_TOOL_OUTPUT_BYTES:
            raise RecoverableToolError(
                "output_limit", "workspace_patch result exceeds the Tool output limit"
            )
        cancellation.raise_if_cancelled()
        path.write_text(updated, encoding="utf-8")
        return {"path": path.relative_to(self.workspace).as_posix(), "changed": True}

    async def _workspace_write(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        content = _required_string(arguments, "content", allow_empty=True)
        overwrite = _required_boolean(arguments, "overwrite")
        if path.exists() and (not path.is_file() or not overwrite):
            raise RecoverableToolError("already_exists", "Workspace path already exists")
        if not path.parent.is_dir():
            raise RecoverableToolError(
                "missing_parent", "Workspace parent directory does not exist"
            )
        if len(content.encode("utf-8")) > _MAX_TOOL_OUTPUT_BYTES:
            raise RecoverableToolError("output_limit", "Workspace content exceeds the Tool limit")
        path.write_text(content, encoding="utf-8")
        return {"path": path.relative_to(self.workspace).as_posix(), "changed": True}

    async def _workspace_apply_patch(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        patch = _required_string(arguments, "patch")
        encoded = patch.encode("utf-8")
        if len(encoded) > _MAX_TOOL_OUTPUT_BYTES:
            raise RecoverableToolError("output_limit", "Unified diff exceeds the Tool limit")
        git = _resolve_trusted_executable("git", self.workspace)
        check = await self._execute_argv(
            (str(git), "apply", "--check", "--whitespace=nowarn", "-"),
            cancellation,
            stdin=encoded,
        )
        if check["exit_code"] != 0:
            raise RecoverableToolError(
                "patch_rejected", "Unified diff context or paths are invalid"
            )
        applied = await self._execute_argv(
            (str(git), "apply", "--whitespace=nowarn", "-"),
            cancellation,
            stdin=encoded,
        )
        if applied["exit_code"] != 0:  # pragma: no cover - check/apply race
            raise RecoverableToolError("patch_failed", "Unified diff could not be applied")
        return {"changed": True}

    async def _workspace_delete(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        if not path.is_file():
            raise RecoverableToolError("not_found", "Workspace text file does not exist")
        path.unlink()
        return {"path": path.relative_to(self.workspace).as_posix(), "deleted": True}

    async def _workspace_move(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        source = self._resolve(_required_string(arguments, "source"))
        target = self._resolve(_required_string(arguments, "target"))
        if not source.is_file():
            raise RecoverableToolError("not_found", "Workspace source file does not exist")
        if target.exists():
            raise RecoverableToolError("already_exists", "Workspace target already exists")
        if not target.parent.is_dir():
            raise RecoverableToolError("missing_parent", "Workspace target parent does not exist")
        source.replace(target)
        return {
            "source": source.relative_to(self.workspace).as_posix(),
            "target": target.relative_to(self.workspace).as_posix(),
            "moved": True,
        }

    async def _workspace_mkdir(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        if path.exists():
            raise RecoverableToolError("already_exists", "Workspace path already exists")
        path.mkdir(parents=False)
        return {"path": path.relative_to(self.workspace).as_posix(), "created": True}

    async def _command(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        argv_value = arguments.get("argv")
        if (
            not isinstance(argv_value, list)
            or not argv_value
            or not all(isinstance(item, str) and item for item in argv_value)
        ):
            raise RecoverableToolError(
                "invalid_arguments", "command argv must be a non-empty string array"
            )
        argv = tuple(cast(str, item) for item in argv_value)
        try:
            executable = _command_name(argv[0], "command executable")
        except ValueError as error:
            raise RecoverableToolError(
                "invalid_arguments", "command executable must be an unqualified name"
            ) from error
        trusted_path = self._command_policies.get((_command_key(executable), *argv[1:]))
        if trusted_path is None:
            raise RecoverableToolError(
                "command_not_allowed", "command argv is not allowed by the configured policy"
            )
        return await self._execute_argv((str(trusted_path), *argv[1:]), cancellation)

    async def _shell_exec(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        shell = _required_string(arguments, "shell")
        command = _required_string(arguments, "command")
        wait = _required_boolean(arguments, "wait")
        executable = self._shell_paths.get(shell)
        if executable is None:
            raise RecoverableToolError("shell_not_allowed", "Shell is not enabled by the Endpoint")
        argv = _shell_argv(executable, command)
        if wait:
            return await self._execute_argv(argv, cancellation)
        environment = _command_environment((executable,))
        options = _process_options(self.workspace, environment, stdin=asyncio.subprocess.PIPE)
        process = await asyncio.create_subprocess_exec(*argv, **options)  # type: ignore[arg-type]
        process_id = new_id()
        self._shell_processes[process_id] = (process, environment)
        return {"process_id": process_id, "running": True}

    async def _shell_write(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        process_id = _required_id(arguments, "process_id")
        text = _required_string(arguments, "text", allow_empty=True)
        process_entry = self._shell_processes.get(process_id)
        if process_entry is None or process_entry[0].stdin is None:
            raise RecoverableToolError("process_not_found", "Shell process is not running")
        process_entry[0].stdin.write(text.encode("utf-8"))
        await process_entry[0].stdin.drain()
        return {"process_id": process_id, "written_bytes": len(text.encode("utf-8"))}

    async def _shell_terminate(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        process_id = _required_id(arguments, "process_id")
        process_entry = self._shell_processes.pop(process_id, None)
        if process_entry is None:
            raise RecoverableToolError("process_not_found", "Shell process is not running")
        process, environment = process_entry
        await _terminate_process_tree(process, environment)
        return {"process_id": process_id, "terminated": True}

    async def _git(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        argv = _required_argv(arguments, "argv")
        category = _git_permission(argv)
        if category not in self.git_permissions:
            raise RecoverableToolError(
                "git_not_allowed", f"Git operation requires explicit {category} permission"
            )
        assert self._git_path is not None
        result = await self._execute_argv((str(self._git_path), *argv), cancellation)
        result["permission"] = category
        return result

    async def _execute_argv(
        self,
        argv: tuple[str, ...],
        cancellation: CancellationToken,
        *,
        stdin: bytes | None = None,
    ) -> dict[str, JsonValue]:
        environment = _command_environment((Path(argv[0]),))
        secret_values = _sensitive_environment_values()
        options = _process_options(
            self.workspace,
            environment,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            **options,  # type: ignore[arg-type]
        )
        if stdin is not None:
            assert process.stdin is not None
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
        output_budget = _CommandOutputBudget(_MAX_TOOL_OUTPUT_BYTES)
        capture_task = asyncio.create_task(
            _capture_command_output(process, output_budget),
            name="ehai-builtin-command-output",
        )
        cancellation_task = asyncio.create_task(
            cancellation.wait_cancelled(),
            name="ehai-builtin-command-cancellation",
        )
        output_limit_task = asyncio.create_task(
            output_budget.exceeded.wait(),
            name="ehai-builtin-command-output-limit",
        )
        try:
            done, _ = await asyncio.wait(
                {capture_task, cancellation_task, output_limit_task},
                timeout=self.command_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                await asyncio.shield(_terminate_process_tree(process, environment))
                await asyncio.shield(_settle_capture(capture_task))
                cancellation.raise_if_cancelled()
            if output_limit_task in done:
                await asyncio.shield(_terminate_process_tree(process, environment))
                await asyncio.shield(_settle_capture(capture_task))
                raise ValueError("command output exceeds the Tool output limit")
            if capture_task not in done:
                await asyncio.shield(_terminate_process_tree(process, environment))
                await asyncio.shield(_settle_capture(capture_task))
                raise ValueError("command exceeded its Tool timeout")
            return_code, stdout, stderr = capture_task.result()
        except asyncio.CancelledError:
            await asyncio.shield(_terminate_process_tree(process, environment))
            await asyncio.shield(_settle_capture(capture_task))
            raise
        finally:
            for task in (capture_task, cancellation_task, output_limit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                capture_task,
                cancellation_task,
                output_limit_task,
                return_exceptions=True,
            )
        return {
            "exit_code": return_code,
            "stdout": redact_codex_bytes(stdout, secret_values).decode(errors="replace"),
            "stderr": redact_codex_bytes(stderr, secret_values).decode(errors="replace"),
        }

    async def _submit_candidate(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        result: dict[str, JsonValue] = {
            "name": _tool_non_empty_text(_required_string(arguments, "name"), "name"),
            "media_type": _tool_non_empty_text(
                _required_string(arguments, "media_type"),
                "media_type",
            ),
            "content": _required_string(arguments, "content", allow_empty=True),
        }
        if self._submit_candidate_validator is not None:
            self._submit_candidate_validator(result)
        return result

    def _resolve(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise RecoverableToolError("invalid_path", "Workspace Tool paths must be relative")
        resolved = (self.workspace / candidate).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise RecoverableToolError("invalid_path", "Workspace Tool path escapes the Workspace")
        return resolved


async def _bounded_workspace_files(
    root: Path,
    workspace: Path,
    cancellation: CancellationToken,
) -> tuple[tuple[Path, ...], str | None]:
    if root.is_file():
        return (root,), None
    if not root.is_dir():
        return (), None
    files: list[Path] = []
    directories = [root]
    visited = 0
    while directories:
        directory = directories.pop()
        child_directories: list[Path] = []
        child_files: list[Path] = []
        try:
            with os.scandir(directory) as iterator:
                for child in iterator:
                    visited += 1
                    if visited > _MAX_SEARCH_ENTRIES:
                        return tuple(files), "traversal_limit"
                    if visited % _SEARCH_YIELD_INTERVAL == 0:
                        await asyncio.sleep(0)
                        cancellation.raise_if_cancelled()
                    try:
                        if child.is_symlink():
                            continue
                        path = Path(child.path).resolve(strict=True)
                        if not path.is_relative_to(workspace):
                            continue
                        if child.is_dir(follow_symlinks=False):
                            child_directories.append(path)
                        elif child.is_file(follow_symlinks=False):
                            child_files.append(path)
                    except OSError:
                        continue
        except OSError:
            continue
        for path in sorted(child_files, key=lambda item: item.name):
            files.append(path)
            if len(files) >= _MAX_SEARCH_FILES:
                return tuple(files), "file_limit"
        directories.extend(reversed(sorted(child_directories, key=lambda item: item.name)))
    return tuple(files), None


def _bounded_utf8_text(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _workspace_entry_name(entry: dict[str, JsonValue]) -> str:
    return cast(str, entry["name"])


def _json_size(value: JsonValue) -> int:
    return len(json_dumps(value).encode("utf-8"))


def _search_result(matches: list[JsonValue], reason: str | None) -> JsonValue:
    return {
        "matches": matches,
        "truncated": reason is not None,
        "truncation_reason": reason,
    }


def _definition(
    name: str,
    description: str,
    *required_fields: str,
    writes_workspace: bool = False,
    ends_turn: bool = False,
    allow_empty_fields: frozenset[str] = frozenset(),
) -> ToolDefinition:
    properties: dict[str, JsonValue] = {
        field: (
            {"type": "string"}
            if field in allow_empty_fields
            else {"type": "string", "minLength": 1}
        )
        for field in required_fields
    }
    return ToolDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": properties,
            "required": list(required_fields),
            "additionalProperties": False,
        },
        writes_workspace=writes_workspace,
        ends_turn=ends_turn,
    )


def _workspace_write_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            "workspace_write",
            "Create or overwrite one UTF-8 Workspace text file",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content", "overwrite"],
                "additionalProperties": False,
            },
            writes_workspace=True,
        ),
        _definition(
            "workspace_patch",
            "Replace one exact text occurrence in a Workspace file",
            "path",
            "expected",
            "replacement",
            writes_workspace=True,
            allow_empty_fields=frozenset({"replacement"}),
        ),
        _definition(
            "workspace_apply_patch",
            "Apply one bounded unified text diff inside the Workspace",
            "patch",
            writes_workspace=True,
        ),
        _definition(
            "workspace_delete",
            "Delete one Workspace text file",
            "path",
            writes_workspace=True,
        ),
        _definition(
            "workspace_move",
            "Move one Workspace text file without overwriting",
            "source",
            "target",
            writes_workspace=True,
        ),
        _definition(
            "workspace_mkdir",
            "Create one Workspace directory whose parent exists",
            "path",
            writes_workspace=True,
        ),
    )


def _shell_definitions(shells: tuple[str, ...]) -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            "shell_exec",
            "Start a command in one Endpoint-enabled shell",
            {
                "type": "object",
                "properties": {
                    "shell": {"type": "string", "enum": list(shells)},
                    "command": {"type": "string", "minLength": 1},
                    "wait": {"type": "boolean"},
                },
                "required": ["shell", "command", "wait"],
                "additionalProperties": False,
            },
            writes_workspace=True,
        ),
        _definition(
            "shell_write",
            "Write UTF-8 text to a running shell process",
            "process_id",
            "text",
            writes_workspace=True,
            allow_empty_fields=frozenset({"text"}),
        ),
        _definition(
            "shell_terminate",
            "Terminate a running shell process tree",
            "process_id",
            writes_workspace=True,
        ),
    )


def _git_definition() -> ToolDefinition:
    return ToolDefinition(
        "git",
        "Run one structured Git argv operation subject to its permission category",
        {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                }
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        writes_workspace=True,
    )


def _array_definition(
    name: str,
    description: str,
    allowed_argv: tuple[tuple[str, ...], ...],
) -> ToolDefinition:
    allowed_values = [cast(JsonValue, list(argv)) for argv in allowed_argv]
    return ToolDefinition(
        name,
        f"{description}. Allowed exact argv values: {json_dumps(allowed_values)}",
        {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                }
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        writes_workspace=True,
    )


def _required_string(
    arguments: Mapping[str, JsonValue],
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise RecoverableToolError("invalid_arguments", f"{name} must be text")
    return value


def _required_boolean(arguments: Mapping[str, JsonValue], name: str) -> bool:
    value = arguments.get(name)
    if not isinstance(value, bool):
        raise RecoverableToolError("invalid_arguments", f"{name} must be a boolean")
    return value


def _required_id(arguments: Mapping[str, JsonValue], name: str) -> ID:
    try:
        return normalize_id(ID(_required_string(arguments, name)))
    except ValueError as error:
        raise RecoverableToolError("invalid_arguments", f"{name} must be an identifier") from error


def _required_argv(arguments: Mapping[str, JsonValue], name: str) -> tuple[str, ...]:
    value = arguments.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise RecoverableToolError("invalid_arguments", f"{name} must be a non-empty string array")
    return tuple(cast(str, item) for item in value)


def _git_permission(argv: tuple[str, ...]) -> str:
    command = argv[0].lower()
    if command in _GIT_READ_COMMANDS:
        return "git.read"
    if command == "branch" and all(
        not item.startswith(("-d", "-D", "-m", "-M", "-c", "-C")) for item in argv[1:]
    ):
        return "git.read"
    if command in _GIT_LOCAL_WRITE_COMMANDS or command == "branch":
        return "git.local_write"
    if command in _GIT_REMOTE_WRITE_COMMANDS:
        return "git.remote_write"
    if command in _GIT_DANGEROUS_COMMANDS:
        return "git.dangerous"
    return "git.dangerous"


def _shell_argv(executable: Path, command: str) -> tuple[str, ...]:
    name = executable.stem.lower()
    if name in {"powershell", "pwsh"}:
        return (str(executable), "-NoProfile", "-NonInteractive", "-Command", command)
    if name == "cmd":
        return (str(executable), "/D", "/S", "/C", command)
    if name == "bash":
        return (str(executable), "--noprofile", "--norc", "-c", command)
    raise RecoverableToolError("shell_not_supported", "Endpoint shell is not supported")


def _process_options(
    cwd: Path,
    environment: Mapping[str, str],
    *,
    stdin: int,
) -> dict[str, object]:
    options: dict[str, object] = {
        "cwd": cwd,
        "env": dict(environment),
        "stdin": stdin,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return options


def _tool_non_empty_text(value: str, name: str) -> str:
    if not value.strip():
        raise RecoverableToolError("invalid_arguments", f"{name} must not be blank")
    return value.strip()


def _non_empty_text(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()


def _resolve_allowed_commands(
    commands: tuple[tuple[str, ...], ...],
    workspace: Path,
) -> dict[tuple[str, ...], Path]:
    resolved: dict[tuple[str, ...], Path] = {}
    for configured_argv in commands:
        if not configured_argv or not all(
            isinstance(item, str) and item for item in configured_argv
        ):
            raise ValueError("allowed command argv must be a non-empty string tuple")
        name = _command_name(configured_argv[0], "allowed command")
        executable = _resolve_trusted_executable(name, workspace)
        aliases = {_command_key(name), _command_key(executable.name)}
        if os.name == "nt" and executable.suffix.lower() in _windows_executable_extensions():
            aliases.add(_command_key(executable.stem))
        for alias in aliases:
            policy = (alias, *configured_argv[1:])
            existing = resolved.get(policy)
            if existing is not None and existing != executable:
                raise ValueError(f"allowed command policy {configured_argv!r} is ambiguous")
            resolved[policy] = executable
    return resolved


async def _capture_command_output(
    process: asyncio.subprocess.Process,
    budget: _CommandOutputBudget,
) -> tuple[int, bytes, bytes]:
    tasks = (
        asyncio.create_task(
            _read_command_stream(process.stdout, budget),
            name="ehai-builtin-command-stdout",
        ),
        asyncio.create_task(
            _read_command_stream(process.stderr, budget),
            name="ehai-builtin-command-stderr",
        ),
        asyncio.create_task(process.wait(), name="ehai-builtin-command-wait"),
    )
    try:
        stdout, stderr, return_code = await asyncio.gather(*tasks)
        return return_code, stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _read_command_stream(
    reader: asyncio.StreamReader | None,
    budget: _CommandOutputBudget,
) -> bytes:
    if reader is None:
        return b""
    chunks: list[bytes] = []
    while chunk := await reader.read(_STREAM_CHUNK_BYTES):
        captured = budget.capture(chunk)
        if captured:
            chunks.append(captured)
    return b"".join(chunks)


async def _settle_capture(task: asyncio.Task[tuple[int, bytes, bytes]]) -> None:
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_WINDOWS_HELPER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    environment: Mapping[str, str],
) -> None:
    pid = getattr(process, "pid", None)
    if os.name == "nt" and isinstance(pid, int) and pid > 0 and pid != os.getpid():
        system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
        taskkill = (
            str(Path(system_root) / "System32" / "taskkill.exe") if system_root else "taskkill.exe"
        )
        try:
            helper = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(pid),
                "/T",
                "/F",
                env=dict(environment),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(helper.wait(), timeout=_WINDOWS_HELPER_TIMEOUT_SECONDS)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    helper.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(helper.wait(), timeout=_WINDOWS_HELPER_TIMEOUT_SECONDS)
        except OSError:
            pass
    elif os.name != "nt" and isinstance(pid, int) and pid > 0 and pid != os.getpid():
        kill_process_group = os.killpg  # type: ignore[attr-defined]
        with suppress(OSError):
            kill_process_group(pid, signal.SIGTERM)
        await asyncio.sleep(_COMMAND_CANCEL_GRACE_SECONDS)
        with suppress(OSError):
            kill_process_group(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_COMMAND_CANCEL_GRACE_SECONDS)


def _command_environment(executables: tuple[Path, ...]) -> dict[str, str]:
    source = {key.upper(): value for key, value in os.environ.items()}
    environment = {
        name: source[name]
        for name in _COMMAND_ENVIRONMENT_NAMES
        if name in source and _SENSITIVE_ENVIRONMENT_NAME.search(name) is None
    }
    path_entries = list(dict.fromkeys(str(path.parent) for path in executables))
    system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    if system_root:
        path_entries.append(str(Path(system_root) / "System32"))
    environment["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    return environment


def _sensitive_environment_values() -> tuple[str, ...]:
    values = {
        value
        for key, value in os.environ.items()
        if len(value) >= 4 and _SENSITIVE_ENVIRONMENT_NAME.search(key) is not None
    }
    return tuple(sorted(values, key=len, reverse=True))


def _resolve_trusted_executable(name: str, workspace: Path) -> Path:
    candidates = _executable_candidates(name)
    for value in os.environ.get("PATH", "").split(os.pathsep):
        path_value = value.strip().strip('"')
        if not path_value:
            continue
        configured_directory = Path(path_value).expanduser()
        if not configured_directory.is_absolute():
            continue
        try:
            directory = configured_directory.resolve(strict=True)
        except OSError:
            continue
        if directory == workspace or directory.is_relative_to(workspace):
            continue
        for candidate_name in candidates:
            try:
                candidate = (directory / candidate_name).resolve(strict=True)
            except OSError:
                continue
            if candidate == workspace or candidate.is_relative_to(workspace):
                continue
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    raise ValueError(f"allowed command {name!r} was not found on the trusted PATH")


def _executable_candidates(name: str) -> tuple[str, ...]:
    if os.name != "nt":
        return (name,)
    extensions = _windows_executable_extensions()
    suffix = Path(name).suffix.lower()
    if suffix:
        if suffix not in extensions:
            raise ValueError(f"allowed command {name!r} does not use a PATHEXT extension")
        return (name,)
    return tuple(name + extension for extension in extensions)


def _windows_executable_extensions() -> tuple[str, ...]:
    raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    extensions = tuple(
        dict.fromkeys(
            value.lower() if value.startswith(".") else f".{value.lower()}"
            for item in raw.split(os.pathsep)
            if (value := item.strip())
        )
    )
    return extensions or (".com", ".exe", ".bat", ".cmd")


def _command_name(value: str, label: str) -> str:
    name = _non_empty_text(value, label)
    if Path(name).is_absolute() or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"{label} must be an unqualified executable name")
    return name


def _command_key(value: str) -> str:
    return os.path.normcase(value)
