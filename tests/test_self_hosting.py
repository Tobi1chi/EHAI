"""The user-approved, two-generation self-hosting E2E and its behavior Gate.

This driver invokes public entry points against real retained executions. It
never authors the feature, creates domain records, or supplies a winning plan.
Run ``verify-result --help`` for the first development round's behavior Gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from uuid import uuid4


def run_command(argv: list[str], workspace: Path) -> str:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        argv,
        cwd=workspace,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv}\n{result.stderr}")
    return result.stdout


def get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def invoke_version(args: argparse.Namespace) -> int:
    """Record the actually imported host version in the same process as the CLI."""
    workspace = Path(args.workspace).resolve(strict=True)
    evidence = Path(args.evidence).resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    cli_args = args.cli_args[1:] if args.cli_args[:1] == ["--"] else args.cli_args
    if not cli_args:
        raise ValueError("invoke requires normal EHAI CLI arguments after --")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    child = """
import json, os, subprocess, sys
from pathlib import Path
import ehai
from ehai.interfaces.cli import create_parser, main
workspace = Path.cwd().resolve()
source = Path(ehai.__file__).resolve()
if not source.is_relative_to(workspace / 'src'):
    raise RuntimeError(f'Wrong EHAI host source: {source}')
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
arguments = create_parser().parse_args(sys.argv[2:])
identity = {'workspace':str(workspace), 'source':str(source), 'commit':commit,
            'python':sys.executable, 'pid':os.getpid(), 'command':arguments.command,
            'plan_revision_id':getattr(arguments,'plan_revision_id',None),
            'run_id':getattr(arguments,'run_id',None),
            'idempotency_key':getattr(arguments,'idempotency_key',None)}
with Path(sys.argv[1]).open('x', encoding='utf-8') as output:
    json.dump(identity, output)
exit_code = main(sys.argv[2:])
identity['exit_code'] = exit_code
Path(sys.argv[1]).write_text(json.dumps(identity), encoding='utf-8')
raise SystemExit(exit_code)
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("VIRTUAL_ENV", None)
    argv = [
        uv,
        "run",
        "--project",
        str(workspace),
        "--frozen",
        "python",
        "-c",
        child,
        str(evidence),
        *cli_args,
    ]
    with evidence.with_suffix(".stdout.jsonl").open("x", encoding="utf-8") as output:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        assert process.stdout is not None
        for line in process.stdout:
            output.write(line)
            output.flush()
            print(line, end="", flush=True)
        process.stdout.close()
        return process.wait()


def verify_chain(args: argparse.Namespace) -> None:
    """Verify the completed two-round chain, not just a pair of planned launches."""
    workspace = Path(args.workspace).resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    results = []
    identities = []
    for run_id, host_path in (
        (args.first_run_id, args.first_host),
        (args.second_run_id, args.second_host),
    ):
        result = json.loads(
            run_command(
                [
                    uv,
                    "run",
                    "--project",
                    str(workspace),
                    "--frozen",
                    "ehai",
                    "--database",
                    args.database,
                    "--artifacts",
                    args.artifacts,
                    "get-result",
                    "--run-id",
                    run_id,
                ],
                workspace,
            )
        )
        assert result["run"]["status"] == "completed"
        assert result["result"]["check_result"]["passed"] is True
        host_file = Path(host_path).resolve(strict=True)
        identity = json.loads(host_file.read_text(encoding="utf-8"))
        assert identity["exit_code"] == 0, "Recorded host invocation did not finish successfully"
        assert identity["command"] in {"execute-plan", "resume-session"}
        if identity["command"] == "execute-plan":
            assert identity["plan_revision_id"] == result["run"]["plan_revision_id"]
        else:
            assert identity["run_id"] == run_id
        documents = []
        for line in host_file.with_suffix(".stdout.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                document = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(document, dict) and isinstance(document.get("run"), dict):
                documents.append(document)
        assert any(
            doc["run"].get("run_id") == run_id and doc["run"].get("status") == "completed"
            for doc in documents
        ), "Host output must demonstrate completion of this exact Run"
        results.append(result)
        identities.append(identity)
    first_commit = results[0]["result"]["commit"]
    second_commit = results[1]["result"]["commit"]
    assert first_commit and first_commit != identities[0]["commit"]
    assert identities[1]["commit"] == first_commit, "Second host is not the first delivered version"
    assert second_commit and second_commit != first_commit
    print(
        json.dumps(
            {
                "self_hosting_chain_passed": True,
                "first_commit": first_commit,
                "second_host_commit": identities[1]["commit"],
                "second_commit": second_commit,
            }
        )
    )


def verify_phase_summary(document: dict, plan: dict, trace: dict) -> None:
    """Compare the real first delivery's phase history with independent queries.

    This is the second feature's assertion within the same self-hosting E2E,
    not a fabricated execution or a separate smoke suite.
    """
    summary = document["phase_summary"]
    assert summary["availability"] == "available"
    assert summary["plan_revision_id"] == trace["run"]["plan_revision_id"]
    assert summary["process_revision_id"] == plan["process_revision_id"]
    assert summary["diagnostics"] == []
    phases = summary["phases"]
    assert [p["phase_id"] for p in phases] == [p["phase_id"] for p in plan["phases"]]
    assert len(phases) == 2, "This E2E uses the actual two-phase first delivery"
    nodes = {n["plan_node_id"]: n for n in plan["nodes"]}
    attempts = {a["attempt_id"]: a for a in trace["attempts"]}
    checks = {c["check_run_id"]: c for c in trace["check_runs"]}
    artifacts = {a["artifact_id"]: a for a in trace["artifacts"]}
    checkpoints = {c["checkpoint_id"]: c for c in trace["checkpoints"]}
    events = {e["event"]["id"]: e["event"] for e in trace["events"]}
    for phase, original in zip(phases, plan["phases"], strict=True):
        for key, value in original.items():
            assert phase[key] == value, f"Phase structure mismatch: {key}"
        assert phase["diagnostics"] == []
        assert [m["plan_node_id"] for m in phase["members"]] == phase["node_ids"]
        for member in phase["members"]:
            node_id = member["plan_node_id"]
            for key in ("title", "kind", "status"):
                assert member[key] == nodes[node_id][key]
            expected_attempts = [
                a["attempt_id"] for a in trace["attempts"] if a["plan_node_id"] == node_id
            ]
            expected_artifacts = [
                a["artifact_id"] for a in trace["artifacts"] if a["plan_node_id"] == node_id
            ]
            assert member["attempt_ids"] == expected_attempts
            assert member["artifact_ids"] == expected_artifacts
        required = nodes[phase["gate_node_id"]]["required_check_ids"]
        assert phase["required_check_ids"] == required and required
        acceptance = phase["acceptance"]
        assert acceptance["status"] == "passed"
        assert isinstance(acceptance["reason_code"], str) and acceptance["reason_code"]
        reviewer = attempts[acceptance["reviewer_attempt_id"]]
        assert reviewer["plan_node_id"] == phase["reviewer_node_id"]
        assert reviewer["status"] == "succeeded"
        checkpoint = checkpoints[acceptance["checkpoint_id"]]
        gate = checkpoint["gate_decision"]
        assert gate["passed"] is True
        assert acceptance["gate_id"] == gate["gate_id"]
        assert gate["run_id"] == trace["run"]["run_id"]
        assert gate["plan_node_id"] == phase["gate_node_id"]
        assert gate["attempt_id"] == reviewer["attempt_id"]
        assert gate["required_check_ids"] == required
        selected_checks = [checks[cid] for cid in acceptance["check_run_ids"]]
        assert {c["check_id"] for c in selected_checks} == set(required)
        assert all(
            c["attempt_id"] == reviewer["attempt_id"]
            and c["result"] is not None
            and c["result"]["passed"] is True
            for c in selected_checks
        )
        assert set(gate["evidence_artifact_ids"]) <= set(acceptance["evidence_artifact_ids"])
        assert acceptance["evidence_artifact_ids"] and acceptance["event_ids"]
        assert set(acceptance["evidence_artifact_ids"]) <= artifacts.keys()
        assert set(acceptance["event_ids"]) <= events.keys()
        current_history = []
        for history in phase["review_history"]:
            attempt = attempts[history["reviewer_attempt_id"]]
            assert history["reviewer_node_id"] == attempt["plan_node_id"]
            assert history["process_revision_id"] == attempt["process_revision_id"]
            assert set(history["artifact_ids"]) <= artifacts.keys()
            assert history["event_ids"] and set(history["event_ids"]) <= events.keys()
            for check in history["checks"]:
                assert check == checks[check["check_run_id"]]
                assert check["attempt_id"] == attempt["attempt_id"]
            if history["checkpoint_id"] is not None:
                historical_gate = checkpoints[history["checkpoint_id"]]["gate_decision"]
                assert historical_gate["gate_id"] == history["gate_id"]
                assert historical_gate["passed"] == history["gate_passed"]
            if history["gate_id"] == acceptance["gate_id"]:
                current_history.append(history)
        assert len(current_history) == 1
        assert current_history[0]["reviewer_attempt_id"] == reviewer["attempt_id"]
    # These IDs are recorded real product outcomes, not generated expected state.
    first_history = phases[0]["review_history"]
    rejected = [h for h in first_history if h["gate_id"] == "a75ddfa7-a62a-48eb-8231-bf065ad3dc94"]
    assert len(rejected) == 1 and rejected[0]["gate_passed"] is False
    assert rejected[0]["reviewer_attempt_id"] == "f483d75e-18c2-4ee8-ac5b-6cb778aa29a4"
    assert any(
        c["check_run_id"] == "a815e098-1fbb-4a4a-872e-b842ffe05d7f"
        and c["result"]["passed"] is False
        and c["human_decision"] is not None
        for c in rejected[0]["checks"]
    )
    assert phases[0]["acceptance"]["checkpoint_id"] == "1355e19a-077a-4409-9d16-4bc22007ae44"
    assert phases[1]["acceptance"]["checkpoint_id"] == "54137347-ff6d-46b0-8973-73bde2e4e12d"
    assert phases[1]["acceptance"]["check_run_ids"] == ["565c98f0-d80c-4c4a-a8c7-9932100ae229"]
    assert document["result"]["commit"] == "2dcd2b9112abd1bf8e96a6b8475bd9a651f58875"


def verify_result(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).resolve(strict=True)
    source_database = Path(args.database).resolve(strict=True)
    artifacts = Path(args.artifacts).resolve(strict=True)
    evidence = Path(tempfile.mkdtemp(prefix="ehai-result-gate-"))
    database = evidence / "state.sqlite"
    # Copy real retained history without inventing or changing any domain state.
    with (
        closing(sqlite3.connect(source_database.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(database)) as destination,
    ):
        source.backup(destination)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    project = [uv, "run", "--project", str(workspace), "--frozen"]
    python = run_command(
        [*project, "python", "-c", "import sys;print(sys.executable)"], workspace
    ).strip()
    cli_args = ["--database", str(database), "--artifacts", str(artifacts)]
    cli = json.loads(
        run_command([*project, "ehai", *cli_args, "get-result", "--run-id", args.run_id], workspace)
    )
    assert cli["run"]["status"] == "completed", "Gate requires a real completed execution"
    assert cli["result"]["commit"], "Fixture must include real code delivery"
    assert cli["result"]["check_result"]["passed"] is True
    assert cli["trace_ids"]["attempt_ids"] and cli["trace_ids"]["check_run_ids"]
    if args.mode == "verify-phases":
        plan = json.loads(
            run_command(
                [*project, "ehai", *cli_args, "get-run-plan", "--run-id", args.run_id], workspace
            )
        )
        trace_document = json.loads(
            run_command(
                [*project, "ehai", *cli_args, "get-trace", "--run-id", args.run_id], workspace
            )
        )
        verify_phase_summary(cli, plan, trace_document)
        (evidence / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        previous = Path(args.previous_workspace).resolve(strict=True)
        old_result = json.loads(
            run_command(
                [
                    uv,
                    "run",
                    "--project",
                    str(previous),
                    "--frozen",
                    "ehai",
                    *cli_args,
                    "get-result",
                    "--run-id",
                    args.run_id,
                ],
                previous,
            )
        )
        assert {k: v for k, v in cli.items() if k != "phase_summary"} == old_result
        # This is a preserved real rejection-time snapshot, never rewritten to
        # simulate failure. Query it without running its foreground coordinator.
        rejected_source = Path(args.rejected_database).resolve(strict=True)
        rejected_database = evidence / "rejected.sqlite"
        with (
            closing(sqlite3.connect(rejected_source.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(rejected_database)) as destination,
        ):
            source.backup(destination)
        rejected = json.loads(
            run_command(
                [
                    *project,
                    "ehai",
                    "--database",
                    str(rejected_database),
                    "--artifacts",
                    str(artifacts),
                    "get-result",
                    "--run-id",
                    args.run_id,
                ],
                workspace,
            )
        )
        assert rejected["run"]["status"] == "paused"
        rejected_phases = rejected["phase_summary"]["phases"]
        assert len(rejected_phases) == 2
        assert rejected_phases[0]["acceptance"]["status"] == "failed"
        assert rejected_phases[0]["acceptance"]["gate_id"] == "a75ddfa7-a62a-48eb-8231-bf065ad3dc94"
        assert rejected_phases[0]["acceptance"]["checkpoint_id"] is None
        assert rejected_phases[1]["acceptance"]["status"] == "not_started"
        assert rejected_phases[1]["acceptance"]["gate_id"] is None
        (evidence / "rejected-result.json").write_text(json.dumps(rejected), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log_path = evidence / "server.log"
    # Directly launch the installed version's normal API entry point. The real
    # Codex adapter is dormant: this server only queries the completed fixture.
    argv = [
        python,
        "-c",
        "from ehai.interfaces.runtime import main;raise SystemExit(main())",
        *cli_args,
        "--worker",
        "codex",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(argv, cwd=workspace, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 40
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"API exited during startup; see {log_path}")
                try:
                    get_json(base_url + "/openapi.json")
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"API did not start; see {log_path}") from None
                    time.sleep(0.2)
            result_url = base_url + f"/api/v1/runs/{args.run_id}/result"
            response = get_json(result_url)
            assert isinstance(response, dict) and response.get("data") == cli, (
                "HTTP/CLI result mismatch"
            )
            trace_response = get_json(base_url + f"/api/v1/runs/{args.run_id}/trace")
            assert isinstance(trace_response, dict)
            trace = trace_response["data"]
            checks = {item["check_run_id"]: item for item in trace["check_runs"]}
            reported_checks = cli["result"]["check_result"]["checks"]
            for check in reported_checks:
                assert checks[check["check_run_id"]] == check
                assert check["run_id"] == args.run_id
                assert check["attempt_id"] == cli["result"]["attempt_id"]
            reported_ids = {check["check_id"] for check in reported_checks}
            assert any(
                checkpoint["gate_decision"]["passed"]
                and checkpoint["gate_decision"]["run_id"] == args.run_id
                and set(checkpoint["gate_decision"]["required_check_ids"]) <= reported_ids
                and checkpoint["gate_decision"]["evidence_artifact_ids"]
                for checkpoint in trace["checkpoints"]
            ), "Reported checks must resolve to a real passing Gate"
            unknown_run = str(uuid4())
            try:
                run_command(
                    [*project, "ehai", *cli_args, "get-result", "--run-id", unknown_run], workspace
                )
            except RuntimeError as error:
                assert "not found" in str(error).lower()
            else:
                raise AssertionError("CLI must reject an unknown Run")
            try:
                get_json(base_url + f"/api/v1/runs/{unknown_run}/result")
            except urllib.error.HTTPError as error:
                assert error.code == 404, f"Unknown Run returned HTTP {error.code}"
                assert json.load(error)["error"]["code"] == "not_found"
            else:
                raise AssertionError("Unknown Run must return HTTP 404")
            npm = shutil.which("npm.cmd") or shutil.which("npm")
            node = shutil.which("node")
            if npm is None or node is None:
                raise RuntimeError("npm and node are required for the generated Client")
            client_root = workspace / "control-plane"
            if not (client_root / "node_modules").is_dir():
                run_command([npm, "ci", "--ignore-scripts"], client_root)
            run_command([npm, "run", "typecheck"], client_root)
            run_command([npm, "run", "build"], client_root)
            client_js = (
                "const m=await import(process.argv[1]);"
                "const c=new m.EhaiApiClient(process.argv[2]);"
                "const result=await c[process.argv[3]](process.argv[4]);"
                "try{await c[process.argv[3]](process.argv[5]);throw Error('missing 404');}"
                "catch(e){if(!(e instanceof m.EhaiApiError)||e.status!==404)throw e;}"
                "console.log(JSON.stringify(result));"
            )
            client_result = json.loads(
                run_command(
                    [
                        node,
                        "--input-type=module",
                        "-e",
                        client_js,
                        (client_root / "dist" / "generated" / "ehai-client.js").as_uri(),
                        base_url,
                        args.client_method,
                        args.run_id,
                        unknown_run,
                    ],
                    workspace,
                )
            )
            assert client_result == response, "Generated Client returned a different result"
            (evidence / "result.json").write_text(json.dumps(response), encoding="utf-8")
            (evidence / "trace.json").write_text(json.dumps(trace_response), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "passed": True,
                        "workspace": str(workspace),
                        "fixture_run_id": args.run_id,
                        "evidence": str(evidence),
                    }
                )
            )
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="mode", required=True)
    verify = subcommands.add_parser("verify-result")
    verify.add_argument("--workspace", default=".")
    verify.add_argument("--database", required=True)
    verify.add_argument("--artifacts", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--client-method", default="getRunResult")
    phases = subcommands.add_parser("verify-phases")
    phases.add_argument("--workspace", default=".")
    phases.add_argument("--database", required=True)
    phases.add_argument("--artifacts", required=True)
    phases.add_argument("--run-id", required=True)
    phases.add_argument("--client-method", default="getRunResult")
    phases.add_argument("--previous-workspace", required=True)
    phases.add_argument("--rejected-database", required=True)
    invoke = subcommands.add_parser("invoke")
    invoke.add_argument("--workspace", required=True)
    invoke.add_argument("--evidence", required=True)
    invoke.add_argument("cli_args", nargs=argparse.REMAINDER)
    chain = subcommands.add_parser("verify-chain")
    chain.add_argument("--workspace", required=True)
    chain.add_argument("--database", required=True)
    chain.add_argument("--artifacts", required=True)
    chain.add_argument("--first-run-id", required=True)
    chain.add_argument("--second-run-id", required=True)
    chain.add_argument("--first-host", required=True)
    chain.add_argument("--second-host", required=True)
    args = parser.parse_args()
    if args.mode in ("verify-result", "verify-phases"):
        verify_result(args)
        return 0
    if args.mode == "verify-chain":
        verify_chain(args)
        return 0
    return invoke_version(args)


if __name__ == "__main__":
    raise SystemExit(main())
