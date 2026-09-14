"""Run role harnesses through upstream Pi, never through an EHAI model loop."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ehai import ID, JsonValue, json_dumps, json_loads
from ehai.application.agent_contracts import (
    AgentCancelledError,
    CancellationToken,
    ModelMessage,
    RecoverableToolError,
    ToolCall,
)
from ehai.application.agent_roles import AgentRoleConfig, BeforeStepMessages, ToolRegistry
from ehai.application.agent_trace import (
    AgentTrace,
    AgentTraceStore,
)
from ehai.application.agent_trace import (
    AgentTraceEventType as TraceType,
)
from ehai.infrastructure.pi_config import PiBackendConfig, check_node_version, validate_pi_cli
from ehai.infrastructure.pi_rpc import PiRpcError, PiRpcProcess


class PiExecutionUnknownError(PiRpcError):
    """A stopped invocation has unresolved model/tool outcome; never auto-resend it."""


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


class RoleExecution(Protocol):
    @property
    def attempt_id(self) -> ID: ...


class PiRoleRunner:
    def __init__(
        self,
        session_store: AgentTraceStore,
        *,
        backend: PiBackendConfig,
        state_root: Path,
    ) -> None:
        self.session_store = session_store
        self.backend = backend
        self.state_root = state_root.resolve()

    def create_session(self, agent_session_ref_id: ID | None = None) -> AgentTrace:
        create = getattr(self.session_store, "create", None)
        if create is None:
            raise ValueError("Role trace store cannot allocate a Session identity")
        result: AgentTrace = create(agent_session_ref_id)
        return result

    async def run(
        self,
        *,
        config: AgentRoleConfig,
        registry: ToolRegistry,
        session: AgentTrace,
        execution: RoleExecution,
        instruction: str,
        context: Mapping[str, JsonValue],
        model: str,
        reasoning_effort: str | None,
        workspace: Path,
        cancellation: CancellationToken | None = None,
        before_step_messages: BeforeStepMessages | None = None,
        native_session_id: str | None = None,
    ) -> str:
        attempt_id: ID = execution.attempt_id
        if session.is_turn_complete(attempt_id):
            return session.final_text(attempt_id) or ""
        if session.has_turn(attempt_id):
            raise PiExecutionUnknownError(
                "Pi invocation was already started; inspect its outcome before continuing"
            )
        starts = [event for event in session.events if event.type is TraceType.TURN_STARTED]
        if any(not session.is_turn_complete(event.attempt_id) for event in starts):
            raise PiExecutionUnknownError(
                "An earlier Pi invocation is incomplete; do not continue its native history"
            )
        if session.events and (
            not starts or any(event.payload.get("backend") != "pi" for event in starts)
        ):
            raise ValueError("Legacy model history cannot be resumed as a Pi session")
        if any(
            event.payload.get("configuration_hash") != self.backend.configuration_hash
            for event in starts
        ):
            raise ValueError("Pi Session configuration differs from the authorized backend")
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        validate_pi_cli(self.backend.cli)
        tool_set, executor = registry.freeze(config)
        native = self.backend.native_documents()
        home = self.state_root / str(session.agent_session_ref_id)
        home.mkdir(parents=True, exist_ok=True)
        for filename, document in native.items():
            (home / filename).write_text(json_dumps(document), encoding="utf-8")
        nonce = secrets.token_hex(24)
        specification = home / f"tools-{attempt_id}.json"
        specification.write_text(
            json_dumps(
                {
                    "nonce": nonce,
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        }
                        for tool in tool_set.definitions
                    ],
                }
            ),
            encoding="utf-8",
        )
        environment = self.backend.environment(home)
        await check_node_version(self.backend.node, environment)
        environment["PI_CODING_AGENT_DIR"] = str(home)
        environment["EHAI_PI_TOOL_SPEC"] = str(specification)
        bridge = Path(__file__).with_name("pi_business_tools.mjs")
        if not bridge.is_file():
            raise ValueError("Pi backend is missing the installed EHAI tool bridge")
        command = [
            str(self.backend.node),
            str(self.backend.cli),
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
            self.backend.provider,
            "--model",
            model,
            "--session-id",
            native_session_id or str(session.agent_session_ref_id),
            "--session-dir",
            str(home / "sessions"),
            "--system-prompt",
            config.system_prompt,
        ]
        if reasoning_effort is not None:
            command.extend(("--thinking", reasoning_effort))

        def record(kind: TraceType, payload: Mapping[str, JsonValue]) -> None:
            expected = len(session.events)
            event = session.append(attempt_id, kind, payload)
            self.session_store.append(session.agent_session_ref_id, expected, (event,))

        queued: dict[str, ModelMessage] = {}

        async def inject(rpc: PiRpcProcess) -> None:
            if before_step_messages is None:
                return
            for message in before_step_messages(session.agent_session_ref_id):
                digest = hashlib.sha256(message.content.encode("utf-8")).hexdigest()
                if digest in queued:
                    continue
                queued[digest] = message
                await rpc.request("steer", {"message": message.content})

        final_text: str | None = None
        try:
            async with PiRpcProcess(command, workspace=workspace, environment=environment) as rpc:
                state = await rpc.request("get_state")
                data = state.get("data")
                if not isinstance(data, dict) or data.get("sessionId") != (
                    native_session_id or str(session.agent_session_ref_id)
                ):
                    raise PiRpcError("Pi session identity did not match the requested role")
                selected = data.get("model")
                if (
                    not isinstance(selected, dict)
                    or selected.get("id") != model
                    or selected.get("provider") != self.backend.provider
                ):
                    raise PiRpcError("Pi did not select the exact authorized provider/model")
                if reasoning_effort is not None and data.get("thinkingLevel") != reasoning_effort:
                    raise PiRpcError("Pi did not accept the authorized thinking level")
                if not starts and data.get("messageCount") != 0:
                    raise PiRpcError("Untracked native history cannot become a fresh EHAI Session")
                if starts and (
                    not isinstance(data.get("messageCount"), int) or data["messageCount"] == 0
                ):
                    raise PiRpcError(
                        "Native Pi history is missing; refusing a silent fresh Session"
                    )
                record(
                    TraceType.TURN_STARTED,
                    {
                        "backend": "pi",
                        "role": config.role.value,
                        "configuration_hash": self.backend.configuration_hash,
                        "native_session": data.get("sessionFile"),
                    },
                )
                await inject(rpc)
                await rpc.request(
                    "prompt",
                    {
                        "message": json_dumps(
                            {
                                "instruction": instruction,
                                "context": dict(config.context_builder(context)),
                            }
                        )
                    },
                )
                cancel_wait = asyncio.create_task(token.wait_cancelled())
                try:
                    while True:
                        event_wait = asyncio.create_task(rpc.next_event())
                        try:
                            done, _ = await asyncio.wait(
                                (event_wait, cancel_wait), return_when=asyncio.FIRST_COMPLETED
                            )
                            if cancel_wait in done:
                                await rpc.request("clear_queue")
                                await rpc.request("abort")
                                token.raise_if_cancelled()
                            event = _decode_bridge_event(await event_wait, nonce)
                        finally:
                            if not event_wait.done():
                                event_wait.cancel()
                                await asyncio.gather(event_wait, return_exceptions=True)
                        kind = event.get("type")
                        if kind == "ehai_context_prepared":
                            hashes = event.get("input_hashes")
                            if event.get("nonce") != nonce or not isinstance(hashes, list):
                                raise PiRpcError("Invalid Pi context confirmation")
                            for digest in hashes:
                                message = (
                                    queued.pop(digest, None) if isinstance(digest, str) else None
                                )
                                if message is not None:
                                    record(
                                        TraceType.MESSAGE_RECEIVED,
                                        {
                                            "content": message.content,
                                            "role": "user",
                                            "delivery_boundary": "native_provider_input_prepared",
                                        },
                                    )
                                    if message.acknowledge is not None:
                                        message.acknowledge()
                        elif kind == "message_end":
                            message_data = event.get("message")
                            if (
                                isinstance(message_data, dict)
                                and message_data.get("role") == "assistant"
                            ):
                                usage = message_data.get("usage")
                                if isinstance(usage, dict):
                                    safe_usage: dict[str, JsonValue] = {
                                        key: value
                                        for key, value in usage.items()
                                        if key
                                        in {
                                            "input",
                                            "output",
                                            "cacheRead",
                                            "cacheWrite",
                                            "totalTokens",
                                        }
                                        and type(value) is int
                                        and value >= 0
                                    }
                                    cost = usage.get("cost")
                                    total = cost.get("total") if isinstance(cost, dict) else None
                                    if (
                                        isinstance(total, (int, float))
                                        and not isinstance(total, bool)
                                        and math.isfinite(total)
                                        and total >= 0
                                    ):
                                        safe_usage["estimated_cost"] = total
                                    record(
                                        TraceType.BACKEND_EVENT,
                                        {
                                            "backend": "pi",
                                            "type": "usage",
                                            "usage": safe_usage,
                                        },
                                    )
                        elif kind == "ehai_tool_call":
                            if event.get("nonce") != nonce or final_text is not None:
                                raise PiRpcError(
                                    "Unexpected tool request after result or from another lane"
                                )
                            call_id, name, args = (
                                event.get("call_id"),
                                event.get("name"),
                                event.get("arguments"),
                            )
                            if (
                                not isinstance(call_id, str)
                                or not isinstance(name, str)
                                or not isinstance(args, dict)
                            ):
                                raise PiRpcError("Invalid Pi business tool request")
                            definition = tool_set.require(name)
                            record(
                                TraceType.TOOL_CALLED,
                                {
                                    "call_id": call_id,
                                    "name": name,
                                    "arguments": args,
                                    "writes_workspace": definition.writes_workspace,
                                },
                            )
                            error = False
                            try:
                                batch = event.get("batch_call_ids")
                                valid_finish = (
                                    isinstance(batch, list)
                                    and bool(batch)
                                    and batch[-1] == call_id
                                    and (not config.final_tool_requires_only or len(batch) == 1)
                                )
                                if definition.ends_turn and not valid_finish:
                                    result: JsonValue = {
                                        "accepted": False,
                                        "error": "Submit the finish tool alone after other tools",
                                    }
                                else:
                                    result = await executor.execute(
                                        ToolCall(call_id, name, args), token
                                    )
                            except RecoverableToolError as failure:
                                error = True
                                result = {"code": failure.code, "error": failure.safe_message}
                            finish = (
                                definition.ends_turn
                                and not error
                                and not (
                                    isinstance(result, dict) and result.get("accepted") is False
                                )
                            )
                            record(
                                TraceType.TOOL_ERROR if error else TraceType.TOOL_RESULT,
                                {"call_id": call_id, "error" if error else "result": result},
                            )
                            if finish:
                                final_text = json_dumps(result)
                            envelope = base64.b64encode(
                                json_dumps(
                                    {
                                        "nonce": nonce,
                                        "call_id": call_id,
                                        "result": result,
                                        "is_error": error,
                                        "finish": finish,
                                    }
                                ).encode("utf-8")
                            ).decode("ascii")
                            # Queue context before releasing the native tool continuation.
                            if not finish:
                                await inject(rpc)
                            await rpc.request(
                                "prompt", {"message": f"/ehai-tool-result {envelope}"}
                            )
                        elif kind == "agent_settled":
                            if final_text is None:
                                raise PiRpcError("Pi settled without a host-validated role result")
                            record(TraceType.FINAL, {"text": final_text})
                            record(TraceType.TURN_ENDED, {"backend": "pi"})
                            return final_text
                        elif kind in {
                            "agent_start",
                            "agent_end",
                            "turn_end",
                            "auto_compaction_start",
                            "auto_compaction_end",
                            "auto_retry_start",
                            "auto_retry_end",
                        }:
                            record(TraceType.BACKEND_EVENT, {"backend": "pi", "type": kind})
                finally:
                    cancel_wait.cancel()
                    await asyncio.gather(cancel_wait, return_exceptions=True)
        except (asyncio.CancelledError, AgentCancelledError):
            if session.has_turn(attempt_id):
                record(TraceType.BACKEND_ERROR, {"backend": "pi", "outcome": "cancelled_unknown"})
            raise
        except (PiRpcError, OSError, TimeoutError) as failure:
            if session.has_turn(attempt_id):
                record(TraceType.BACKEND_ERROR, {"backend": "pi", "outcome": "unknown"})
            raise PiExecutionUnknownError(
                "Pi invocation did not yield a confirmed result; no prompt replay was attempted"
            ) from failure
        finally:
            await executor.aclose()
