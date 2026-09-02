from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ehai import ID, new_id
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.queries import QueryService
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, Checkpoint, CheckResult, CheckRun, CheckSpec, Gate
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import (
    Branch,
    Edge,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.workers import (
    AgentSessionRef,
    AttemptActivity,
    BuiltinExecutionRef,
    ExecutionHandle,
    ExternalExecutionRef,
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.sqlite import PersistenceConflictError, SQLiteDatabase

NOW = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)


def _plan(goal: Goal, contract: CompletionContract) -> PlanRevision:
    fork = PlanNode(new_id(), "fork", "explore", kind=PlanNodeKind.FORK)
    first = PlanNode(
        new_id(),
        "first",
        "try first",
        required_check_ids=contract.required_check_ids,
    )
    second = PlanNode(new_id(), "second", "try second")
    merge = PlanNode(new_id(), "merge", "merge", kind=PlanNodeKind.MERGE)
    selected = Branch(
        new_id(), "first", fork.plan_node_id, (first.plan_node_id,), merge.plan_node_id
    ).select()
    pruned = Branch(
        new_id(), "second", fork.plan_node_id, (second.plan_node_id,), merge.plan_node_id
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
            new_id(),
            fork.plan_node_id,
            second.plan_node_id,
            EdgeType.EXPLORATION,
            pruned.branch_id,
        ),
        Edge(new_id(), second.plan_node_id, merge.plan_node_id, EdgeType.MERGE, pruned.branch_id),
    )
    return PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(fork, first, second, merge),
        edges=edges,
        branches=(selected, pruned),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )


def _selected_branch_context(plan: PlanRevision) -> tuple[ID, ID, ID]:
    selected = next(branch for branch in plan.branches if branch.status.value == "selected")
    return selected.fork_node_id, selected.branch_id, selected.node_ids[0]


def _persist_plan_context(
    database: SQLiteDatabase,
) -> tuple[Project, Goal, CompletionContract, PlanRevision]:
    project = Project.create("P1", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "prove", goal_id=new_id(), created_at=NOW)
    contract = CompletionContract.draft(
        goal.goal_id,
        ("check",),
        (new_id(),),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    plan = _plan(goal, contract)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.commit()
    return project, goal, contract, plan


def _rehydrate_plan(
    plan: PlanRevision,
    *,
    nodes: tuple[PlanNode, ...] | None = None,
) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=plan.nodes if nodes is None else nodes,
        edges=plan.edges,
        branches=plan.branches,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
    )


def test_p2_worker_registry_and_execution_bindings_round_trip_together(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "p2-routing.sqlite3")
    _, goal, _, plan = _persist_plan_context(database)
    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        created_at=NOW,
    )
    builtin_attempt = Attempt(run.run_id, plan.nodes[1].plan_node_id, 1, created_at=NOW)
    external_attempt = Attempt(run.run_id, plan.nodes[2].plan_node_id, 2, created_at=NOW)
    capability = WorkerCapability("workspace.read")
    builtin_profile = WorkerProfile(
        "builtin",
        WorkerKind.BUILTIN,
        "gpt-test",
        frozenset({capability}),
        credential_ref=OPENAI_CREDENTIAL_REF,
    )
    external_profile = WorkerProfile(
        "codex",
        WorkerKind.CODEX_CLI,
        "gpt-test",
        frozenset({capability}),
    )
    builtin_endpoint = WorkerEndpoint(
        "builtin",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "builtin",
        1,
    )
    external_endpoint = WorkerEndpoint(
        "codex",
        WorkerKind.CODEX_CLI,
        WorkerEndpointType.COMMAND,
        "config:codex-cli",
        1,
    )
    builtin_session = AgentSessionRef(
        run.run_id,
        builtin_profile.worker_profile_id,
        builtin_endpoint.worker_endpoint_id,
        "builtin-session",
        True,
        created_at=NOW,
    )
    external_session = AgentSessionRef(
        run.run_id,
        external_profile.worker_profile_id,
        external_endpoint.worker_endpoint_id,
        "codex-thread",
        False,
        created_at=NOW,
    )
    builtin_ref = BuiltinExecutionRef(
        builtin_attempt.attempt_id,
        builtin_session.agent_session_ref_id,
    )
    external_ref = ExternalExecutionRef(
        external_attempt.attempt_id,
        external_session.agent_session_ref_id,
        "codex-turn",
    )

    with database.unit_of_work() as uow:
        uow.worker_registry.put_worker_profile(builtin_profile)
        uow.worker_registry.put_worker_profile(external_profile)
        uow.worker_registry.put_worker_endpoint(builtin_endpoint)
        uow.worker_registry.put_worker_endpoint(external_endpoint)
        uow.states.put_run(run)
        uow.states.put_attempt(builtin_attempt)
        uow.states.put_attempt(external_attempt)
        uow.states.put_agent_session_ref(builtin_session)
        uow.states.put_agent_session_ref(external_session)
        uow.states.put_builtin_execution_ref(builtin_ref)
        uow.states.put_external_execution_ref(external_ref)
        builtin_attempt = builtin_attempt.assign(
            profile=builtin_profile,
            endpoint=builtin_endpoint,
            session=builtin_session,
            deadline_at=NOW + timedelta(minutes=5),
            lease_expires_at=NOW + timedelta(seconds=30),
        ).bind_execution(ExecutionHandle(builtin=builtin_ref))
        external_attempt = external_attempt.assign(
            profile=external_profile,
            endpoint=external_endpoint,
            session=external_session,
            deadline_at=NOW + timedelta(minutes=5),
            lease_expires_at=NOW + timedelta(seconds=30),
        ).bind_execution(ExecutionHandle(external=external_ref))
        external_attempt = external_attempt.observe(
            AttemptActivity.WAITING,
            event_cursor="cursor-1",
            heartbeat_at=NOW + timedelta(seconds=1),
            progress_at=NOW + timedelta(seconds=1),
            lease_expires_at=NOW + timedelta(seconds=30),
        )
        uow.states.put_attempt(builtin_attempt)
        uow.states.put_attempt(external_attempt)
        uow.commit()

    with database.read_session() as session:
        assert session.worker_registry.list_worker_profiles() == tuple(
            sorted(
                (builtin_profile, external_profile),
                key=lambda item: item.worker_profile_id,
            )
        )
        assert session.worker_registry.list_worker_endpoints() == tuple(
            sorted(
                (builtin_endpoint, external_endpoint),
                key=lambda item: item.worker_endpoint_id,
            )
        )
        assert session.states.get_agent_session_ref(builtin_session.agent_session_ref_id) == (
            builtin_session
        )
        assert (
            session.states.get_builtin_execution_ref(builtin_ref.builtin_execution_ref_id)
            == builtin_ref
        )
        assert (
            session.states.get_external_execution_ref(external_ref.external_execution_ref_id)
            == external_ref
        )
        assert session.states.list_attempts(run.run_id) == (
            builtin_attempt,
            external_attempt,
        )

    queries = QueryService(read_session_factory=database.read_session)
    assert len(queries.list_worker_profiles()) == 2
    assert len(queries.list_worker_endpoints()) == 2
    runtime = queries.get_attempt_runtime(external_attempt.attempt_id)
    assert runtime.provider_session_id == "codex-thread"
    assert runtime.provider_execution_id == "codex-turn"
    assert runtime.activity is AttemptActivity.WAITING
    connection = database.connect()
    try:
        stored_credential = connection.execute(
            "SELECT credential_ref FROM worker_profiles WHERE worker_profile_id = ?",
            (builtin_profile.worker_profile_id,),
        ).fetchone()[0]
        assert stored_credential == OPENAI_CREDENTIAL_REF
    finally:
        connection.close()


def test_contract_history_allows_confirmation_but_rejects_content_rewrite(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "contract-history.sqlite3")
    project = Project.create("history", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "history", goal_id=new_id(), created_at=NOW)
    draft = CompletionContract.draft(
        goal.goal_id,
        ("original",),
        (new_id(),),
        completion_contract_id=new_id(),
        created_at=NOW,
    )
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(draft)
        uow.commit()

    confirmed = draft.confirm(confirmed_at=NOW + timedelta(seconds=1))
    with database.unit_of_work() as uow:
        uow.states.put_completion_contract(confirmed)
        uow.commit()

    rewritten = CompletionContract(
        completion_contract_id=confirmed.completion_contract_id,
        goal_id=confirmed.goal_id,
        version=confirmed.version,
        criteria=("rewritten",),
        required_check_ids=confirmed.required_check_ids,
        created_at=confirmed.created_at,
        confirmed_at=confirmed.confirmed_at,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="history changed"),
    ):
        uow.states.put_completion_contract(rewritten)

    with database.unit_of_work() as uow:
        assert uow.states.get_completion_contract(confirmed.completion_contract_id) == confirmed


def test_plan_revision_rejects_structure_rewrite_and_illegal_status_jump(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "plan-history.sqlite3")
    _, _, _, plan = _persist_plan_context(database)
    original = plan.nodes[0]
    renamed = PlanNode.rehydrate(
        plan_node_id=original.plan_node_id,
        title="rewritten title",
        instruction=original.instruction,
        kind=original.kind,
        required_dependency_ids=original.required_dependency_ids,
        required_check_ids=original.required_check_ids,
        status=original.status,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="structure changed"),
    ):
        uow.states.put_plan_revision(_rehydrate_plan(plan, nodes=(renamed, *plan.nodes[1:])))

    completed = PlanNode.rehydrate(
        plan_node_id=original.plan_node_id,
        title=original.title,
        instruction=original.instruction,
        kind=original.kind,
        required_dependency_ids=original.required_dependency_ids,
        required_check_ids=original.required_check_ids,
        status=PlanNodeStatus.COMPLETED,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="illegal persisted status"),
    ):
        uow.states.put_plan_revision(_rehydrate_plan(plan, nodes=(completed, *plan.nodes[1:])))


def test_execution_writes_reject_cross_aggregate_ownership(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "ownership.sqlite3")
    _, goal, _, plan = _persist_plan_context(database)
    _, _, node_id = _selected_branch_context(plan)
    wrong_run = Run(
        goal_id=new_id(),
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="Goal does not match"),
    ):
        uow.states.put_run(wrong_run)

    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    )
    with database.unit_of_work() as uow:
        uow.states.put_run(run)
        uow.commit()

    wrong_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=new_id(),
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="is not in PlanRevision"),
    ):
        uow.states.put_attempt(wrong_attempt)

    attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    )
    with database.unit_of_work() as uow:
        uow.states.put_attempt(attempt)
        uow.commit()

    wrong_check = CheckRun(
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        check_id=new_id(),
        check_run_id=new_id(),
        created_at=NOW,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="not required by PlanNode"),
    ):
        uow.states.put_check_run(wrong_check)

    another_node_id = next(node.plan_node_id for node in plan.nodes if node.plan_node_id != node_id)
    wrong_artifact = Artifact(
        artifact_id=new_id(),
        kind=ArtifactKind.EVIDENCE,
        name="wrong",
        media_type="text/plain",
        size_bytes=1,
        sha256="b" * 64,
        relative_path="artifacts/wrong.txt",
        created_at=NOW,
        run_id=run.run_id,
        plan_node_id=another_node_id,
        attempt_id=attempt.attempt_id,
    )
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="does not match Attempt"),
    ):
        uow.states.put_artifact(wrong_artifact)


def test_all_p1_state_round_trips_with_relational_references(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "roundtrip.sqlite3")
    project = Project.create("P1", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "prove the loop", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    check_spec = CheckSpec(
        "artifact",
        CheckKind.ARTIFACT,
        "artifact must exist",
        check_id=check_id,
    )
    contract = CompletionContract.draft(
        goal.goal_id,
        ("all checks pass",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    plan = _plan(goal, contract)
    fork_id, branch_id, node_id = _selected_branch_context(plan)
    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    ).start(at=NOW + timedelta(seconds=1))
    artifact_id = new_id()
    attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW + timedelta(seconds=1),
    ).start(at=NOW + timedelta(seconds=2))
    attempt = attempt.succeed((artifact_id,), at=NOW + timedelta(seconds=3))
    artifact = Artifact(
        artifact_id=artifact_id,
        kind=ArtifactKind.EVIDENCE,
        name="result.json",
        media_type="application/json",
        size_bytes=2,
        sha256="a" * 64,
        relative_path=f"artifacts/{artifact_id}.json",
        created_at=NOW + timedelta(seconds=3),
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
    )
    check_run = CheckRun(
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        check_id=check_id,
        check_run_id=new_id(),
        created_at=NOW + timedelta(seconds=3),
    ).start(at=NOW + timedelta(seconds=4))
    result = CheckResult(
        check_id=check_id,
        check_run_id=check_run.check_run_id,
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        passed=True,
        evaluated_at=NOW + timedelta(seconds=5),
        evidence_artifact_ids=(artifact.artifact_id,),
        output="ok",
    )
    check_run = check_run.complete(result, at=NOW + timedelta(seconds=6))
    decision = Gate(required_check_ids=(check_id,)).evaluate(
        (result,),
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        at=NOW + timedelta(seconds=7),
    )
    gate_event = Event(
        type=EventType.GATE_PASSED,
        correlation_id=decision.gate_id,
        run_id=run.run_id,
        payload={"gate_id": decision.gate_id},
        occurred_at=NOW + timedelta(seconds=7),
    )

    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_check_spec(plan.plan_revision_id, check_spec)
        uow.states.put_run(run)
        uow.states.put_attempt(attempt)
        uow.states.put_artifact(artifact)
        uow.states.put_check_run(check_run)
        stored_event = uow.events.append(gate_event)
        checkpoint = Checkpoint(
            plan_revision=plan,
            run=run,
            event_offset=stored_event.offset,
            gate_decision=decision,
            branch_selections={fork_id: branch_id},
            artifact_refs=(artifact.artifact_id,),
            checkpoint_id=new_id(),
            created_at=NOW + timedelta(seconds=8),
        )
        uow.states.put_checkpoint(checkpoint)
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.states.get_project(project.project_id) == project
        assert uow.states.list_projects() == (project,)
        assert uow.states.get_goal(goal.goal_id) == goal
        assert uow.states.list_goals(project.project_id) == (goal,)
        assert uow.states.get_completion_contract(contract.completion_contract_id) == contract
        assert uow.states.list_completion_contracts(goal.goal_id) == (contract,)
        assert uow.states.get_plan_revision(plan.plan_revision_id) == plan
        assert uow.states.list_plan_revisions(goal.goal_id) == (plan,)
        assert uow.states.get_check_spec(check_spec.check_id) == check_spec
        assert uow.states.list_check_specs(plan.plan_revision_id) == (check_spec,)
        assert uow.states.get_run(run.run_id) == run
        assert uow.states.list_runs(goal.goal_id) == (run,)
        assert uow.states.get_attempt(attempt.attempt_id) == attempt
        assert uow.states.list_attempts(run.run_id) == (attempt,)
        assert uow.states.get_artifact(artifact.artifact_id) == artifact
        assert uow.states.list_artifacts_for_run(run.run_id) == (artifact,)
        assert uow.states.get_check_run(check_run.check_run_id) == check_run
        assert uow.states.list_check_runs(run.run_id) == (check_run,)
        assert uow.states.get_checkpoint(checkpoint.checkpoint_id) == checkpoint
        assert uow.states.list_checkpoints(run.run_id) == (checkpoint,)

    wrong_event = Event(
        type=EventType.RUN_STARTED,
        correlation_id=run.run_id,
        run_id=run.run_id,
        payload={"gate_id": decision.gate_id},
        occurred_at=NOW + timedelta(seconds=9),
    )
    with database.unit_of_work() as uow:
        wrong_offset = uow.events.append(wrong_event).offset
        with pytest.raises(PersistenceConflictError, match="GatePassed Event"):
            uow.states.put_checkpoint(
                Checkpoint(
                    plan_revision=plan,
                    run=run,
                    event_offset=wrong_offset,
                    gate_decision=decision,
                    branch_selections={fork_id: branch_id},
                    artifact_refs=(artifact.artifact_id,),
                    created_at=NOW + timedelta(seconds=10),
                )
            )

    missing_check_decision = replace(decision, required_check_ids=(check_id, new_id()))
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="lacks a passing evidenced CheckRun"),
    ):
        uow.states.put_checkpoint(
            Checkpoint(
                plan_revision=plan,
                run=run,
                event_offset=stored_event.offset,
                gate_decision=missing_check_decision,
                branch_selections={fork_id: branch_id},
                artifact_refs=(artifact.artifact_id,),
                created_at=NOW + timedelta(seconds=10),
            )
        )

    stale_attempt_decision = replace(decision, attempt_id=new_id())
    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match=r"Attempt .* is not persisted"),
    ):
        uow.states.put_checkpoint(
            Checkpoint(
                plan_revision=plan,
                run=run,
                event_offset=stored_event.offset,
                gate_decision=stale_attempt_decision,
                branch_selections={fork_id: branch_id},
                artifact_refs=(artifact.artifact_id,),
                created_at=NOW + timedelta(seconds=10),
            )
        )

    other_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node_id,
        sequence=2,
        attempt_id=new_id(),
        created_at=NOW + timedelta(seconds=9),
    )
    other_artifact = Artifact(
        artifact_id=new_id(),
        kind=ArtifactKind.EVIDENCE,
        name="other.json",
        media_type="application/json",
        size_bytes=2,
        sha256="c" * 64,
        relative_path="artifacts/other.json",
        created_at=NOW + timedelta(seconds=9),
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=other_attempt.attempt_id,
    )
    cross_check_run = CheckRun(
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        check_id=check_id,
        created_at=NOW + timedelta(seconds=9),
    ).start(at=NOW + timedelta(seconds=10))
    cross_result = CheckResult(
        check_id=check_id,
        check_run_id=cross_check_run.check_run_id,
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        passed=True,
        evaluated_at=NOW + timedelta(seconds=11),
        evidence_artifact_ids=(other_artifact.artifact_id,),
        output="cross-scope evidence",
    )
    cross_check_run = cross_check_run.complete(cross_result, at=NOW + timedelta(seconds=12))
    cross_decision = Gate(required_check_ids=(check_id,)).evaluate(
        (cross_result,),
        run_id=run.run_id,
        plan_node_id=node_id,
        attempt_id=attempt.attempt_id,
        at=NOW + timedelta(seconds=12),
    )
    cross_event = Event(
        type=EventType.GATE_PASSED,
        correlation_id=cross_decision.gate_id,
        run_id=run.run_id,
        payload={"gate_id": cross_decision.gate_id},
        occurred_at=NOW + timedelta(seconds=12),
    )
    with database.unit_of_work() as uow:
        uow.states.put_attempt(other_attempt)
        uow.states.put_artifact(other_artifact)
        uow.states.put_check_run(cross_check_run)
        cross_offset = uow.events.append(cross_event).offset
        with pytest.raises(PersistenceConflictError, match="belongs to another execution scope"):
            uow.states.put_checkpoint(
                Checkpoint(
                    plan_revision=plan,
                    run=run,
                    event_offset=cross_offset,
                    gate_decision=cross_decision,
                    branch_selections={fork_id: branch_id},
                    artifact_refs=(other_artifact.artifact_id,),
                    created_at=NOW + timedelta(seconds=13),
                )
            )
