from __future__ import annotations

from threading import Event as ThreadEvent
from threading import Lock, Thread

from ehai import ID
from ehai.application.checks import CheckRunner
from ehai.application.commands import ApprovePlan, CreateGoal, CreateProject, ProposePlan, StartRun
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION, DeterministicPlanner
from ehai.application.service import ExecutionService
from ehai.application.workers import CandidateArtifact, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind
from ehai.domain.execution import Run, RunStatus
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase


class _SerialProbeWorker:
    def __init__(self) -> None:
        self.first_entered = ThreadEvent()
        self.second_entered = ThreadEvent()
        self.release_first = ThreadEvent()
        self._lock = Lock()
        self._calls = 0
        self._active = 0
        self.max_active = 0

    def execute(self, request: WorkerRequest) -> WorkerResult:
        del request
        with self._lock:
            self._calls += 1
            call = self._calls
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if call == 1:
                self.first_entered.set()
                assert self.release_first.wait(timeout=5)
            else:
                self.second_entered.set()
            return WorkerResult(
                artifacts=(
                    CandidateArtifact(
                        ArtifactKind.CANDIDATE,
                        f"candidate-{call}.txt",
                        "text/plain",
                        b"candidate",
                    ),
                ),
                summary="candidate",
            )
        finally:
            with self._lock:
                self._active -= 1

    def cancel(self, attempt_id: ID) -> None:
        del attempt_id


def test_execution_service_serializes_attempts_across_runs(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "serial.sqlite3")
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = _SerialProbeWorker()
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
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=orchestrator,
    )
    project = service.create_project(CreateProject("project", "serial"))
    plan_ids: list[ID] = []
    for index in range(2):
        goal = service.create_goal(CreateGoal(f"goal-{index}", project.project_id, f"goal {index}"))
        plan = service.propose_plan(
            ProposePlan(
                f"plan-{index}",
                goal.goal_id,
                (NON_EMPTY_ARTIFACT_CRITERION,),
            )
        )
        service.approve_plan(
            ApprovePlan(
                f"approve-{index}",
                plan.plan_revision_id,
                plan.completion_contract_id,
            )
        )
        plan_ids.append(plan.plan_revision_id)

    completed: list[Run] = []
    failures: list[BaseException] = []

    def start(index: int) -> None:
        try:
            completed.append(service.start_run(StartRun(f"start-{index}", plan_ids[index])))
        except BaseException as error:
            failures.append(error)

    first = Thread(target=start, args=(0,))
    second = Thread(target=start, args=(1,))
    first.start()
    assert worker.first_entered.wait(timeout=5)
    second.start()
    assert not worker.second_entered.wait(timeout=0.2)
    worker.release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert len(completed) == 2
    assert all(run.status is RunStatus.COMPLETED for run in completed)
    assert worker.max_active == 1
