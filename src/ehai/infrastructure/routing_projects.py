"""Hand off engineering work to an explicitly bound, separate local Project host."""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ehai import JsonValue, json_loads, normalize_id, utc_now
from ehai.application.ports import StateConflictError
from ehai.application.routing_lab import RoutingLabService
from ehai.application.routing_models import (
    RoutingProjectBinding,
    RoutingProjectChange,
    RoutingRequest,
)

_PROJECT_PROMPT = """Prepare a concrete code/workflow-branch change plan for this target Project.
Inspect the requested files and applicable project guidelines first. Keep investigation scoped
to the requested behavior; use the provided planning tool contracts rather than investigating
framework internals to discover how those tools work. When requirements are clear, construct
the design and graph and call finish_plan: it submits a draft, not approval or execution.
Plan work only in authorized candidate worktrees; do not modify any
running EHAI installation, private settings or deployment. Preserve the normal explicit plan
approval, execution authorization and human final acceptance. Do not approve, start or publish.
"""


def _data(response: httpx.Response) -> dict[str, JsonValue]:
    if response.status_code >= 400:
        raise StateConflictError(f"Target Project API returned HTTP {response.status_code}")
    body = json_loads(response.text)
    if not isinstance(body, dict):
        raise ValueError("Target Project API returned an invalid response")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("Target Project API returned an invalid response")
    return data


class RoutingProjectBoundary:
    def __init__(self, protected_roots: tuple[Path, ...]) -> None:
        self.protected_roots = tuple(p.resolve() for p in protected_roots)

    @staticmethod
    def base_url(url: str) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/api/v1"}
        ):
            raise ValueError("Change Project must use a local core API URL without credentials")
        return parsed._replace(path="", query="", fragment="").geturl().rstrip("/")

    def resolve(self, api_url: str, project_id: str) -> RoutingProjectBinding:
        base = self.base_url(api_url)
        identity = str(normalize_id(project_id))
        with httpx.Client(base_url=base + "/api/v1/", trust_env=False, timeout=15) as client:
            project = _data(client.get("projects/" + identity)).get("project")
            runtime = _data(client.get("runtime/context"))
        if not isinstance(project, dict) or project.get("project_id") != identity:
            raise ValueError("Target Project identity did not match")
        workspace = runtime.get("workspace")
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise ValueError("Target core must expose its explicit local workspace")
        path = Path(workspace).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Target Project workspace is not a directory")
        if any(
            path == root or path.is_relative_to(root) or root.is_relative_to(path)
            for root in self.protected_roots
        ):
            raise StateConflictError(
                "Target overlaps the running framework or private state; "
                "use an independent checkout"
            )
        return RoutingProjectBinding(api_url=base, project_id=identity, workspace=str(path))

    def verify(self, target: RoutingProjectBinding) -> None:
        if self.resolve(target.api_url, target.project_id) != target:
            raise StateConflictError("Target Project workspace changed since it was authorized")


class RoutingProjectDispatcher:
    def __init__(
        self,
        labs: RoutingLabService,
        boundary: RoutingProjectBoundary,
        timeout_seconds: float,
    ) -> None:
        self.labs = labs
        self.boundary = boundary
        self.timeout_seconds = timeout_seconds

    def _claim(self, request_id: str) -> tuple[RoutingRequest, RoutingProjectChange] | None:
        with self.labs.store.transaction(write=True) as tx:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            fallback = record.fallback
            if fallback is None or fallback.project_change is None:
                return None
            change = fallback.project_change
            if fallback.status != "completed" or change.status != "pending":
                return None
            change = change.model_copy(update={"status": "dispatching"})
            self.labs._save_request(
                tx,
                record.model_copy(
                    update={
                        "fallback": fallback.model_copy(update={"project_change": change}),
                        "updated_at": utc_now(),
                    }
                ),
            )
            return record, change

    def _save(self, request_id: str, change: RoutingProjectChange) -> None:
        with self.labs.store.transaction(write=True) as tx:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            if record.fallback is None:
                raise StateConflictError("Missing project handoff owner")
            self.labs._save_request(
                tx,
                record.model_copy(
                    update={
                        "fallback": record.fallback.model_copy(update={"project_change": change}),
                        "updated_at": utc_now(),
                    }
                ),
            )

    async def dispatch(self, request_id: str) -> None:
        claimed = self._claim(request_id)
        if claimed is None:
            return
        record, change = claimed
        assert record.fallback is not None
        key = "routing-project:" + record.fallback.attempt_id
        target = change.target
        write_started = False
        try:
            await asyncio.to_thread(self.boundary.verify, target)
            async with httpx.AsyncClient(
                base_url=target.api_url + "/api/v1/",
                trust_env=False,
                timeout=self.timeout_seconds,
            ) as client:
                write_started = True
                goal = _data(
                    await client.post(
                        "goals",
                        json={
                            "idempotency_key": key + ":goal",
                            "project_id": target.project_id,
                            "objective": change.objective,
                        },
                    )
                )
                goal_id = goal.get("goal_id")
                if not isinstance(goal_id, str):
                    raise ValueError("Target did not return a Goal identity")
                change = change.model_copy(update={"goal_id": str(normalize_id(goal_id))})
                self._save(request_id, change)
                await asyncio.to_thread(self.boundary.verify, target)
                discussion = _data(
                    await client.post(
                        "planning/discuss",
                        json={
                            "idempotency_key": key + ":plan",
                            "goal_id": change.goal_id,
                            "message": (
                                _PROJECT_PROMPT
                                + "Originating request: "
                                + record.request_id
                                + ".\n"
                                "Requested change:\n" + change.objective
                            ),
                        },
                    )
                )
                conversation_id = discussion.get("conversation_id")
                turns = discussion.get("turns")
                if (
                    discussion.get("goal_id") != change.goal_id
                    or not isinstance(conversation_id, str)
                    or not isinstance(turns, list)
                    or not turns
                    or not isinstance(turns[-1], dict)
                ):
                    raise ValueError("Target planning response did not match the Goal")
                plan_id = turns[-1].get("plan_revision_id")
                change = change.model_copy(
                    update={
                        "conversation_id": str(normalize_id(conversation_id)),
                        "plan_revision_id": None
                        if plan_id is None
                        else str(normalize_id(str(plan_id))),
                        "status": "waiting_approval" if plan_id is not None else "needs_input",
                    }
                )
                self._save(request_id, change)
        except asyncio.CancelledError:
            self._save(
                request_id,
                change.model_copy(
                    update={
                        "status": "unknown",
                        "error": "host_shutdown_during_handoff",
                    }
                ),
            )
            raise
        except Exception as error:
            self._save(
                request_id,
                change.model_copy(
                    update={
                        "status": "unknown" if write_started else "failed",
                        "error": type(error).__name__,
                    }
                ),
            )
