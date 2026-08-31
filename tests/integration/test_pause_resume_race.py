from __future__ import annotations

from threading import Event as ThreadEvent
from threading import Lock, Thread

from ehai import ID
from ehai.application.checks import CheckRunner
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    PauseRun,
    ProposePlan,
    ResumeRun,
    StartRun,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION, DeterministicPlanner
from ehai.application.ports import CommandReceipt
from ehai.application.run_control import RunController
from ehai.application.service import ExecutionService
from ehai.application.workers import (
    CandidateArtifact,
    WorkerCancelledError,
    WorkerRequest,
    WorkerResult,
)
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase


class _PauseResumeWorker:
    def __init__(self) -> None:
        self.first_entered = ThreadEvent()
        self.cancel_requested = ThreadEvent()
        self.allow_raise = ThreadEvent()
        self._lock = Lock()
        self._execute_count = 0
        self.first_request: WorkerRequest | None = None
        self.cancelled_attempt_ids: list[ID] = []

    @property
    def execute_count(self) -> int:
        with self._lock:
            return self._execute_count

    def execute(self, request: WorkerRequest) -> WorkerResult:
        with self._lock:
            self._execute_count += 1
            call = self._execute_count
        if call == 1:
            self.first_request = request
            self.first_entered.set()
            assert self.cancel_requested.wait(timeout=5)
            assert self.allow_raise.wait(timeout=5)
            raise WorkerCancelledError(
                request.run_id,
                request.attempt_id,
                request.plan_node_id,
                "controlled pause cancellation",
            )
        return WorkerResult(
            artifacts=(
                CandidateArtifact(
                    ArtifactKind.CANDIDATE,
                    "resumed-candidate.txt",
                    "text/plain",
                    b"resumed candidate",
                ),
            ),
            summary="resumed candidate",
        )

    def cancel(self, attempt_id: ID) -> None:
        self.cancelled_attempt_ids.append(attempt_id)
        self.cancel_requested.set()


class _ObservedRunController(RunController):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resume_called = ThreadEvent()

    def resume(self, run_id: ID, *, receipt: CommandReceipt | None = None) -> Run:
        self.resume_called.set()
        return super().resume(run_id, receipt=receipt)


class _ObservedLock:
    def __init__(self) -> None:
        self._lock = Lock()
        self.contended = ThreadEvent()

    def __enter__(self) -> None:
        if self._lock.locked():
            self.contended.set()
        self._lock.acquire()

    def __exit__(self, *_args: object) -> None:
        self._lock.release()


def test_immediate_resume_waits_for_paused_attempt_to_settle(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "pause-resume-race.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = _PauseResumeWorker()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=CheckRunner(
            {
                CheckKind.ARTIFACT: ArtifactCheckAdapter(
                    artifact_store,
                    {},
                    default_rule=ArtifactCheckRule(),
                )
            }
        ),
        workspace=tmp_path,
        cancellation_settle_seconds=0.5,
    )
    controller = _ObservedRunController(database.unit_of_work, worker)
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=orchestrator,
        run_controller=controller,
    )
    execution_lock = _ObservedLock()
    service._execution_lock = execution_lock  # type: ignore[assignment]
    project = service.create_project(CreateProject("project", "pause resume race"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "finish after resume"))
    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,)))
    service.approve_plan(ApprovePlan("approve", plan.plan_revision_id, plan.completion_contract_id))

    results: dict[str, Run] = {}
    failures: dict[str, BaseException] = {}
    resume_started = ThreadEvent()

    def start() -> None:
        try:
            results["start"] = service.start_run(StartRun("start", plan.plan_revision_id))
        except BaseException as error:
            failures["start"] = error

    def resume(run_id: ID) -> None:
        try:
            resume_started.set()
            results["resume"] = service.resume_run(ResumeRun("resume", run_id))
        except BaseException as error:
            failures["resume"] = error

    start_thread = Thread(target=start)
    resume_thread: Thread | None = None
    paused: Run | None = None
    state_while_start_locked: Run | None = None
    start_thread.start()
    try:
        assert worker.first_entered.wait(timeout=5)
        assert worker.first_request is not None
        run_id = worker.first_request.run_id

        paused = service.pause_run(PauseRun("pause", run_id))
        resume_thread = Thread(target=resume, args=(run_id,))
        resume_thread.start()

        assert resume_started.wait(timeout=5)
        assert execution_lock.contended.wait(timeout=5)
        assert not controller.resume_called.is_set()
        state_while_start_locked = service.get_run(run_id)
    finally:
        worker.cancel_requested.set()
        worker.allow_raise.set()
        start_thread.join(timeout=5)
        if resume_thread is not None:
            resume_thread.join(timeout=5)

    assert not start_thread.is_alive()
    assert resume_thread is not None and not resume_thread.is_alive()
    assert paused is not None and paused.status is RunStatus.PAUSED
    assert state_while_start_locked is not None
    assert state_while_start_locked.status is RunStatus.PAUSED
    assert controller.resume_called.is_set()
    assert failures == {}
    assert results["start"].status is RunStatus.PAUSED
    assert results["resume"].status is RunStatus.COMPLETED

    with database.unit_of_work() as uow:
        attempts = uow.states.list_attempts(results["resume"].run_id)
    assert len(attempts) == 2
    assert tuple(attempt.status for attempt in attempts) == (
        AttemptStatus.CANCELLED,
        AttemptStatus.SUCCEEDED,
    )
    assert worker.execute_count == 2
    assert worker.cancelled_attempt_ids == [attempts[0].attempt_id]
