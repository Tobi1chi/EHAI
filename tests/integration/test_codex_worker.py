from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ehai import new_id
from ehai.application.workers import (
    WorkerAdapter,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerTimedOutError,
)
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode
from ehai.infrastructure.workers import CodexWorkerAdapter

NOW = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)

_FAKE_CODEX = r"""
import json
import os
import pathlib
import signal
import sys
import time

mode = sys.argv[1]
record_path = pathlib.Path(sys.argv[2])
args = sys.argv[3:]
prompt = sys.stdin.read()

def option(name):
    return args[args.index(name) + 1]

schema_path = pathlib.Path(option("--output-schema"))
last_message_path = pathlib.Path(option("--output-last-message"))
record_path.write_text(
    json.dumps(
        {
            "args": args,
            "prompt": prompt,
            "schema": json.loads(schema_path.read_text(encoding="utf-8")),
            "environment": {
                name: name in os.environ
                for name in (
                    "OPENAI_API_KEY",
                    "SERVICE_TOKEN",
                    "EHAI_SECRET",
                    "DATABASE_PASSWORD",
                    "AUTHORIZED_TOKEN",
                )
            },
        }
    ),
    encoding="utf-8",
)

if mode in {"timeout", "cancel"}:
    print("token=partial-stdout-secret", flush=True)
    print("password=partial-stderr-secret", file=sys.stderr, flush=True)
    time.sleep(60)

if mode == "nonzero":
    print("token=stderr-secret", file=sys.stderr, flush=True)
    raise SystemExit(7)

if mode == "missing":
    print(json.dumps({"type": "future.event"}), flush=True)
    raise SystemExit(0)

if mode == "invalid":
    last_message_path.write_text("not json", encoding="utf-8")
    raise SystemExit(0)

content = "hello"
summary = "candidate prepared"
if mode == "env-echo":
    content = os.environ["AUTHORIZED_TOKEN"]
    summary = os.environ["AUTHORIZED_TOKEN"]
    print(os.environ["AUTHORIZED_TOKEN"], flush=True)
    print(os.environ["AUTHORIZED_TOKEN"], file=sys.stderr, flush=True)
elif mode == "redact":
    content = "token=candidate-secret Bearer candidate-bearer sk-candidatekey"
    summary = "password=summary-secret"
    print("x" * 5000, flush=True)
    print("secret=stdout-secret", flush=True)
    print("Authorization: Bearer stderr-bearer", file=sys.stderr, flush=True)
    print("api_key=stderr-key", file=sys.stderr, flush=True)
elif mode == "flood":
    print("x" * 2_000_000, flush=True)
    print("y" * 2_000_000, file=sys.stderr, flush=True)
else:
    print(json.dumps({"type": "future.event", "payload": {"ignored": True}}), flush=True)
    if mode != "quiet":
        print("ordinary diagnostic", file=sys.stderr, flush=True)

last_message_path.write_text(
    json.dumps(
        {
            "summary": summary,
            "artifacts": [
                {
                    "kind": "candidate",
                    "name": "result.txt",
                    "media_type": "text/plain",
                    "content": content,
                }
            ],
        }
    ),
    encoding="utf-8",
)
"""


def _request() -> WorkerRequest:
    goal_id = new_id()
    run = Run(goal_id, new_id(), run_id=new_id(), created_at=NOW).start(at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal_id,
        ("artifact:non-empty",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    node = (
        PlanNode(
            new_id(),
            "produce candidate",
            "Write a concise candidate result.",
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
        created_at=NOW,
    ).start(at=NOW)
    return WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        context={"prior": "fact"},
    )


def _adapter(
    tmp_path: Path,
    mode: str,
    *,
    timeout_seconds: float = 2.0,
    max_output_bytes: int = 4096,
    env_overrides: dict[str, str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[CodexWorkerAdapter, Path]:
    executable = tmp_path / "fake_codex.py"
    executable.write_text(_FAKE_CODEX, encoding="utf-8")
    record = tmp_path / f"{mode}-record.json"
    adapter = CodexWorkerAdapter(
        workspace=tmp_path,
        executable=(sys.executable, str(executable), mode, str(record)),
        timeout_seconds=timeout_seconds,
        cancel_grace_seconds=0.2,
        max_output_bytes=max_output_bytes,
        env_overrides=env_overrides,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return adapter, record


def _wait_for_file(path: Path, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path}"


def _create_kill_on_close_job() -> int:
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_uint64),
            ("write_operation_count", ctypes.c_uint64),
            ("other_operation_count", ctypes.c_uint64),
            ("read_transfer_count", ctypes.c_uint64),
            ("write_transfer_count", ctypes.c_uint64),
            ("other_transfer_count", ctypes.c_uint64),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_int64),
            ("per_job_user_time_limit", ctypes.c_int64),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    if not configured:
        error = ctypes.get_last_error()
        _close_windows_handle(int(job))
        raise OSError(error, "SetInformationJobObject failed")
    return int(job)


def _assign_process_to_job(job: int, pid: int) -> None:
    from ctypes import wintypes

    if pid <= 0 or pid in {os.getpid(), os.getppid()}:
        raise RuntimeError("helper PID was not safe for Job assignment")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    process_handle = kernel32.OpenProcess(0x0001 | 0x0100 | 0x1000, False, pid)
    if not process_handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess for Job assignment failed")
    try:
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    finally:
        _close_windows_handle(int(process_handle))


def _close_windows_handle(handle: int) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def _terminate_windows_job(job: int) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    if not kernel32.TerminateJobObject(job, 1):
        raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")


def _all_result_text(result) -> str:
    return "\n".join(
        [
            result.summary,
            result.raw_output.decode("utf-8", errors="replace"),
            *result.diagnostics,
            *(artifact.content.decode("utf-8", errors="replace") for artifact in result.artifacts),
        ]
    )


def test_codex_worker_invokes_exact_non_shell_contract_and_tolerates_unknown_jsonl(
    tmp_path: Path,
) -> None:
    request = _request()
    adapter, record_path = _adapter(tmp_path, "normal")

    result = adapter.execute(request)

    assert isinstance(adapter, WorkerAdapter)
    assert result.summary == "candidate prepared"
    assert result.artifacts[0].kind is ArtifactKind.CANDIDATE
    assert result.artifacts[0].content == b"hello"
    assert tuple(artifact.kind for artifact in result.artifacts[-2:]) == (
        ArtifactKind.WORKER_OUTPUT,
        ArtifactKind.LOG,
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    args = record["args"]
    assert args[:8] == [
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--json",
    ]
    assert args[-3:] == ["--cd", str(tmp_path.resolve()), "-"]
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert record["schema"]["additionalProperties"] is False
    assert "EHAI CODEX WORKER PROTOCOL v1" in record["prompt"]
    assert '"prior":"fact"' in record["prompt"]
    assert "codex jsonl event: future.event" in result.diagnostics


def test_codex_worker_nonzero_exit_is_scoped_and_redacted(tmp_path: Path) -> None:
    request = _request()
    adapter, _ = _adapter(tmp_path, "nonzero")

    with pytest.raises(WorkerExecutionError) as exc_info:
        adapter.execute(request)

    error = exc_info.value
    assert (error.run_id, error.attempt_id, error.plan_node_id) == (
        request.run_id,
        request.attempt_id,
        request.plan_node_id,
    )
    assert "exited with code 7" in error.reason
    assert b"stderr-secret" not in error.log_output
    assert b"[REDACTED]" in error.log_output


def test_codex_worker_does_not_create_empty_log_artifact(tmp_path: Path) -> None:
    adapter, _ = _adapter(tmp_path, "quiet")

    result = adapter.execute(_request())

    assert ArtifactKind.WORKER_OUTPUT in {artifact.kind for artifact in result.artifacts}
    assert ArtifactKind.LOG not in {artifact.kind for artifact in result.artifacts}


def test_codex_worker_timeout_terminates_process_and_is_scoped(tmp_path: Path) -> None:
    request = _request()
    adapter, _ = _adapter(tmp_path, "timeout", timeout_seconds=0.1)

    with pytest.raises(WorkerTimedOutError, match="timed out") as exc_info:
        adapter.execute(request)

    assert exc_info.value.attempt_id == request.attempt_id
    assert b"partial-stdout-secret" not in exc_info.value.raw_output
    assert b"partial-stderr-secret" not in exc_info.value.log_output
    assert b"[REDACTED]" in exc_info.value.raw_output


def test_codex_worker_cancel_stops_active_attempt_from_another_thread(tmp_path: Path) -> None:
    request = _request()
    adapter, record_path = _adapter(tmp_path, "cancel", timeout_seconds=10)
    failures: list[BaseException] = []

    def run() -> None:
        try:
            adapter.execute(request)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    _wait_for_file(record_path)
    adapter.cancel(request.attempt_id)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], WorkerCancelledError)
    assert "cancelled" in str(failures[0])
    assert b"partial-stdout-secret" not in failures[0].raw_output
    assert b"[REDACTED]" in failures[0].log_output
    adapter.cancel(request.attempt_id)


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_windows_job_isolated_cleanup_terminates_real_descendant(
    tmp_path: Path,
    mode: str,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows Job Object cleanup probe")
    helper = Path(__file__).parents[1] / "helpers" / "windows_codex_cleanup_probe.py"
    job = _create_kill_on_close_job()
    process: subprocess.Popen[str] | None = None
    assigned = False
    try:
        process = subprocess.Popen(
            [sys.executable, str(helper), "controller", mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=tmp_path,
        )
        _assign_process_to_job(job, process.pid)
        assigned = True
        if process.stdin is None:
            raise RuntimeError("cleanup helper stdin was unavailable")
        process.stdin.write("start\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=25)
    finally:
        close_error: OSError | None = None
        try:
            _close_windows_handle(job)
        except OSError as error:
            close_error = error
            try:
                _terminate_windows_job(job)
            finally:
                with suppress(OSError):
                    _close_windows_handle(job)
        finally:
            if process is not None and process.poll() is None:
                if not assigned:
                    if process.stdin is not None:
                        process.stdin.close()
                    process.kill()
                process.wait(timeout=5)
        if close_error is not None:
            raise close_error

    assert process is not None
    assert process.returncode == 0
    assert stderr == ""
    lines = stdout.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result == {
        "descendant_exit": "signaled",
        "mode": mode,
        "result": "passed",
        "worker_error": ("WorkerTimedOutError" if mode == "timeout" else "WorkerCancelledError"),
    }


def test_codex_worker_cancel_before_spawn_never_launches_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ehai.infrastructure.workers import codex as codex_module

    request = _request()
    adapter, record_path = _adapter(tmp_path, "normal")
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    original_schema = codex_module.codex_output_schema_json

    def delayed_schema() -> str:
        entered.set()
        assert release.wait(timeout=2)
        return original_schema()

    monkeypatch.setattr(codex_module, "codex_output_schema_json", delayed_schema)

    def run() -> None:
        try:
            adapter.execute(request)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(timeout=2)
    adapter.cancel(request.attempt_id)
    release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], WorkerCancelledError)
    assert not record_path.exists()


def test_codex_worker_cancel_before_execute_is_consumed_without_spawn(tmp_path: Path) -> None:
    request = _request()
    adapter, record_path = _adapter(tmp_path, "normal")

    adapter.cancel(request.attempt_id)
    with pytest.raises(WorkerCancelledError, match="before process spawn"):
        adapter.execute(request)

    assert not record_path.exists()


def test_codex_worker_cancel_requests_scoped_windows_tree_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific process-tree contract")
    request = _request()
    adapter, record_path = _adapter(tmp_path, "cancel", timeout_seconds=10)
    cleanup_calls: list[tuple[int, bool]] = []
    failures: list[BaseException] = []

    async def record_tree_cleanup(pid: int, *, force: bool) -> str | None:
        cleanup_calls.append((pid, force))
        return None

    monkeypatch.setattr(adapter, "_taskkill", record_tree_cleanup)

    def run() -> None:
        try:
            adapter.execute(request)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    _wait_for_file(record_path)
    adapter.cancel(request.attempt_id)
    thread.join(timeout=4)

    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], WorkerCancelledError)
    assert len(cleanup_calls) == 2
    assert cleanup_calls[0][0] > 0
    assert cleanup_calls == [
        (cleanup_calls[0][0], False),
        (cleanup_calls[0][0], True),
    ]


def test_windows_cleanup_forces_captured_descendants_after_root_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific process-tree contract")
    adapter, _ = _adapter(tmp_path, "normal")
    cleanup_calls: list[tuple[int, bool]] = []

    class ExitedProcess:
        pid = 424_242
        returncode = 0

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("an exited root must not be killed directly")

    async def descendants(_root_pid: int) -> tuple[tuple[int, ...], str | None]:
        return (515_151, 616_161), None

    async def record_tree_cleanup(pid: int, *, force: bool) -> str | None:
        cleanup_calls.append((pid, force))
        return None

    monkeypatch.setattr(adapter, "_windows_descendant_pids", descendants)
    monkeypatch.setattr(adapter, "_taskkill", record_tree_cleanup)

    asyncio.run(adapter._terminate_tree(ExitedProcess()))  # type: ignore[arg-type]

    assert cleanup_calls == [
        (424_242, False),
        (616_161, True),
        (515_151, True),
    ]


@pytest.mark.parametrize(
    ("mode", "message"),
    [("missing", "no final structured result"), ("invalid", "invalid structured result")],
)
def test_codex_worker_fails_closed_on_missing_or_invalid_final_result(
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    request = _request()
    adapter, _ = _adapter(tmp_path, mode)

    with pytest.raises(WorkerExecutionError, match=message) as exc_info:
        adapter.execute(request)

    assert exc_info.value.attempt_id == request.attempt_id


def test_codex_worker_truncates_and_redacts_all_returned_output(tmp_path: Path) -> None:
    request = _request()
    adapter, _ = _adapter(tmp_path, "redact", max_output_bytes=256)

    result = adapter.execute(request)

    text = _all_result_text(result)
    for secret in (
        "candidate-secret",
        "candidate-bearer",
        "sk-candidatekey",
        "summary-secret",
        "stdout-secret",
        "stderr-bearer",
        "stderr-key",
    ):
        assert secret not in text
    assert "[REDACTED]" in text
    assert "codex stdout was truncated" in result.diagnostics
    assert all(len(artifact.content) <= 256 for artifact in result.artifacts[-2:])


def test_codex_worker_drains_output_flood_with_bounded_capture(tmp_path: Path) -> None:
    adapter, _ = _adapter(tmp_path, "flood", max_output_bytes=256)

    result = adapter.execute(_request())

    assert len(result.raw_output) == 256
    assert result.raw_output.endswith(b"...[truncated]...\n")
    assert "codex stdout was truncated" in result.diagnostics
    assert "codex stderr was truncated" in result.diagnostics
    assert "ignored 1 oversized codex JSONL lines" in result.diagnostics


def test_codex_worker_default_environment_excludes_secrets_and_allows_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENAI_API_KEY",
        "SERVICE_TOKEN",
        "EHAI_SECRET",
        "DATABASE_PASSWORD",
        "AUTHORIZED_TOKEN",
    ):
        monkeypatch.setenv(name, f"bare-{name.lower()}")
    default_adapter, default_record = _adapter(tmp_path, "normal")
    default_adapter.execute(_request())
    default_environment = json.loads(default_record.read_text())["environment"]
    assert not any(default_environment.values())

    override_adapter, override_record = _adapter(
        tmp_path,
        "normal",
        env_overrides={"AUTHORIZED_TOKEN": os.environ["AUTHORIZED_TOKEN"]},
    )
    override_adapter.execute(_request())
    override_environment = json.loads(override_record.read_text())["environment"]
    assert override_environment["AUTHORIZED_TOKEN"] is True
    assert sum(override_environment.values()) == 1


def test_codex_worker_redacts_bare_explicit_environment_values(tmp_path: Path) -> None:
    secret = "bare-authorized-value-91f7"
    adapter, _ = _adapter(
        tmp_path,
        "env-echo",
        env_overrides={"AUTHORIZED_TOKEN": secret},
    )

    result = adapter.execute(_request())

    text = _all_result_text(result)
    assert secret not in text
    assert "[REDACTED]" in text


def test_codex_worker_passes_model_and_reasoning_as_non_shell_argv(tmp_path: Path) -> None:
    adapter, record_path = _adapter(
        tmp_path,
        "normal",
        model="gpt-5.5",
        reasoning_effort="high",
    )

    adapter.execute(_request())

    args = json.loads(record_path.read_text(encoding="utf-8"))["args"]
    assert args[:4] == [
        "--model",
        "gpt-5.5",
        "--config",
        'model_reasoning_effort="high"',
    ]
    assert "--ask-for-approval" in args
    assert "exec" in args


def test_codex_worker_launch_error_is_scoped(tmp_path: Path) -> None:
    request = _request()
    adapter = CodexWorkerAdapter(
        workspace=tmp_path,
        executable=tmp_path / "does-not-exist",
        timeout_seconds=1,
    )

    with pytest.raises(WorkerExecutionError, match="could not start") as exc_info:
        adapter.execute(request)

    assert exc_info.value.run_id == request.run_id


def test_codex_worker_validates_bounded_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sandbox"):
        CodexWorkerAdapter(workspace=tmp_path, sandbox="unbounded")
    with pytest.raises(ValueError, match="timeout_seconds"):
        CodexWorkerAdapter(workspace=tmp_path, timeout_seconds=0)
    with pytest.raises(ValueError, match="max_output_bytes"):
        CodexWorkerAdapter(workspace=tmp_path, max_output_bytes=1)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="positive and finite"):
            CodexWorkerAdapter(workspace=tmp_path, timeout_seconds=value)
        with pytest.raises(ValueError, match="positive and finite"):
            CodexWorkerAdapter(workspace=tmp_path, cancel_grace_seconds=value)
