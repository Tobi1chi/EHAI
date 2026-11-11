"""Translate the pinned v2 item/turn stream and server approvals into Hub events."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import Counter, deque
from collections.abc import Mapping

from ehai import JsonValue, json_dumps
from ehai.hub.adapters import HarnessFailure, ToolBatch
from ehai.hub.adapters.codex.process import AppServer
from ehai.hub.protocol import NativeDecision, ToolResult

# Codex's own read-only tools over the loaded MCP servers' resources (only `ehai` is
# loaded, see the launch check). They run inside Codex as server "codex" and are
# recorded, not forwarded; any other non-session MCP tool fails the session.
_CODEX_RESOURCE_TOOLS = frozenset(
    {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}
)
# Item types mapped to Hub events; other completed items are recorded as native `other`.
_MAPPED_ITEMS = frozenset(
    {"userMessage", "agentMessage", "reasoning", "mcpToolCall", "commandExecution", "fileChange"}
)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _paths(item: dict[str, JsonValue]) -> list[JsonValue]:
    # v2/ItemStartedNotification.json: FileUpdateChange and PatchChangeKind.update.
    paths: list[JsonValue] = []
    changes = item.get("changes")
    for change in changes if isinstance(changes, list) else []:
        if isinstance(change, dict):
            path = change.get("path")
            if isinstance(path, str):
                paths.append(path)
            moved = _object(change.get("kind")).get("move_path")
            if isinstance(moved, str):
                paths.append(moved)
    return paths


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodexSession:
    def __init__(
        self, process: AppServer, thread_id: str, tools: set[str], effort: str | None
    ) -> None:
        self._process = process
        self._thread = thread_id
        self._tools = tools
        self._effort = effort
        self._turn: str | None = None
        self._ended: set[str] = set()
        self._queued: list[str] = []
        self._input_lock = asyncio.Lock()
        self._inputs: Counter[str] = Counter()
        self._seen_users: set[str] = set()
        self._items: dict[str, dict[str, JsonValue]] = {}
        self._approvals: dict[str, tuple[JsonValue, str, dict[str, JsonValue]]] = {}
        self._out: deque[dict[str, JsonValue]] = deque()
        self._messages: list[str] = []
        self._usage: dict[str, JsonValue] | None = None
        self._published: dict[str, tuple[str, JsonValue]] = {}
        self._unpublished: dict[str, tuple[str, JsonValue]] = {}
        self._calls_changed = asyncio.Event()

    @property
    def native_session(self) -> JsonValue:
        return {"thread_id": self._thread}

    async def next_event(self) -> dict[str, JsonValue]:
        # Hub appends the previous event before asking again. Only release MCP after
        # every assistant message preceding its item has reached that log.
        if not self._out and self._unpublished:
            self._published.update(self._unpublished)
            self._unpublished.clear()
            self._calls_changed.set()
        while not self._out:
            await self._normalize(await self._process.next())
        return self._out.popleft()

    def _announce(self, *, tool: bool = False) -> None:
        texts, self._messages = self._messages, []
        if not texts and (tool or self._usage is not None):
            texts = [""]
        for index, text in enumerate(texts):
            self._out.append(
                {
                    "type": "assistant_message",
                    "text": text,
                    "stop_reason": "toolUse" if tool else "stop",
                    "usage": self._usage if index == len(texts) - 1 else None,
                }
            )
        if texts:
            self._usage = None

    async def _normalize(self, event: dict[str, JsonValue]) -> None:
        method, params = event.get("method"), _object(event.get("params"))
        if "id" in event:
            self._approval(event["id"], str(method), params)
        elif method in {"item/started", "item/completed"}:
            # v2/Item{Started,Completed}Notification.json / ThreadItem.
            item = _object(params.get("item"))
            item_id, kind = str(item.get("id")), item.get("type")
            self._items[item_id] = item
            if kind == "userMessage" and item_id not in self._seen_users:
                content = item.get("content")
                hashes: list[JsonValue] = []
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str) and self._inputs[_hash(text)]:
                            digest = _hash(text)
                            self._inputs[digest] -= 1
                            hashes.append(digest)
                if hashes:
                    self._seen_users.add(item_id)
                    self._out.append({"type": "input_prepared", "input_hashes": hashes})
            elif kind == "agentMessage" and method == "item/completed":
                text = item.get("text")
                if isinstance(text, str):
                    self._messages.append(text)
            elif kind == "mcpToolCall" and item.get("server") == "codex":
                if item.get("tool") not in _CODEX_RESOURCE_TOOLS:
                    raise HarnessFailure(
                        f"Codex requested built-in MCP tool {item.get('tool')!r}"
                        " outside this session"
                    )
                if method == "item/completed":
                    self._native_other(item_id, item, f"mcp:codex/{item.get('tool')}")
            elif kind == "mcpToolCall" and method == "item/started":
                if item.get("server") != "ehai" or item.get("tool") not in self._tools:
                    raise HarnessFailure(
                        f"Codex requested MCP tool {item.get('server')!r}/{item.get('tool')!r}"
                        " outside this session"
                    )
                self._announce(tool=True)
                self._unpublished[item_id] = (str(item["tool"]), item.get("arguments"))
            elif kind not in _MAPPED_ITEMS and method == "item/completed":
                self._native_other(item_id, item, str(kind))
            elif kind in {"commandExecution", "fileChange"} and method == "item/completed":
                self._announce()
                self._out.append(
                    {
                        "type": "native_item",
                        "item_id": item_id,
                        "kind": "command" if kind == "commandExecution" else "file_change",
                        "command": item.get("command"),
                        "cwd": item.get("cwd"),
                        "paths": _paths(item),
                        "status": item.get("status"),
                        "exit_code": item.get("exitCode"),
                    }
                )
        elif method == "thread/tokenUsage/updated":
            # v2/ThreadTokenUsageUpdatedNotification.json. `last` is one model call,
            # not thread `total`; cached tokens are included in Codex inputTokens.
            last = _object(_object(params.get("tokenUsage")).get("last"))
            names = {
                "inputTokens": "input",
                "outputTokens": "output",
                "cachedInputTokens": "cacheRead",
                "cacheWriteInputTokens": "cacheWrite",
            }
            counts = {
                key: value
                for key, value in last.items()
                if key in names and type(value) is int and value >= 0
            }
            self._usage = {names[key]: value for key, value in counts.items()}
            if "inputTokens" in counts:
                self._usage["input"] = (
                    counts["inputTokens"]
                    - counts.get("cachedInputTokens", 0)
                    - counts.get("cacheWriteInputTokens", 0)
                )
            self._announce()
        elif method == "turn/started":
            turn_id = _object(params.get("turn")).get("id")
            if isinstance(turn_id, str) and turn_id not in self._ended:
                self._turn = turn_id
        elif method == "turn/completed":
            # v2/TurnCompletedNotification.json: TurnStatus / TurnError.
            turn = _object(params.get("turn"))
            self._ended.add(str(turn.get("id")))
            self._turn = None
            self._announce()
            status = turn.get("status")
            if status == "completed":
                self._out.extend([{"type": "lifecycle", "name": "turn_end"}, {"type": "settled"}])
            else:
                self._out.append(
                    {
                        "type": "failed",
                        "reason": f"Codex turn {status}: "
                        + (
                            "cancelled; stopping commands is unconfirmed"
                            if status == "interrupted"
                            else json_dumps(turn.get("error"))
                        ),
                    }
                )
        elif method == "ehai/unsupportedRequest":
            self._out.append({"type": "lifecycle", "name": f"unsupported:{params.get('name')}"})
        elif method == "error":
            # v2/ErrorNotification.json: recoverable errors precede a retry, whereas
            # terminal errors are also carried by turn/completed.
            if params.get("willRetry") is True:
                self._out.append({"type": "lifecycle", "name": "retry"})
            else:
                self._out.append({"type": "failed", "reason": json_dumps(params.get("error"))})

    def _native_other(self, item_id: str, item: dict[str, JsonValue], label: str) -> None:
        self._announce()
        status = item.get("status")
        self._out.append(
            {
                "type": "native_item",
                "item_id": item_id,
                "kind": "other",
                "command": label,
                "cwd": None,
                "paths": [],
                "status": status if isinstance(status, str) else "completed",
                "exit_code": None,
            }
        )

    def _approval(self, request_id: JsonValue, method: str, params: dict[str, JsonValue]) -> None:
        # {CommandExecution,FileChange,Permissions}RequestApprovalParams.json.
        self._announce()
        item = self._items.get(str(params.get("itemId")), {})
        kind = {
            "item/commandExecution/requestApproval": "command",
            "item/fileChange/requestApproval": "file_change",
            "item/permissions/requestApproval": "permissions",
        }.get(method, "other")
        if kind == "command":
            if params.get("additionalPermissions") or params.get("networkApprovalContext"):
                kind = "permissions"
            elif params.get("kind", "command") != "command":
                kind = "other"
        action_id = uuid.uuid4().hex  # Approval callbacks can share an itemId.
        self._approvals[action_id] = (request_id, method, params)
        self._out.append(
            {
                "type": "native_action",
                "action_id": action_id,
                "kind": kind,
                "command": params.get("command") or item.get("command"),
                "cwd": params.get("cwd") or item.get("cwd"),
                "paths": _paths(params) or _paths(item),
                "reason": params.get("reason"),
            }
        )
        if kind == "other":
            self._out.append({"type": "lifecycle", "name": f"unsupported:{method}"})

    async def native_decision(self, action_id: str, decision: NativeDecision) -> None:
        approval = self._approvals.pop(action_id, None)
        if approval is None:
            raise HarnessFailure("Codex approval is missing or has already been answered")
        request_id, method, params = approval
        # {CommandExecution,FileChange}RequestApprovalResponse.json: single-operation
        # accept/decline only; never acceptForSession or a policy amendment.
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result: dict[str, JsonValue] = {
                "decision": "accept" if decision.approved else "decline"
            }
        elif method == "item/permissions/requestApproval":
            # PermissionsRequestApprovalResponse.json: denial is an empty grant.
            result = {
                "permissions": params.get("permissions", {}) if decision.approved else {},
                "scope": "turn",
                "strictAutoReview": True,
            }
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            # {ExecCommand,ApplyPatch}ApprovalResponse.json / ReviewDecision.
            # Legacy requests are `other` to the core, which denies them.
            result = {
                "decision": "approved"
                if decision.approved
                else {"denied": {"rejection": decision.reason or "Denied by EHAI"}}
            }
        else:
            await self._process.unsupported(request_id)
            return
        await self._process.write({"id": request_id, "result": result})

    async def await_tool_call(
        self, name: str, arguments: dict[str, JsonValue], meta: Mapping[str, JsonValue]
    ) -> ToolBatch:
        key = meta.get("callId")
        if not isinstance(key, str):
            raise HarnessFailure("Codex MCP call is missing _meta.callId")
        try:
            async with asyncio.timeout(10.0):
                while key not in self._published:
                    self._calls_changed.clear()
                    await self._calls_changed.wait()
        except TimeoutError as error:
            raise HarnessFailure("Codex MCP call never matched a published item") from error
        tool, expected = self._published.pop(key)
        if tool != name or json_dumps(expected) != json_dumps(arguments):
            raise HarnessFailure("Codex MCP call differs from its app-server item")
        # ItemStartedNotification.json and RawResponseItemCompletedNotification.json
        # carry turnId, not a response boundary or a complete list of sibling calls.
        return ToolBatch(key, (key,))

    def _input(self, messages: list[str]) -> list[JsonValue]:
        for message in messages:
            self._inputs[_hash(message)] += 1
        return [{"type": "text", "text": message} for message in messages]

    async def prompt(self, message: str) -> None:
        async with self._input_lock:
            if self._turn is not None:
                raise HarnessFailure("Codex already has an active turn; use steer")
            messages, self._queued = [message, *self._queued], []
            # v2/TurnStart{Params,Response}.json; ReasoningEffort is a nonempty string
            # in this snapshot (not an enum). Pass the requested value unchanged.
            params: dict[str, JsonValue] = {
                "threadId": self._thread,
                "input": self._input(messages),
            }
            if self._effort is not None:
                params["effort"] = self._effort
            result = await self._process.call("turn/start", params)
            turn_id = _object(result.get("turn")).get("id")
            if not isinstance(turn_id, str):
                raise HarnessFailure("Codex turn/start omitted turn identity; outcome unknown")
            if turn_id not in self._ended:
                self._turn = turn_id

    async def steer(self, messages: list[str]) -> None:
        async with self._input_lock:
            if self._turn is None:
                self._queued.extend(messages)
            else:
                # v2/TurnSteer{Params,Response}.json. Never retry against another turn.
                await self._process.call(
                    "turn/steer",
                    {
                        "threadId": self._thread,
                        "expectedTurnId": self._turn,
                        "input": self._input(messages),
                    },
                )

    async def tool_result(self, result: ToolResult) -> None:
        del result
        raise HarnessFailure("Codex receives EHAI tool results over MCP")

    async def cancel(self) -> None:
        # v2/TurnInterruptParams.json, v2/ThreadBackgroundTerminalsCleanParams.json.
        # Always clean, including if the turn completed while interrupt was in flight.
        async with self._input_lock:
            try:
                if self._turn is not None:
                    await self._process.call(
                        "turn/interrupt", {"threadId": self._thread, "turnId": self._turn}
                    )
            finally:
                await self._process.call(
                    "thread/backgroundTerminals/clean", {"threadId": self._thread}
                )

    async def close(self, *, abort: bool) -> None:
        async def stop() -> None:
            try:
                if not abort:
                    await self._process.call(
                        "thread/backgroundTerminals/clean", {"threadId": self._thread}
                    )
            finally:
                await self._process.close(abort=abort)

        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
