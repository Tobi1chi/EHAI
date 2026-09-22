"""Own isolated local execution hosts without moving their execution state upstream."""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import httpx

from ehai import JsonValue, json_loads
from ehai.infrastructure.pi_config import PiBackendConfig
from ehai.interfaces.workspace_models import (
    PublicRuntimeSettings,
    WorkspaceDescriptor,
    WorkspaceRegistration,
)


class WorkspaceSupervisorError(ValueError):
    """Invalid registration or lifecycle request."""

    code = "invalid_workspace"
    status_code = 422


class WorkspaceConflict(WorkspaceSupervisorError):
    """Conflicting configuration, capacity, ownership, or active work."""

    code = "workspace_conflict"
    status_code = 409


class WorkspaceNotFound(WorkspaceSupervisorError):
    """Workspace ID is not registered in this manager."""

    code = "workspace_not_found"
    status_code = 404


SupervisorError = WorkspaceSupervisorError


class ProcessFileLock:
    """An OS-held lock released even when its owning process crashes."""

    def __init__(self, path: Path) -> None:
        self._file = path.open("a+b")
        if path.stat().st_size == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._file.close()
            raise WorkspaceConflict(
                "Another process owns this manager or workspace host"
            ) from error

    def close(self) -> None:
        self._file.close()


@dataclass
class _Entry:
    registration: WorkspaceRegistration
    pi_backend_path: str | None
    status: str = "registered"
    failure_category: str | None = None
    process: asyncio.subprocess.Process | None = None
    token: str | None = None
    url: str | None = None
    log: BinaryIO | None = None
    shutdown_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    start_owner: int | None = None


def _atomic_json(path: Path, document: object) -> None:
    import json

    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class WorkspaceSupervisor:
    """Manage child processes with fixed per-workspace profiles and bounded capacity."""

    def __init__(
        self,
        root: Path,
        *,
        max_worker_capacity: int = 8,
        max_planner_capacity: int = 8,
        startup_timeout_seconds: float = 30,
        shutdown_timeout_seconds: float = 15,
    ) -> None:
        if max_worker_capacity < 1 or max_planner_capacity < 1:
            raise ValueError("Manager capacity limits must be positive")
        self.root = root.expanduser().resolve()
        self._lock: ProcessFileLock | None = None
        self._mutex = asyncio.Lock()
        self._max_worker_capacity = max_worker_capacity
        self._max_planner_capacity = max_planner_capacity
        self._startup_timeout = startup_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._entries: dict[str, _Entry] = {}
        self._closed = False

    async def open(self) -> None:
        async with self._mutex:
            if self._closed:
                raise WorkspaceConflict("Workspace manager is closed")
            if self._lock is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self._lock = ProcessFileLock(self.root / "manager.lock")
            try:
                self._load_manifest()
            except BaseException:
                self._lock.close()
                self._lock = None
                raise

    def _load_manifest(self) -> None:
        try:
            manifest = self.root / "manifest.json"
            if manifest.exists():
                value = json_loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise WorkspaceSupervisorError("Unsupported workspace manager manifest")
                rows = value.get("workspaces")
                if not isinstance(rows, list):
                    raise WorkspaceSupervisorError("Invalid workspace manager manifest")
                for row in rows:
                    if not isinstance(row, dict):
                        raise WorkspaceSupervisorError("Invalid workspace manager registration")
                    registration = WorkspaceRegistration.model_validate(row.get("registration"))
                    pinned = row.get("pi_backend_path")
                    if pinned is not None and not isinstance(pinned, str):
                        raise WorkspaceSupervisorError("Invalid private backend reference")
                    self._entries[registration.workspace_id] = _Entry(registration, pinned)
        except BaseException:
            self._entries.clear()
            raise

    async def register(self, request: WorkspaceRegistration) -> WorkspaceDescriptor:
        try:
            return await self._register(request)
        except WorkspaceSupervisorError:
            raise
        except (OSError, ValueError) as error:
            raise WorkspaceSupervisorError(
                "Workspace directory or private Pi configuration is invalid"
            ) from error

    async def _register(self, request: WorkspaceRegistration) -> WorkspaceDescriptor:
        async with self._mutex:
            self._require_open()
            path = Path(request.path).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise WorkspaceSupervisorError("Workspace path must be an existing directory")
            self._validate_manager_separation(path)
            runtime = request.runtime
            if runtime.pi_config_path is not None:
                runtime = runtime.model_copy(
                    update={
                        "pi_config_path": str(
                            Path(runtime.pi_config_path).expanduser().resolve(strict=True)
                        )
                    }
                )
            request = request.model_copy(update={"path": str(path), "runtime": runtime})
            existing = self._entries.get(request.workspace_id)
            if existing is not None:
                if existing.registration != request:
                    raise WorkspaceConflict(
                        "Workspace ID already has a different immutable profile"
                    )
                return self._descriptor(existing)
            for entry in self._entries.values():
                other = Path(entry.registration.path)
                if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                    raise WorkspaceConflict("Workspace directories must not be equal or nested")
            backend = (
                None
                if runtime.pi_config_path is None
                else PiBackendConfig.from_path(Path(runtime.pi_config_path))
            )
            directory = self.root / "workspaces" / request.workspace_id
            directory.mkdir(parents=True, exist_ok=True)
            pinned_path = None
            if backend is not None:
                native_directory = directory / "pi-agent"
                native_directory.mkdir(exist_ok=True)
                for name, document in backend.native_documents().items():
                    _atomic_json(native_directory / name, document)
                document = backend.to_document()
                document["agent_dir"] = str(native_directory)
                pinned = directory / "pi-backend.json"
                _atomic_json(pinned, document)
                pinned_path = str(pinned)
            entry = _Entry(request, pinned_path)
            self._entries[request.workspace_id] = entry
            try:
                self._save_manifest()
            except BaseException:
                self._entries.pop(request.workspace_id)
                raise
            return self._descriptor(entry)

    async def list(self) -> list[WorkspaceDescriptor]:
        async with self._mutex:
            return [self._descriptor(entry) for entry in self._entries.values()]

    async def get(self, workspace_id: str) -> WorkspaceDescriptor:
        async with self._mutex:
            return self._descriptor(self._entry(workspace_id))

    async def endpoint(self, workspace_id: str) -> str:
        async with self._mutex:
            entry = self._entry(workspace_id)
            if self._descriptor(entry).status != "ready" or entry.url is None:
                raise WorkspaceConflict("Workspace host is not ready; explicitly start it first")
            return entry.url

    async def execution_config(self, workspace_id: str) -> dict[str, JsonValue] | None:
        async with self._mutex:
            entry = self._entry(workspace_id)
            if (
                self._descriptor(entry).status != "ready"
                or entry.url is None
                or entry.token is None
            ):
                raise WorkspaceConflict("Workspace host is not ready")
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            try:
                response = await client.get(
                    entry.url + "/_ehai_internal/execution-config",
                    headers={"X-EHAI-Child-Token": entry.token},
                )
                response.raise_for_status()
                document = json_loads(response.text)
            except (httpx.HTTPError, ValueError) as error:
                raise WorkspaceConflict(
                    "Workspace execution configuration is unavailable"
                ) from error
        if not isinstance(document, dict):
            raise WorkspaceConflict("Workspace returned invalid execution configuration")
        config = document.get("execution_config")
        if config is not None and not isinstance(config, dict):
            raise WorkspaceConflict("Workspace returned invalid execution configuration")
        return config

    async def start(self, workspace_id: str) -> WorkspaceDescriptor:
        try:
            return await self._start(workspace_id)
        except asyncio.CancelledError:
            entry = self._entries.get(workspace_id)
            if entry is not None and entry.start_owner == id(asyncio.current_task()):
                await asyncio.shield(self._shutdown(entry, force=True))
            raise

    async def _start(self, workspace_id: str) -> WorkspaceDescriptor:
        async with self._mutex:
            self._require_open()
            entry = self._entry(workspace_id)
            current = self._descriptor(entry)
            if current.status == "ready":
                return current
            if current.status in {"starting", "stopping"}:
                raise WorkspaceConflict("Workspace lifecycle operation is already in progress")
            try:
                current_path = Path(entry.registration.path).resolve(strict=True)
            except OSError as error:
                raise WorkspaceConflict("Registered workspace directory is unavailable") from error
            if str(current_path) != entry.registration.path or not current_path.is_dir():
                raise WorkspaceConflict("Registered workspace directory binding has changed")
            self._validate_manager_separation(current_path)
            reserved = [
                item
                for item in self._entries.values()
                if self._descriptor(item).status in {"starting", "ready", "stopping"}
            ]
            worker_total = sum(item.registration.runtime.worker_capacity for item in reserved)
            planner_total = sum(item.registration.runtime.planner_capacity for item in reserved)
            settings = entry.registration.runtime
            if (
                worker_total + settings.worker_capacity > self._max_worker_capacity
                or planner_total + settings.planner_capacity > self._max_planner_capacity
            ):
                raise WorkspaceConflict(
                    "Starting this workspace exceeds manager capacity reservations"
                )
            directory = self.root / "workspaces" / workspace_id
            directory.mkdir(parents=True, exist_ok=True)
            launch = directory / "child.json"
            _atomic_json(
                launch,
                {
                    "registration": entry.registration.model_dump(mode="json"),
                    "pi_backend_path": entry.pi_backend_path,
                },
            )
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
            entry.url = f"http://127.0.0.1:{port}"
            entry.token = secrets.token_urlsafe(32)
            # One workspace must not inherit credentials declared only by another.
            # Native Pi further narrows this environment for each model process.
            allowed_environment = {
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "PATH",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "APPDATA",
                "LOCALAPPDATA",
                "PROGRAMDATA",
                "LANG",
                "LC_ALL",
            }
            if entry.pi_backend_path is not None:
                backend = PiBackendConfig.from_path(Path(entry.pi_backend_path))
                allowed_environment.update(backend.environment_names)
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in allowed_environment
            }
            environment["EHAI_WORKSPACE_CHILD_TOKEN"] = entry.token
            entry.status, entry.failure_category = "starting", None
            entry.start_owner = id(asyncio.current_task())
            entry.log = (directory / "child.log").open("ab")
            try:
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "ehai.interfaces.workspace_child",
                        "--configuration",
                        str(launch),
                        "--port",
                        str(port),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=entry.log,
                        stderr=entry.log,
                        env=environment,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                )
                try:
                    entry.process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    entry.process = await spawning
                    raise
            except (OSError, ValueError):
                entry.status, entry.failure_category = "failed", "launch_failed"
                entry.log.close()
                entry.log = None
                return self._descriptor(entry)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            while asyncio.get_running_loop().time() < deadline:
                if entry.process is None or entry.process.returncode is not None or self._closed:
                    break
                try:
                    response = await client.get(
                        entry.url + "/_ehai_internal/identity",
                        headers={"X-EHAI-Child-Token": entry.token},
                    )
                    if response.status_code == 200:
                        identity = response.json()
                        if (
                            identity.get("workspace_id") != workspace_id
                            or identity.get("path") != entry.registration.path
                        ):
                            entry.failure_category = "identity_mismatch"
                            break
                        async with self._mutex:
                            if not self._closed:
                                entry.status = "ready"
                                return self._descriptor(entry)
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.1)
        async with self._mutex:
            entry.failure_category = entry.failure_category or (
                "child_exited"
                if entry.process is not None and entry.process.returncode is not None
                else "startup_timeout"
            )
        await self._shutdown(entry, force=True)
        entry.status = "failed"
        return self._descriptor(entry)

    async def stop(self, workspace_id: str) -> WorkspaceDescriptor:
        async with self._mutex:
            self._require_open()
            entry = self._entry(workspace_id)
            previous = self._descriptor(entry).status
            if previous in {"starting", "stopping"}:
                raise WorkspaceConflict("Workspace lifecycle operation is already in progress")
            entry.status = "stopping"
        try:
            await self._shutdown(entry, force=False)
        except WorkspaceConflict:
            if not self._closed:
                entry.status = previous
            raise
        return self._descriptor(entry)

    async def close(self) -> None:
        async with self._mutex:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._entries.values())
        try:
            await asyncio.gather(*(self._shutdown(entry, force=True) for entry in entries))
        finally:
            if self._lock is not None:
                self._lock.close()
                self._lock = None

    async def _shutdown(self, entry: _Entry, *, force: bool) -> None:
        async with entry.shutdown_lock:
            await self._shutdown_owned(entry, force=force)

    async def _shutdown_owned(self, entry: _Entry, *, force: bool) -> None:
        process = entry.process
        if process is None or process.returncode is not None:
            entry.status = "stopped"
            self._close_log(entry)
            return
        if entry.url is not None and entry.token is not None:
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    response = await client.post(
                        entry.url + "/_ehai_internal/shutdown",
                        headers={"X-EHAI-Child-Token": entry.token},
                        json={"force": force},
                    )
                if response.status_code == 409 and not force:
                    raise WorkspaceConflict(
                        "Workspace has active execution or planning; pause or finish it first"
                    )
                if not response.is_success and not force:
                    raise WorkspaceConflict("Workspace shutdown was not acknowledged")
            except httpx.HTTPError as error:
                if not force:
                    raise WorkspaceConflict(
                        "Workspace is unreachable; shutdown outcome is unknown"
                    ) from error
        entry.status = "stopping"
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
            entry.status = "stopped"
        except TimeoutError:
            if os.name == "nt":
                # The Windows venv executable is a redirector. Its owned process tree
                # contains the actual Python child; terminating only the wrapper leaks it.
                terminator = await asyncio.create_subprocess_exec(
                    str(
                        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
                        / "System32"
                        / "taskkill.exe"
                    ),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await asyncio.wait_for(terminator.wait(), timeout=5)
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
            entry.status, entry.failure_category = "failed", "shutdown_timeout"
        finally:
            self._close_log(entry)

    def _close_log(self, entry: _Entry) -> None:
        if entry.log is not None:
            entry.log.close()
            entry.log = None

    def _entry(self, workspace_id: str) -> _Entry:
        try:
            return self._entries[workspace_id]
        except KeyError as error:
            raise WorkspaceNotFound("Workspace ID is not registered") from error

    def _descriptor(self, entry: _Entry) -> WorkspaceDescriptor:
        if (
            entry.process is not None
            and entry.process.returncode is not None
            and entry.status in {"starting", "ready"}
        ):
            entry.status, entry.failure_category = "failed", "child_exited"
            self._close_log(entry)
        return WorkspaceDescriptor.model_validate(
            {
                "workspace_id": entry.registration.workspace_id,
                "path": entry.registration.path,
                "runtime": PublicRuntimeSettings.model_validate(
                    entry.registration.runtime.model_dump(exclude={"pi_config_path"})
                ),
                "status": entry.status,
                "failure_category": entry.failure_category,
            }
        )

    def _save_manifest(self) -> None:
        _atomic_json(
            self.root / "manifest.json",
            {
                "version": 1,
                "workspaces": [
                    {
                        "registration": entry.registration.model_dump(mode="json"),
                        "pi_backend_path": entry.pi_backend_path,
                    }
                    for entry in self._entries.values()
                ],
            },
        )

    def _require_open(self) -> None:
        if self._closed or self._lock is None:
            raise WorkspaceConflict("Workspace manager is not open")

    def _validate_manager_separation(self, path: Path) -> None:
        manager_root = self.root.resolve(strict=True)
        if (
            path == manager_root
            or path.is_relative_to(manager_root)
            or manager_root.is_relative_to(path)
        ):
            raise WorkspaceConflict(
                "Workspace and manager data directories must not be equal or nested; "
                "keep manager data outside every execution workspace"
            )
