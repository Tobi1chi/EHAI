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
    "create-note": ("/notes", ()),
    "add-note-message": ("/notes/{note_id}/messages", ()),
    "decide-note": ("/notes/{note_id}/decisions", ()),
    "configure-project": ("/projects/{project_id}/configuration", ()),
    "register-event-consumer": ("/event-consumers", ("consumer_id",)),
    "read-consumer-events": ("/event-consumers/{consumer_id}/batches", ("limit",)),
    "ack-consumer-events": ("/event-consumers/{consumer_id}/ack", ("batch_token",)),
}
FILE_COMMANDS = frozenset({"create-note", "add-note-message", "decide-note", "configure-project"})
TOKEN_COMMANDS = frozenset(
    {"register-event-consumer", "read-consumer-events", "ack-consumer-events", *MANAGER_COMMANDS}
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
    for field in ("note_id", "consumer_id", "project_id", "run_id", "workspace_id"):
        if "{" + field + "}" in path:
            parser.add_argument("--" + field.replace("_", "-"), required=True)
