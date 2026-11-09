"""Run role harnesses through the Hub, never through an EHAI model loop.

The Hub launches the harness and relays its events. This side owns everything that is an
EHAI fact: the trace, tool execution, injected messages and the decision that a turn ended
with a host-validated result.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from ehai import ID, JsonValue, json_dumps, json_loads
from ehai.application.agent_contracts import (
    AgentCancelledError,
    CancellationToken,
    ModelMessage,
    RecoverableToolError,
    ToolCall,
    ToolExecutor,
    ToolSet,
)
from ehai.application.agent_roles import (
    AgentRoleConfig,
    BeforeStepMessages,
    NativeAuthorization,
    RoleExecution,
    ToolRegistry,
)
from ehai.application.agent_trace import AgentTrace, AgentTraceStore
from ehai.application.agent_trace import AgentTraceEventType as TraceType
from ehai.application.sanitization import redact_sensitive_text
from ehai.hub.client import HubClient, HubConnection, HubUnavailableError, default_hub
from ehai.hub.protocol import (
    HEARTBEAT_SECONDS,
    AssistantMessage,
    EventBatch,
    Failed,
    HarnessSelection,
    HubEvent,
    InputPrepared,
    Lifecycle,
    NativeAction,
    NativeDecision,
    NativeItem,
    NativePolicy,
    Settled,
    StartSession,
    ToolCallEvent,
    ToolResult,
    ToolSpec,
)
from ehai.infrastructure.harnesses import HarnessConfig
from ehai.infrastructure.host_tools import NATIVE_REPLACED_TOOLS

# Native tools must be disabled or constrained by the harness (ADR 0007, section 11).
_REQUIRED_CAPABILITIES = ("tools.native.restrictable",)
_EVENT_WAIT_SECONDS = 20.0


class HarnessExecutionUnknownError(RuntimeError):
    """A stopped invocation has unresolved model/tool outcome; never auto-resend it."""


class HarnessProtocolError(RuntimeError):
    """The harness reported something that cannot belong to this invocation."""


class HarnessTextOnlyError(HarnessProtocolError):
    """The harness settled normally without calling the role's finish tool."""


class HubRoleRunner:
    def __init__(
        self,
        session_store: AgentTraceStore,
        *,
        harness: HarnessConfig,
        state_root: Path | None = None,
        hub: HubClient | None = None,
    ) -> None:
        self.session_store = session_store
        self.harness = harness
        # Without a root the compatibility layer keeps native sessions in its own place.
        self.state_root = None if state_root is None else state_root.resolve()
        self.hub = hub or default_hub()

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
        allow_text_only: bool = False,
        finish_reminder: str | None = None,
    ) -> str:
        attempt_id: ID = execution.attempt_id
        if session.is_turn_complete(attempt_id):
            return session.final_text(attempt_id) or ""
        if session.has_turn(attempt_id):
            raise HarnessExecutionUnknownError(
                "Harness invocation was already started; inspect its outcome before continuing"
            )
        starts = [event for event in session.events if event.type is TraceType.TURN_STARTED]
        if any(not session.is_turn_complete(event.attempt_id) for event in starts):
            raise HarnessExecutionUnknownError(
                "An earlier harness invocation is incomplete; do not continue its native history"
            )
        kind = self.harness.kind
        if session.events and (
            not starts or any(event.payload.get("backend") != kind for event in starts)
        ):
            raise ValueError(f"Legacy model history cannot be resumed as a {kind} session")
        if any(
            event.payload.get("configuration_hash") != self.harness.fingerprint for event in starts
        ):
            raise ValueError(f"{kind} Session configuration differs from the authorized backend")
        native_kept = self.harness.supports("tools.native.kept")
        missing = [
            capability
            for capability in (
                *_REQUIRED_CAPABILITIES,
                *(("tools.native.approval",) if native_kept else ()),
                *(("session.resume",) if starts else ()),
            )
            if not self.harness.supports(capability)
        ]
        if missing:
            raise ValueError(f"Harness {kind} does not declare {', '.join(missing)}")
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        tool_set, executor = registry.freeze(config)
        native_policy = None
        if native_kept:
            tool_set = ToolSet(
                tuple(
                    tool for tool in tool_set.definitions if tool.name not in NATIVE_REPLACED_TOOLS
                )
            )
            authorization = registry.native_authorization
            native_policy = NativePolicy(
                sandbox=(
                    "workspace-write"
                    if authorization is not None and authorization.allow_workspace_write
                    else "read-only"
                ),
                network=False,
            )
        request = StartSession(
            invocation_id=str(attempt_id),
            state_key=str(session.agent_session_ref_id),
            native_session_id=native_session_id or str(session.agent_session_ref_id),
            harness=HarnessSelection(
                kind=kind,
                version=self.harness.version,
                settings=(
                    self.harness.settings
                    if self.state_root is None
                    else {**self.harness.settings, "state_root": str(self.state_root)}
                ),
            ),
            fresh=not starts,
            system_prompt=config.system_prompt,
            tools=[
                ToolSpec(name=tool.name, description=tool.description, parameters=tool.input_schema)
                for tool in tool_set.definitions
            ],
            model=model,
            reasoning_effort=reasoning_effort,
            workspace=str(workspace),
            native_policy=native_policy,
        )

        invocation = _Invocation(
            backend=kind,
            store=self.session_store,
            session=session,
            attempt_id=attempt_id,
            config=config,
            tool_set=tool_set,
            executor=executor,
            token=token,
            before_step_messages=before_step_messages,
            workspace=workspace,
            native_policy=native_policy,
            native_authorization=registry.native_authorization,
            protected_paths=self.harness.protected_paths,
            finish_reminder=finish_reminder,
        )
        try:
            async with self.hub.connect() as hub:
                started = await hub.start(request)
                invocation.attach(hub, started.session_id)
                failed = False
                heartbeat = asyncio.create_task(self._heartbeat(hub, started.session_id))
                try:
                    invocation.record(
                        TraceType.TURN_STARTED,
                        {
                            "backend": kind,
                            "role": config.role.value,
                            "configuration_hash": self.harness.fingerprint,
                            "native_session": started.native_session,
                        },
                    )
                    await invocation.inject()
                    await hub.prompt(
                        started.session_id,
                        json_dumps(
                            {
                                "instruction": instruction,
                                "context": dict(config.context_builder(context)),
                            }
                        ),
                    )
                    return await invocation.follow()
                except BaseException:
                    failed = True
                    raise
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    # Do not leave the harness running, even when this caller is cancelled.
                    cleanup = asyncio.create_task(
                        self._close(hub, started.session_id, abort=failed)
                    )
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
        except (asyncio.CancelledError, AgentCancelledError):
            if session.has_turn(attempt_id):
                invocation.record(
                    TraceType.BACKEND_ERROR, {"backend": kind, "outcome": "cancelled_unknown"}
                )
            raise
        except (HubUnavailableError, HarnessProtocolError, OSError, TimeoutError) as failure:
            if allow_text_only and isinstance(failure, HarnessTextOnlyError):
                raise
            if session.has_turn(attempt_id):
                invocation.record(TraceType.BACKEND_ERROR, {"backend": kind, "outcome": "unknown"})
            raise HarnessExecutionUnknownError(
                "Harness invocation did not yield a confirmed result; "
                f"no prompt replay was attempted: {failure}"
            ) from failure
        finally:
            await executor.aclose()

    async def _heartbeat(self, hub: HubConnection, session_id: str) -> None:
        # Long host tool calls must not let the Hub reap a session it thinks was abandoned.
        # A failed heartbeat does not interrupt a running tool; the next Hub request reports
        # the failure and the invocation becomes an unknown outcome.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            with suppress(HubUnavailableError):
                await hub.heartbeat(session_id)

    async def _close(self, hub: HubConnection, session_id: str, *, abort: bool) -> None:
        # An unreachable Hub reaps the session itself after its idle timeout.
        with suppress(HubUnavailableError):
            await hub.close(session_id, abort=abort)


class _Invocation:
    """One role invocation: follows Hub events and keeps every EHAI fact on this side."""

    def __init__(
        self,
        *,
        backend: str,
        store: AgentTraceStore,
        session: AgentTrace,
        attempt_id: ID,
        config: AgentRoleConfig,
        tool_set: ToolSet,
        executor: ToolExecutor,
        token: CancellationToken,
        before_step_messages: BeforeStepMessages | None,
        workspace: Path,
        native_policy: NativePolicy | None,
        native_authorization: NativeAuthorization | None,
        protected_paths: tuple[Path, ...],
        finish_reminder: str | None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.session = session
        self.attempt_id = attempt_id
        self.config = config
        self.tool_set = tool_set
        self.executor = executor
        self.token = token
        self.before_step_messages = before_step_messages
        self.workspace = workspace.resolve()
        self.native_policy = native_policy
        self.native_authorization = native_authorization
        self.protected_paths = protected_paths
        self.finish_reminder = finish_reminder
        self.finish_reminder_used = False
        self.queued: dict[str, ModelMessage] = {}
        self.final_text: str | None = None
        self.step_pending = False
        self._hub: HubConnection | None = None
        self._session_id = ""

    def attach(self, hub: HubConnection, session_id: str) -> None:
        self._hub = hub
        self._session_id = session_id

    @property
    def hub(self) -> HubConnection:
        assert self._hub is not None
        return self._hub

    def record(self, kind: TraceType, payload: Mapping[str, JsonValue]) -> None:
        expected = len(self.session.events)
        event = self.session.append(self.attempt_id, kind, payload)
        self.store.append(self.session.agent_session_ref_id, expected, (event,))

    async def inject(self) -> None:
        if self.before_step_messages is None:
            return
        fresh: list[str] = []
        for message in self.before_step_messages(self.session.agent_session_ref_id):
            digest = hashlib.sha256(message.content.encode("utf-8")).hexdigest()
            if digest in self.queued:
                continue
            self.queued[digest] = message
            fresh.append(message.content)
        if fresh:
            await self.hub.steer(self._session_id, fresh)

    async def follow(self) -> str:
        cursor = 0
        pending: list[HubEvent] = []
        cancel_wait = asyncio.create_task(self.token.wait_cancelled())
        try:
            while True:
                if not pending:
                    batch = await self._next_batch(cursor, cancel_wait)
                    pending = list(batch.events)
                    if not pending and batch.state != "running":
                        raise HarnessProtocolError("Hub session ended without settling")
                    continue
                event = pending.pop(0)
                cursor = event.seq
                if isinstance(event, Settled):
                    if self.final_text is None:
                        if self.finish_reminder is not None and not self.finish_reminder_used:
                            self.finish_reminder_used = True
                            self.record(
                                TraceType.MESSAGE_RECEIVED,
                                {
                                    "content": self.finish_reminder,
                                    "role": "user",
                                    "delivery_boundary": "finish_reminder",
                                },
                            )
                            await self.hub.prompt(self._session_id, self.finish_reminder)
                            continue
                        raise HarnessTextOnlyError(
                            "Harness settled without a host-validated role result"
                        )
                    self.record(TraceType.FINAL, {"text": self.final_text})
                    self.record(TraceType.TURN_ENDED, {"backend": self.backend})
                    return self.final_text
                await self._handle(event)
        finally:
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _next_batch(self, cursor: int, cancel_wait: asyncio.Task[None]) -> EventBatch:
        fetch = asyncio.create_task(
            self.hub.events(self._session_id, after=cursor, wait=_EVENT_WAIT_SECONDS)
        )
        try:
            done, _ = await asyncio.wait((fetch, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                await self.hub.cancel(self._session_id)
                self.token.raise_if_cancelled()
            return await fetch
        finally:
            if not fetch.done():
                fetch.cancel()
                await asyncio.gather(fetch, return_exceptions=True)

    async def _handle(self, event: HubEvent) -> None:
        if isinstance(event, InputPrepared):
            for digest in event.input_hashes:
                message = self.queued.pop(digest, None)
                if message is not None:
                    self.record(
                        TraceType.MESSAGE_RECEIVED,
                        {
                            "content": message.content,
                            "role": "user",
                            "delivery_boundary": "native_provider_input_prepared",
                        },
                    )
                    if message.acknowledge is not None:
                        message.acknowledge()
        elif isinstance(event, AssistantMessage):
            self.step_pending = event.stop_reason in ("stop", "length", "toolUse")
            if event.text:
                safe_text = redact_sensitive_text(event.text)
                self.record(
                    TraceType.MODEL_MESSAGE,
                    {
                        "role": "assistant",
                        "text": safe_text[:4000],
                        "truncated": len(safe_text) > 4000,
                    },
                )
            if event.usage is not None:
                self.record(
                    TraceType.BACKEND_EVENT,
                    {"backend": self.backend, "type": "usage", "usage": event.usage},
                )
        elif isinstance(event, ToolCallEvent):
            await self._call_tool(event)
        elif isinstance(event, NativeAction):
            decision = (
                NativeDecision(approved=False, reason="Role result already submitted")
                if self.final_text is not None
                else _native_decision(
                    event,
                    workspace=self.workspace,
                    policy=self.native_policy,
                    authorization=self.native_authorization,
                    protected_paths=self.protected_paths,
                    allow_native_commands=self.config.allow_native_commands,
                )
            )
            self._record_native(event.model_dump(mode="json"))
            self._record_native(
                {"type": "native_decision", "action_id": event.action_id, **decision.model_dump()}
            )
            await self.hub.native_decision(self._session_id, event.action_id, decision)
        elif isinstance(event, NativeItem):
            self._record_native(event.model_dump(mode="json"))
        elif isinstance(event, Lifecycle):
            if event.name == "turn_end" and self.step_pending:
                self.record(
                    TraceType.BACKEND_EVENT, {"backend": self.backend, "type": "execution_step"}
                )
                self.step_pending = False
            self.record(TraceType.BACKEND_EVENT, {"backend": self.backend, "type": event.name})
        elif isinstance(event, Failed):
            raise HarnessProtocolError(event.reason)

    def _record_native(self, payload: Mapping[str, JsonValue]) -> None:
        safe = json_loads(redact_sensitive_text(json_dumps({"backend": self.backend, **payload})))
        assert isinstance(safe, dict)
        self.record(TraceType.BACKEND_EVENT, safe)

    async def _call_tool(self, event: ToolCallEvent) -> None:
        if self.final_text is not None:
            raise HarnessProtocolError("Unexpected tool request after result or from another lane")
        definition = self.tool_set.require(event.name)
        self.record(
            TraceType.TOOL_CALLED,
            {
                "call_id": event.call_id,
                "name": event.name,
                "arguments": event.arguments,
                "writes_workspace": definition.writes_workspace,
            },
        )
        error = False
        try:
            batch = event.batch_call_ids
            valid_finish = (
                bool(batch)
                and batch[-1] == event.call_id
                and (not self.config.final_tool_requires_only or len(batch) == 1)
            )
            if definition.ends_turn and not valid_finish:
                result: JsonValue = {
                    "accepted": False,
                    "error": "Submit the finish tool alone after other tools",
                }
            else:
                result = await self.executor.execute(
                    ToolCall(event.call_id, event.name, event.arguments), self.token
                )
        except RecoverableToolError as failure:
            error = True
            result = {"code": failure.code, "error": failure.safe_message}
        finish = (
            definition.ends_turn
            and not error
            and not (isinstance(result, dict) and result.get("accepted") is False)
        )
        self.record(
            TraceType.TOOL_ERROR if error else TraceType.TOOL_RESULT,
            {"call_id": event.call_id, "error" if error else "result": result},
        )
        if finish:
            self.final_text = json_dumps(result)
        else:
            # Queue context before releasing the native tool continuation.
            await self.inject()
        await self.hub.tool_result(
            self._session_id,
            ToolResult(call_id=event.call_id, result=result, is_error=error, finish=finish),
        )


def _native_decision(
    action: NativeAction,
    *,
    workspace: Path,
    policy: NativePolicy | None,
    authorization: NativeAuthorization | None,
    protected_paths: tuple[Path, ...],
    allow_native_commands: bool = True,
) -> NativeDecision:
    """Decide one native request from host authorization and the Attempt's sandbox."""
    if policy is None:
        return NativeDecision(approved=False, reason="Native tools are not enabled")
    if action.kind not in {"command", "file_change"}:
        return NativeDecision(approved=False, reason="Permission escalation or unknown operation")
    if action.kind == "command" and not allow_native_commands:
        return NativeDecision(approved=False, reason="Role cannot run native commands")
    workspace = workspace.resolve()
    try:
        cwd = (workspace / action.cwd).resolve() if action.cwd else workspace
        if action.kind == "file_change":
            if (
                policy.sandbox != "workspace-write"
                or authorization is None
                or not authorization.allow_workspace_write
            ):
                return NativeDecision(approved=False, reason="Role cannot write the workspace")
            if not action.paths or any(not path for path in action.paths):
                return NativeDecision(
                    approved=False, reason="File changes must identify their paths"
                )
            paths = tuple((cwd / path).resolve() for path in action.paths)
            protected = tuple(path.resolve() for path in protected_paths)
            approved = all(
                path.is_relative_to(workspace)
                and not any(
                    path.is_relative_to(root) or root.is_relative_to(path) for root in protected
                )
                for path in paths
            )
            return NativeDecision(
                approved=approved,
                reason="Workspace file changes"
                if approved
                else "Outside workspace or protected path",
            )
    except (OSError, ValueError, RuntimeError):
        return NativeDecision(approved=False, reason="Native paths could not be resolved")
    if not action.cwd or not cwd.is_relative_to(workspace):
        return NativeDecision(approved=False, reason="Command cwd must be inside the workspace")
    if not action.command or not action.command.strip():
        return NativeDecision(approved=False, reason="Command is missing")
    if policy.sandbox == "read-only" or (authorization is not None and authorization.has_shell):
        return NativeDecision(approved=True, reason="Read-only sandbox or authorized Shell")
    try:
        argv = tuple(shlex.split(action.command))
    except ValueError:
        return NativeDecision(approved=False, reason="Command could not be split into words")
    # A command-list or expansion cannot inherit the permission of its first git command.
    simple = not any(char in action.command for char in "\n\r;&|<>($`)")
    approved = authorization is not None and (
        authorization.allows_command(argv) or (simple and authorization.allows_git(argv))
    )
    return NativeDecision(
        approved=approved,
        reason="Authorized command or Git category" if approved else "Command is not authorized",
    )
