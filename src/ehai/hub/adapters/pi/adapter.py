"""Pi compatibility layer: launch upstream Pi in RPC mode and normalize its events.

Pi is used unmodified through its public RPC protocol and extension API. EHAI tools are
exposed through the business-tool bridge extension; every tool call is forwarded to the
core through the Hub and never executed here.
"""

from __future__ import annotations

import base64
import math
import secrets
from pathlib import Path

from ehai import JsonValue, json_dumps, json_loads
from ehai.hub.adapters import HarnessFailure, HarnessRequestError
from ehai.hub.adapters.pi.config import PI_VERSION, PiBackendConfig, check_node_version
from ehai.hub.adapters.pi.rpc import PiRpcError, PiRpcProcess
from ehai.hub.protocol import StartSession, ToolResult

_LIFECYCLE = frozenset(
    {
        "agent_start",
        "agent_end",
        "turn_end",
        "auto_compaction_start",
        "auto_compaction_end",
        "auto_retry_start",
        "auto_retry_end",
    }
)
_USAGE_KEYS = frozenset({"input", "output", "cacheRead", "cacheWrite", "totalTokens"})


def _decode_bridge_event(event: dict[str, JsonValue], nonce: str) -> dict[str, JsonValue]:
    """Unwrap only our private envelopes on Pi's public RPC notification channel."""
    if event.get("type") != "extension_ui_request" or event.get("method") != "notify":
        return event
    message = event.get("message")
    prefix = "ehai.bridge.v1:"
    if not isinstance(message, str) or not message.startswith(prefix):
        return event
    try:
        payload = json_loads(message[len(prefix) :])
    except ValueError as error:
        raise PiRpcError("Invalid EHAI bridge notification") from error
    if (
        not isinstance(payload, dict)
        or payload.get("nonce") != nonce
        or payload.get("type") not in ("ehai_context_prepared", "ehai_tool_call")
    ):
        raise PiRpcError("Unexpected EHAI bridge notification")
    return payload


def _usage(value: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, JsonValue] = {
        key: item
        for key, item in value.items()
        if key in _USAGE_KEYS and type(item) is int and item >= 0
    }
    cost = value.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    if (
        isinstance(total, (int, float))
        and not isinstance(total, bool)
        and math.isfinite(total)
        and total >= 0
    ):
        usage["estimated_cost"] = total
    return usage


class PiSession:
    def __init__(self, rpc: PiRpcProcess, nonce: str, native_session: JsonValue) -> None:
        self._rpc = rpc
        self._nonce = nonce
        self._native_session = native_session

    @property
    def native_session(self) -> JsonValue:
        return self._native_session

    async def next_event(self) -> dict[str, JsonValue]:
        try:
            while True:
                event = _decode_bridge_event(await self._rpc.next_event(), self._nonce)
                normalized = self._normalize(event)
                if normalized is not None:
                    return normalized
        except PiRpcError as error:
            raise HarnessFailure(str(error)) from error

    def _normalize(self, event: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
        kind = event.get("type")
        if kind == "ehai_context_prepared":
            hashes = event.get("input_hashes")
            if not isinstance(hashes, list) or not all(isinstance(item, str) for item in hashes):
                raise PiRpcError("Invalid Pi context confirmation")
            return {"type": "input_prepared", "input_hashes": hashes}
        if kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            blocks = message.get("content")
            text = (
                "\n".join(
                    str(block["text"])
                    for block in blocks
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
                if isinstance(blocks, list)
                else ""
            )
            stop_reason = message.get("stopReason")
            return {
                "type": "assistant_message",
                "text": text,
                "stop_reason": stop_reason if isinstance(stop_reason, str) else None,
                "usage": _usage(message.get("usage")),
            }
        if kind == "ehai_tool_call":
            call_id, name, arguments = (
                event.get("call_id"),
                event.get("name"),
                event.get("arguments"),
            )
            batch = event.get("batch_call_ids")
            if (
                not isinstance(call_id, str)
                or not isinstance(name, str)
                or not isinstance(arguments, dict)
            ):
                raise PiRpcError("Invalid Pi business tool request")
            return {
                "type": "tool_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "batch_call_ids": (
                    [item for item in batch if isinstance(item, str)]
                    if isinstance(batch, list)
                    else []
                ),
            }
        if kind == "agent_settled":
            return {"type": "settled"}
        if isinstance(kind, str) and kind in _LIFECYCLE:
            return {"type": "lifecycle", "name": kind}
        return None

    async def _request(self, command: str, parameters: dict[str, JsonValue] | None = None) -> None:
        try:
            await self._rpc.request(command, parameters)
        except PiRpcError as error:
            raise HarnessFailure(str(error)) from error

    async def steer(self, messages: list[str]) -> None:
        for message in messages:
            await self._request("steer", {"message": message})

    async def prompt(self, message: str) -> None:
        await self._request("prompt", {"message": message})

    async def tool_result(self, result: ToolResult) -> None:
        envelope = base64.b64encode(
            json_dumps(
                {
                    "nonce": self._nonce,
                    "call_id": result.call_id,
                    "result": result.result,
                    "is_error": result.is_error,
                    "finish": result.finish,
                }
            ).encode("utf-8")
        ).decode("ascii")
        await self._request("prompt", {"message": f"/ehai-tool-result {envelope}"})

    async def cancel(self) -> None:
        await self._request("clear_queue")
        await self._request("abort")

    async def close(self, *, abort: bool) -> None:
        await self._rpc.shutdown(abort=abort)


class PiAdapter:
    kind = "pi"
    version = PI_VERSION

    async def launch(self, request: StartSession) -> PiSession:
        settings = dict(request.harness.settings)
        state_root = settings.pop("state_root", None)
        try:
            if not isinstance(state_root, str) or not Path(state_root).is_absolute():
                raise ValueError("Pi settings require an absolute state_root")
            backend = PiBackendConfig.from_document(settings)
            native = backend.native_documents()
            home = Path(state_root).resolve() / request.state_key
            home.mkdir(parents=True, exist_ok=True)
            for filename, document in native.items():
                (home / filename).write_text(json_dumps(document), encoding="utf-8")
            nonce = secrets.token_hex(24)
            specification = home / f"tools-{request.invocation_id}.json"
            specification.write_text(
                json_dumps(
                    {
                        "nonce": nonce,
                        "tools": [
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.parameters,
                            }
                            for tool in request.tools
                        ],
                    }
                ),
                encoding="utf-8",
            )
            environment = backend.environment(home)
            await check_node_version(backend.node, environment)
            environment["PI_CODING_AGENT_DIR"] = str(home)
            environment["EHAI_PI_TOOL_SPEC"] = str(specification)
            bridge = Path(__file__).with_name("business_tools.mjs")
            if not bridge.is_file():
                raise ValueError("Pi backend is missing the installed EHAI tool bridge")
        except ValueError as error:
            raise HarnessRequestError(str(error)) from error
        command = [
            str(backend.node),
            str(backend.cli),
            "--mode",
            "rpc",
            "--offline",
            "--no-builtin-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--extension",
            str(bridge),
            "--provider",
            backend.provider,
            "--model",
            request.model,
            "--session-id",
            request.native_session_id,
            "--session-dir",
            str(home / "sessions"),
            "--system-prompt",
            request.system_prompt,
        ]
        if request.reasoning_effort is not None:
            command.extend(("--thinking", request.reasoning_effort))
        try:
            rpc = PiRpcProcess(command, workspace=Path(request.workspace), environment=environment)
        except ValueError as error:
            raise HarnessRequestError(str(error)) from error
        try:
            await rpc.__aenter__()
        except OSError as error:
            raise HarnessFailure("Pi process could not be started") from error
        try:
            data = (await rpc.request("get_state")).get("data")
            if not isinstance(data, dict) or data.get("sessionId") != request.native_session_id:
                raise PiRpcError("Pi session identity did not match the requested role")
            selected = data.get("model")
            if (
                not isinstance(selected, dict)
                or selected.get("id") != request.model
                or selected.get("provider") != backend.provider
            ):
                raise PiRpcError("Pi did not select the exact authorized provider/model")
            if (
                request.reasoning_effort is not None
                and data.get("thinkingLevel") != request.reasoning_effort
            ):
                raise PiRpcError("Pi did not accept the authorized thinking level")
            if request.fresh and data.get("messageCount") != 0:
                raise PiRpcError("Untracked native history cannot become a fresh EHAI Session")
            if not request.fresh and (
                not isinstance(data.get("messageCount"), int) or data["messageCount"] == 0
            ):
                raise PiRpcError("Native Pi history is missing; refusing a silent fresh Session")
        except BaseException as error:
            await rpc.shutdown(abort=True)
            if isinstance(error, PiRpcError):
                raise HarnessFailure(str(error)) from error
            raise
        return PiSession(rpc, nonce, data.get("sessionFile"))
