"""Bounded local-process connector for the P1 Codex Worker."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from ehai import ID, normalize_id
from ehai.application.workers import (
    CandidateArtifact,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
    WorkerTimedOutError,
)
from ehai.domain.artifacts import ArtifactKind
from ehai.infrastructure.codex_transport import (
    CodexCancellation,
    CodexProcessOutcome,
    CodexProcessTransport,
    bounded_codex_text,
    read_codex_final,
    redact_codex_bytes,
    redact_codex_text,
    sanitize_codex_output,
)
from ehai.infrastructure.workers.codex_protocol import (
    CodexProtocolError,
    build_codex_prompt,
    codex_output_schema_json,
    parse_codex_result,
)


class CodexWorkerAdapter(CodexProcessTransport):
    """Execute one Codex CLI process per Worker Attempt with bounded capture."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        executable: str | Path | Sequence[str] = "codex",
        sandbox: str = "read-only",
        timeout_seconds: float = 300.0,
        cancel_grace_seconds: float = 2.0,
        max_output_bytes: int = 1_048_576,
        env_overrides: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            workspace=workspace,
            executable=executable,
            sandbox=sandbox,
            timeout_seconds=timeout_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
            max_output_bytes=max_output_bytes,
            env_overrides=env_overrides,
        )
        self._active: dict[ID, CodexCancellation] = {}
        self._pending_cancellations: set[ID] = set()
        self._active_lock = threading.Lock()

    def execute(self, request: WorkerRequest) -> WorkerResult:
        """Run Codex once and return only candidate and diagnostic Artifacts."""
        if not isinstance(request, WorkerRequest):
            raise TypeError("request must be a WorkerRequest")
        slot = self._register(request)
        try:
            prompt = build_codex_prompt(request).encode("utf-8")
            with tempfile.TemporaryDirectory(prefix="ehai-codex-worker-") as temporary:
                temporary_path = Path(temporary)
                schema_path = temporary_path / "output-schema.json"
                last_message_path = temporary_path / "last-message.json"
                schema_path.write_text(codex_output_schema_json(), encoding="utf-8")
                if slot.requested.is_set():
                    raise self._error(
                        WorkerCancelledError,
                        request,
                        "Codex execution was cancelled before process spawn",
                        diagnostics=("cancellation requested before Codex process spawn",),
                    )
                command = self._command(schema_path, last_message_path)
                try:
                    outcome = asyncio.run(self._execute_async(command, prompt, slot))
                except OSError as error:
                    raise self._error(
                        WorkerExecutionError,
                        request,
                        "Codex process could not start: "
                        f"{type(error).__name__}: "
                        f"{redact_codex_text(str(error), self._secret_values)}",
                    ) from error
                if slot.requested.is_set() and not outcome.timed_out:
                    outcome = CodexProcessOutcome(
                        return_code=outcome.return_code,
                        stdout=outcome.stdout,
                        stderr=outcome.stderr,
                        diagnostics=outcome.diagnostics,
                        event_types=outcome.event_types,
                        cancelled=True,
                        communication_error=outcome.communication_error,
                    )
                return self._interpret(request, last_message_path, outcome)
        finally:
            self._unregister(request.attempt_id, slot)

    def cancel(self, attempt_id: ID) -> None:
        """Record cancellation and wake the active process loop, even before spawn."""
        normalized_id = normalize_id(attempt_id)
        with self._active_lock:
            slot = self._active.get(normalized_id)
            if slot is None:
                self._pending_cancellations.add(normalized_id)
                return
        slot.request()

    def _interpret(
        self,
        request: WorkerRequest,
        last_message_path: Path,
        outcome: CodexProcessOutcome,
    ) -> WorkerResult:
        stdout = sanitize_codex_output(outcome.stdout, self._max_output_bytes, self._secret_values)
        stderr = sanitize_codex_output(outcome.stderr, self._max_output_bytes, self._secret_values)
        diagnostics = tuple(
            bounded_codex_text(redact_codex_text(item, self._secret_values), 500)
            for item in outcome.diagnostics
        )
        if outcome.cancelled:
            raise self._error(
                WorkerCancelledError,
                request,
                "Codex execution was cancelled",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        if outcome.timed_out:
            raise self._error(
                WorkerTimedOutError,
                request,
                f"Codex execution timed out after {self._timeout_seconds:g} seconds",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        if outcome.communication_error is not None:
            raise self._error(
                WorkerExecutionError,
                request,
                outcome.communication_error,
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        if outcome.return_code != 0:
            raise self._error(
                WorkerExecutionError,
                request,
                f"Codex exited with code {outcome.return_code}",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            )
        try:
            final_text = read_codex_final(last_message_path, self._max_output_bytes)
        except (OSError, UnicodeError, ValueError) as error:
            raise self._error(
                WorkerExecutionError,
                request,
                str(error),
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            ) from error
        try:
            parsed = parse_codex_result(final_text).worker_result
        except CodexProtocolError as error:
            raise self._error(
                WorkerExecutionError,
                request,
                f"Codex returned an invalid structured result: {error}",
                raw_output=stdout,
                log_output=stderr,
                diagnostics=diagnostics,
            ) from error
        return self._result(parsed, final_text, stdout, stderr, diagnostics)

    def _result(
        self,
        parsed: WorkerResult,
        final_text: str,
        stdout: bytes,
        stderr: bytes,
        diagnostics: tuple[str, ...],
    ) -> WorkerResult:
        candidates = tuple(
            _sanitize_candidate(artifact, self._secret_values) for artifact in parsed.artifacts
        )
        worker_output = sanitize_codex_output(
            final_text.encode("utf-8") + b"\n--- codex stdout jsonl ---\n" + stdout,
            self._max_output_bytes,
            self._secret_values,
        )
        output_artifact = CandidateArtifact(
            kind=ArtifactKind.WORKER_OUTPUT,
            name="codex-worker-output.txt",
            media_type="text/plain; charset=utf-8",
            content=worker_output,
        )
        log_artifacts = (
            (
                CandidateArtifact(
                    kind=ArtifactKind.LOG,
                    name="codex-stderr.log",
                    media_type="text/plain; charset=utf-8",
                    content=stderr,
                ),
            )
            if stderr
            else ()
        )
        return WorkerResult(
            artifacts=(*candidates, output_artifact, *log_artifacts),
            summary=redact_codex_text(parsed.summary, self._secret_values),
            raw_output=stdout,
            diagnostics=("codex exited with code 0", *diagnostics),
        )

    def _register(self, request: WorkerRequest) -> CodexCancellation:
        slot = CodexCancellation()
        with self._active_lock:
            if request.attempt_id in self._active:
                raise WorkerExecutionError(
                    request.run_id,
                    request.attempt_id,
                    request.plan_node_id,
                    "Codex process is already active for this Attempt",
                )
            if request.attempt_id in self._pending_cancellations:
                self._pending_cancellations.remove(request.attempt_id)
                slot.requested.set()
            self._active[request.attempt_id] = slot
        return slot

    def _unregister(self, attempt_id: ID, slot: CodexCancellation) -> None:
        with self._active_lock:
            if self._active.get(attempt_id) is slot:
                del self._active[attempt_id]

    def _error(
        self,
        error_type: type[WorkerExecutionError],
        request: WorkerRequest,
        reason: str,
        *,
        raw_output: bytes = b"",
        log_output: bytes = b"",
        diagnostics: tuple[str, ...] = (),
    ) -> WorkerExecutionError:
        return error_type(
            request.run_id,
            request.attempt_id,
            request.plan_node_id,
            redact_codex_text(reason, self._secret_values),
            raw_output=raw_output,
            log_output=log_output,
            diagnostics=diagnostics,
        )


def _sanitize_candidate(
    artifact: CandidateArtifact,
    secret_values: tuple[str, ...] = (),
) -> CandidateArtifact:
    return CandidateArtifact(
        kind=artifact.kind,
        name=redact_codex_text(artifact.name, secret_values),
        media_type=artifact.media_type,
        content=redact_codex_bytes(artifact.content, secret_values),
    )
