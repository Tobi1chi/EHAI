"""The product E2E: one deterministic coding Run through the normal host entries.

The driver only uses public entry points: the ``ehai-api`` host process, the ``ehai``
CLI client and the HTTP API. It never writes domain records or reads SQLite.

The host runs the production Pi Worker: every role goes through the Hub (a loopback
``ehai-hub`` child), the Pi compatibility layer and the pinned upstream Pi, and code
results come from EHAI-owned Git worktrees. Only the model is replaced: Pi talks to a
scripted OpenAI-compatible provider started by this driver, so there are no real model
calls. Install Pi first with ``npm ci --prefix agent-backends/pi --ignore-scripts``.

Scenario:

1. Import an external plan with two independent tasks, a join and a Reviewer phase.
   Task A carries a human acceptance Gate; the final Gate runs a host command.
2. Approve and start the Run. B completes while A waits for the human decision,
   and the join stays pending.
3. Kill the host process without a graceful shutdown, then restart it on the same
   database. The Run, the pending human request and the event consumer batch survive.
4. Decide the human Check through the CLI. The Run completes; repeating the decision
   with the same idempotency key is accepted without reopening any request.

Real model calls and integrate-run are outside this deterministic E2E and are
verified manually; see docs/REFACTOR_PLAN.md.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

POLL_SECONDS = 0.2
WAIT_SECONDS = 90.0
REPOSITORY = Path(__file__).resolve().parent.parent
PI_CLI = (
    REPOSITORY / "agent-backends/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
)
MODEL = "scripted-model"
PROVIDER_KEY = "EHAI_E2E_PROVIDER_KEY"


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


class ScriptedProvider:
    """An OpenAI-compatible streaming endpoint that answers like a minimal Agent.

    A work node first writes one file named after its node into its worktree, then submits
    a text candidate; a reviewer submits a passing ``review.json`` citing every input.
    """

    def __init__(self) -> None:
        provider = self
        self.requests = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in provider.answer(body):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def answer(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": "scripted",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        tools = {tool["function"]["name"] for tool in body.get("tools", [])}
        called = [
            call["function"]["name"]
            for message in body["messages"]
            if message["role"] == "assistant"
            for call in message.get("tool_calls") or ()
        ]
        if "submit_candidate" not in tools or "submit_candidate" in called:
            return [chunk({"role": "assistant", "content": "Done."}), chunk({}, "stop")]
        prompt = next(message for message in body["messages"] if message["role"] == "user")
        content = prompt["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        context = json.loads(text)["context"]
        scope = context["execution_scope"]
        name = "submit_candidate"
        if scope["kind"] == "reviewer":
            review = {
                "summary": f"{scope['title']}: inputs reviewed",
                "findings": [],
                "evidence_artifact_ids": [
                    artifact["artifact_id"] for artifact in context["input_artifacts"]
                ],
                "recommended_action": "pass",
            }
            arguments = {
                "name": "review.json",
                "media_type": "application/json",
                "content": json.dumps(review),
            }
        elif "workspace_write" not in called:
            name, arguments = (
                "workspace_write",
                {
                    "path": _node_file(scope["title"]),
                    "content": f"{scope['title']}\n",
                    "overwrite": True,
                },
            )
        else:
            arguments = {
                "name": "result.txt",
                "media_type": "text/plain",
                "content": f"{scope['title']} result",
            }
        call = {
            "index": 0,
            "id": f"call-{self.requests}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        return [chunk({"role": "assistant", "tool_calls": [call]}), chunk({}, "tool_calls")]

    def __enter__(self) -> ScriptedProvider:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _node_file(title: str) -> str:
    return title.lower().replace(" ", "-") + ".txt"


def _pi_backend(root: Path, provider: ScriptedProvider) -> Path:
    """Write an isolated Pi configuration that points at the scripted provider."""
    node = shutil.which("node")
    if node is None or not PI_CLI.is_file():
        pytest.fail(
            "The E2E needs Node >=22.19 and the pinned Pi: "
            "npm ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund"
        )
    agent_dir = root / "pi"
    agent_dir.mkdir()
    settings = {"retry": {"enabled": False, "provider": {"maxRetries": 0}}}
    (agent_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    models = {
        "providers": {
            "scripted": {
                "baseUrl": provider.url,
                "api": "openai-completions",
                "apiKey": f"${PROVIDER_KEY}",
                "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                "models": [{"id": MODEL}],
            }
        }
    }
    (agent_dir / "models.json").write_text(json.dumps(models), encoding="utf-8")
    backend = {
        "node": str(Path(node).resolve()),
        "cli": str(PI_CLI),
        "agent_dir": str(agent_dir.resolve()),
        "provider": "scripted",
        "environment_names": [PROVIDER_KEY],
    }
    path = root / "pi-backend.json"
    path.write_text(json.dumps(backend), encoding="utf-8")
    return path


def _git_workspace(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text("E2E workspace\n", encoding="utf-8")
    identity = (
        "-c",
        "user.name=EHAI E2E",
        "-c",
        "user.email=e2e@example.invalid",
        "-c",
        "commit.gpgsign=false",
    )
    for argv in (["init", "-q"], ["add", "README.md"], [*identity, "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True)


class Host:
    """One ``ehai-api`` process bound to a fixed database, artifacts and workspace."""

    def __init__(self, root: Path, port: int, pi_backend: Path) -> None:
        self.root = root
        self.port = port
        self.pi_backend = pi_backend
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
            "pi",
            "--pi-config",
            str(self.pi_backend),
            "--agent-model",
            MODEL,
            "--planner",
            "single",
            "--worker-workspace",
            str(self.root / "workspace"),
            "--p2-runtime",
            "--port",
            str(self.port),
        ]
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, PROVIDER_KEY: "scripted"},
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

    def _listening(self) -> bool:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.settimeout(1)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def _kill_tree(self) -> None:
        assert self.process is not None
        if sys.platform == "win32":
            # The console-script launcher runs the host as a child process; killing only
            # the launcher would leave the real host serving the port and database.
            taskkill = (
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            )
            subprocess.run(
                [str(taskkill), "/PID", str(self.process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=30)
        self.process = None
        # A restart is only meaningful once no old process still answers on the port.
        _wait(lambda: not self._listening(), "the killed host to release its port", self)

    def kill(self) -> None:
        """Stop the host abruptly, as a crash or power loss would."""
        self._kill_tree()

    def stop(self) -> None:
        if self.process is None:
            return
        if sys.platform == "win32":
            self._kill_tree()
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
def _host(root: Path, provider: ScriptedProvider) -> Iterator[Host]:
    _git_workspace(root / "workspace")
    host = Host(root, _free_port(), _pi_backend(root, provider))
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
    with ScriptedProvider() as provider, _host(tmp_path, provider) as host:
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
        # The HTTP start uses the host's own execution configuration.
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
        # The code result is a new commit captured from an EHAI-owned worktree. It carries the
        # file each work node wrote, so upstream results reached the join, and a non-empty patch.
        assert result["commit"] != result["base_commit"] and result["diff_artifact_ids"]
        tree = subprocess.run(
            ["git", "-C", result["workspace"], "ls-tree", "-r", "--name-only", result["commit"]],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        expected = {_node_file(title) for title in ("Task A", "Task B", "Join A and B")}
        assert expected <= set(tree)
        assert Path(result["diff_path"]).read_text(encoding="utf-8").strip()
        # Every node ran through Pi: A, B, the join and the Reviewer each asked the model once.
        assert provider.requests >= 4
        project_view = host.cli("get-project", "--project-id", project["project_id"])
        runs = [item for goal_view in project_view["goals"] for item in goal_view["runs"]]
        assert [item["run"]["run_id"] for item in runs] == [run_id]
        assert runs[0]["completed_results"]
