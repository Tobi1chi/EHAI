"""Foreground execution sessions and their credential-free configuration."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ehai import ID, JsonValue, json_dumps, json_loads, new_id, normalize_id, utc_now
from ehai.application.agent_trace import AgentTraceEventType
from ehai.application.commands import PauseRun, ResumeRun, StartRun
from ehai.application.goal_budgets import GoalWorkerBudget, validate_goal_worker_budget
from ehai.application.legacy_config import ResponsesEndpointCapabilities
from ehai.application.pause_causes import PauseCause
from ehai.application.process_adjustments import ProcessAdjustmentPolicy
from ehai.application.queries import QueryService
from ehai.application.scheduler import ConcurrentRuntime
from ehai.application.service import ExecutionService
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import AttemptActivity
from ehai.infrastructure.agent_traces import SQLiteAgentTraceStore
from ehai.infrastructure.pi_config import PiBackendConfig
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.public_documents import public_json_value

if TYPE_CHECKING:
    from ehai.application.process_adjustments import ProcessAdjustments
    from ehai.interfaces.runtime import LocalRuntimeComposition


_CONFIG_VERSION = 1
_TERMINAL_RUN_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
_WORKER_KINDS = {"builtin", "pi", "codex-server"}
_APPROVAL_POLICIES = {"untrusted", "on-request", "never"}
_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
_GIT_PERMISSIONS = {
    "git.read",
    "git.local_write",
    "git.remote_write",
    "git.dangerous",
}
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "token",
    "access_key",
)
_CODE_DELIVERY_FIELDS = (
    "workspace",
    "base_commit",
    "commit",
    "diff_path",
    "attempt_id",
    "run_id",
    "diff_artifact_ids",
)


class SessionHostError(RuntimeError):
    """A foreground session could not be created or resumed safely."""


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """The exact, credential-free settings pinned to one execution Run."""

    worker_kind: str
    model: str
    reasoning_effort: str | None
    capacity: int
    allowed_commands: tuple[tuple[str, ...], ...]
    available_shells: tuple[str, ...]
    git_permissions: frozenset[str]
    workspace: Path
    endpoint_capabilities: ResponsesEndpointCapabilities
    command_timeout_seconds: float = 30.0
    codex_server_executable: tuple[str, ...] = ("codex",)
    codex_server_approval_policy: str = "on-request"
    codex_server_sandbox: str = "workspace-write"
    process_adjustment: ProcessAdjustmentPolicy | None = None
    goal_worker_budget: GoalWorkerBudget | None = None
    pi_backend: PiBackendConfig | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_kind", _worker_kind(self.worker_kind))
        object.__setattr__(self, "model", _text(self.model, "model"))
        if self.reasoning_effort is not None:
            object.__setattr__(
                self,
                "reasoning_effort",
                _text(self.reasoning_effort, "reasoning_effort"),
            )
        if self.worker_kind == "pi" and self.pi_backend is None:
            raise ValueError("Pi execution config requires the pi backend object")
        if type(self.capacity) is not int or self.capacity < 1:
            raise ValueError("capacity must be a positive integer")
        commands = tuple(
            tuple(_text(argument, "allowed command argument") for argument in argv)
            for argv in self.allowed_commands
        )
        if any(not argv for argv in commands):
            raise ValueError("allowed_commands must contain non-empty argv arrays")
        if len(set(commands)) != len(commands):
            raise ValueError("allowed_commands must not contain duplicates")
        object.__setattr__(self, "allowed_commands", commands)
        shells = tuple(_text(shell, "available shell") for shell in self.available_shells)
        if len(set(shells)) != len(shells):
            raise ValueError("available_shells must not contain duplicates")
        object.__setattr__(self, "available_shells", shells)
        permissions = frozenset(self.git_permissions)
        if not permissions.issubset(_GIT_PERMISSIONS):
            raise ValueError("git_permissions contains an unknown permission")
        object.__setattr__(self, "git_permissions", permissions)
        workspace = Path(self.workspace).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        object.__setattr__(self, "workspace", workspace)
        if not isinstance(self.endpoint_capabilities, ResponsesEndpointCapabilities):
            raise TypeError("endpoint_capabilities must be ResponsesEndpointCapabilities")
        if (
            isinstance(self.command_timeout_seconds, bool)
            or not isinstance(self.command_timeout_seconds, (int, float))
            or not math.isfinite(self.command_timeout_seconds)
            or self.command_timeout_seconds <= 0
        ):
            raise ValueError("command_timeout_seconds must be positive and finite")
        object.__setattr__(self, "command_timeout_seconds", float(self.command_timeout_seconds))
        executable = tuple(
            _text(item, "Codex App Server executable argument")
            for item in self.codex_server_executable
        )
        if not executable:
            raise ValueError("codex_server_executable must not be empty")
        object.__setattr__(self, "codex_server_executable", executable)
        if self.codex_server_approval_policy not in _APPROVAL_POLICIES:
            raise ValueError("unsupported Codex App Server approval policy")
        if self.codex_server_sandbox not in _SANDBOX_MODES:
            raise ValueError("unsupported Codex App Server sandbox mode")
        if self.process_adjustment is not None and not isinstance(
            self.process_adjustment, ProcessAdjustmentPolicy
        ):
            raise TypeError("process_adjustment must be a ProcessAdjustmentPolicy or None")
        if self.goal_worker_budget is not None and not isinstance(
            self.goal_worker_budget, GoalWorkerBudget
        ):
            raise TypeError("goal_worker_budget must be a GoalWorkerBudget or None")

    @classmethod
    def from_path(cls, path: Path) -> ExecutionConfig:
        document = json_loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("execution config must contain a JSON object")
        return cls.from_document(document)

    @classmethod
    def from_document(cls, document: Mapping[str, JsonValue]) -> ExecutionConfig:
        if not isinstance(document, Mapping):
            raise ValueError("execution config must contain a JSON object")
        _reject_sensitive_keys(document)
        allowed = {
            "config_version",
            "worker_kind",
            "model",
            "reasoning_effort",
            "capacity",
            "allowed_commands",
            "available_shells",
            "git_permissions",
            "workspace",
            "endpoint_capabilities",
            "command_timeout_seconds",
            "codex_server",
            "process_adjustment",
            "goal_worker_budget",
            "pi",
        }
        unknown = sorted(set(document) - allowed)
        if unknown:
            raise ValueError(f"execution config contains unknown fields: {unknown}")
        if document.get("config_version", _CONFIG_VERSION) != _CONFIG_VERSION:
            raise ValueError("unsupported execution config version")
        server = document.get("codex_server", {})
        if not isinstance(server, dict):
            raise ValueError("codex_server must be an object")
        unknown_server = sorted(set(server) - {"executable", "approval_policy", "sandbox"})
        if unknown_server:
            raise ValueError(f"codex_server contains unknown fields: {unknown_server}")
        worker_kind = document.get("worker_kind")
        model = document.get("model")
        workspace = document.get("workspace")
        if not isinstance(worker_kind, str):
            raise ValueError("worker_kind must be text")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be non-empty text")
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("workspace must be non-empty text")
        reasoning = document.get("reasoning_effort")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError("reasoning_effort must be text or null")
        executable_value = server.get("executable", ["codex"])
        executable = (
            [executable_value]
            if isinstance(executable_value, str)
            else _string_list(executable_value, "codex_server.executable")
        )
        approval_policy = server.get("approval_policy", "on-request")
        sandbox = server.get("sandbox", "workspace-write")
        if not isinstance(approval_policy, str) or not isinstance(sandbox, str):
            raise ValueError("codex_server policy fields must be text")
        adjustment = document.get("process_adjustment")
        if adjustment is not None and not isinstance(adjustment, dict):
            raise ValueError("process_adjustment must be an object or null")
        goal_budget = document.get("goal_worker_budget")
        if goal_budget is not None and not isinstance(goal_budget, dict):
            raise ValueError("goal_worker_budget must be an object or null")
        pi_document = document.get("pi")
        if pi_document is not None and not isinstance(pi_document, dict):
            raise ValueError("pi must be a backend configuration object")
        return cls(
            pi_backend=None if pi_document is None else PiBackendConfig.from_document(pi_document),
            worker_kind=worker_kind,
            model=model,
            reasoning_effort=reasoning,
            capacity=_integer(document.get("capacity", 1), "capacity"),
            allowed_commands=_argv_list(document.get("allowed_commands", []), "allowed_commands"),
            available_shells=tuple(
                _string_list(document.get("available_shells", []), "available_shells")
            ),
            git_permissions=frozenset(
                _string_list(document.get("git_permissions", []), "git_permissions")
            ),
            workspace=Path(workspace),
            endpoint_capabilities=_endpoint_capabilities(document.get("endpoint_capabilities", {})),
            command_timeout_seconds=_number(
                document.get("command_timeout_seconds", 30.0),
                "command_timeout_seconds",
            ),
            codex_server_executable=tuple(executable),
            codex_server_approval_policy=approval_policy,
            codex_server_sandbox=sandbox,
            process_adjustment=(
                None if adjustment is None else ProcessAdjustmentPolicy.from_document(adjustment)
            ),
            goal_worker_budget=(
                None if goal_budget is None else GoalWorkerBudget.from_document(goal_budget)
            ),
        )

    def to_document(self) -> dict[str, JsonValue]:
        document: dict[str, JsonValue] = {
            "config_version": _CONFIG_VERSION,
            "worker_kind": self.worker_kind,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "capacity": self.capacity,
            "allowed_commands": cast(JsonValue, [list(argv) for argv in self.allowed_commands]),
            "available_shells": cast(JsonValue, list(self.available_shells)),
            "git_permissions": cast(JsonValue, sorted(self.git_permissions)),
            "workspace": str(self.workspace),
            "endpoint_capabilities": {
                "supports_background": self.endpoint_capabilities.supports_background,
                "supports_unique_items": self.endpoint_capabilities.supports_unique_items,
                "supports_idempotent_create": self.endpoint_capabilities.supports_idempotent_create,
                "supports_previous_response_id": (
                    self.endpoint_capabilities.supports_previous_response_id
                ),
                "supports_response_retrieval": (
                    self.endpoint_capabilities.supports_response_retrieval
                ),
            },
            "command_timeout_seconds": self.command_timeout_seconds,
            "codex_server": {
                "executable": cast(JsonValue, list(self.codex_server_executable)),
                "approval_policy": self.codex_server_approval_policy,
                "sandbox": self.codex_server_sandbox,
            },
        }
        # Preserve the canonical document/fingerprint of previously authorized
        # configurations. Omitted policy never silently enables model calls.
        if self.process_adjustment is not None:
            document["process_adjustment"] = self.process_adjustment.to_document()
        if self.goal_worker_budget is not None:
            document["goal_worker_budget"] = self.goal_worker_budget.to_document()
        if self.pi_backend is not None:
            document["pi"] = self.pi_backend.to_document()
        return document

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json_dumps(self.to_document()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _SessionNotice:
    kind: str
    message: str
    attempt_id: ID | None = None

    def to_document(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "message": self.message, "attempt_id": self.attempt_id}


def authorize_run(database: SQLiteDatabase, run_id: ID, config: ExecutionConfig) -> Run:
    normalized_id = normalize_id(run_id)
    with database.unit_of_work() as uow:
        run = uow.states.get_run(normalized_id)
        if run is None:
            raise SessionHostError(f"Run {normalized_id} is not persisted")
        events = uow.events.list_events()
        stored = _stored_config(events, normalized_id)
        expected = config.to_document()
        validate_goal_worker_budget(
            uow,
            run.goal_id,
            None if config.goal_worker_budget is None else config.goal_worker_budget.to_document(),
        )
        if stored is not None and ExecutionConfig.from_document(stored).to_document() != expected:
            raise SessionHostError(f"Run {normalized_id} already has another execution config")
        if stored is not None and not _authorized(events, normalized_id):
            raise SessionHostError(f"Run {normalized_id} has no explicit authorization")
        if run.status is RunStatus.PENDING:
            run = run.start(at=utc_now())
            uow.states.put_run(run)
            uow.events.append(
                Event(
                    type=EventType.RUN_STARTED,
                    run_id=run.run_id,
                    correlation_id=run.run_id,
                    payload={
                        "run_id": run.run_id,
                        "execution_config": expected,
                        "execution_config_fingerprint": config.fingerprint,
                        "authorization": {"explicit": True},
                    },
                    occurred_at=run.started_at,
                )
            )
        elif stored is None:
            raise SessionHostError(
                f"Run {normalized_id} is already {run.status.value} without an authorized config"
            )
        uow.commit()
        return run


def load_execution_config(database: SQLiteDatabase, run_id: ID) -> ExecutionConfig:
    normalized_id = normalize_id(run_id)
    with database.read_session() as session:
        if session.states.get_run(normalized_id) is None:
            raise SessionHostError(f"Run {normalized_id} is not persisted")
        document = _stored_config(session.events.list_events(), normalized_id)
        authorized = _authorized(session.events.list_events(), normalized_id)
    if document is None or not authorized:
        raise SessionHostError(f"Run {normalized_id} has no explicit execution config")
    try:
        return ExecutionConfig.from_document(document)
    except (TypeError, ValueError) as error:
        raise SessionHostError(f"Run {normalized_id} has an invalid execution config") from error


def prepare_execute_run(
    service: ExecutionService,
    database: SQLiteDatabase,
    plan_revision_id: ID,
    idempotency_key: str,
    config: ExecutionConfig,
) -> Run:
    del database
    return service.start_run(
        StartRun(
            idempotency_key,
            normalize_id(plan_revision_id),
            authorized_execution_config_json=json_dumps(config.to_document()),
        )
    )


def prepare_resume_run(
    service: ExecutionService,
    database: SQLiteDatabase,
    run_id: ID,
    *,
    process_adjustments: ProcessAdjustments | None = None,
) -> tuple[Run, ExecutionConfig]:
    normalized_id = normalize_id(run_id)
    config = load_execution_config(database, normalized_id)
    run = service.get_run(normalized_id)
    if run.status is RunStatus.PAUSED:
        # A rejected Reviewer needs a scoped process adjustment, not a retry
        # of its succeeded Attempt. Preserve the pause and failed Gate until
        # the foreground coordinator applies and reviews the repair process.
        if (
            process_adjustments is None
            or normalized_id not in process_adjustments.pending_run_ids()
        ):
            run = service.resume_run(ResumeRun(str(new_id()), normalized_id))
    elif run.status is RunStatus.PENDING:
        run = authorize_run(database, normalized_id, config)
    ensure_dispatch_work(database, run)
    return run, config


def ensure_dispatch_work(database: SQLiteDatabase, run: Run) -> None:
    if run.status is not RunStatus.RUNNING:
        return
    with database.unit_of_work() as uow:
        work = tuple(item for item in uow.states.list_dispatch_work() if item.run_id == run.run_id)
        if not work:
            uow.states.put_dispatch_work(DispatchWork(run_id=run.run_id))
        elif work[0].status is DispatchWorkStatus.COMPLETED:
            uow.states.put_dispatch_work(work[0].requeue())
        uow.commit()


class ForegroundSessionHost:
    """Own the local Runtime until completion or a user-facing notice."""

    def __init__(
        self,
        composition: LocalRuntimeComposition,
        *,
        stderr: object,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._composition = composition
        self._stderr = stderr
        if not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._quiesced = False

    async def run(self, run_id: ID) -> dict[str, JsonValue]:
        normalized_id = normalize_id(run_id)
        runtime = self._composition.runtime
        if not isinstance(runtime, ConcurrentRuntime):
            raise SessionHostError("foreground execution requires the concurrent P2 Runtime")
        self._assert_exclusive(normalized_id)
        runtime_task: asyncio.Task[tuple[Run, ...]] | None = None
        adjustment_notice: _SessionNotice | None = None
        self._composition.runtime_control.mark_runtime_starting()
        try:

            async def execute_runtime() -> tuple[Run, ...]:
                nonlocal adjustment_notice
                await runtime.recover_startup()
                while True:
                    current = self._read_run(normalized_id)
                    if current.status in _TERMINAL_RUN_STATUSES:
                        return (current,)
                    self._composition.runtime_control.mark_runtime_healthy()
                    completed = await runtime.run_until_idle()
                    current = self._read_run(normalized_id)
                    coordinator = self._composition.process_adjustments
                    if current.status is RunStatus.PAUSED and coordinator is not None:
                        await runtime.quiesce_run(normalized_id)
                        runtime.release_run_dispatch(normalized_id)
                        adjustment = await coordinator.advance(normalized_id)
                        if adjustment.resumed:
                            runtime.resume_run_scheduling(normalized_id)
                            continue
                        adjustment_notice = _SessionNotice(
                            "process_adjustment_stopped", adjustment.reason
                        )
                    return completed

            runtime_task = asyncio.create_task(execute_runtime())
            last_progress: str | None = None
            while True:
                done, _ = await asyncio.wait({runtime_task}, timeout=self._poll_interval_seconds)
                current = self._read_run(normalized_id)
                waiting = self._waiting_notice(normalized_id)
                if waiting is not None:
                    await self._quiesce_and_pause(
                        normalized_id,
                        pause_cause=(
                            PauseCause.UNKNOWN_EXECUTION
                            if waiting.kind == "provider_outcome_unknown"
                            else PauseCause.WORKER_WAITING
                        ),
                    )
                    return await self._document(normalized_id, waiting)
                if done:
                    runtime_task.result()
                    if current.status in _TERMINAL_RUN_STATUSES:
                        return await self._document(normalized_id)
                    if self._composition.execution_service.orchestrator.waiting_for_intervention(
                        normalized_id, only_if_idle=True
                    ):
                        runtime.release_waiting_run(normalized_id)
                        return await self._document(
                            normalized_id,
                            _SessionNotice(
                                "intervention_waiting",
                                "A retained path needs a version-bound user reply; use "
                                "get-run-interventions and reply-intervention, then resume-session",
                            ),
                        )
                    if self._composition.execution_service.orchestrator.waiting_for_human(
                        normalized_id, only_if_idle=True
                    ):
                        runtime.release_waiting_run(normalized_id)
                        return await self._document(
                            normalized_id,
                            _SessionNotice(
                                "human_gate_waiting",
                                "Required human Checks are awaiting a version-bound decision; "
                                "use get-run-checks and decide-human-check, then resume-session",
                            ),
                        )
                    await self._quiesce_and_pause(
                        normalized_id,
                        pause_cause=PauseCause.RUNTIME_IDLE,
                    )
                    return await self._document(
                        normalized_id,
                        adjustment_notice
                        or _SessionNotice(
                            "runtime_idle",
                            f"Runtime became idle while Run remained {current.status.value}",
                        ),
                    )
                progress = self._progress_line(normalized_id, current)
                if progress != last_progress:
                    _write_line(self._stderr, progress)
                    last_progress = progress
        except asyncio.CancelledError:
            await self._quiesce_and_pause(
                normalized_id,
                pause_cause=PauseCause.OPERATOR,
            )
            raise
        except Exception as error:
            await self._quiesce_and_pause(
                normalized_id,
                pause_cause=PauseCause.RUNTIME_ERROR,
            )
            return await self._document(
                normalized_id, _SessionNotice("runtime_error", _error(error))
            )
        finally:
            if runtime_task is not None and not runtime_task.done():
                runtime_task.cancel()
                await asyncio.gather(runtime_task, return_exceptions=True)
            self._composition.runtime_control.mark_runtime_stopped()
            await _close_connectors(self._composition)

    def _assert_exclusive(self, run_id: ID) -> None:
        with self._composition.database.read_session() as session:
            for work in session.states.list_dispatch_work():
                if work.run_id == run_id or work.status not in {
                    DispatchWorkStatus.PENDING,
                    DispatchWorkStatus.CLAIMED,
                }:
                    continue
                other = session.states.get_run(work.run_id)
                if other is not None and other.status not in _TERMINAL_RUN_STATUSES:
                    raise SessionHostError(
                        f"another non-terminal Run {other.run_id} has active dispatch work"
                    )

    def _read_run(self, run_id: ID) -> Run:
        with self._composition.database.read_session() as session:
            run = session.states.get_run(run_id)
        if run is None:
            raise SessionHostError(f"Run {run_id} is not persisted")
        return run

    def _waiting_notice(self, run_id: ID) -> _SessionNotice | None:
        with self._composition.database.read_session() as session:
            attempts = session.states.list_attempts(run_id)
        attempt = next(
            (
                item
                for item in attempts
                if item.status is AttemptStatus.RUNNING and item.activity is AttemptActivity.WAITING
            ),
            None,
        )
        if (
            attempt is not None
            and attempt.agent_session_ref_id is not None
            and attempt.execution_handle is not None
            and (
                attempt.execution_handle.builtin is not None
                or (
                    self._composition.execution_config is not None
                    and self._composition.execution_config.worker_kind == "pi"
                )
            )
        ):
            builtin_session = SQLiteAgentTraceStore(self._composition.database).load(
                attempt.agent_session_ref_id
            )
            if any(
                event.attempt_id == attempt.attempt_id
                and (
                    event.type is AgentTraceEventType.BACKEND_ERROR
                    or (
                        event.type is AgentTraceEventType.MODEL_TRANSPORT
                        and event.payload.get("phase") == "unknown_outcome"
                    )
                )
                for event in builtin_session.events
            ):
                return _SessionNotice(
                    "provider_outcome_unknown",
                    "Provider response outcome is unknown; inspect the transport trace and "
                    "external state before explicitly resuming",
                    attempt.attempt_id,
                )
        return (
            None
            if attempt is None
            else _SessionNotice(
                "worker_waiting",
                "Worker is waiting for an external decision or input",
                attempt.attempt_id,
            )
        )

    async def _quiesce_and_pause(
        self,
        run_id: ID,
        *,
        pause_cause: PauseCause = PauseCause.RUNTIME_IDLE,
    ) -> None:
        runtime = self._composition.runtime
        if not isinstance(runtime, ConcurrentRuntime):
            return
        if not self._quiesced:
            await runtime.quiesce_run(run_id)
            self._quiesced = True
        current = self._read_run(run_id)
        if pause_cause is PauseCause.OPERATOR:
            if current.status in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED}:
                self._composition.execution_service.pause_run(
                    PauseRun(str(new_id()), normalize_id(run_id))
                )
        elif current.status in {RunStatus.PENDING, RunStatus.RUNNING}:
            self._composition.execution_service.pause_run_internal(
                normalize_id(run_id),
                pause_cause=pause_cause,
            )
        runtime.release_run_dispatch(run_id)

    async def _document(
        self,
        run_id: ID,
        notice: _SessionNotice | None = None,
    ) -> dict[str, JsonValue]:
        document = await get_live_result_document(
            self._composition.database,
            self._composition.artifact_root,
            run_id,
            self._composition.connector,
        )
        document["notice"] = None if notice is None else notice.to_document()
        return document

    def _progress_line(self, run_id: ID, run: Run) -> str:
        with self._composition.database.read_session() as session:
            attempts = session.states.list_attempts(run_id)
        active = sum(item.status is AttemptStatus.RUNNING for item in attempts)
        return (
            f"ehai run={run_id} status={run.status.value} attempts={len(attempts)} active={active}"
        )


def get_result_document(
    database_path: Path,
    artifact_root: Path,
    run_id: ID,
) -> dict[str, JsonValue]:
    database = SQLiteDatabase(database_path)
    return _result_document(database, artifact_root, normalize_id(run_id))


async def get_live_result_document(
    database: SQLiteDatabase,
    artifact_root: Path,
    run_id: ID,
    connector: object,
) -> dict[str, JsonValue]:
    normalized_run_id = normalize_id(run_id)
    document = _result_document(database, artifact_root, normalized_run_id)
    result_method = getattr(connector, "result", None)
    if not callable(result_method):
        return document
    try:
        value = result_method(normalize_id(run_id))
        if inspect.isawaitable(value):
            value = await value
    except Exception as error:
        document["result_error"] = _error(error)
        return document
    if isinstance(value, Mapping):
        delivery = value.get("code_delivery")
        result = document.get("result")
        if isinstance(delivery, Mapping) and isinstance(result, dict):
            _merge_live_delivery(result, delivery, normalized_run_id)
    return document


def _result_document(
    database: SQLiteDatabase,
    artifact_root: Path,
    run_id: ID,
) -> dict[str, JsonValue]:
    queries = QueryService(
        read_session_factory=database.read_session,
        builtin_session_reader=SQLiteAgentTraceStore(database),
    )
    trace = queries.get_execution_trace(run_id)
    public_trace = public_json_value(trace)
    if not isinstance(public_trace, dict):
        raise SessionHostError("execution trace did not produce a JSON object")
    code_delivery: dict[str, JsonValue] = {
        "workspace": None,
        "base_commit": None,
        "commit": None,
        "diff_path": None,
        "attempt_id": None,
        "run_id": run_id,
        "diff_artifact_ids": [],
        "check_result": None,
        "configured_workspace": None,
    }
    config = _stored_config(trace.events, run_id)
    if config is not None and isinstance(config.get("workspace"), str):
        code_delivery["configured_workspace"] = config["workspace"]
    for stored in trace.events:
        payload = stored.event.payload
        delivery = payload.get("code_delivery")
        if isinstance(delivery, Mapping):
            for key in ("workspace", "base_commit", "commit", "diff_path", "attempt_id", "run_id"):
                value = delivery.get(key)
                if isinstance(value, str):
                    code_delivery[key] = value
            diff_ids = delivery.get("diff_artifact_ids")
            if isinstance(diff_ids, list) and all(isinstance(item, str) for item in diff_ids):
                code_delivery["diff_artifact_ids"] = cast(JsonValue, diff_ids)
    public_attempts = public_trace.get("attempts")
    public_checks = public_trace.get("check_runs")
    delivery_attempt_id = code_delivery.get("attempt_id")
    if isinstance(public_checks, list):
        checks_by_attempt: dict[str, list[JsonValue]] = {}
        for item in public_checks:
            if not isinstance(item, dict):
                continue
            attempt_id = item.get("attempt_id")
            if isinstance(attempt_id, str):
                checks_by_attempt.setdefault(attempt_id, []).append(item)
        if isinstance(delivery_attempt_id, str):
            checks = checks_by_attempt.get(delivery_attempt_id, [])
            has_corresponding_delivery_checks = bool(checks)
        else:
            latest_attempt_id = _latest_attempt_with_checks(public_attempts, checks_by_attempt)
            checks = (
                checks_by_attempt.get(latest_attempt_id, [])
                if latest_attempt_id is not None
                else []
            )
            has_corresponding_delivery_checks = True
        if has_corresponding_delivery_checks:
            check_run_ids: list[JsonValue] = []
            for check in checks:
                if not isinstance(check, dict):
                    continue
                check_run_id = check.get("check_run_id")
                if isinstance(check_run_id, str):
                    check_run_ids.append(check_run_id)
            code_delivery["check_result"] = cast(
                JsonValue,
                {
                    "passed": _checks_passed(checks),
                    "check_run_ids": check_run_ids,
                    "checks": checks,
                },
            )
    trace_ids: dict[str, JsonValue] = {
        "attempt_ids": _ids(public_attempts, "attempt_id"),
        "artifact_ids": _ids(public_trace.get("artifacts"), "artifact_id"),
        "check_run_ids": _ids(public_checks, "check_run_id"),
        "checkpoint_ids": _ids(public_trace.get("checkpoints"), "checkpoint_id"),
        "event_ids": cast(JsonValue, [stored.event.id for stored in trace.events]),
    }
    return {
        "run": public_trace.get("run"),
        "result": {
            **code_delivery,
            "artifact_root": str(Path(artifact_root).resolve()),
        },
        "trace_ids": trace_ids,
    }


def _latest_attempt_with_checks(
    attempts: JsonValue | None,
    checks_by_attempt: Mapping[str, Sequence[JsonValue]],
) -> str | None:
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict):
            continue
        attempt_id = attempt.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id in checks_by_attempt:
            return attempt_id
    return None


def _merge_live_delivery(
    result: dict[str, JsonValue], delivery: Mapping[str, object], run_id: ID
) -> None:
    """Use a live snapshot only when it cannot replace durable result evidence."""
    if delivery.get("run_id") != run_id:
        return
    live_attempt_id = delivery.get("attempt_id")
    if not isinstance(live_attempt_id, str):
        return
    durable_attempt_id = result.get("attempt_id")
    if not isinstance(durable_attempt_id, str) or live_attempt_id != durable_attempt_id:
        return
    durable_commit = result.get("commit")
    live_commit = delivery.get("commit")
    if not isinstance(durable_commit, str) or live_commit != durable_commit:
        return
    _copy_delivery_fields(result, delivery, only_missing=True)


def _copy_delivery_fields(
    target: dict[str, JsonValue], source: Mapping[str, object], *, only_missing: bool
) -> None:
    for key in _CODE_DELIVERY_FIELDS:
        if only_missing and target.get(key) is not None:
            continue
        value = source.get(key)
        if key == "diff_artifact_ids":
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                target[key] = cast(JsonValue, list(value))
        elif isinstance(value, str):
            target[key] = value


def _stored_config(events: Sequence[object], run_id: ID) -> dict[str, JsonValue] | None:
    documents: list[dict[str, JsonValue]] = []
    for stored in events:
        event = getattr(stored, "event", None)
        if (
            not isinstance(event, Event)
            or event.run_id != run_id
            or event.type is not EventType.RUN_STARTED
        ):
            continue
        document = event.payload.get("execution_config")
        if isinstance(document, dict):
            documents.append(document)
    if not documents:
        return None
    if any(document != documents[0] for document in documents[1:]):
        raise SessionHostError(f"Run {run_id} has conflicting execution config events")
    return documents[0]


def _authorized(events: Sequence[object], run_id: ID) -> bool:
    for stored in events:
        event = getattr(stored, "event", None)
        if (
            not isinstance(event, Event)
            or event.run_id != run_id
            or event.type is not EventType.RUN_STARTED
        ):
            continue
        authorization = event.payload.get("authorization")
        if isinstance(authorization, dict) and authorization.get("explicit") is True:
            return True
    return False


def _checks_passed(checks: Sequence[JsonValue]) -> bool | None:
    if not checks:
        return None
    has_unknown = False
    for check in checks:
        if not isinstance(check, dict):
            has_unknown = True
            continue
        result = check.get("result")
        passed = result.get("passed") if isinstance(result, dict) else None
        if passed is False:
            return False
        if passed is not True:
            has_unknown = True
    return None if has_unknown else True


def _ids(value: JsonValue | None, key: str) -> list[JsonValue]:
    if not isinstance(value, list):
        return []
    return [
        item[key] for item in value if isinstance(item, dict) and isinstance(item.get(key), str)
    ]


def _worker_kind(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("worker_kind must be text")
    normalized = value.strip().casefold().replace("_", "-")
    if normalized == "codex-app-server":
        normalized = "codex-server"
    if normalized not in _WORKER_KINDS:
        raise ValueError(f"unsupported worker_kind: {value!r}")
    return normalized


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _integer(value: JsonValue, field_name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer")
    return value


def _number(value: JsonValue, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite numeric")
    return float(value)


def _string_list(value: JsonValue, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a JSON string array")
    return [_text(item, field_name) for item in value]


def _argv_list(value: JsonValue, field_name: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array of argv arrays")
    result: list[tuple[str, ...]] = []
    for argv in value:
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise ValueError(f"{field_name} must be a JSON array of argv arrays")
        result.append(tuple(_text(item, field_name) for item in argv))
    return tuple(result)


def _endpoint_capabilities(value: JsonValue) -> ResponsesEndpointCapabilities:
    if not isinstance(value, dict):
        raise ValueError("endpoint_capabilities must be an object")
    allowed = {
        "supports_background",
        "supports_unique_items",
        "supports_idempotent_create",
        "supports_previous_response_id",
        "supports_response_retrieval",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"endpoint_capabilities contains unknown fields: {unknown}")
    parsed: dict[str, bool | None] = {}
    for key in allowed:
        item = value.get(key)
        if item is not None and not isinstance(item, bool):
            raise ValueError(f"endpoint_capabilities {key} must be boolean or null")
        parsed[key] = item
    return ResponsesEndpointCapabilities(
        supports_background=True
        if parsed["supports_background"] is None
        else parsed["supports_background"],
        supports_unique_items=True
        if parsed["supports_unique_items"] is None
        else parsed["supports_unique_items"],
        supports_idempotent_create=parsed["supports_idempotent_create"],
        supports_previous_response_id=parsed["supports_previous_response_id"] is not False,
        supports_response_retrieval=parsed["supports_response_retrieval"] is not False,
    )


def _reject_sensitive_keys(value: Mapping[str, JsonValue]) -> None:
    for key, child in value.items():
        if any(marker in key.casefold() for marker in _SENSITIVE_KEY_MARKERS):
            raise ValueError(f"execution config cannot contain credential field {key!r}")
        if isinstance(child, dict):
            _reject_sensitive_keys(child)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    _reject_sensitive_keys(item)


def _error(error: BaseException) -> str:
    value = f"{type(error).__name__}: {error}"
    return value if len(value) <= 2_000 else value[:1_997] + "..."


def _write_line(stream: object, value: str) -> None:
    write = getattr(stream, "write", None)
    flush = getattr(stream, "flush", None)
    if callable(write):
        write(value + "\n")
        if callable(flush):
            flush()


async def _close_connectors(composition: LocalRuntimeComposition) -> None:
    closed: set[int] = set()
    for connector in (composition.connector, composition.base_connector):
        if id(connector) in closed:
            continue
        closed.add(id(connector))
        close = getattr(connector, "close", None)
        if not callable(close):
            continue
        result = close()
        if inspect.isawaitable(result):
            await result
