"""Reusable independent Connector client: HTTP plus its own durable outbox."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from ehai import JsonValue, utc_now
from ehai.application.connector_models import (
    ConnectorClaim,
    ConnectorManifest,
    ReportConnectorResultRequest,
    SubmitConnectorEventRequest,
)


@dataclass(frozen=True)
class ConnectorOutcome:
    status: Literal["completed", "failed", "unknown"]
    output: dict[str, JsonValue] | None = None
    error: str | None = None
    external_operation_ref: str | None = None
    event_type: str | None = None


class ConnectorAdapter(Protocol):
    def execute(self, claim: ConnectorClaim) -> ConnectorOutcome: ...


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Connector file must contain an object")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8"
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "process.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _api_base(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Core URL must not contain credentials, query or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Use HTTPS or a loopback HTTP core URL")
    base = url.rstrip("/")
    return base + "/" if base.endswith("/api/v1") else base + "/api/v1/"


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> Any:
    response = client.post(path, json=body)
    if response.status_code >= 400:
        # No response body or credentials in terminal errors.
        raise RuntimeError(f"Core rejected connector request with HTTP {response.status_code}")
    return response.json()["data"]


def _initialize(
    directory: Path,
    api_url: str,
    project_id: str,
    manifest: ConnectorManifest,
    configuration: dict[str, Any],
    name: str,
) -> None:
    config_path = directory / "bridge.json"
    if config_path.exists():
        raise ValueError("Connector is already initialized; reuse its state directory")
    registration_path = directory / "registration.json"
    if not registration_path.exists():
        token = secrets.token_urlsafe(32)
        _write(directory / "bridge-token.json", {"token": token})
        _write(
            registration_path,
            {
                "api_url": _api_base(api_url),
                "project_id": project_id,
                "worker_id": str(uuid4()),
                "request": {
                    "idempotency_key": str(uuid4()),
                    "name": name,
                    "manifest": manifest.model_dump(mode="json"),
                    "configuration": configuration,
                    "credential_sha256": hashlib.sha256(token.encode()).hexdigest(),
                },
            },
        )
    registration = _read(registration_path)
    if (
        registration["api_url"] != _api_base(api_url)
        or registration["project_id"] != project_id
        or registration["request"]["configuration"] != configuration
        or registration["request"]["manifest"] != manifest.model_dump(mode="json")
    ):
        raise ValueError("Pending registration belongs to different connector settings")
    with httpx.Client(base_url=registration["api_url"], trust_env=False, timeout=30) as client:
        connection = _post(client, f"projects/{project_id}/connectors", registration["request"])
    _write(
        config_path,
        {
            "api_url": registration["api_url"],
            "connector_id": connection["connector_id"],
            "worker_id": registration["worker_id"],
        },
    )
    print(json.dumps({"connector_id": connection["connector_id"], "state_dir": str(directory)}))


def _run_once(
    directory: Path, core: httpx.Client, adapter: ConnectorAdapter, config: dict[str, Any]
) -> bool:
    pending_path = directory / "pending.json"
    fresh = False
    if pending_path.exists():
        pending = _read(pending_path)
    else:
        raw = _post(
            core,
            f"connector-bridge/{config['connector_id']}/claims",
            {"protocol_version": 1, "worker_id": config["worker_id"]},
        )
        if raw is None:
            return False
        pending = {"claim": raw}
        _write(pending_path, pending)
        fresh = True
    claim = ConnectorClaim.model_validate(pending["claim"])
    if "report" not in pending:
        if not fresh:
            claim = claim.model_copy(update={"mode": "reconcile"})
        result = adapter.execute(claim)
        report = ReportConnectorResultRequest(
            delivery_id=str(uuid4()),
            call_id=claim.call.call_id,
            claim_token=claim.claim_token,
            status=result.status,
            output=result.output,
            error=result.error,
            external_operation_ref=result.external_operation_ref,
        )
        pending["report"] = report.model_dump(mode="json")
        if result.event_type is not None and result.output is not None:
            event = SubmitConnectorEventRequest(
                event_id=str(uuid5(NAMESPACE_URL, claim.call.call_id + ":" + result.event_type)),
                event_type=result.event_type,
                schema_version="1",
                occurred_at=utc_now(),
                subject=result.external_operation_ref or claim.call.call_id,
                data=result.output,
            )
            pending["event"] = event.model_dump(mode="json")
        _write(pending_path, pending)
    prefix = f"connector-bridge/{config['connector_id']}/"
    _post(core, prefix + "results", pending["report"])
    if "event" in pending:
        _post(core, prefix + "events", pending["event"])
    receipts = directory / "receipts"
    _write(receipts / (claim.call.call_id + ".json"), pending)
    pending_path.unlink()
    print(json.dumps({"call_id": claim.call.call_id, "status": pending["report"]["status"]}))
    return True
