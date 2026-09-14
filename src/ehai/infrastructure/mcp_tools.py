"""Controlled MCP stdio discovery and ToolRegistry adaptation."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol, cast

from ehai import JsonValue, json_dumps, json_loads
from ehai.application.agent_contracts import (
    CancellationToken,
    RecoverableToolError,
    ToolDefinition,
    ToolHandler,
)

_MAX_MCP_RESULT_BYTES = 1024 * 1024
_MCP_NAME = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass(frozen=True, slots=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]


class MCPClient(Protocol):
    def list_tools(self) -> tuple[MCPToolSpec, ...]: ...

    def call_tool(self, name: str, arguments: Mapping[str, JsonValue]) -> JsonValue: ...

    def close(self) -> None: ...


class StdioMCPClient:
    """Small MCP JSON-RPC stdio client for a configured local server command."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        command = tuple(argv)
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("MCP server argv must be a non-empty string sequence")
        if timeout_seconds <= 0:
            raise ValueError("MCP timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._next_id = 0
        self._lock = threading.Lock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_minimal_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self._process.stdout is not None
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ehai", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})

    def list_tools(self) -> tuple[MCPToolSpec, ...]:
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("MCP tools/list result has no tools array")
        parsed: list[MCPToolSpec] = []
        for item in tools:
            if not isinstance(item, dict):
                raise RuntimeError("MCP tool entry is not an object")
            name = item.get("name")
            description = item.get("description", "")
            schema = item.get("inputSchema")
            if not isinstance(name, str) or not name or not isinstance(schema, dict):
                raise RuntimeError("MCP tool schema is invalid")
            parsed.append(
                MCPToolSpec(name, str(description), cast(Mapping[str, JsonValue], schema))
            )
        return tuple(parsed)

    def call_tool(self, name: str, arguments: Mapping[str, JsonValue]) -> JsonValue:
        return self._request("tools/call", {"name": name, "arguments": dict(arguments)})

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)

    def _request(self, method: str, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                try:
                    line = self._lines.get(timeout=self._timeout_seconds)
                except queue.Empty as error:
                    raise TimeoutError(f"MCP {method} timed out") from error
                if line is None:
                    raise RuntimeError(f"MCP server exited during {method}")
                document = json.loads(line)
                if not isinstance(document, dict) or document.get("id") != request_id:
                    continue
                if "error" in document:
                    raise RuntimeError(f"MCP {method} returned an error")
                result = document.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(f"MCP {method} result is not an object")
                return cast(dict[str, JsonValue], result)

    def _notify(self, method: str, params: Mapping[str, JsonValue]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, document: Mapping[str, object]) -> None:
        if self._process.stdin is None or self._process.poll() is not None:
            raise RuntimeError("MCP server is not running")
        self._process.stdin.write(json.dumps(document, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def _read_lines(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)


class MCPToolProvider:
    """Freeze approved tools discovered from one MCP server."""

    def __init__(
        self,
        server: str,
        client: MCPClient,
        *,
        approved_tools: frozenset[str],
    ) -> None:
        if not server.strip():
            raise ValueError("MCP server name must not be blank")
        self.server = server.strip()
        self._client = client
        definitions: list[ToolDefinition] = []
        handlers: dict[str, ToolHandler] = {}
        for spec in client.list_tools():
            if spec.name not in approved_tools:
                continue
            exposed_name = f"mcp__{_safe_name(self.server)}__{_safe_name(spec.name)}"
            definitions.append(
                ToolDefinition(exposed_name, spec.description or spec.name, spec.input_schema)
            )

            async def invoke(
                arguments: dict[str, JsonValue],
                cancellation: CancellationToken,
                *,
                tool_name: str = spec.name,
            ) -> JsonValue:
                cancellation.raise_if_cancelled()
                started = monotonic()
                try:
                    result = await asyncio.to_thread(self._client.call_tool, tool_name, arguments)
                except Exception as error:
                    raise RecoverableToolError("mcp_error", "MCP tool call failed") from error
                cancellation.raise_if_cancelled()
                serialized = json_dumps(result)
                if len(serialized.encode("utf-8")) > _MAX_MCP_RESULT_BYTES:
                    raise RecoverableToolError("output_limit", "MCP result exceeds the Tool limit")
                return {
                    "server": self.server,
                    "tool": tool_name,
                    "result": json_loads(serialized),
                    "duration_ms": int((monotonic() - started) * 1000),
                    "approved": True,
                    "recoverable": True,
                }

            handlers[exposed_name] = invoke
        if not definitions:
            raise ValueError("MCP provider has no approved discovered tools")
        self.definitions = tuple(definitions)
        self.handlers: Mapping[str, ToolHandler] = handlers

    async def aclose(self) -> None:
        await asyncio.to_thread(self._client.close)


def _safe_name(value: str) -> str:
    normalized = _MCP_NAME.sub("_", value.strip())
    if not normalized:
        raise ValueError("MCP name is invalid")
    return normalized[:64]


def _minimal_environment() -> dict[str, str]:
    names = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")
    return {name: os.environ[name] for name in names if name in os.environ}
