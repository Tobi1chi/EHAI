"""CLI registration for note, event-consumer and project-configuration APIs."""

from __future__ import annotations

import argparse
from pathlib import Path

from ehai.interfaces.workspace_models import WORKSPACE_ID_PATTERN as WORKSPACE_ID_PATTERN

MANAGER_QUERIES = {
    "list-workspaces": "/workspaces",
    "get-workspace": "/workspaces/{workspace_id}",
    "get-workspace-execution-config": "/workspaces/{workspace_id}/execution-config",
    "get-workspace-overview": "/workspace-overview",
}
MANAGER_COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "register-workspace": ("/workspaces", ()),
    "start-workspace": ("/workspaces/{workspace_id}/start", ()),
    "stop-workspace": ("/workspaces/{workspace_id}/stop", ()),
}

QUERY_ROUTES = {
    **MANAGER_QUERIES,
    "get-planner-capacity": "/planning/capacity",
    "list-workflows": "/workflows",
    "get-routine-scheduler": "/routines/scheduler",
    "list-workflow-runs": "/projects/{project_id}/workflow-runs",
    "get-workflow-run": "/workflow-runs/{workflow_run_id}",
    "get-workflow-execution": "/workflow-runs/{workflow_run_id}/execution",
    "list-connectors": "/projects/{project_id}/connectors",
    "get-connector": "/connectors/{connector_id}",
    "list-connector-calls": "/connectors/{connector_id}/calls",
    "get-connector-call": "/connector-calls/{call_id}",
    "list-routing-labs": "/projects/{project_id}/routing-labs",
    "get-routing-lab": "/routing-labs/{lab_id}",
    "list-routing-requests": "/routing-labs/{lab_id}/requests",
    "get-routing-request": "/routing-requests/{request_id}",
    "list-routing-replays": "/routing-labs/{lab_id}/replays",
    "get-routing-replay": "/routing-replays/{replay_id}",
    "get-routing-metrics": "/routing-labs/{lab_id}/metrics",
    "list-life-tasks": "/projects/{project_id}/life-tasks",
    "get-life-task": "/life-tasks/{task_id}",
    "list-routines": "/projects/{project_id}/routines",
    "get-routine": "/routines/{routine_id}",
    "list-notes": "/notes",
    "get-note": "/notes/{note_id}",
    "get-event-consumer": "/event-consumers/{consumer_id}",
    "get-configuration-host": "/project-configuration-host",
    "get-project-configuration": "/projects/{project_id}/configuration",
    "get-project-configuration-versions": "/projects/{project_id}/configuration/versions",
    "get-run-configuration": "/runs/{run_id}/configuration",
}
COMMAND_ROUTES: dict[str, tuple[str, tuple[str, ...]]] = {
    **MANAGER_COMMANDS,
    "create-routing-lab": ("/projects/{project_id}/routing-labs", ()),
    "configure-routing-fallback": ("/routing-labs/{lab_id}/fallback", ()),
    "configure-routing-project": ("/routing-labs/{lab_id}/change-project", ()),
    "submit-routing-request": ("/routing-labs/{lab_id}/requests", ()),
    "advance-routing-lab": ("/routing-labs/{lab_id}/advance", ()),
    "resolve-routing-request": ("/routing-requests/{request_id}/resolve", ()),
    "record-routing-feedback": ("/routing-requests/{request_id}/feedback", ()),
    "propose-routing-recipe": ("/routing-labs/{lab_id}/candidates", ()),
    "start-routing-replay": ("/routing-labs/{lab_id}/replays", ()),
    "publish-routing-recipe": ("/routing-recipes/{recipe_id}/publish", ()),
    "pause-routing-recipe": ("/routing-recipes/{recipe_id}/pause", ()),
    "register-connector": ("/projects/{project_id}/connectors", ()),
    "invoke-connector": ("/connectors/{connector_id}/calls", ()),
    "reconcile-connector-call": ("/connector-calls/{call_id}/reconcile", ()),
    "start-workflow": ("/projects/{project_id}/workflow-runs", ()),
    "decide-workflow": ("/workflow-runs/{workflow_run_id}/decisions", ()),
    "update-life-task": ("/life-tasks/{task_id}", ()),
    "create-routine": ("/projects/{project_id}/routines", ()),
    "update-routine": ("/routines/{routine_id}", ()),
    "create-note": ("/notes", ()),
    "add-note-message": ("/notes/{note_id}/messages", ()),
    "decide-note": ("/notes/{note_id}/decisions", ()),
    "configure-project": ("/projects/{project_id}/configuration", ()),
    "register-event-consumer": ("/event-consumers", ("consumer_id",)),
    "read-consumer-events": ("/event-consumers/{consumer_id}/batches", ("limit",)),
    "ack-consumer-events": ("/event-consumers/{consumer_id}/ack", ("batch_token",)),
}
FILE_COMMANDS = frozenset(
    {
        "create-note",
        "add-note-message",
        "decide-note",
        "configure-project",
        "start-workflow",
        "decide-workflow",
        "update-life-task",
        "create-routine",
        "update-routine",
        "register-connector",
        "invoke-connector",
        "reconcile-connector-call",
        "create-routing-lab",
        "configure-routing-fallback",
        "configure-routing-project",
        "submit-routing-request",
        "resolve-routing-request",
        "record-routing-feedback",
        "propose-routing-recipe",
        "start-routing-replay",
        "publish-routing-recipe",
        "pause-routing-recipe",
    }
)
TOKEN_COMMANDS = frozenset(
    {
        "register-event-consumer",
        "read-consumer-events",
        "ack-consumer-events",
        "advance-routing-lab",
        *MANAGER_COMMANDS,
    }
)


def register_backend_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Keep all writes explicit; structured bodies use the same documented HTTP contract."""
    for name, path in QUERY_ROUTES.items():
        command = commands.add_parser(name, help="query the owning backend (requires --api-url)")
        _path_arguments(command, path)
        if name == "list-notes":
            for field in ("project-id", "goal-id", "run-id"):
                command.add_argument("--" + field)
            command.add_argument("--include-resolved", action="store_true")
    for name, (path, _) in COMMAND_ROUTES.items():
        command = commands.add_parser(name, help="call the owning backend (requires --api-url)")
        _path_arguments(command, path)
        if name == "register-workspace":
            command.add_argument(
                "--file", type=Path, required=True, help="WorkspaceRegistration JSON"
            )
        elif name in FILE_COMMANDS:
            command.add_argument(
                "--file",
                type=Path,
                required=True,
                help="JSON body; idempotency key supplied separately",
            )
            command.add_argument("--idempotency-key", required=True)
        elif name == "register-event-consumer":
            command.add_argument("--consumer-id", required=True)
        elif name == "read-consumer-events":
            command.add_argument("--limit", type=int, default=100)
        elif name == "ack-consumer-events":
            command.add_argument("--batch-token", required=True)


def _path_arguments(parser: argparse.ArgumentParser, path: str) -> None:
    for field in (
        "note_id",
        "consumer_id",
        "project_id",
        "run_id",
        "workspace_id",
        "workflow_run_id",
        "task_id",
        "routine_id",
        "connector_id",
        "call_id",
        "lab_id",
        "request_id",
        "recipe_id",
        "replay_id",
    ):
        if "{" + field + "}" in path:
            parser.add_argument("--" + field.replace("_", "-"), required=True)
