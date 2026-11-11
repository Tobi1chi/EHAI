"""Read-only attention projection over independent workspace core responses."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ehai import parse_utc_datetime, utc_now
from ehai.application.inbox import InboxKind
from ehai.infrastructure.workspace_supervisor import SupervisorError, WorkspaceSupervisor
from ehai.interfaces.workspace_models import WorkspaceId


class LifeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: WorkspaceId
    project_id: str = Field(min_length=1)


class LifeBindingData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: LifeBinding | None


class LifeBindingResponse(BaseModel):
    data: LifeBindingData


class AttentionDecision(BaseModel):
    kind: InboxKind
    request_id: str


class AttentionItem(BaseModel):
    kind: Literal["thread", "inbox", "life_task"]
    workspace_id: str
    project_id: str
    object_id: str
    label: Literal["life", "work"]
    title: str
    state: str
    next_action: str | None
    run_id: str | None
    at: AwareDatetime | None
    decisions: list[AttentionDecision] = Field(default_factory=list)


class AttentionUnavailable(BaseModel):
    workspace_id: str
    code: str
    message: str


class AttentionData(BaseModel):
    observed_at: AwareDatetime
    life_binding: LifeBinding | None
    needs_you: list[AttentionItem]
    in_progress: list[AttentionItem]
    done: list[AttentionItem]
    done_truncated: bool
    unavailable: list[AttentionUnavailable]
    notes: list[str]
    consistent_across_workspaces: Literal[False] = False


class AttentionResponse(BaseModel):
    data: AttentionData


def group_workspace_attention(
    workspace_id: str,
    threads: list[dict[str, Any]],
    inbox: list[dict[str, Any]],
    life_tasks: list[dict[str, Any]],
    binding: LifeBinding | None,
    observed_at: datetime,
) -> tuple[list[AttentionItem], list[AttentionItem], list[AttentionItem]]:
    """Pure grouping of one core's payloads; no reads or cross-core state inference."""
    needs_you: list[AttentionItem] = []
    in_progress: list[AttentionItem] = []
    done: list[AttentionItem] = []

    def add(item: AttentionItem, group: str) -> None:
        if group == "needs_you":
            needs_you.append(item)
        elif group == "in_progress":
            in_progress.append(item)
        elif item.at is not None and observed_at - timedelta(days=7) <= item.at <= observed_at:
            done.append(item)

    def label(project_id: str) -> Literal["life", "work"]:
        return (
            "life"
            if binding is not None
            and (workspace_id, project_id) == (binding.workspace_id, binding.project_id)
            else "work"
        )

    goal_ids = {thread["goal_id"] for thread in threads}
    for thread in threads:
        phase = thread["phase"]
        if phase in {"needs_attention", "awaiting_approval", "paused", "failed"}:
            group = "needs_you"
        elif phase in {"planning", "running", "idle"}:
            group = "in_progress"
        elif phase == "completed":
            group = "done"
        else:
            continue
        add(
            AttentionItem(
                kind="thread",
                workspace_id=workspace_id,
                project_id=thread["project_id"],
                object_id=thread["goal_id"],
                label=label(thread["project_id"]),
                title=thread["objective"],
                state=phase,
                next_action=thread["next_action"]["kind"],
                run_id=thread["run_id"],
                at=thread["last_activity_at"],
                decisions=[
                    AttentionDecision(kind=item["kind"], request_id=item["request_id"])
                    for item in sorted(
                        inbox,
                        key=lambda item: (
                            item["created_at"] or item["first_observed_at"] or "",
                            item["kind"],
                            item["request_id"],
                        ),
                    )
                    if item["pending"] and item["owner"]["goal_id"] == thread["goal_id"]
                ],
            ),
            group,
        )

    for item in inbox:
        owner = item["owner"]
        if not item["pending"] or owner["goal_id"] in goal_ids:
            continue
        needs_you.append(
            AttentionItem(
                kind="inbox",
                workspace_id=workspace_id,
                project_id=owner["project_id"],
                object_id=item["request_id"],
                label=label(owner["project_id"]),
                title=item["question"],
                state=item["kind"],
                next_action=item["next_step"],
                run_id=owner["run_id"],
                at=item["created_at"] or item["first_observed_at"],
            )
        )

    for task in life_tasks:
        status = task["status"]
        if status == "cancelled":
            continue
        due = None if task["due_at"] is None else parse_utc_datetime(task["due_at"])
        overdue = status == "open" and due is not None and due < observed_at
        add(
            AttentionItem(
                kind="life_task",
                workspace_id=workspace_id,
                project_id=task["project_id"],
                object_id=task["task_id"],
                label=label(task["project_id"]),
                title=task["title"],
                state="overdue" if overdue else status,
                next_action=None,
                run_id=None,
                at=task["updated_at"] if status == "done" else due,
            ),
            "done" if status == "done" else "needs_you" if overdue else "in_progress",
        )
    return needs_you, in_progress, done


async def read_attention(
    supervisor: WorkspaceSupervisor, client: httpx.AsyncClient, binding: LifeBinding | None
) -> AttentionData:
    """Read each workspace once, isolating failures and never retrying a core request."""
    observed_at = utc_now()
    slots = asyncio.Semaphore(4)

    async def summarize(workspace_id: str) -> AttentionData:
        result = AttentionData(
            observed_at=observed_at,
            life_binding=binding,
            needs_you=[],
            in_progress=[],
            done=[],
            done_truncated=False,
            unavailable=[],
            notes=[],
        )
        async with slots:
            try:
                endpoint = await supervisor.endpoint(workspace_id)

                async def read(path: str) -> Any:
                    response = await client.get(endpoint + path, timeout=10)
                    response.raise_for_status()
                    return response.json()["data"]

                threads = await read("/api/v1/threads")
                inbox = await read("/api/v1/inbox")
                tasks = []
                if binding is not None and binding.workspace_id == workspace_id:
                    try:
                        tasks = await read(
                            "/api/v1/projects/" + quote(binding.project_id, safe="") + "/life-tasks"
                        )
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code != 404:
                            raise
                        result.notes.append(f"life project not found in workspace {workspace_id}")
                result.needs_you, result.in_progress, result.done = group_workspace_attention(
                    workspace_id, threads["threads"], inbox["items"], tasks, binding, observed_at
                )
            except SupervisorError as error:
                result.unavailable.append(
                    AttentionUnavailable(
                        workspace_id=workspace_id, code=error.code, message=str(error)
                    )
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                result.unavailable.append(
                    AttentionUnavailable(
                        workspace_id=workspace_id,
                        code="workspace_query_failed",
                        message="Workspace query unavailable; no request was retried",
                    )
                )
        return result

    workspaces = await supervisor.list()
    results = await asyncio.gather(*(summarize(w.workspace_id) for w in workspaces))
    needs_you = [item for result in results for item in result.needs_you]
    in_progress = [item for result in results for item in result.in_progress]
    done = [item for result in results for item in result.done]
    needs_you.sort(key=lambda item: (item.at is None, item.at or observed_at))
    for items in (in_progress, done):
        items.sort(key=lambda item: (item.at is not None, item.at or observed_at), reverse=True)
    return AttentionData(
        observed_at=observed_at,
        life_binding=binding,
        needs_you=needs_you,
        in_progress=in_progress,
        done=done[:20],
        done_truncated=len(done) > 20,
        unavailable=[error for result in results for error in result.unavailable],
        notes=(["life binding not set"] if binding is None else [])
        + [note for result in results for note in result.notes],
    )
