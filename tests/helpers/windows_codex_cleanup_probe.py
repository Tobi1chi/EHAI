"""Job-isolated real-process probe for Windows Codex descendant cleanup."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ehai.application.workers import WorkerRequest

_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_SYNCHRONIZE = 0x00100000
_PROBE_TIMEOUT_SECONDS = 12.0


def _request() -> WorkerRequest:
    from ehai import new_id
    from ehai.application.workers import WorkerRequest
    from ehai.domain.execution import Attempt, Run
    from ehai.domain.goal import CompletionContract
    from ehai.domain.planning import PlanNode

    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    goal_id = new_id()
    run = Run(goal_id, new_id(), run_id=new_id(), created_at=now).start(at=now)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal_id,
        ("artifact:non-empty",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=now,
    ).confirm(confirmed_at=now)
    node = (
        PlanNode(
            new_id(),
            "probe descendant cleanup",
            "Wait for the controlled cleanup probe.",
            required_check_ids=(check_id,),
        )
        .mark_ready()
        .start()
    )
    attempt = Attempt(
        run.run_id,
        node.plan_node_id,
        1,
        attempt_id=new_id(),
        created_at=now,
    ).start(at=now)
    return WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        context={"probe": "windows-job-isolated"},
    )


def _open_process_for_wait(pid: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    return int(handle)


def _wait_for_process_exit(handle: int, timeout_milliseconds: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    result = kernel32.WaitForSingleObject(handle, timeout_milliseconds)
    if result != _WAIT_OBJECT_0:
        raise RuntimeError(f"descendant wait returned status {result}")


def _require_process_active(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    result = kernel32.WaitForSingleObject(handle, 0)
    if result != _WAIT_TIMEOUT:
        raise RuntimeError(f"descendant was not active before cleanup: status {result}")


def _close_handle(handle: int | None) -> None:
    if handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    if not kernel32.CloseHandle(handle):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def _wait_for_pid_record(path: Path) -> tuple[int, int]:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            time.sleep(0.01)
            continue
        root_pid = document.get("root_pid")
        descendant_pid = document.get("descendant_pid")
        if type(root_pid) is int and type(descendant_pid) is int:
            return root_pid, descendant_pid
        raise RuntimeError("PID record had invalid fields")
    raise TimeoutError("PID record was not created within the probe deadline")


def _validate_pids(root_pid: int, descendant_pid: int) -> None:
    current_pid = os.getpid()
    parent_pid = os.getppid()
    if root_pid <= 0 or descendant_pid <= 0:
        raise RuntimeError("probe PIDs must be positive")
    if root_pid == descendant_pid:
        raise RuntimeError("probe root and descendant PIDs must differ")
    if root_pid in {current_pid, parent_pid} or descendant_pid in {current_pid, parent_pid}:
        raise RuntimeError("probe PIDs must not target the controller or its parent")


def _run_controller(mode: str) -> int:
    if sys.stdin.readline().strip() != "start":
        print(json.dumps({"mode": mode, "result": "failed", "stage": "handshake"}))
        return 2

    from ehai.application.workers import WorkerCancelledError, WorkerTimedOutError
    from ehai.infrastructure.workers import CodexWorkerAdapter

    descendant_handle: int | None = None
    result: dict[str, str]
    return_code: int
    try:
        with tempfile.TemporaryDirectory(prefix="ehai-codex-cleanup-probe-") as temporary:
            workspace = Path(temporary)
            pid_record = workspace / "pids.json"
            request = _request()
            adapter = CodexWorkerAdapter(
                workspace=workspace,
                executable=(
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "fake-codex",
                    mode,
                    str(pid_record),
                ),
                timeout_seconds=2.0 if mode == "timeout" else 8.0,
                cancel_grace_seconds=0.2,
                max_output_bytes=4096,
            )
            failures: list[BaseException] = []

            def execute() -> None:
                try:
                    adapter.execute(request)
                except BaseException as error:
                    failures.append(error)

            thread = threading.Thread(
                target=execute,
                name="codex-cleanup-probe",
                daemon=True,
            )
            thread.start()
            root_pid, descendant_pid = _wait_for_pid_record(pid_record)
            _validate_pids(root_pid, descendant_pid)
            descendant_handle = _open_process_for_wait(descendant_pid)
            _require_process_active(descendant_handle)
            if not thread.is_alive():
                raise RuntimeError("Adapter finished before descendant handle was opened")
            if mode == "cancel":
                adapter.cancel(request.attempt_id)
            thread.join(timeout=_PROBE_TIMEOUT_SECONDS)
            if thread.is_alive():
                raise TimeoutError("Adapter did not finish within the probe deadline")
            expected_error = WorkerTimedOutError if mode == "timeout" else WorkerCancelledError
            if len(failures) != 1 or not isinstance(failures[0], expected_error):
                raise RuntimeError("Adapter returned an unexpected probe outcome")
            _wait_for_process_exit(descendant_handle, 5000)
        result = {
            "descendant_exit": "signaled",
            "mode": mode,
            "result": "passed",
            "worker_error": expected_error.__name__,
        }
        return_code = 0
    except BaseException as error:
        result = {
            "error_type": type(error).__name__,
            "mode": mode,
            "result": "failed",
        }
        return_code = 1
    try:
        _close_handle(descendant_handle)
    except BaseException as error:
        result = {
            "error_type": type(error).__name__,
            "mode": mode,
            "result": "failed",
        }
        return_code = 1
    print(json.dumps(result, sort_keys=True))
    return return_code


def _run_fake_codex(mode: str, pid_record: Path) -> int:
    descendant = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "descendant"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    pid_record.write_text(
        json.dumps({"descendant_pid": descendant.pid, "root_pid": os.getpid()}),
        encoding="utf-8",
    )
    time.sleep(60)
    return 0


def _run_descendant() -> int:
    time.sleep(60)
    return 0


def main() -> int:
    if os.name != "nt":
        return 3
    command = sys.argv[1]
    if command == "controller":
        return _run_controller(sys.argv[2])
    if command == "fake-codex":
        return _run_fake_codex(sys.argv[2], Path(sys.argv[3]))
    if command == "descendant":
        return _run_descendant()
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
