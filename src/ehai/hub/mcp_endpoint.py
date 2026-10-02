"""Per-session MCP endpoint through which an MCP-capable harness calls EHAI tools.

Each Hub session gets its own bearer token. The endpoint exposes only that session's
tools, and every call becomes the same ``tool_call`` event the core already serves; the
result the core sends back is returned to the harness. Nothing is executed here.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Protocol

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from ehai import JsonValue, json_dumps
from ehai.hub.protocol import ToolResult, ToolSpec

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class SessionTools(Protocol):
    @property
    def tools(self) -> list[ToolSpec]: ...

    async def call_tool_via_mcp(self, name: str, arguments: dict[str, JsonValue]) -> ToolResult: ...


def _bearer(scope: Scope) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == b"authorization":
            text = bytes(value).decode("latin-1")
            return text[7:] if text.startswith("Bearer ") else ""
    return ""


class SessionToolEndpoint:
    """One streamable-HTTP MCP server; the bearer token selects the Hub session."""

    def __init__(self, resolve: Callable[[str], SessionTools | None]) -> None:
        self._resolve = resolve
        server: Server[Any, Any] = Server("ehai-hub")

        def current() -> SessionTools:
            request = server.request_context.request
            token = "" if request is None else _bearer(request.scope)
            session = self._resolve(token)
            if session is None:
                raise PermissionError("EHAI tool session is unknown or closed")
            return session

        @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=tool.name, description=tool.description, inputSchema=tool.parameters
                )
                for tool in current().tools
            ]

        # The core validates arguments so that rejected calls stay in its trace.
        @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            session = current()
            if name not in {tool.name for tool in session.tools}:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="Tool is not in this session")],
                    isError=True,
                )
            result = await session.call_tool_via_mcp(name, dict(arguments or {}))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json_dumps(result.result))],
                isError=result.is_error,
            )

        self.manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = _bearer(scope)
        if scope.get("type") == "http" and (not token or self._resolve(token) is None):
            body = b'{"code":"unauthorized","message":"EHAI tool session is unknown or closed"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.manager.handle_request(scope, receive, send)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
