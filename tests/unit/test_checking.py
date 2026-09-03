from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from ehai import ID, new_id
from ehai.domain.checking import (
    CheckKind,
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    CheckSpec,
    Gate,
    GateDecision,
    InvalidCheckRunTransition,
)
from ehai.domain.execution import Run
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanRevision,
    PlanRevisionStatus,
)

NOW = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)


def make_check_run(
    check_id: ID | None = None,
    *,
    run_id: ID | None = None,
    plan_node_id: ID | None = None,
    attempt_id: ID | None = None,
) -> CheckRun:
    return CheckRun(
        run_id=run_id or new_id(),
        plan_node_id=plan_node_id or new_id(),
        attempt_id=attempt_id or new_id(),
        check_id=check_id or new_id(),
        created_at=NOW,
    )


def result_for(
    check_run: CheckRun,
    *,
    passed: bool = True,
    evidence: tuple[ID, ...] = (),
) -> CheckResult:
    return CheckResult(
        check_id=check_run.check_id,
        check_run_id=check_run.check_run_id,
        run_id=check_run.run_id,
        plan_node_id=check_run.plan_node_id,
        attempt_id=check_run.attempt_id,
        passed=passed,
        evaluated_at=NOW + timedelta(seconds=2),
        evidence_artifact_ids=evidence,
        output="checker output",
        failure_reason=None if passed else "criterion not met",
    )


def evaluate(
    gate: Gate,
    check_run: CheckRun,
    results: tuple[CheckResult, ...],
) -> GateDecision:
    return gate.evaluate(
        results,
        run_id=check_run.run_id,
        plan_node_id=check_run.plan_node_id,
        attempt_id=check_run.attempt_id,
        at=NOW + timedelta(seconds=3),
    )


def make_linear_plan(*, plan_revision_id: ID | None = None) -> PlanRevision:
    goal_id = new_id()
    node = PlanNode(new_id(), "work", "do the work")
    return PlanRevision.rehydrate(
        plan_revision_id=plan_revision_id or new_id(),
        goal_id=goal_id,
        version=1,
        completion_contract_id=new_id(),
        completion_contract_version=1,
        nodes=(node,),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )


def rehydrate_plan(
    plan: PlanRevision,
    *,
    plan_revision_id: ID | None = None,
    nodes: tuple[PlanNode, ...] | None = None,
    branches: tuple[Branch, ...] | None = None,
) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=plan_revision_id or plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=nodes if nodes is not None else plan.nodes,
        edges=plan.edges,
        branches=branches if branches is not None else plan.branches,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
    )


def test_check_spec_is_validated_and_immutable() -> None:
    spec = CheckSpec(
        name="unit tests",
        kind=CheckKind.COMMAND,
        description="run the focused test suite",
        command_argv=("uv", "run", "pytest", "-q"),
    )
    assert spec.required
    assert spec.command_argv == ("uv", "run", "pytest", "-q")
    with pytest.raises(FrozenInstanceError):
        spec.required = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="name must not be blank"):
        CheckSpec(name=" ", kind=CheckKind.ARTIFACT, description="inspect output")
    with pytest.raises(ValueError, match="requires a Command Check"):
        CheckSpec(
            name="artifact",
            kind=CheckKind.ARTIFACT,
            description="inspect output",
            command_argv=("uv", "--version"),
        )
    with pytest.raises(ValueError, match="not shell text"):
        CheckSpec(
            name="command",
            kind=CheckKind.COMMAND,
            description="run command",
            command_argv="uv --version",  # type: ignore[arg-type]
        )

    semantic = CheckSpec(
        name="terms",
        kind=CheckKind.SEMANTIC,
        description="required terms",
        semantic_required_terms=(" PlanGraph ", "ExecutionTrace"),
    )
    assert semantic.semantic_required_terms == ("plangraph", "executiontrace")
    with pytest.raises(ValueError, match="contains duplicates"):
        CheckSpec(
            name="terms",
            kind=CheckKind.SEMANTIC,
            description="required terms",
            semantic_required_terms=("PlanGraph", "plangraph"),
        )


def test_check_run_completion_keeps_execution_status_separate_from_verdict() -> None:
    check_run = make_check_run().start(at=NOW + timedelta(seconds=1))
    failing_verdict = result_for(check_run, passed=False, evidence=(new_id(),))
    completed = check_run.complete(failing_verdict, at=NOW + timedelta(seconds=3))

    assert completed.status is CheckRunStatus.COMPLETED
    assert completed.result is failing_verdict
    assert not completed.result.passed
    assert completed.failure_reason is None


def test_check_run_rejects_mismatched_result_and_illegal_transition() -> None:
    check_run = make_check_run()
    unrelated = make_check_run(check_run.check_id)
    result = result_for(unrelated, passed=True, evidence=(new_id(),))

    with pytest.raises(InvalidCheckRunTransition) as exc_info:
        check_run.fail("could not execute")
    assert exc_info.value.check_run_id == check_run.check_run_id
    assert exc_info.value.run_id == check_run.run_id
    assert exc_info.value.plan_node_id == check_run.plan_node_id

    with pytest.raises(ValueError, match="result ownership does not match"):
        check_run.start(at=NOW).complete(result, at=NOW + timedelta(seconds=1))


@pytest.mark.parametrize("owner_field", ["run_id", "plan_node_id", "attempt_id"])
def test_check_run_rejects_each_cross_scope_result(owner_field: str) -> None:
    check_run = make_check_run().start(at=NOW)
    stale = replace(result_for(check_run, evidence=(new_id(),)), **{owner_field: new_id()})

    with pytest.raises(ValueError, match="result ownership does not match"):
        check_run.complete(stale, at=NOW + timedelta(seconds=3))


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("fail", CheckRunStatus.FAILED),
        ("time_out", CheckRunStatus.TIMED_OUT),
        ("interrupt", CheckRunStatus.INTERRUPTED),
    ],
)
def test_check_run_terminal_failures_keep_reason(
    method_name: str,
    expected: CheckRunStatus,
) -> None:
    check_run = make_check_run().start(at=NOW)
    terminal = getattr(check_run, method_name)("runner stopped", at=NOW + timedelta(seconds=1))
    assert terminal.status is expected
    assert terminal.failure_reason == "runner stopped"


def test_check_run_terminal_constructor_requires_explicit_rehydrate_boundary() -> None:
    running = make_check_run().start(at=NOW)
    result = result_for(running, evidence=(new_id(),))
    with pytest.raises(ValueError, match=r"transition or rehydrate\(\)"):
        CheckRun(
            run_id=running.run_id,
            plan_node_id=running.plan_node_id,
            attempt_id=running.attempt_id,
            check_id=running.check_id,
            check_run_id=running.check_run_id,
            status=CheckRunStatus.COMPLETED,
            created_at=NOW,
            started_at=NOW,
            ended_at=NOW + timedelta(seconds=3),
            result=result,
        )

    restored = CheckRun.rehydrate(
        run_id=running.run_id,
        plan_node_id=running.plan_node_id,
        attempt_id=running.attempt_id,
        check_id=running.check_id,
        check_run_id=running.check_run_id,
        status=CheckRunStatus.COMPLETED,
        created_at=NOW,
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=3),
        result=result,
    )
    assert restored.status is CheckRunStatus.COMPLETED


def test_gate_passes_only_when_every_required_result_has_evidence() -> None:
    first = make_check_run()
    second = make_check_run(
        run_id=first.run_id,
        plan_node_id=first.plan_node_id,
        attempt_id=first.attempt_id,
    )
    first_artifact = new_id()
    second_artifact = new_id()
    gate = Gate(required_check_ids=(first.check_id, second.check_id))

    decision = evaluate(
        gate,
        first,
        (
            result_for(first, evidence=(first_artifact,)),
            result_for(second, evidence=(second_artifact,)),
        ),
    )

    assert decision.passed
    assert decision.failed_check_ids == ()
    assert decision.evidence_artifact_ids == (first_artifact, second_artifact)
    assert decision.reason is None


@pytest.mark.parametrize("case", ["missing", "failed", "no_evidence", "duplicate"])
def test_gate_fails_closed_for_incomplete_or_ambiguous_results(case: str) -> None:
    check_run = make_check_run()
    gate = Gate(required_check_ids=(check_run.check_id,))
    evidence = (new_id(),)
    cases = {
        "missing": (),
        "failed": (result_for(check_run, passed=False, evidence=evidence),),
        "no_evidence": (result_for(check_run),),
        "duplicate": (
            result_for(check_run, evidence=evidence),
            result_for(check_run, evidence=evidence),
        ),
    }

    decision = evaluate(gate, check_run, cases[case])

    assert not decision.passed
    assert decision.failed_check_ids == (check_run.check_id,)
    assert decision.reason


@pytest.mark.parametrize("owner_field", ["run_id", "plan_node_id", "attempt_id"])
def test_gate_fails_closed_for_stale_cross_scope_result(owner_field: str) -> None:
    check_run = make_check_run()
    gate = Gate(required_check_ids=(check_run.check_id,))
    stale = replace(result_for(check_run, evidence=(new_id(),)), **{owner_field: new_id()})

    decision = evaluate(gate, check_run, (stale,))

    assert not decision.passed
    assert decision.run_id == check_run.run_id
    assert decision.plan_node_id == check_run.plan_node_id
    assert decision.attempt_id == check_run.attempt_id
    assert decision.failed_check_ids == (check_run.check_id,)
    assert decision.reason and "another execution scope" in decision.reason


def test_gate_uses_only_required_specs() -> None:
    required = CheckSpec("required", CheckKind.COMMAND, "must pass")
    optional = CheckSpec("optional", CheckKind.SEMANTIC, "nice to have", required=False)
    gate = Gate.from_specs((required, optional))
    check_run = make_check_run(required.check_id)

    decision = evaluate(gate, check_run, (result_for(check_run, evidence=(new_id(),)),))
    assert decision.required_check_ids == (required.check_id,)
    assert decision.passed


def test_gate_decision_validates_fail_closed_invariants() -> None:
    check_id = new_id()
    with pytest.raises(ValueError, match="requires evidence"):
        GateDecision(
            gate_id=new_id(),
            run_id=new_id(),
            plan_node_id=new_id(),
            attempt_id=new_id(),
            passed=True,
            evaluated_at=NOW,
            required_check_ids=(check_id,),
            failed_check_ids=(),
            evidence_artifact_ids=(),
        )
    with pytest.raises(ValueError, match="requires failed checks and a reason"):
        GateDecision(
            gate_id=new_id(),
            run_id=new_id(),
            plan_node_id=new_id(),
            attempt_id=new_id(),
            passed=False,
            evaluated_at=NOW,
            required_check_ids=(check_id,),
            failed_check_ids=(),
            evidence_artifact_ids=(),
            reason="missing",
        )


def make_branch_plan() -> PlanRevision:
    fork = PlanNode(new_id(), "fork", "explore", kind=PlanNodeKind.FORK)
    first = PlanNode(new_id(), "first", "try first")
    second = PlanNode(new_id(), "second", "try second")
    merge = PlanNode(new_id(), "merge", "merge", kind=PlanNodeKind.MERGE)
    selected = Branch(
        new_id(),
        "selected",
        fork.plan_node_id,
        (first.plan_node_id,),
        merge.plan_node_id,
    ).select()
    pruned = Branch(
        new_id(),
        "pruned",
        fork.plan_node_id,
        (second.plan_node_id,),
        merge.plan_node_id,
    ).prune()
    edges = (
        Edge(
            new_id(),
            fork.plan_node_id,
            first.plan_node_id,
            EdgeType.EXPLORATION,
            selected.branch_id,
        ),
        Edge(new_id(), first.plan_node_id, merge.plan_node_id, EdgeType.MERGE, selected.branch_id),
        Edge(
            new_id(), fork.plan_node_id, second.plan_node_id, EdgeType.EXPLORATION, pruned.branch_id
        ),
        Edge(new_id(), second.plan_node_id, merge.plan_node_id, EdgeType.MERGE, pruned.branch_id),
    )
    return PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=new_id(),
        version=1,
        completion_contract_id=new_id(),
        completion_contract_version=1,
        nodes=(fork, first, second, merge),
        edges=edges,
        branches=(selected, pruned),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )


def make_checkpoint_context() -> tuple[PlanRevision, Run, CheckRun, GateDecision, ID]:
    plan = make_branch_plan()
    selected = next(branch for branch in plan.branches if branch.status is BranchStatus.SELECTED)
    run = Run(
        goal_id=plan.goal_id,
        plan_revision_id=plan.plan_revision_id,
        created_at=NOW,
    ).start(at=NOW)
    check_run = make_check_run(run_id=run.run_id, plan_node_id=selected.node_ids[0])
    evidence_id = new_id()
    decision = evaluate(
        Gate(required_check_ids=(check_run.check_id,)),
        check_run,
        (result_for(check_run, evidence=(evidence_id,)),),
    )
    return plan, run, check_run, decision, evidence_id


def test_checkpoint_validates_graph_gate_evidence_and_freezes_selections() -> None:
    plan, run, _, decision, evidence_id = make_checkpoint_context()
    selected = next(branch for branch in plan.branches if branch.status is BranchStatus.SELECTED)
    mutable_selections = {selected.fork_node_id: selected.branch_id}

    checkpoint = Checkpoint(
        plan_revision=plan,
        run=run,
        event_offset=7,
        gate_decision=decision,
        branch_selections=mutable_selections,
        artifact_refs=(evidence_id,),
        created_at=NOW,
    )
    mutable_selections.clear()

    assert checkpoint.run_id == run.run_id
    assert checkpoint.plan_revision_id == plan.plan_revision_id
    assert checkpoint.branch_selections == {selected.fork_node_id: selected.branch_id}
    with pytest.raises(TypeError):
        checkpoint.branch_selections[selected.fork_node_id] = new_id()  # type: ignore[index]

    with pytest.raises(ValueError, match="gate evidence missing"):
        Checkpoint(
            plan_revision=plan,
            run=run,
            event_offset=7,
            gate_decision=decision,
            branch_selections={selected.fork_node_id: selected.branch_id},
            artifact_refs=(),
        )


def test_checkpoint_rejects_mismatched_plan_gate_scope_and_invalid_offset() -> None:
    plan, run, check_run, decision, evidence_id = make_checkpoint_context()
    selected = next(branch for branch in plan.branches if branch.status is BranchStatus.SELECTED)
    selections = {selected.fork_node_id: selected.branch_id}
    failed = GateDecision(
        gate_id=new_id(),
        run_id=run.run_id,
        plan_node_id=check_run.plan_node_id,
        attempt_id=check_run.attempt_id,
        passed=False,
        evaluated_at=NOW,
        required_check_ids=(check_run.check_id,),
        failed_check_ids=(check_run.check_id,),
        evidence_artifact_ids=(),
        reason="missing result",
    )

    with pytest.raises(ValueError, match="event_offset must be positive"):
        Checkpoint(
            plan_revision=plan,
            run=run,
            event_offset=0,
            gate_decision=decision,
            branch_selections=selections,
            artifact_refs=(evidence_id,),
        )
    with pytest.raises(ValueError, match="references plan revision"):
        Checkpoint(
            plan_revision=rehydrate_plan(plan, plan_revision_id=new_id()),
            run=run,
            event_offset=1,
            gate_decision=decision,
            branch_selections=selections,
            artifact_refs=(evidence_id,),
        )
    with pytest.raises(ValueError, match="did not pass"):
        Checkpoint(
            plan_revision=plan,
            run=run,
            event_offset=1,
            gate_decision=failed,
            branch_selections=selections,
        )
    with pytest.raises(ValueError, match="belongs to run"):
        Checkpoint(
            plan_revision=plan,
            run=run,
            event_offset=1,
            gate_decision=replace(decision, run_id=new_id()),
            branch_selections=selections,
            artifact_refs=(evidence_id,),
        )


def test_checkpoint_rejects_arbitrary_or_invalid_branch_selections() -> None:
    plan, run, _, decision, evidence_id = make_checkpoint_context()
    selected = next(branch for branch in plan.branches if branch.status is BranchStatus.SELECTED)
    common = {
        "run": run,
        "event_offset": 1,
        "gate_decision": decision,
        "artifact_refs": (evidence_id,),
    }

    with pytest.raises(ValueError, match="unknown Branch"):
        Checkpoint(
            plan_revision=plan,
            branch_selections={selected.fork_node_id: new_id()},
            **common,
        )

    other_fork = PlanNode(new_id(), "other fork", "other", kind=PlanNodeKind.FORK)
    plan_with_other_fork = rehydrate_plan(plan, nodes=(*plan.nodes, other_fork))
    with pytest.raises(ValueError, match="does not belong to fork"):
        Checkpoint(
            plan_revision=plan_with_other_fork,
            branch_selections={other_fork.plan_node_id: selected.branch_id},
            **common,
        )

    active_branches = tuple(
        Branch(
            branch.branch_id,
            branch.label,
            branch.fork_node_id,
            branch.node_ids,
            branch.merge_node_id,
        )
        if branch.branch_id == selected.branch_id
        else branch
        for branch in plan.branches
    )
    plan_with_active_branch = rehydrate_plan(plan, branches=active_branches)
    with pytest.raises(ValueError, match="is not selected"):
        Checkpoint(
            plan_revision=plan_with_active_branch,
            branch_selections={selected.fork_node_id: selected.branch_id},
            **common,
        )
