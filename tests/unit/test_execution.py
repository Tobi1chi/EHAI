from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ehai import new_id
from ehai.domain.checking import GateDecision
from ehai.domain.execution import (
    Attempt,
    AttemptStatus,
    InvalidAttemptTransition,
    InvalidRunTransition,
    Run,
    RunStatus,
)

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


def make_run() -> Run:
    return Run(goal_id=new_id(), plan_revision_id=new_id(), created_at=NOW)


def make_attempt() -> Attempt:
    return Attempt(
        run_id=new_id(),
        plan_node_id=new_id(),
        sequence=1,
        created_at=NOW,
    )


def passing_decision(run: Run) -> GateDecision:
    return GateDecision(
        gate_id=new_id(),
        run_id=run.run_id,
        plan_node_id=new_id(),
        attempt_id=new_id(),
        passed=True,
        evaluated_at=NOW + timedelta(seconds=2),
        required_check_ids=(new_id(),),
        failed_check_ids=(),
        evidence_artifact_ids=(new_id(),),
    )


def test_run_moves_through_running_pause_resume_and_completion() -> None:
    run = make_run().start(at=NOW + timedelta(seconds=1))
    paused = run.pause()
    resumed = paused.resume()
    completed = resumed.complete(passing_decision(resumed), at=NOW + timedelta(seconds=2))

    assert run.status is RunStatus.RUNNING
    assert paused.status is RunStatus.PAUSED
    assert resumed.status is RunStatus.RUNNING
    assert completed.status is RunStatus.COMPLETED
    assert completed.started_at == NOW + timedelta(seconds=1)
    assert completed.ended_at == NOW + timedelta(seconds=2)
    assert make_run().cancel(at=NOW + timedelta(seconds=1)).status is RunStatus.CANCELLED


def test_run_completion_requires_passing_evidenced_decision_for_same_run() -> None:
    running = make_run().start(at=NOW)
    check_id = new_id()
    failed = GateDecision(
        gate_id=new_id(),
        run_id=running.run_id,
        plan_node_id=new_id(),
        attempt_id=new_id(),
        passed=False,
        evaluated_at=NOW,
        required_check_ids=(check_id,),
        failed_check_ids=(check_id,),
        evidence_artifact_ids=(),
        reason="check failed",
    )
    stale = passing_decision(make_run())

    with pytest.raises(ValueError, match="must pass with evidence"):
        running.complete(failed, at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="belongs to run"):
        running.complete(stale, at=NOW + timedelta(seconds=1))


def test_run_failure_requires_reason_and_terminal_state_is_immutable() -> None:
    running = make_run().start(at=NOW + timedelta(seconds=1))

    with pytest.raises(ValueError, match="reason must not be blank"):
        running.fail(" ")

    failed = running.pause().fail("operator recovery failed", at=NOW + timedelta(seconds=2))
    assert failed.status is RunStatus.FAILED
    assert failed.status_reason == "operator recovery failed"

    with pytest.raises(InvalidRunTransition) as exc_info:
        failed.start()
    assert exc_info.value.run_id == failed.run_id
    assert str(failed.run_id) in str(exc_info.value)


def test_run_rejects_illegal_transition_and_inconsistent_timestamps() -> None:
    run = make_run()
    with pytest.raises(InvalidRunTransition) as exc_info:
        run.pause()
    assert exc_info.value.source is RunStatus.PENDING
    assert exc_info.value.target is RunStatus.PAUSED

    with pytest.raises(ValueError, match="started_at precedes created_at"):
        run.start(at=NOW - timedelta(seconds=1))


def test_run_terminal_constructor_requires_explicit_rehydrate_boundary() -> None:
    run = make_run()
    with pytest.raises(ValueError, match=r"transition or rehydrate\(\)"):
        Run(
            goal_id=run.goal_id,
            plan_revision_id=run.plan_revision_id,
            run_id=run.run_id,
            status=RunStatus.COMPLETED,
            created_at=NOW,
            started_at=NOW,
            ended_at=NOW + timedelta(seconds=1),
        )

    restored = Run.rehydrate(
        goal_id=run.goal_id,
        plan_revision_id=run.plan_revision_id,
        run_id=run.run_id,
        status=RunStatus.COMPLETED,
        created_at=NOW,
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
    )
    assert restored.status is RunStatus.COMPLETED


def test_attempt_success_is_only_worker_success_and_freezes_artifacts() -> None:
    artifact_ids = (new_id(), new_id())
    running = make_attempt().start(at=NOW + timedelta(seconds=1))
    succeeded = running.succeed(artifact_ids, at=NOW + timedelta(seconds=2))

    assert succeeded.status is AttemptStatus.SUCCEEDED
    assert succeeded.artifact_ids == artifact_ids
    assert not hasattr(succeeded, "plan_node_status")
    with pytest.raises(FrozenInstanceError):
        succeeded.status = AttemptStatus.FAILED  # type: ignore[misc]


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("fail", AttemptStatus.FAILED),
        ("time_out", AttemptStatus.TIMED_OUT),
        ("interrupt", AttemptStatus.INTERRUPTED),
    ],
)
def test_attempt_records_failure_timeout_and_recovery_interruption(
    method_name: str,
    expected: AttemptStatus,
) -> None:
    running = make_attempt().start(at=NOW + timedelta(seconds=1))
    terminal = getattr(running, method_name)("process stopped", at=NOW + timedelta(seconds=2))

    assert terminal.status is expected
    assert terminal.outcome_reason == "process stopped"
    assert terminal.ended_at == NOW + timedelta(seconds=2)


def test_attempt_can_be_cancelled_before_or_during_execution() -> None:
    pending = make_attempt()
    cancelled_pending = pending.cancel("run cancelled", at=NOW + timedelta(seconds=1))
    cancelled_running = pending.start(at=NOW).cancel(at=NOW + timedelta(seconds=1))

    assert cancelled_pending.status is AttemptStatus.CANCELLED
    assert cancelled_pending.started_at is None
    assert cancelled_running.status is AttemptStatus.CANCELLED


def test_attempt_illegal_transition_reports_attempt_run_and_node_ids() -> None:
    attempt = make_attempt()

    with pytest.raises(InvalidAttemptTransition) as exc_info:
        attempt.succeed()

    error = exc_info.value
    assert error.attempt_id == attempt.attempt_id
    assert error.run_id == attempt.run_id
    assert error.plan_node_id == attempt.plan_node_id
    assert str(attempt.attempt_id) in str(error)
    assert str(attempt.run_id) in str(error)
    assert str(attempt.plan_node_id) in str(error)


def test_attempt_validates_sequence_duplicate_artifacts_and_timestamps() -> None:
    with pytest.raises(ValueError, match="sequence must be positive"):
        Attempt(run_id=new_id(), plan_node_id=new_id(), sequence=0)

    running = make_attempt().start(at=NOW)
    artifact_id = new_id()
    with pytest.raises(ValueError, match="must not contain duplicates"):
        running.succeed((artifact_id, artifact_id), at=NOW + timedelta(seconds=1))

    with pytest.raises(ValueError, match="ended_at precedes"):
        running.fail("boom", at=NOW - timedelta(seconds=1))


def test_attempt_terminal_constructor_requires_explicit_rehydrate_boundary() -> None:
    attempt = make_attempt()
    with pytest.raises(ValueError, match=r"transition or rehydrate\(\)"):
        Attempt(
            run_id=attempt.run_id,
            plan_node_id=attempt.plan_node_id,
            sequence=attempt.sequence,
            attempt_id=attempt.attempt_id,
            status=AttemptStatus.INTERRUPTED,
            created_at=NOW,
            started_at=NOW,
            ended_at=NOW + timedelta(seconds=1),
            outcome_reason="process restarted",
        )

    restored = Attempt.rehydrate(
        run_id=attempt.run_id,
        plan_node_id=attempt.plan_node_id,
        sequence=attempt.sequence,
        attempt_id=attempt.attempt_id,
        status=AttemptStatus.INTERRUPTED,
        artifact_ids=(),
        created_at=NOW,
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        outcome_reason="process restarted",
    )
    assert restored.status is AttemptStatus.INTERRUPTED
