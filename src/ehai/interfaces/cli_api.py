"""CLI transport for the existing HTTP API; no local execution or state changes."""

from __future__ import annotations

import argparse
import math
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ehai import JsonValue, json_dumps, json_loads, normalize_id

_QUERIES = {
    "get-plan-import-schema": "/plans/import-schema",
    "get-run": "/runs/{run_id}",
    "get-run-plan": "/runs/{run_id}/plan",
    "get-run-checks": "/runs/{run_id}/checks",
    "get-run-adoptions": "/runs/{run_id}/adoptions",
    "get-run-interventions": "/runs/{run_id}/interventions",
    "get-run-trajectory-reviews": "/runs/{run_id}/trajectory-reviews",
    "get-trace": "/runs/{run_id}/trace",
    "get-result": "/runs/{run_id}/result",
    "get-plan": "/plans/{plan_revision_id}",
    "get-plan-checks": "/plans/{plan_revision_id}/checks",
    "get-discussion": "/planning/{conversation_id}",
    "get-process-revision": "/process-revisions/{process_revision_id}",
    "get-process-draft": "/process-drafts/{draft_id}",
    "get-run-process-drafts": "/runs/{run_id}/process-drafts",
    "get-process-review": "/process-reviews/{review_id}",
    "get-process-draft-reviews": "/process-drafts/{draft_id}/reviews",
    "get-runtime-health": "/runtime/health",
    "get-worker-profiles": "/workers/profiles",
    "get-worker-endpoints": "/workers/endpoints",
    "get-attempt-runtime": "/attempts/{attempt_id}/runtime",
    "get-worker-requests": "/attempts/{attempt_id}/worker-requests",
}
_COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "integrate-run": ("/runs/{run_id}/integrate", ("expected_process_revision_id",)),
    "import-plan": ("/plans/import", ("goal_id",)),
    "create-project": ("/projects", ("name",)),
    "create-goal": ("/goals", ("project_id", "objective")),
    "propose-plan": ("/plans/propose", ("goal_id",)),
    "discuss-plan": ("/planning/discuss", ("goal_id", "conversation_id", "source_run_id")),
    "replan-plan": ("/plans/replan", ("base_plan_revision_id", "source_run_id")),
    "approve-plan": ("/plans/approve", ("plan_revision_id", "completion_contract_id")),
    "start-run": ("/runs/start", ("plan_revision_id",)),
    "pause-run": ("/runs/{run_id}/pause", ()),
    "resume-run": ("/runs/{run_id}/resume", ()),
    "cancel-run": ("/runs/{run_id}/cancel", ("reason",)),
    "propose-process": ("/commands/propose-process", ("run_id", "reason")),
    "review-process": ("/commands/review-process", ("draft_id",)),
    "apply-process": ("/commands/apply-process", ("review_id",)),
    "decide-human-check": (
        "/check-runs/{check_run_id}/decision",
        ("request_token", "passed", "actor", "comment"),
    ),
    "reply-intervention": (
        "/interventions/{intervention_id}/reply",
        ("request_token", "actor", "message"),
    ),
    "suspend-attempt": (
        "/attempts/{attempt_id}/suspend",
        ("review_id", "through_sequence", "actor", "reason"),
    ),
    "cancel-attempt": ("/attempts/{attempt_id}/cancel", ()),
    "extend-attempt-deadline": ("/attempts/{attempt_id}/deadline", ("deadline_at",)),
    "resolve-worker-request": ("/worker-requests/{worker_request_id}/resolve", ()),
    "decline-worker-request": ("/worker-requests/{worker_request_id}/decline", ()),
}

API_ONLY_COMMANDS = frozenset(
    {
        "get-runtime-health",
        "get-worker-profiles",
        "get-worker-endpoints",
        "get-attempt-runtime",
        "get-worker-requests",
        "suspend-attempt",
        "cancel-attempt",
        "extend-attempt-deadline",
        "resolve-worker-request",
        "decline-worker-request",
    }
)


class ApiCommandError(Exception):
    """Structured remote error, preserving the API's code and HTTP status."""

    def __init__(self, document: dict[str, JsonValue]) -> None:
        super().__init__("EHAI API command failed")
        self.document = document


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _object_file(path: object) -> dict[str, JsonValue]:
    from pathlib import Path

    if not isinstance(path, Path):
        raise ValueError("a JSON file path is required")
    value = json_loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON file must contain an object")
    return value


def _request_arguments(args: argparse.Namespace) -> tuple[str, str, dict[str, JsonValue] | None]:
    command = args.command
    values = vars(args).copy()
    # All path variables are public UUIDs, never unescaped user-provided URL paths.
    for key, value in values.items():
        if key.endswith("_id") and value is not None:
            values[key] = str(normalize_id(value))
    if command in _QUERIES:
        return "GET", _QUERIES[command].format_map(values), None
    if command not in _COMMANDS:
        raise ValueError(
            f"{command} is local-only; use start-run/resume-run for API-hosted execution"
        )
    path, fields = _COMMANDS[command]
    body: dict[str, JsonValue] = (
        {} if command == "integrate-run" else {"idempotency_key": args.idempotency_key}
    )
    for field in fields:
        body[field] = values[field]
    if command in {"propose-plan", "discuss-plan", "replan-plan"}:
        body["criteria"] = list(args.criterion or ())
    if command == "discuss-plan":
        body["message"] = (
            args.message
            if args.message_file is None
            else args.message_file.read_text(encoding="utf-8-sig")
        )
    if command == "start-run":
        if not args.authorize or args.execution_config is None:
            raise ValueError("API start-run requires --execution-config and explicit --authorize")
        body["execution_config"] = _object_file(args.execution_config)
    if command == "resolve-worker-request":
        body["resolution"] = _object_file(args.resolution_file)
    if command == "import-plan":
        body["plan"] = _object_file(args.file)
    return "POST", path.format_map(values), body


def dispatch_api(args: argparse.Namespace) -> JsonValue:
    """Send exactly one request; never retry, redirect, or fall back to a local DB."""
    method, path, body = _request_arguments(args)
    return request_api(args.api_url, method, "/api/v1" + path, body, args.api_timeout_seconds)


def request_api(
    api_url: str,
    method: str,
    path: str,
    body: dict[str, JsonValue] | None = None,
    timeout: float | None = None,
    *,
    unwrap: bool = True,
) -> JsonValue:
    """Shared CLI/MCP HTTP transport. Callers select fixed API routes, not arbitrary URLs."""
    parsed = urlsplit(api_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/api/v1"}
    ):
        raise ValueError(
            "--api-url must be an HTTP(S) server origin or /api/v1 URL without credentials"
        )
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("--api-timeout-seconds must be finite and positive")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    request = Request(
        origin + path,
        data=None if body is None else json_dumps(body).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            document = json_loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            detail = json_loads(error.read().decode("utf-8"))
        except (ValueError, UnicodeError):
            detail = {"error": {"code": "http_error", "message": "Non-JSON API error response"}}
        raise ApiCommandError({"http_status": error.code, "response": detail}) from error
    except (URLError, OSError, HTTPException, ValueError) as error:
        raise ApiCommandError(
            {
                "error": "API response was not confirmed; no retry was attempted. "
                "For a write, query the host before submitting another command.",
                "error_type": type(error).__name__,
            }
        ) from error
    if not unwrap:
        return document
    if not isinstance(document, dict) or "data" not in document:
        raise ApiCommandError({"error": "Invalid API response envelope; no retry was attempted"})
    return document["data"]
