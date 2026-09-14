"""Normal CLI backend discovery, isolated from legacy model/Run assembly."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from ehai import JsonValue
from ehai.infrastructure.pi_config import (
    PI_VERSION,
    check_node_version,
    isolated_pi_environment,
    validate_pi_cli,
)
from ehai.infrastructure.pi_rpc import PiRpcError, PiRpcProcess


def resolve_planner_kind(kind: str | None, *, pi_configured: bool, model: str | None) -> str:
    """Explicit model/backend intent must not fall through to a scripted Planner."""
    if kind is not None:
        return kind
    return "pi" if pi_configured or model is not None else "single"


async def inspect_pi_backend(*, node: str, cli: Path) -> dict[str, JsonValue]:
    """Start upstream Pi without credentials, tools, prompts, or startup networking."""
    node_path = shutil.which(node)
    if node_path is None:
        raise ValueError("Node executable was not found; provide --node with an absolute path")
    node_file = Path(node_path).resolve(strict=True)
    if node_file.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("--node must select a native executable, not a shell script")
    cli = cli.resolve(strict=True)
    validate_pi_cli(cli)
    with TemporaryDirectory(prefix="ehai-pi-inspect-") as directory:
        root = Path(directory)
        environment = isolated_pi_environment(root, node_file)
        node_version = await check_node_version(node_file, environment)
        command = (
            str(node_file),
            str(cli),
            "--mode",
            "rpc",
            "--offline",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
        )
        async with PiRpcProcess(command, workspace=root, environment=environment) as rpc:
            response = await rpc.request("get_state")
            state = response.get("data")
            if not isinstance(state, dict):
                raise PiRpcError("Pi get_state did not return a state object")
            if state.get("isStreaming") is not False or state.get("messageCount") != 0:
                raise PiRpcError("Pi inspection unexpectedly entered an active conversation")
            if not isinstance(state.get("sessionId"), str) or not state["sessionId"]:
                raise PiRpcError("Pi did not provide a session identity")
            # Exercise correlated parallel control requests on the real public protocol.
            models, stats = await asyncio.gather(
                rpc.request("get_available_models"), rpc.request("get_session_stats")
            )
            model_data = models.get("data")
            if not isinstance(model_data, dict) or not isinstance(model_data.get("models"), list):
                raise PiRpcError("Pi did not return a model inventory")
            if not isinstance(stats.get("data"), dict):
                raise PiRpcError("Pi did not return session statistics")
            process_id = rpc.pid
        return {
            "backend": "pi",
            "version": PI_VERSION,
            "node_version": node_version,
            "protocol": "stdio-jsonl",
            "control_channel_verified": True,
            "model_execution_verified": False,
            "worker_integration_available": True,
            "model_requests": 0,
            "tools_enabled": False,
            "session_messages": 0,
            "process_id": process_id,
            "process_closed": True,
            "stderr_bytes": rpc.stderr_bytes,
        }
