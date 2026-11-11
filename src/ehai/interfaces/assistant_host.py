"""Manager-owned conversation lifecycle and HTTP-only child-core reader."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ehai import JsonValue, json_loads, new_id, utc_now
from ehai.application.agent_contracts import RecoverableToolError
from ehai.application.agent_roles import AgentRoleExecution
from ehai.application.agent_trace import AgentTraceEventType
from ehai.application.sanitization import redact_sensitive_text
from ehai.infrastructure.agent_traces import SQLiteAgentTraceStore
from ehai.infrastructure.assistant_role import (
    AssistantReader,
    AssistantRef,
    ReplyRef,
    assistant_role,
    validate_answer_text,
)
from ehai.infrastructure.harnesses import load_harness_file, resolve_harness_async
from ehai.infrastructure.hub_runtime import HarnessTextOnlyError, HubRoleRunner
from ehai.infrastructure.sqlite.database import SQLiteDatabase
from ehai.infrastructure.workspace_supervisor import (
    SupervisorError,
    WorkspaceConflict,
    WorkspaceSupervisor,
)
from ehai.interfaces.attention import LifeBindingData, read_attention
from ehai.interfaces.local_app import load_local_config, write_private_json

# The manager's Uvicorn logger is configured at INFO and captured in manager.log;
# an unconfigured module logger would silently discard successful turn outcomes.
_LOG = logging.getLogger("uvicorn.error.assistant")


class AssistantMessage(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    refs: list[AssistantRef]
    outcome: Literal["reply", "text_only", "failed"] | None
    error: str | None
    created_at: AwareDatetime


class AssistantView(BaseModel):
    available: bool
    unavailable_reason: str | None
    harness: Literal["pi", "codex"] | None
    model: str | None
    busy: bool
    messages: list[AssistantMessage]


class AssistantResponse(BaseModel):
    data: AssistantView


class SendAssistantMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[AssistantMessage] = Field(default_factory=list)


class ManagerAssistantReader:
    def __init__(
        self,
        supervisor: WorkspaceSupervisor,
        client: httpx.AsyncClient,
        binding: Callable[[], LifeBindingData],
    ) -> None:
        self.supervisor = supervisor
        self.client = client
        self.binding = binding

    async def core(self, workspace_id: str, path: str, *, optional: bool = False) -> Any:
        try:
            endpoint = await self.supervisor.endpoint(workspace_id)
            response = await self.client.get(endpoint + "/api/v1" + path, timeout=10)
            if optional and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()["data"]
        except SupervisorError as error:
            raise RecoverableToolError(error.code, str(error)) from error
        except httpx.HTTPStatusError as error:
            message = (
                "Object not found in the named workspace"
                if error.response.status_code == 404
                else "Workspace read failed; no request was retried"
            )
            raise RecoverableToolError("workspace_read_failed", message) from error
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise RecoverableToolError(
                "workspace_read_failed", "Workspace read unavailable; no request was retried"
            ) from error

    async def read_attention(self) -> dict[str, JsonValue]:
        result = await read_attention(self.supervisor, self.client, self.binding().binding)
        return cast(dict[str, JsonValue], result.model_dump(mode="json"))

    async def read_thread(self, workspace_id: str, goal_id: str) -> dict[str, JsonValue]:
        path = "/threads/" + quote(goal_id, safe="")
        thread = await self.core(workspace_id, path)
        items: list[Any] = []
        after = 0
        while True:
            page = await self.core(workspace_id, f"{path}/timeline?limit=100&after={after}")
            items = (items + page["items"])[-15:]
            next_after = page["next_after"]
            if next_after is None:
                break
            if next_after <= after:
                raise RecoverableToolError(
                    "invalid_timeline", "Timeline pagination did not advance"
                )
            after = next_after
        return {"thread": thread, "timeline": items}

    async def read_run(self, workspace_id: str, run_id: str) -> dict[str, JsonValue]:
        path = "/runs/" + quote(run_id, safe="")
        run = await self.core(workspace_id, path)
        plan = await self.core(workspace_id, path + "/plan")
        document = await self.core(workspace_id, path + "/result", optional=True)
        result = None if document is None else document["result"]
        summary = None
        if result is not None:
            check = result["check_result"]
            summary = {
                "commit": result["commit"],
                "check_passed": None if check is None else check["passed"],
                "checks": []
                if check is None
                else [
                    {
                        "check_id": item["check_id"],
                        "status": item["status"],
                        "passed": None if item["result"] is None else item["result"]["passed"],
                    }
                    for item in check["checks"]
                ],
                "has_diff": bool(result["diff_path"] or result["diff_artifact_ids"]),
                "diff_statistics": await self.diff_statistics(workspace_id, path),
            }
        return {
            "run": {key: run[key] for key in ("run_id", "status", "status_reason")},
            "nodes": [{"title": node["title"], "status": node["status"]} for node in plan["nodes"]],
            "result": summary,
        }

    async def diff_statistics(self, workspace_id: str, run_path: str) -> dict[str, JsonValue]:
        try:
            return await self._diff_statistics(workspace_id, run_path)
        except Exception:
            return {"available": False, "reason": "diff could not be read"}

    async def _diff_statistics(self, workspace_id: str, run_path: str) -> dict[str, JsonValue]:
        integrations = await self.core(workspace_id, run_path + "/integrations")
        if not integrations:
            return {"available": False, "reason": "not yet integrated"}
        latest = integrations[-1]
        if not latest.get("diff_path"):
            return {"available": False, "reason": "latest integration has no complete diff"}
        diff = await self.core(
            workspace_id,
            run_path + "/integrations/" + quote(latest["integration_id"], safe="") + "/diff",
        )
        if diff["truncated"]:
            return {"available": False, "reason": "integration diff is truncated"}
        # Never expose diff text to the model, or read a child-local diff_path.
        files = added = removed = 0
        in_hunk = False
        for line in diff["content"].splitlines():
            if line.startswith("diff --git "):
                files += 1
                in_hunk = False
            elif line.startswith("@@ "):
                in_hunk = True
            elif in_hunk:
                added += line.startswith("+")
                removed += line.startswith("-")
        return {
            "available": True,
            "files": files,
            "added": added,
            "removed": removed,
        }

    async def read_life_tasks(self) -> dict[str, JsonValue]:
        binding = self.binding().binding
        if binding is None:
            return {"reason": "no life binding", "open": [], "done": []}
        tasks = await self.core(
            binding.workspace_id, "/projects/" + quote(binding.project_id, safe="") + "/life-tasks"
        )
        done = sorted(
            (task for task in tasks if task["status"] == "done"),
            key=lambda task: task["updated_at"],
            reverse=True,
        )[:5]
        return {
            "workspace_id": binding.workspace_id,
            "project_id": binding.project_id,
            "open": [task for task in tasks if task["status"] == "open"],
            "done": done,
        }

    async def validate_ref(self, ref: ReplyRef) -> AssistantRef:
        path_id = quote(ref.object_id, safe="")
        inbox_kind = None
        if ref.kind == "thread":
            await self.core(ref.workspace_id, "/threads/" + path_id)
        elif ref.kind == "run":
            await self.core(ref.workspace_id, "/runs/" + path_id)
        elif ref.kind == "diff":
            document = await self.core(ref.workspace_id, "/runs/" + path_id + "/result")
            result = document["result"]
            if not (result["diff_path"] or result["diff_artifact_ids"]):
                raise RecoverableToolError("no_diff", "This run has no diff to reference")
        elif ref.kind == "inbox":
            inbox = await self.core(ref.workspace_id, "/inbox")
            item = next(
                (
                    item
                    for item in inbox["items"]
                    if item["request_id"] == ref.object_id and item["pending"]
                ),
                None,
            )
            if item is None:
                raise RecoverableToolError(
                    "inbox_not_pending",
                    "Only pending inbox items can be referenced; it may already be resolved",
                )
            inbox_kind = item["kind"]
        else:
            binding = self.binding().binding
            if binding is None or binding.workspace_id != ref.workspace_id:
                raise RecoverableToolError(
                    "no_life_binding", "Reference must belong to the bound life project"
                )
            task = await self.core(ref.workspace_id, "/life-tasks/" + path_id)
            if task["project_id"] != binding.project_id:
                raise RecoverableToolError(
                    "wrong_life_project", "Task is not in the bound life project"
                )
        return AssistantRef(**ref.model_dump(), inbox_kind=inbox_kind)


class AssistantHost:
    def __init__(self, home: Path | None) -> None:
        self.home = home
        self.conversation = Conversation()
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.store: SQLiteAgentTraceStore | None = None

    @property
    def root(self) -> Path:
        assert self.home is not None
        return self.home / "assistant"

    def open(self) -> None:
        if self.home is None:
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)
        (self.root / "workspace").mkdir(exist_ok=True)
        (self.root / "state").mkdir(exist_ok=True)
        self.store = SQLiteAgentTraceStore(SQLiteDatabase(self.root / "trace.sqlite"))
        with suppress(FileNotFoundError):
            self.conversation = Conversation.model_validate_json(
                (self.root / "conversation.json").read_text(encoding="utf-8")
            )
        if self.conversation.messages and self.conversation.messages[-1].role == "user":
            self.append_answer("", [], "failed", "interrupted by restart")
            self.save()
            _LOG.info("assistant turn outcome=failed reason=interrupted by restart")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def save(self) -> None:
        write_private_json(
            self.root / "conversation.json", self.conversation.model_dump(mode="json")
        )

    def view(self) -> AssistantView:
        harness: Literal["pi", "codex"] | None = None
        model = None
        reason = (
            "Assistant requires a manager started with ehai up (--local-home)"
            if self.home is None
            else None
        )
        if self.home is not None:
            try:
                config = load_local_config(self.home)
                harness = config.default_harness
                if harness == "codex":
                    if config.codex is None:
                        raise ValueError("Codex configuration is missing")
                    model = config.codex.model
                else:
                    model = config.model
            except (OSError, ValueError):
                reason = "Local assistant configuration could not be read; check ehai init"
        return AssistantView(
            available=reason is None,
            unavailable_reason=reason,
            harness=harness,
            model=model,
            busy=self.lock.locked(),
            messages=list(self.conversation.messages),
        )

    def require_idle(self) -> None:
        view = self.view()
        if not view.available:
            raise WorkspaceConflict(view.unavailable_reason or "Assistant unavailable")
        if self.lock.locked():
            raise WorkspaceConflict("Assistant is answering; wait for this turn to finish")

    async def send(self, text: str, reader: AssistantReader) -> AssistantView:
        self.require_idle()
        await self.lock.acquire()
        try:
            message = AssistantMessage(
                message_id=str(new_id()),
                role="user",
                text=redact_sensitive_text(text),
                refs=[],
                outcome=None,
                error=None,
                created_at=utc_now(),
            )
            self.conversation.messages.append(message)
            try:
                self.save()
            except BaseException:
                self.conversation.messages.pop()
                raise
            self.task = asyncio.create_task(self.turn(reader))
            return self.view()
        except BaseException:
            self.lock.release()
            raise

    async def reset(self) -> AssistantView:
        self.require_idle()
        async with self.lock:
            previous = self.conversation
            self.conversation = Conversation()
            try:
                self.save()
            except BaseException:
                self.conversation = previous
                raise
        return self.view()

    def append_answer(
        self,
        text: str,
        refs: list[AssistantRef],
        outcome: Literal["reply", "text_only", "failed"],
        error: str | None,
    ) -> None:
        self.conversation.messages.append(
            AssistantMessage(
                message_id=str(new_id()),
                role="assistant",
                text=text,
                refs=refs,
                outcome=outcome,
                error=error,
                created_at=utc_now(),
            )
        )

    async def turn(self, reader: AssistantReader) -> None:
        try:
            async with asyncio.timeout(300):
                await self.execute(reader)
        except asyncio.CancelledError:
            self.append_answer("", [], "failed", "interrupted by shutdown")
            raise
        except Exception as error:
            reason = type(error).__name__ + ": " + redact_sensitive_text(str(error))[:180]
            self.append_answer("", [], "failed", reason)
        finally:
            try:
                self.save()
                last = self.conversation.messages[-1]
                _LOG.info("assistant turn outcome=%s error=%s", last.outcome, last.error)
            except Exception:
                _LOG.exception(
                    "Assistant conversation could not be saved; no turn will be replayed"
                )
            finally:
                self.lock.release()

    async def execute(self, reader: AssistantReader) -> None:
        assert self.home is not None and self.store is not None
        config = load_local_config(self.home)
        if config.default_harness == "codex":
            if config.codex is None:
                raise ValueError("Codex configuration is missing")
            harness = await asyncio.to_thread(load_harness_file, self.home / "codex-harness.json")
            model, effort = config.codex.model, config.codex.reasoning_effort
        else:
            harness = await resolve_harness_async("pi", config.backend_document())
            model, effort = config.model, config.reasoning_effort
        if harness.kind != config.default_harness:
            raise ValueError("Configured harness does not match the selected assistant harness")
        runner = HubRoleRunner(
            self.store,
            harness=harness,
            state_root=self.root / "state" if harness.kind == "pi" else None,
        )
        session = runner.create_session()
        execution = AgentRoleExecution(session.agent_session_ref_id)
        role, registry = assistant_role(reader)
        context: dict[str, JsonValue] = {
            "local_time": datetime.now().astimezone().isoformat(),
            "messages": [
                {
                    "role": message.role,
                    "text": message.text,
                    "refs": [ref.model_dump(mode="json") for ref in message.refs],
                }
                for message in self.conversation.messages[-10:]
            ],
        }
        try:
            result = await runner.run(
                config=role,
                registry=registry,
                session=session,
                execution=execution,
                instruction=(
                    "Read current facts, then answer the latest user message by calling the reply "
                    "tool with text and refs. Plain text without reply is discarded."
                ),
                context=context,
                model=model,
                reasoning_effort=effort,
                workspace=self.root / "workspace",
                allow_text_only=True,
                finish_reminder=(
                    "You ended without calling reply, so the user saw nothing. Call reply now, "
                    "alone, with your answer text and refs for the items you mentioned."
                ),
            )
            value = json_loads(result)
            if not isinstance(value, dict):
                raise ValueError("Invalid role result")
            text = value["text"]
            refs = value["refs"]
            if not isinstance(text, str) or not isinstance(refs, list):
                raise ValueError("Invalid role result")
            self.append_answer(
                text, [AssistantRef.model_validate(ref) for ref in refs], "reply", None
            )
        except HarnessTextOnlyError:
            trace = self.store.load(session.agent_session_ref_id)
            messages = [
                event
                for event in trace.events
                if event.attempt_id == execution.attempt_id
                and event.type is AgentTraceEventType.MODEL_MESSAGE
                and event.payload.get("role") == "assistant"
            ]
            text = messages[-1].payload.get("text") if messages else None
            try:
                if not isinstance(text, str) or messages[-1].payload.get("truncated"):
                    raise ValueError("No complete assistant text")
                validate_answer_text(text)
            except ValueError:
                self.append_answer("", [], "failed", "the answer did not follow the reply format")
            else:
                self.append_answer(text, [], "text_only", None)
