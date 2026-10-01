"""The product E2E: one deterministic coding Run through the normal host entries.

The driver only uses public entry points: the ``ehai-api`` host process, the ``ehai``
CLI client and the HTTP API. It never writes domain records or reads SQLite.

Scenario (no model calls; the host runs the built-in scripted Worker):

1. Import an external plan with two independent tasks, a join and a Reviewer phase.
   Task A carries a human acceptance Gate; the final Gate runs a host command.
2. Approve and start the Run. B completes while A waits for the human decision,
   and the join stays pending.
3. Kill the host process without a graceful shutdown, then restart it on the same
   database. The Run, the pending human request and the event consumer batch survive.
4. Decide the human Check through the CLI. The Run completes; repeating the decision
   with the same idempotency key is accepted without reopening any request.

The real Pi path (model calls, Git worktrees, integrate-run) is outside this
deterministic E2E and is verified manually; see docs/REFACTOR_PLAN.md.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

POLL_SECONDS = 0.2
WAIT_SECONDS = 60.0


def _script(name: str) -> str:
    """Resolve a console script installed next to the running interpreter."""
    directory = Path(sys.executable).parent
    found = shutil.which(name, path=str(directory))
    if found is None:
        pytest.fail(f"{name} is not installed next to {sys.executable}; run uv sync")
    return found


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Host:
    """One ``ehai-api`` process bound to a fixed database, artifacts and workspace."""

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[bytes] | None = None
        self.starts = 0

    def start(self) -> None:
        self.starts += 1
        log = (self.root / f"host-{self.starts}.log").open("wb")
        argv = [
            _script("ehai-api"),
            "--database",
            str(self.root / "state" / "ehai.sqlite"),
            "--artifacts",
            str(self.root / "state" / "artifacts"),
            "--worker",
            "fake",
            "--planner",
            "single",
            "--worker-workspace",
            str(self.root / "workspace"),
            "--p2-runtime",
            "--port",
            str(self.port),
        ]
        self.process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
        )
        log.close()
        _wait(lambda: self._healthy(), "host to become healthy", self)

    def _healthy(self) -> bool:
        if self.process is not None and self.process.poll() is not None:
            pytest.fail(f"host exited with {self.process.returncode}\n{self.log()}")
        try:
            response = httpx.get(f"{self.url}/api/v1/runtime/health", timeout=2)
        except httpx.TransportError:
            return False
        return response.status_code == 200 and response.json()["data"]["status"] == "healthy"

    def kill(self) -> None:
        """Stop the host abruptly, as a crash or power loss would."""
        assert self.process is not None
        if sys.platform == "win32":
            self.process.kill()
        else:
            os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=30)
        self.process = None

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=30)
        self.process = None

    def log(self) -> str:
        return "\n".join(
            path.read_text(errors="replace")[-4000:]
            for path in sorted(self.root.glob("host-*.log"))
        )

    def cli(self, *args: str) -> Any:
        completed = subprocess.run(
            [_script("ehai"), "--api-url", self.url, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(
                f"ehai {' '.join(args)} failed ({completed.returncode})\n"
                f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
            )
        return json.loads(completed.stdout)

    def http(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = httpx.request(method, f"{self.url}/api/v1{path}", json=body, timeout=30)
        if response.status_code >= 300:
            pytest.fail(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json()["data"]


def _wait(predicate: Callable[[], bool], what: str, host: Host) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(f"timed out waiting for {what}\n{host.log()}")


@contextmanager
def _host(root: Path) -> Iterator[Host]:
    (root / "workspace").mkdir(parents=True)
    host = Host(root, _free_port())
    try:
        host.start()
        yield host
    finally:
        host.stop()


def _plan_document() -> dict[str, Any]:
    def node(key: str, title: str, kind: str = "work") -> dict[str, Any]:
        return {
            "key": key,
            "title": title,
            "instruction": f"{title}. Submit the result as a candidate Artifact.",
            "kind": kind,
            "required_capabilities": None,
            "session_policy": None,
        }

    def edge(source: str, target: str) -> dict[str, Any]:
        return {
            "source": source,
            "target": target,
            "edge_type": "dependency",
            "branch_key": None,
            "condition": None,
        }

    return {
        "schema_version": 1,
        "design_document": (
            "Two independent tasks feed a join; a Reviewer closes the delivery phase. "
            "Task A needs human acceptance. The final Gate runs a host command."
        ),
        "nodes": [
            node("a", "Task A"),
            node("b", "Task B"),
            node("join", "Join A and B"),
            node("review", "Review delivery", "reviewer"),
        ],
        "edges": [edge("a", "join"), edge("b", "join"), edge("join", "review")],
        "branches": [],
        "phases": [
            {
                "phase_key": "delivery",
                "title": "Delivery",
                "node_keys": ["a", "b", "join", "review"],
                "reviewer_node_key": "review",
                "gate_node_key": "review",
                "rework_node_keys": ["a", "b", "join"],
            }
        ],
        "node_gates": [
            {
                "node_key": "a",
                "name": "Human acceptance of Task A",
                "argv": [],
                "human_question": "Is the Task A artifact acceptable?",
            }
        ],
        "final_gate": {"argv": [sys.executable, "-c", "pass"], "human_question": None},
    }


def _node_status(host: Host, run_id: str) -> dict[str, str]:
    plan = host.cli("get-run-plan", "--run-id", run_id)
    return {node["title"]: node["status"] for node in plan["nodes"]}


def _run_completed(host: Host, run_id: str) -> bool:
    run = host.cli("get-run", "--run-id", run_id)
    if run["status"] in {"failed", "cancelled", "paused"}:
        pytest.fail(f"Run stopped as {run['status']}: {run['status_reason']}\n{host.log()}")
    return bool(run["status"] == "completed")


def _human_checks(host: Host, run_id: str) -> list[dict[str, Any]]:
    inbox = host.cli("list-inbox", "--run-id", run_id)
    return [item for item in inbox["items"] if item["kind"] == "human_check"]


def test_product_e2e(tmp_path: Path) -> None:
    with _host(tmp_path) as host:
        assert host.cli("get-runtime-health")["status"] == "healthy"
        consumer = host.cli("register-event-consumer", "--consumer-id", "e2e-observer")
        assert consumer["consumer_id"] == "e2e-observer"

        # 1. Project, Goal and an externally authored plan.
        project = host.cli("create-project", "--name", "E2E project", "--idempotency-key", "p")
        goal = host.cli(
            "create-goal",
            "--project-id",
            project["project_id"],
            "--objective",
            "Deliver A and B, join them and pass review and the final Gate",
            "--idempotency-key",
            "g",
        )
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(json.dumps(_plan_document()), encoding="utf-8")
        plan = host.cli(
            "import-plan",
            "--goal-id",
            goal["goal_id"],
            "--file",
            str(plan_file),
            "--idempotency-key",
            "import",
        )
        assert plan["status"] == "draft"

        # 2. Approval and execution authorization are separate explicit steps.
        approved = host.cli(
            "approve-plan",
            "--plan-revision-id",
            plan["plan_revision_id"],
            "--completion-contract-id",
            plan["completion_contract_id"],
            "--idempotency-key",
            "approve",
        )
        assert approved["status"] == "approved"
        # The CLI client requires an explicit Pi/Codex execution configuration; the
        # scripted host accepts the HTTP start without one.
        run = host.http(
            "POST",
            "/runs/start",
            {"idempotency_key": "start", "plan_revision_id": plan["plan_revision_id"]},
        )
        run_id = run["run_id"]

        _wait(lambda: len(_human_checks(host, run_id)) == 1, "the human request", host)
        _wait(
            lambda: _node_status(host, run_id)["Task B"] == "completed",
            "Task B to complete beside the waiting Task A",
            host,
        )
        statuses = _node_status(host, run_id)
        assert statuses["Task A"] == "verifying"
        assert statuses["Join A and B"] == "pending"
        assert statuses["Review delivery"] == "pending"
        request = _human_checks(host, run_id)[0]
        assert request["actionable"] is True
        assert request["owner"]["node_title"] == "Task A"
        batch = host.cli("read-consumer-events", "--consumer-id", "e2e-observer")
        assert batch["batch_token"] is not None and batch["events"]

        # 3. Crash and restart on the same state.
        host.kill()
        host.start()

        assert host.cli("get-run", "--run-id", run_id)["status"] == "running"
        assert _node_status(host, run_id) == statuses
        recovered = _human_checks(host, run_id)
        assert len(recovered) == 1
        assert recovered[0]["request_token"] == request["request_token"]
        replayed = host.cli("read-consumer-events", "--consumer-id", "e2e-observer")
        assert replayed["batch_token"] == batch["batch_token"]
        assert [event["offset"] for event in replayed["events"]] == [
            event["offset"] for event in batch["events"]
        ]
        host.cli(
            "ack-consumer-events",
            "--consumer-id",
            "e2e-observer",
            "--batch-token",
            batch["batch_token"],
        )

        # 4. The human decision resumes the Run to completion.
        decision_args = (
            "decide-human-check",
            "--check-run-id",
            recovered[0]["actions"][0]["arguments"]["check_run_id"],
            "--request-token",
            recovered[0]["request_token"],
            "--passed",
            "--actor",
            "e2e",
            "--comment",
            "Task A accepted",
            "--idempotency-key",
            "decide",
        )
        first = host.cli(*decision_args)
        _wait(lambda: _run_completed(host, run_id), "the Run to complete", host)
        assert set(_node_status(host, run_id).values()) == {"completed"}
        replay = host.cli(*decision_args)
        assert (replay["run_id"], replay["status"]) == (first["run_id"], "completed")
        assert _human_checks(host, run_id) == []

        result = host.cli("get-result", "--run-id", run_id)["result"]
        checks = result["check_result"]["checks"]
        assert checks and all(check["status"] == "completed" for check in checks)
        project_view = host.cli("get-project", "--project-id", project["project_id"])
        runs = [item for goal_view in project_view["goals"] for item in goal_view["runs"]]
        assert [item["run"]["run_id"] for item in runs] == [run_id]
        assert runs[0]["completed_results"]
