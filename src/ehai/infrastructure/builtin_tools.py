"""Fixed, bounded Tool runtime for the P2 Built-in Agent."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from ehai import ID, JsonValue, normalize_id
from ehai.application.builtin_agent import (
    CancellationToken,
    ToolDefinition,
    ToolExecutor,
    ToolHandler,
    ToolSet,
)
from ehai.application.ports import ArtifactStore

_MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
_MAX_SEARCH_MATCHES = 100


class BuiltinToolRuntime:
    """Own the seven fixed P2 Tools for one bounded Workspace."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        workspace: Path,
        allowed_commands: tuple[str, ...],
        command_timeout_seconds: float = 120.0,
    ) -> None:
        self.artifact_store = artifact_store
        self.workspace = workspace.resolve()
        self.allowed_commands = frozenset(
            _non_empty_text(item, "allowed command").lower() for item in allowed_commands
        )
        if not self.allowed_commands:
            raise ValueError("BuiltinToolRuntime requires at least one allowed command")
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self.command_timeout_seconds = command_timeout_seconds
        self.tool_set = ToolSet(
            (
                _definition("artifact_read", "Read one immutable Artifact", "artifact_id"),
                _definition("workspace_list", "List one Workspace directory", "path"),
                _definition(
                    "workspace_search",
                    "Search UTF-8 Workspace files",
                    "path",
                    "query",
                ),
                _definition("workspace_read", "Read one UTF-8 Workspace file", "path"),
                _definition(
                    "workspace_patch",
                    "Replace one exact text occurrence in a Workspace file",
                    "path",
                    "expected",
                    "replacement",
                    writes_workspace=True,
                ),
                _array_definition(
                    "command",
                    "Run an allowed argv command without a shell",
                ),
                _definition(
                    "submit_candidate",
                    "Submit the final candidate Artifact content",
                    "name",
                    "media_type",
                    "content",
                    ends_turn=True,
                ),
            )
        )
        handlers: Mapping[str, ToolHandler] = {
            "artifact_read": self._artifact_read,
            "workspace_list": self._workspace_list,
            "workspace_search": self._workspace_search,
            "workspace_read": self._workspace_read,
            "workspace_patch": self._workspace_patch,
            "command": self._command,
            "submit_candidate": self._submit_candidate,
        }
        self.executor = ToolExecutor(self.tool_set, handlers)

    async def _artifact_read(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        artifact_id = normalize_id(ID(_required_string(arguments, "artifact_id")))
        artifact = self.artifact_store.get(artifact_id)
        if artifact is None:
            raise LookupError(f"Artifact {artifact_id} does not exist")
        content = self.artifact_store.read(artifact_id)
        if len(content) > _MAX_TOOL_OUTPUT_BYTES:
            raise ValueError(f"Artifact {artifact_id} exceeds the Tool output limit")
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
            raise ValueError(f"Workspace path {target.name!r} is not a directory")
        return {
            "entries": [
                {"name": child.name, "is_directory": child.is_dir()}
                for child in sorted(target.iterdir(), key=lambda item: item.name)
            ]
        }

    async def _workspace_search(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        root = self._resolve(_required_string(arguments, "path"))
        query = _non_empty_text(_required_string(arguments, "query"), "query")
        candidates = (root,) if root.is_file() else root.rglob("*")
        matches: list[JsonValue] = []
        for path in candidates:
            cancellation.raise_if_cancelled()
            if not path.is_file() or path.stat().st_size > _MAX_TOOL_OUTPUT_BYTES:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(
                        {
                            "path": path.relative_to(self.workspace).as_posix(),
                            "line": line_number,
                            "text": line,
                        }
                    )
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    async def _workspace_read(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        content = path.read_bytes()
        if len(content) > _MAX_TOOL_OUTPUT_BYTES:
            raise ValueError(f"Workspace file {path.name!r} exceeds the Tool output limit")
        return {"path": path.relative_to(self.workspace).as_posix(), "content": content.decode()}

    async def _workspace_patch(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        path = self._resolve(_required_string(arguments, "path"))
        expected = _required_string(arguments, "expected")
        replacement = _required_string(arguments, "replacement", allow_empty=True)
        content = path.read_text(encoding="utf-8")
        if content.count(expected) != 1:
            raise ValueError("workspace_patch expected text must occur exactly once")
        updated = content.replace(expected, replacement, 1)
        path.write_text(updated, encoding="utf-8")
        return {"path": path.relative_to(self.workspace).as_posix(), "changed": True}

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
            raise ValueError("command argv must be a non-empty string array")
        argv = tuple(cast(str, item) for item in argv_value)
        executable = Path(argv[0]).name.lower()
        if executable not in self.allowed_commands:
            raise ValueError(f"command executable {executable!r} is not allowed")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("command exceeded its Tool timeout") from None
        if len(stdout) + len(stderr) > _MAX_TOOL_OUTPUT_BYTES:
            raise ValueError("command output exceeds the Tool output limit")
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }

    async def _submit_candidate(
        self,
        arguments: dict[str, JsonValue],
        cancellation: CancellationToken,
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        return {
            "name": _non_empty_text(_required_string(arguments, "name"), "name"),
            "media_type": _non_empty_text(
                _required_string(arguments, "media_type"),
                "media_type",
            ),
            "content": _required_string(arguments, "content", allow_empty=True),
        }

    def _resolve(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("Workspace Tool paths must be relative")
        resolved = (self.workspace / candidate).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("Workspace Tool path escapes the Workspace")
        return resolved


def _definition(
    name: str,
    description: str,
    *required_fields: str,
    writes_workspace: bool = False,
    ends_turn: bool = False,
) -> ToolDefinition:
    properties: dict[str, JsonValue] = {field: {"type": "string"} for field in required_fields}
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


def _array_definition(name: str, description: str) -> ToolDefinition:
    return ToolDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
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
        raise ValueError(f"{name} must be text")
    return value


def _non_empty_text(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value.strip()
