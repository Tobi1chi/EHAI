"""User configuration and local daemon commands; workspace ownership stays in the manager."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ehai.hub.adapters.pi.config import (
    PI_VERSION,
    PiBackendConfig,
    check_node_version,
    validate_pi_cli,
)
from ehai.infrastructure.workspace_supervisor import ProcessFileLock
from ehai.interfaces.workspace_models import (
    RuntimeSettings,
    WorkspaceDescriptor,
    WorkspaceRegistration,
)

LOCAL_COMMANDS = frozenset({"init", "up", "down", "status", "add", "doctor"})
_REPOSITORY = Path(__file__).resolve().parents[3]
Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Capacity = Annotated[int, Field(ge=1, le=64)]


class LocalConfig(BaseModel):
    """Editable defaults; registered workspace profiles remain immutable."""

    model_config = ConfigDict(extra="forbid")
    config_version: Literal[1] = 1
    provider: Nonempty
    model: Nonempty
    reasoning_effort: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"
    credential_env: list[str]
    node: Path
    pi_cli: Path
    agent_dir: Path
    worker_capacity: Capacity = 2
    planner_capacity: Capacity = 1
    max_worker_capacity: int = Field(default=8, ge=1)
    max_planner_capacity: int = Field(default=8, ge=1)
    port: int = Field(default=8765, ge=1, le=65535)
    ui_dir: Path

    def backend_document(self) -> dict[str, Any]:
        return {
            "node": str(self.node),
            "cli": str(self.pi_cli),
            "agent_dir": str(self.agent_dir),
            "provider": self.provider,
            "environment_names": self.credential_env,
        }


def write_private_json(path: Path, document: object) -> None:
    """Atomically write local JSON, restricting file access to its owner on POSIX."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        if sys.platform != "win32":
            temporary.chmod(0o600)
        json.dump(document, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    temporary.replace(path)


def _load_config(home: Path) -> LocalConfig:
    try:
        return LocalConfig.model_validate_json((home / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("No config.json; run ehai init first (check EHAI_HOME)") from error


def _prompt(label: str, default: str | None, supplied: str | None, yes: bool) -> str:
    if supplied is not None:
        return supplied
    if yes:
        if default is None:
            raise ValueError(f"{label} is required; supply --model for non-interactive init")
        return default
    if not sys.stdin.isatty():
        raise ValueError(
            "Missing init options on non-interactive stdin; supply all options or --yes"
        )
    answer = input(f"{label}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    if not answer and default is None:
        raise ValueError(f"{label} must not be empty")
    return answer or str(default)


def _node_version(node: Path) -> str:
    return asyncio.run(check_node_version(node, dict(os.environ)))


def _init(home: Path, args: argparse.Namespace) -> int:
    old = _load_config(home) if (home / "config.json").exists() else None
    node = shutil.which("node")
    if node is None:
        raise ValueError("Node not found on PATH; install Node >=22.19.0")
    node_path = Path(node).absolute()
    version = _node_version(node_path)
    cli = (
        _REPOSITORY
        / "agent-backends/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    )
    try:
        validate_pi_cli(cli)
    except ValueError as error:
        raise ValueError(
            "Install pinned Pi: npm ci --prefix agent-backends/pi "
            "--ignore-scripts --no-audit --no-fund"
        ) from error
    provider = _prompt("Provider", old.provider if old else "openai", args.provider, args.yes)
    model = _prompt("Model ID", old.model if old else None, args.model, args.yes)
    effort = _prompt(
        "Reasoning effort",
        old.reasoning_effort if old else "medium",
        args.reasoning_effort,
        args.yes,
    )
    names = args.credential_env
    if names is None:
        default = (
            ",".join(old.credential_env) if old else provider.upper().replace("-", "_") + "_API_KEY"
        )
        names = [
            name.strip()
            for name in _prompt(
                "Credential environment names (comma separated)", default, None, args.yes
            ).split(",")
            if name.strip()
        ]
    values: dict[str, Any] = {}
    for name, label, default_value in (
        ("worker_capacity", "Worker capacity per workspace", old.worker_capacity if old else 2),
        ("planner_capacity", "Planner capacity per workspace", old.planner_capacity if old else 1),
        ("port", "Manager port", old.port if old else 8765),
    ):
        supplied = getattr(args, "local_planner_capacity" if name == "planner_capacity" else name)
        values[name] = int(
            _prompt(
                label, str(default_value), None if supplied is None else str(supplied), args.yes
            )
        )
    config = LocalConfig.model_validate(
        {
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "credential_env": names,
            "node": node_path,
            "pi_cli": cli,
            "agent_dir": old.agent_dir if old else home / "pi",
            "ui_dir": old.ui_dir if old else _REPOSITORY / "control-plane/web/dist",
            "max_worker_capacity": max(
                old.max_worker_capacity if old else 8, values["worker_capacity"]
            ),
            "max_planner_capacity": max(
                old.max_planner_capacity if old else 8, values["planner_capacity"]
            ),
            **values,
        }
    )
    document = config.model_dump(mode="json")
    if old is not None:
        print(
            "".join(
                difflib.unified_diff(
                    (json.dumps(old.model_dump(mode="json"), indent=2) + "\n").splitlines(
                        keepends=True
                    ),
                    (json.dumps(document, indent=2) + "\n").splitlines(keepends=True),
                    fromfile="current config.json",
                    tofile="proposed config.json",
                )
            )
            or "Configuration unchanged."
        )
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError("Existing configuration requires confirmation; use --yes")
            if input("Save configuration? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("Configuration unchanged.")
                return 0
    # Validate before changing existing files; preserve custom native settings.
    with TemporaryDirectory(prefix="ehai-init-") as temporary:
        candidate = Path(temporary)
        for name in ("settings.json", "models.json"):
            source = config.agent_dir / name
            if not source.exists():
                source = _REPOSITORY / "agent-backends/pi/examples" / name
            shutil.copyfile(source, candidate / name)
        backend = config.backend_document()
        backend["agent_dir"] = str(candidate)
        PiBackendConfig.from_document(backend)
        config.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("settings.json", "models.json"):
            if not (config.agent_dir / name).exists():
                write_private_json(
                    config.agent_dir / name,
                    json.loads((candidate / name).read_text(encoding="utf-8")),
                )
    write_private_json(home / "config.json", document)
    write_private_json(home / "pi-backend.json", config.backend_document())
    print(f"Saved {home / 'config.json'}; Node {version}; Pi {PI_VERSION}.")
    print(
        f"Private Pi settings: {config.agent_dir} (existing settings preserved). "
        "No credential values saved."
    )
    print("Set credential variables before ehai up; then run ehai up and ehai add <path>.")
    return 0


def _record(home: Path) -> dict[str, Any] | None:
    path = home / "manager.pid"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _request(
    client: httpx.Client, record: dict[str, Any], method: str, path: str, *, body: object = None
) -> Any:
    response = client.request(
        method,
        f"http://127.0.0.1:{record['port']}" + path,
        headers={"X-EHAI-Manager-Token": record["token"]},
        json=body,
    )
    if not response.is_success:
        raise ValueError(f"Manager HTTP {response.status_code}: {response.text}")
    return response.json()


def _identity(client: httpx.Client, record: dict[str, Any]) -> bool:
    try:
        identity = _request(client, record, "GET", "/_ehai_local/identity")
    except httpx.ConnectError:
        return False
    if identity["pid"] != record["pid"]:
        raise ValueError(
            "Manager PID does not match manager.pid; inspect the daemon before retrying"
        )
    return True


def _workspaces(client: httpx.Client, record: dict[str, Any]) -> list[WorkspaceDescriptor]:
    data = _request(client, record, "GET", "/api/v1/workspaces")["data"]
    return [WorkspaceDescriptor.model_validate(row) for row in data["workspaces"]]


def _offline_registrations(home: Path) -> list[WorkspaceRegistration]:
    manifest = home / "manager/manifest.json"
    if not manifest.exists():
        return []
    document = json.loads(manifest.read_text(encoding="utf-8"))
    return [
        WorkspaceRegistration.model_validate(row["registration"]) for row in document["workspaces"]
    ]


def _show_workspace(workspace: WorkspaceDescriptor) -> None:
    print(
        f"  {workspace.workspace_id}: {workspace.status}"
        + (f" ({workspace.failure_category})" if workspace.failure_category else "")
        + f" — {workspace.path}"
    )


def _ui_address(config: LocalConfig, record: dict[str, Any]) -> None:
    print(f"UI: http://127.0.0.1:{record['port']}/ui/")
    if not (config.ui_dir / "index.html").is_file():
        print("UI is not built; run npm ci and npm run web:build in control-plane, then down/up.")


def _up(home: Path, config: LocalConfig, client: httpx.Client) -> int:
    record = _record(home)
    if record is not None and _identity(client, record):
        print(f"Manager already running (pid {record['pid']}).")
        _ui_address(config, record)
        return 0
    record = {
        "pid": 0,
        "port": config.port,
        "token": secrets.token_urlsafe(32),
        "credential_env": config.credential_env,
    }
    write_private_json(home / "manager.pid", record)
    argv = [
        sys.executable,
        "-m",
        "ehai.interfaces.workspace_api",
        "--data-dir",
        str(home / "manager"),
        "--port",
        str(config.port),
        "--max-worker-capacity",
        str(config.max_worker_capacity),
        "--max-planner-capacity",
        str(config.max_planner_capacity),
        "--local-control-file",
        str(home / "manager.pid"),
    ]
    if config.ui_dir.is_dir():
        argv.extend(["--ui-dir", str(config.ui_dir)])
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with (home / "manager.log").open("ab") as log:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError(
                f"Manager exited ({process.returncode}); inspect {home / 'manager.log'}"
            )
        # Read the actual server PID, including Windows venv redirectors.
        record = _record(home)
        if record is not None and record["pid"] and _identity(client, record):
            print(f"Manager ready (pid {record['pid']}); log: {home / 'manager.log'}")
            failed = False
            for workspace in _workspaces(client, record):
                try:
                    started = WorkspaceDescriptor.model_validate(
                        _request(
                            client,
                            record,
                            "POST",
                            f"/api/v1/workspaces/{workspace.workspace_id}/start",
                            body={},
                        )["data"]
                    )
                    _show_workspace(started)
                    failed |= started.status != "ready"
                except ValueError as error:
                    print(f"  {workspace.workspace_id}: {error}", file=sys.stderr)
                    failed = True
            _ui_address(config, record)
            return 1 if failed else 0
        time.sleep(0.1)
    raise ValueError(f"Manager startup unconfirmed; inspect {home / 'manager.log'} before retrying")


def _down(home: Path, client: httpx.Client) -> int:
    record = _record(home)
    if record is None or not _identity(client, record):
        print("Manager is not running.")
        return 0
    _request(client, record, "POST", "/_ehai_local/shutdown", body={})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not _identity(client, record):
            print("Manager stopped; registered workspaces retained.")
            return 0
        time.sleep(0.1)
    raise ValueError(
        "Manager shutdown unconfirmed; inspect manager.log and workspace status before retrying."
    )


def _add(
    home: Path,
    config: LocalConfig,
    client: httpx.Client,
    record: dict[str, Any],
    args: argparse.Namespace,
) -> int:
    path = args.path.expanduser().resolve(strict=True)
    workspace_id = args.workspace_id
    if workspace_id is None:
        workspace_id = (
            re.sub(r"[^a-z0-9_-]+", "-", path.name.lower()).strip("-_")[:64] or "workspace"
        )
        reserved = {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }
        if workspace_id in reserved:
            workspace_id = "workspace-" + workspace_id
    runtime = RuntimeSettings(
        worker_kind="pi",
        planner_kind="pi",
        worker_model=config.model,
        planner_model=config.model,
        worker_reasoning_effort=config.reasoning_effort,
        planner_reasoning_effort=config.reasoning_effort,
        worker_capacity=config.worker_capacity,
        planner_capacity=config.planner_capacity,
        pi_config_path=str(home / "pi-backend.json"),
        allowed_commands=[json.loads(value) for value in args.allow_command],
        git_permissions=args.git_permission,
        available_shells=args.shell,
    )
    registration = WorkspaceRegistration(workspace_id=workspace_id, path=str(path), runtime=runtime)
    PiBackendConfig.from_document(config.backend_document())
    write_private_json(home / "pi-backend.json", config.backend_document())
    _request(
        client, record, "POST", "/api/v1/workspaces", body=registration.model_dump(mode="json")
    )
    workspace = WorkspaceDescriptor.model_validate(
        _request(client, record, "POST", f"/api/v1/workspaces/{workspace_id}/start", body={})[
            "data"
        ]
    )
    _show_workspace(workspace)
    print(f"API: http://127.0.0.1:{record['port']}/workspaces/{workspace_id}")
    return 0 if workspace.status == "ready" else 1


def _doctor(home: Path, client: httpx.Client) -> int:
    passed = True

    def report(label: str, ok: bool, detail: str) -> None:
        nonlocal passed
        passed &= ok
        print(f"{'PASS' if ok else 'FAIL'} {label}: {detail}")

    config = None
    try:
        config = _load_config(home)
        report("configuration", True, str(home / "config.json"))
    except (OSError, ValueError) as error:
        report("configuration", False, f"{error}; run ehai init")
    if config is not None:
        try:
            report("Node", True, _node_version(config.node))
        except (OSError, ValueError, TimeoutError) as error:
            report("Node", False, f"{error}; install Node >=22.19.0 and rerun init")
        try:
            PiBackendConfig.from_document(config.backend_document())
            report("Pi", True, f"{PI_VERSION}; private settings valid")
        except (OSError, ValueError) as error:
            report("Pi", False, f"{error}; install pinned Pi and check private settings")
        for name in config.credential_env:
            exists = bool(os.environ.get(name))
            report(
                f"credential {name}",
                exists,
                "present in this terminal"
                if exists
                else "missing; set it before ehai up (down/up to refresh daemon environment)",
            )
    try:
        record = _record(home)
        running = record is not None and _identity(client, record)
        report("manager", running, "healthy" if running else "not running; run ehai up")
        if not running:
            for registration in _offline_registrations(home):
                report(
                    f"workspace {registration.workspace_id}",
                    False,
                    "health unavailable while manager is offline; run ehai up",
                )
        if running and record is not None:
            identity = _request(client, record, "GET", "/_ehai_local/identity")
            if config is not None:
                for name in config.credential_env:
                    exists = identity["credentials"].get(name, False)
                    report(
                        f"daemon credential {name}",
                        exists,
                        "present"
                        if exists
                        else "missing; set it in the launching terminal, then down/up",
                    )
            for workspace in _workspaces(client, record):
                try:
                    health = _request(
                        client,
                        record,
                        "GET",
                        f"/workspaces/{workspace.workspace_id}/api/v1/runtime/health",
                    )["data"]
                    ok = workspace.status == "ready" and health["status"] == "healthy"
                    report(
                        f"workspace {workspace.workspace_id}",
                        ok,
                        f"{workspace.status}; runtime {health['status']}",
                    )
                except (httpx.HTTPError, ValueError) as error:
                    report(
                        f"workspace {workspace.workspace_id}",
                        False,
                        f"{error}; inspect manager/workspaces/{workspace.workspace_id}/child.log "
                        "and explicitly start-workspace",
                    )
    except (httpx.HTTPError, OSError, ValueError) as error:
        report("manager", False, f"{error}; inspect manager.log and run ehai status")
    return 0 if passed else 1


def run_local(args: argparse.Namespace) -> int:
    """Dispatch local operations without requiring API or database CLI options."""
    home = Path(os.environ.get("EHAI_HOME", "~/.ehai")).expanduser().resolve()
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with httpx.Client(trust_env=False, timeout=60) as client:
            if args.command == "doctor":
                return _doctor(home, client)
            if args.command == "status":
                record = _record(home)
                if record is None or not _identity(client, record):
                    print("Manager is not running; run ehai up.")
                    for registration in _offline_registrations(home):
                        print(
                            f"  {registration.workspace_id}: health unavailable (manager offline)"
                        )
                    return 1
                print(f"Manager healthy (pid {record['pid']}, port {record['port']}).")
                for workspace in _workspaces(client, record):
                    _show_workspace(workspace)
                return 0
            lock = ProcessFileLock(home / "app.lock")
            try:
                if args.command == "init":
                    return _init(home, args)
                if args.command == "down":
                    return _down(home, client)
                config = _load_config(home)
                if args.command == "up":
                    return _up(home, config, client)
                record = _record(home)
                if record is None or not _identity(client, record):
                    raise ValueError("Manager is not running; run ehai up first")
                return _add(home, config, client, record, args)
            finally:
                lock.close()
    except (OSError, ValueError, httpx.HTTPError, TimeoutError, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; inspect ehai status before retrying.", file=sys.stderr)
        return 130
