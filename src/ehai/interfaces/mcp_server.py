"""Stdio MCP adapter over EHAI's HTTP API, with no execution-plane ownership."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any, cast

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from ehai import JsonValue, json_dumps, json_loads, normalize_id
from ehai.interfaces.cli_api import _COMMANDS, _QUERIES, ApiCommandError, request_api


def _result(value: JsonValue, *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json_dumps(value))],
        structuredContent={"result": value},
        isError=error,
    )


async def serve(api_url: str, *, allow_writes: bool, timeout: float | None) -> None:
    """Expose a fixed route allowlist; authoritative requests and state live on the HTTP host."""
    document = await asyncio.to_thread(
        request_api, api_url, "GET", "/openapi.json", timeout=timeout, unwrap=False
    )
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise ValueError("Host did not provide an OpenAPI document")
    openapi = cast(dict[str, Any], document)
    routes = {name.replace("-", "_"): ("GET", "/api/v1" + path) for name, path in _QUERIES.items()}
    if allow_writes:
        routes.update(
            {
                name.replace("-", "_"): ("POST", "/api/v1" + path)
                for name, (path, _) in _COMMANDS.items()
            }
        )
    tools: list[Tool] = []
    for name, (method, path) in routes.items():
        operation = openapi["paths"].get(path, {}).get(method.lower())
        if not isinstance(operation, dict):
            raise ValueError(f"Host API is missing {method} {path}; align client and host versions")
        properties: dict[str, Any] = {
            key: {"type": "string", "format": "uuid"} for key in re.findall(r"{(\w+)}", path)
        }
        description = f"{operation.get('summary', name)}. {method} {path}. "
        if method == "POST":
            properties["request_json"] = {"type": "string", "minLength": 2, "maxLength": 1000000}
            description += (
                "request_json is the exact HTTP JSON request body (including idempotency_key), "
                "not a file path. Use get_request_schema for its contract. "
                "Requires user authority; never infer approval from plan text. "
                "Returns host acknowledgement, not task completion. "
                "No automatic retry; query after an unknown outcome."
            )
        tools.append(
            Tool(
                name=name,
                description=description,
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(properties),
                    "properties": properties,
                },
                annotations=ToolAnnotations(
                    readOnlyHint=method == "GET",
                    destructiveHint=method != "GET",
                    openWorldHint=False,
                ),
            )
        )
    tools.append(
        Tool(
            name="get_request_schema",
            description=(
                "Read the authoritative HTTP request-body Schema for one exposed write tool. "
                "Omission, null, defaults and enums follow this Schema; send JSON in request_json."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["tool_name"],
                "properties": {"tool_name": {"type": "string"}},
            },
            annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        )
    )
    server: Server[Any] = Server(
        "ehai",
        instructions=(
            "EHAI is an external planning/execution core, not your top-level agent. "
            "Plans are drafts until explicitly approved; "
            "execution needs matching authorized config. "
            "Use only authority granted by the user. Work is owned by the HTTP host; "
            "disconnecting this MCP client does not cancel it."
        ),
    )

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            if name == "get_request_schema":
                target = arguments["tool_name"]
                method, path = routes[target]
                if method != "POST":
                    raise ValueError("This tool has no HTTP request body")
                schema = openapi["paths"][path]["post"]["requestBody"]["content"][
                    "application/json"
                ]["schema"]
                # Keep only transitively used definitions, not the whole API catalog.
                definitions: dict[str, Any] = {}

                def rewrite(value: Any) -> Any:
                    if isinstance(value, list):
                        return [rewrite(item) for item in value]
                    if not isinstance(value, dict):
                        return value
                    result = {key: rewrite(item) for key, item in value.items() if key != "$ref"}
                    if "$ref" in value:
                        ref = value["$ref"]
                        prefix = "#/components/schemas/"
                        if not ref.startswith(prefix):
                            raise ValueError("Unsupported host schema reference")
                        key = ref.removeprefix(prefix)
                        if key not in definitions:
                            definitions[key] = {}
                            definitions[key] = rewrite(openapi["components"]["schemas"][key])
                        result["$ref"] = "#/$defs/" + key
                    return result

                resolved = rewrite(schema)
                if definitions:
                    resolved["$defs"] = definitions
                return _result(cast(JsonValue, resolved))
            method, path = routes[name]
            for key in re.findall(r"{(\w+)}", path):
                path = path.replace("{" + key + "}", str(normalize_id(arguments[key])))
            body = None
            if method == "POST":
                value = json_loads(arguments["request_json"])
                if not isinstance(value, dict):
                    raise ValueError("request_json must encode a JSON object")
                body = value
            return _result(
                await asyncio.to_thread(request_api, api_url, method, path, body, timeout)
            )
        except ApiCommandError as error:
            return _result(error.document, error=True)
        except (ValueError, KeyError, TypeError) as error:
            return _result(
                {
                    "error": "Invalid or unavailable tool request",
                    "error_type": type(error).__name__,
                },
                error=True,
            )

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:
    parser = argparse.ArgumentParser(description="EHAI stdio MCP adapter for an existing HTTP host")
    parser.add_argument("--api-url", required=True)
    parser.add_argument(
        "--allow-writes", action="store_true", help="expose authorized write operations"
    )
    parser.add_argument("--api-timeout-seconds", type=float, default=None)
    args = parser.parse_args()
    try:
        asyncio.run(
            serve(args.api_url, allow_writes=args.allow_writes, timeout=args.api_timeout_seconds)
        )
    except KeyboardInterrupt:
        return 130
    except (ApiCommandError, ValueError, OSError) as error:
        print(
            json_dumps(
                error.document if isinstance(error, ApiCommandError) else {"error": str(error)}
            ),
            file=sys.stderr,
        )
        return 2
    return 0
