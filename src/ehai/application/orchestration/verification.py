"""Run Checks and Gates, take human decisions and record completion."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from ehai import ID, JsonValue, normalize_id
from ehai.application.checks import CheckContext
from ehai.application.orchestration.adoption import AdoptionMixin
from ehai.application.orchestration.common import (
    GateRejectedError,
    OrchestrationError,
    _evaluator_branches,
    _rehydrate_plan,
    _replace_node,
    _required_attempt,
    _required_check_specs,
    _required_node,
    _required_plan,
    _required_process_id,
    _required_run,
    _review_rework_has_active_work,
    _review_rework_uses_planner,
    _VerificationScope,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.orchestration.readiness import (
    _branch_containing,
    ready_nodes,
)
from ehai.application.pause_causes import PauseCause, pause_event_payload
from ehai.application.ports import UnitOfWork
from ehai.application.sanitization import bounded_redacted_text
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import (
    CheckKind,
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    CheckSpec,
    Gate,
    GateDecision,
    HumanCheckEvidence,
    HumanCheckRequest,
)
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import (
    BranchStatus,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanTransitionError,
)


class VerificationMixin:
    """Run Checks and Gates, take human decisions and record completion."""

    def _verification_workspace(self: OrchestratorHost, scope: _VerificationScope) -> Path:
        if scope.adoption is not None:
            if self._adoption_workspace is None:
                raise OrchestrationError("Adoption verification workspace is not configured")
            return self._adoption_workspace(scope.adoption.adoption_id)
        return (
            self._workspace
            if self._execution_workspace is None
            else self._required_execution_workspace(scope.attempt.attempt_id)
        )

    def _check_and_complete(
        self: OrchestratorHost,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        branch_local: bool,
        adoption_id: ID | None = None,
    ) -> Run:
        if not check_specs:
            return self._accept_intermediate(run_id, attempt_id, adoption_id=adoption_id)
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            if scope.run.run_id != run_id:
                raise OrchestrationError("Verification result does not match its target Run")
            verifying = scope.plan_node.status is PlanNodeStatus.VERIFYING
        if verifying:
            return self.finish_pending_gate(
                attempt_id, adoption_id=adoption_id, recover_interrupted_checks=True
            )
        context, prepared = self._start_checks(
            run_id, attempt_id, artifacts, check_specs, adoption_id=adoption_id
        )
        for spec, running_check in prepared:
            if spec.kind is CheckKind.HUMAN:
                continue
            assert context is not None
            try:
                executed = self._check_runner.run(spec, context)
                terminal = _rebind_check_run(running_check, executed)
            except Exception as error:
                terminal = running_check.fail(
                    f"CheckRunner failed: {type(error).__name__}: {error}",
                    at=self._clock(),
                )
            with self._uow_factory() as uow:
                self._persist_terminal_check(uow, _required_run(uow, run_id), terminal)
                uow.commit()
        return self.finish_pending_gate(attempt_id, adoption_id=adoption_id)

    def waiting_for_human(
        self: OrchestratorHost, run_id: ID, *, only_if_idle: bool = False
    ) -> bool:
        """Read durable open human requests without confusing them with operator pause."""
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                return False
            plan = _required_plan(uow, run.run_id)
            if only_if_idle and (
                ready_nodes(plan)
                or any(
                    node.status in {PlanNodeStatus.READY, PlanNodeStatus.STALLED}
                    for node in plan.nodes
                )
            ):
                return False
            verifying = {
                node.plan_node_id for node in plan.nodes if node.status is PlanNodeStatus.VERIFYING
            }
            return any(
                check.status is CheckRunStatus.RUNNING
                and check.human_request is not None
                and check.plan_node_id in verifying
                for check in uow.states.list_check_runs(run_id)
            )

    def record_human_decision(
        self: OrchestratorHost,
        uow: UnitOfWork,
        check_run_id: ID,
        request_token: str,
        *,
        passed: bool,
        actor: str,
        comment: str,
    ) -> tuple[Run, ID]:
        """Validate and record a human result in the caller's receipt transaction."""
        check = uow.states.get_check_run(check_run_id)
        if check is None or check.human_request is None:
            raise OrchestrationError("Human Check request does not exist")
        scope = self._load_verification_scope(uow, check.attempt_id, check.adoption_id)
        run = scope.run
        plan = scope.plan_revision
        contract = scope.completion_contract
        attempt = scope.attempt
        node = scope.plan_node
        if (
            check.run_id != run.run_id
            or check.plan_node_id != node.plan_node_id
            or check.adoption_id != (None if scope.adoption is None else scope.adoption.adoption_id)
        ):
            raise OrchestrationError("Human Check request does not match its verification subject")
        latest = (
            tuple(
                item
                for item in uow.states.list_attempts(run.run_id)
                if item.plan_node_id == node.plan_node_id
            )
            if scope.adoption is None
            else (attempt,)
        )
        specs = _required_check_specs(uow, plan, node)
        spec = next((item for item in specs if item.check_id == check.check_id), None)
        if (
            run.status is not RunStatus.RUNNING
            or node.status is not PlanNodeStatus.VERIFYING
            or attempt.status is not AttemptStatus.SUCCEEDED
            or not latest
            or latest[-1].attempt_id != attempt.attempt_id
            or check.status is not CheckRunStatus.RUNNING
            or spec is None
            or spec.kind is not CheckKind.HUMAN
        ):
            raise OrchestrationError("Human Check request is no longer open for this execution")
        artifacts = scope.artifacts
        current_request = self._human_request(plan, contract, spec, artifacts)
        if check.human_request != current_request or request_token != current_request.request_token:
            raise OrchestrationError("Human Check request or approved evidence version changed")
        for artifact in artifacts:
            if (
                sha256(self._artifact_store.read(artifact.artifact_id)).hexdigest()
                != artifact.sha256
            ):
                raise OrchestrationError(
                    "Human Check Artifact content no longer matches its snapshot"
                )
        at = self._clock()
        result = CheckResult(
            check_id=check.check_id,
            check_run_id=check.check_run_id,
            run_id=check.run_id,
            plan_node_id=check.plan_node_id,
            attempt_id=check.attempt_id,
            adoption_id=check.adoption_id,
            passed=passed,
            evaluated_at=at,
            evidence_artifact_ids=tuple(item.artifact_id for item in artifacts),
            output=comment,
            failure_reason=None if passed else comment,
        )
        self._persist_terminal_check(
            uow, run, check.decide_human(result, actor=actor, comment=comment, at=at)
        )
        return run, attempt.attempt_id

    @staticmethod
    def _human_request(
        plan: PlanRevision,
        contract: CompletionContract,
        spec: CheckSpec,
        artifacts: tuple[Artifact, ...],
    ) -> HumanCheckRequest:
        return HumanCheckRequest(
            plan_revision_id=plan.plan_revision_id,
            completion_contract_id=contract.completion_contract_id,
            completion_contract_version=contract.version,
            question=spec.description,
            evidence=tuple(
                HumanCheckEvidence(artifact.artifact_id, artifact.sha256)
                for artifact in sorted(artifacts, key=lambda item: item.artifact_id)
            ),
        )

    def _load_verification_scope(
        self: OrchestratorHost,
        uow: UnitOfWork,
        attempt_id: ID,
        adoption_id: ID | None,
    ) -> _VerificationScope:
        attempt = _required_attempt(uow, normalize_id(attempt_id))
        normalized_adoption_id = None if adoption_id is None else normalize_id(adoption_id)
        if normalized_adoption_id is None:
            run, plan, _, contract = self._load_run_context(uow, attempt.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(run.run_id)
                if artifact.artifact_id in attempt.artifact_ids
            )
            return _VerificationScope(run, plan, contract, node, attempt, None, artifacts)

        adoption = uow.states.get_result_adoption(normalized_adoption_id)
        if adoption is None:
            raise OrchestrationError(f"ResultAdoption {normalized_adoption_id} is not persisted")
        if adoption.adoption_id != normalized_adoption_id:
            raise OrchestrationError(
                f"ResultAdoption lookup returned another adoption for {normalized_adoption_id}"
            )
        if adoption.source_attempt_id != attempt.attempt_id:
            raise OrchestrationError(
                "Verification Attempt does not match the ResultAdoption source Attempt"
            )
        run, plan, _, contract = self._load_run_context(uow, adoption.target_run_id)
        node = _required_node(plan, adoption.target_plan_node_id)
        self._validate_adopted_scope(uow, run, plan, node, attempt, adoption)
        artifacts = self._adopted_artifacts(uow, attempt, adoption)
        return _VerificationScope(run, plan, contract, node, attempt, adoption, artifacts)

    def finish_pending_gate(
        self: OrchestratorHost,
        attempt_id: ID,
        *,
        adoption_id: ID | None = None,
        recover_interrupted_checks: bool = False,
    ) -> Run:
        """Finish persisted verification without rerunning completed automatic checks."""
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            attempt = scope.attempt
            run = scope.run
            plan = scope.plan_revision
            contract = scope.completion_contract
            node = scope.plan_node
            if run.status is not RunStatus.RUNNING or node.status is not PlanNodeStatus.VERIFYING:
                return run
            specs = _required_check_specs(uow, plan, node)
            expected_adoption_id = None if scope.adoption is None else scope.adoption.adoption_id
            all_checks = tuple(
                item
                for item in uow.states.list_check_runs(run.run_id)
                if (
                    item.attempt_id == attempt.attempt_id
                    and item.plan_node_id == node.plan_node_id
                    and item.adoption_id == expected_adoption_id
                )
            )
            checks = {item.check_id: item for item in all_checks}
            if any(
                check.run_id != run.run_id
                or check.plan_node_id != node.plan_node_id
                or check.attempt_id != attempt.attempt_id
                or check.adoption_id != expected_adoption_id
                for check in all_checks
            ):
                raise OrchestrationError(
                    "Persisted verification Check set belongs to another execution subject"
                )
            if len(checks) != len(all_checks) or set(checks) != set(node.required_check_ids):
                raise OrchestrationError(
                    "Persisted verification Check set is incomplete or ambiguous"
                )
            artifacts = scope.artifacts
            for spec in specs:
                check = checks[spec.check_id]
                if spec.kind is not CheckKind.HUMAN and check.status is CheckRunStatus.RUNNING:
                    if not recover_interrupted_checks:
                        return run
                    checks[spec.check_id] = check.interrupt(
                        "Automatic Check outcome was not persisted before recovery; not replayed",
                        at=self._clock(),
                    )
                    self._persist_terminal_check(uow, run, checks[spec.check_id])
            auto_passed = all(
                result is not None and result.passed
                for spec in specs
                if spec.kind is not CheckKind.HUMAN
                for result in (checks[spec.check_id].result,)
            )
            human_failed = any(
                check.human_request is not None
                and check.result is not None
                and not check.result.passed
                for check in checks.values()
            )
            waiting = False
            for spec in specs:
                check = checks[spec.check_id]
                if spec.kind is not CheckKind.HUMAN or check.status is not CheckRunStatus.RUNNING:
                    continue
                if not auto_passed or human_failed:
                    checks[spec.check_id] = check.cancel(
                        "Another required Check rejected this candidate", at=self._clock()
                    )
                    self._persist_terminal_check(uow, run, checks[spec.check_id])
                    continue
                if check.human_request is None:
                    request = self._human_request(plan, contract, spec, artifacts)
                    check = check.request_human(request)
                    checks[spec.check_id] = check
                    uow.states.put_check_run(check)
                    uow.events.append(
                        self._event(
                            EventType.CHECK_STARTED,
                            run,
                            check.check_run_id,
                            {
                                "check_id": check.check_id,
                                "check_run_id": check.check_run_id,
                                "awaiting_human": True,
                            },
                        )
                    )
                elif check.human_request != self._human_request(plan, contract, spec, artifacts):
                    raise OrchestrationError(
                        "Persisted Human Check request or accepted evidence version changed"
                    )
                waiting = True
            uow.commit()
            if waiting:
                return run
            terminal_checks = tuple(checks[spec.check_id] for spec in specs)
            run_id = run.run_id
            branch_local = _branch_containing(plan, node.plan_node_id) is not None
        results = tuple(
            check_run.result
            for check_run in terminal_checks
            if check_run.status is CheckRunStatus.COMPLETED and check_run.result is not None
        )
        gate = Gate(
            required_check_ids=tuple(check_run.check_id for check_run in terminal_checks),
            gate_id=self._id_factory(),
        )
        decision = gate.evaluate(
            results,
            run_id=run_id,
            plan_node_id=terminal_checks[0].plan_node_id,
            attempt_id=attempt_id,
            at=self._clock(),
            adoption_id=(None if scope.adoption is None else scope.adoption.adoption_id),
        )
        if not decision.passed:
            repair = self._execution_workspace is not None or any(
                phase.reviewer_node_id == decision.plan_node_id for phase in plan.phases
            )
            recorded = self._record_gate_failure(
                run_id,
                terminal_checks,
                decision,
                fail_run=not branch_local and not repair,
                repair=repair,
                adoption_id=(None if scope.adoption is None else scope.adoption.adoption_id),
            )
            if not recorded:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            if repair:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            if branch_local:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            raise GateRejectedError(
                f"run {run_id} failed Gate {decision.gate_id}: {decision.reason}"
            )
        return self._record_completion(
            run_id,
            terminal_checks,
            decision,
            artifacts,
            adoption_id=(None if scope.adoption is None else scope.adoption.adoption_id),
        )

    def _start_checks(
        self: OrchestratorHost,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        adoption_id: ID | None = None,
    ) -> tuple[CheckContext | None, tuple[tuple[CheckSpec, CheckRun], ...]]:
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            run, plan, attempt, node = (
                scope.run,
                scope.plan_revision,
                scope.attempt,
                scope.plan_node,
            )
            if run.run_id != run_id:
                raise OrchestrationError("Check start does not match its target Run")
            if tuple(spec.check_id for spec in check_specs) != node.required_check_ids or any(
                not spec.required for spec in check_specs
            ):
                raise OrchestrationError(
                    f"PlanNode {node.plan_node_id} required CheckSpecs are missing or optional"
                )
            verifying_node = node.begin_verification()
            prepared_workspace = (
                None if scope.adoption is None else self._verification_workspace(scope)
            )
            plan = _replace_node(plan, verifying_node)
            check_runs = tuple(
                CheckRun(
                    run_id=run.run_id,
                    plan_node_id=node.plan_node_id,
                    attempt_id=attempt.attempt_id,
                    adoption_id=adoption_id,
                    check_id=check_id,
                    check_run_id=self._id_factory(),
                    created_at=self._clock(),
                ).start(at=self._clock())
                for check_id in node.required_check_ids
            )
            if not check_runs:
                raise OrchestrationError(f"PlanNode {node.plan_node_id} has no required Checks")
            uow.states.put_execution_plan(run.run_id, plan)
            for spec, check_run in zip(check_specs, check_runs, strict=True):
                uow.states.put_check_run(check_run)
                if spec.kind is CheckKind.HUMAN:
                    continue
                uow.events.append(
                    self._event(
                        EventType.CHECK_STARTED,
                        run,
                        check_run.check_run_id,
                        {
                            "check_id": check_run.check_id,
                            "check_run_id": check_run.check_run_id,
                        },
                    )
                )
            uow.commit()
        if all(spec.kind is CheckKind.HUMAN for spec in check_specs):
            return None, tuple(zip(check_specs, check_runs, strict=True))
        evidence_artifacts = tuple(
            artifact
            for artifact in artifacts
            if artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
        )
        context = CheckContext(
            run=run,
            attempt=attempt,
            plan_node=verifying_node,
            artifacts=evidence_artifacts,
            workspace=(prepared_workspace or self._verification_workspace(scope)),
            code_workspace=self._execution_workspace is not None or scope.adoption is not None,
            adoption=scope.adoption,
        )
        return context, tuple(zip(check_specs, check_runs, strict=True))

    def _required_execution_workspace(self: OrchestratorHost, attempt_id: ID) -> Path:
        if self._execution_workspace is None:
            raise OrchestrationError("Code execution workspace resolver is missing")
        workspace = self._execution_workspace(attempt_id)
        if workspace is None:
            raise OrchestrationError(f"Code workspace for Attempt {attempt_id} is missing")
        return workspace

    def _persist_terminal_check(
        self: OrchestratorHost, uow: UnitOfWork, run: Run, check: CheckRun
    ) -> None:
        if check.ended_at is None:
            raise OrchestrationError("Only terminal Checks can record an outcome")
        if uow.states.get_check_run(check.check_run_id) == check:
            return
        uow.states.put_check_run(check)
        passed = check.result is not None and check.result.passed
        reason = (
            None
            if passed
            else (check.failure_reason if check.result is None else check.result.failure_reason)
        )
        uow.events.append(
            self._event(
                EventType.CHECK_PASSED if passed else EventType.CHECK_FAILED,
                run,
                check.check_run_id,
                {
                    "check_id": check.check_id,
                    "check_run_status": check.status.value,
                    "reason": reason,
                },
            )
        )

    def _record_completion(
        self: OrchestratorHost,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        artifacts: tuple[Artifact, ...],
        *,
        adoption_id: ID | None = None,
    ) -> Run:
        with self._uow_factory() as uow:
            run, plan, goal, _ = self._load_run_context(uow, run_id)
            node = _required_node(plan, decision.plan_node_id)
            expected_adoption_id = None if adoption_id is None else normalize_id(adoption_id)
            if decision.adoption_id != expected_adoption_id:
                raise OrchestrationError(
                    "Gate decision adoption does not match its verification subject"
                )
            if any(
                check.run_id != run.run_id
                or check.plan_node_id != node.plan_node_id
                or check.attempt_id != decision.attempt_id
                or check.adoption_id != expected_adoption_id
                for check in check_runs
            ):
                raise OrchestrationError("Gate checks belong to another verification subject")
            if not self._current_verification(
                uow, run, node, decision.attempt_id, expected_adoption_id
            ):
                return run
            for check_run in check_runs:
                self._persist_terminal_check(uow, run, check_run)
            gate_payload: dict[str, JsonValue] = {"gate_id": decision.gate_id}
            if expected_adoption_id is not None:
                gate_payload.update(
                    {
                        "adoption_id": expected_adoption_id,
                        "target_plan_node_id": node.plan_node_id,
                        "source_attempt_id": decision.attempt_id,
                    }
                )
            gate_event = uow.events.append(
                self._event(
                    EventType.GATE_PASSED,
                    run,
                    decision.gate_id,
                    gate_payload,
                )
            )
            completed_node = node.complete(decision)
            plan = _replace_node(plan, completed_node)
            uow.states.put_execution_plan(run.run_id, plan)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_COMPLETED,
                    run,
                    completed_node.plan_node_id,
                    {"plan_node_id": completed_node.plan_node_id},
                )
            )
            checkpoint = Checkpoint(
                plan_revision=plan,
                process_revision_id=_required_process_id(uow, run.run_id),
                run=run,
                event_offset=gate_event.offset,
                gate_decision=decision,
                branch_selections=_selected_branch_mapping(plan),
                artifact_refs=tuple(artifact.artifact_id for artifact in artifacts),
                checkpoint_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_checkpoint(checkpoint)
            uow.events.append(
                self._event(
                    EventType.CHECKPOINT_CREATED,
                    run,
                    checkpoint.checkpoint_id,
                    {"checkpoint_id": checkpoint.checkpoint_id},
                )
            )
            if not _is_final_completion(plan, completed_node):
                uow.commit()
                return run
            completed_run = run.complete(decision, at=self._clock())
            satisfied_goal = goal.satisfy(decision)
            uow.states.put_run(completed_run)
            uow.states.put_goal(satisfied_goal)
            uow.events.append(
                self._event(
                    EventType.RUN_COMPLETED,
                    completed_run,
                    completed_run.run_id,
                    {"run_id": completed_run.run_id},
                )
            )
            uow.commit()
        return completed_run

    def _record_gate_failure(
        self: OrchestratorHost,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        *,
        fail_run: bool,
        repair: bool = False,
        adoption_id: ID | None = None,
    ) -> bool:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, decision.plan_node_id)
            expected_adoption_id = None if adoption_id is None else normalize_id(adoption_id)
            if decision.adoption_id != expected_adoption_id:
                raise OrchestrationError(
                    "Gate decision adoption does not match its verification subject"
                )
            if not self._current_verification(
                uow, run, node, decision.attempt_id, expected_adoption_id
            ):
                return False
            if any(
                check.run_id != run.run_id
                or check.plan_node_id != node.plan_node_id
                or check.attempt_id != decision.attempt_id
                or check.adoption_id != expected_adoption_id
                for check in check_runs
            ):
                raise OrchestrationError("Gate checks belong to another verification subject")
            for check_run in check_runs:
                self._persist_terminal_check(uow, run, check_run)
            gate_payload: dict[str, JsonValue] = {
                "gate_id": decision.gate_id,
                "reason": decision.reason,
            }
            if expected_adoption_id is not None:
                gate_payload.update(
                    {
                        "adoption_id": expected_adoption_id,
                        "target_plan_node_id": node.plan_node_id,
                        "source_attempt_id": decision.attempt_id,
                    }
                )
            uow.events.append(
                self._event(
                    EventType.GATE_FAILED,
                    run,
                    decision.gate_id,
                    gate_payload,
                )
            )
            failed_node = node.fail()
            failed_plan = _replace_node(plan, failed_node)
            uow.states.put_execution_plan(run.run_id, failed_plan)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": decision.reason},
                )
            )
            if repair:
                if node.kind is PlanNodeKind.REVIEWER:
                    try:
                        self._reopen_reviewed_phase(uow, run, failed_plan, decision)
                    except (OrchestrationError, PlanTransitionError) as error:
                        if any(
                            attempt.status in {AttemptStatus.RUNNING, AttemptStatus.PENDING}
                            for attempt in uow.states.list_attempts(run.run_id)
                        ):
                            # Let the Runtime convergence path cancel pending work and
                            # quiesce live executions before persisting a paused Run.
                            raise
                        paused_reason = bounded_redacted_text(str(error), max_bytes=2_000)
                        if paused_reason is None:
                            paused_reason = "Reviewer phase rework could not be applied"
                        paused = run.pause()
                        uow.states.put_run(paused)
                        uow.events.append(
                            self._event(
                                EventType.RUN_PAUSED,
                                paused,
                                run.run_id,
                                pause_event_payload(
                                    run.run_id,
                                    PauseCause.REVIEW_REWORK,
                                    reason=paused_reason,
                                    gate_id=decision.gate_id,
                                ),
                            )
                        )
                else:
                    uow.states.put_execution_plan(
                        run.run_id, _replace_node(failed_plan, failed_node.retry())
                    )
                    uow.events.append(
                        self._event(
                            EventType.PLAN_NODE_READIED,
                            run,
                            node.plan_node_id,
                            {
                                "plan_node_id": node.plan_node_id,
                                "reason": "repair the candidate against the unchanged Gate",
                                "gate_id": decision.gate_id,
                            },
                        )
                    )
            if fail_run:
                failed_run = run.fail(
                    decision.reason or "Gate rejected candidate",
                    at=self._clock(),
                )
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        EventType.RUN_FAILED,
                        failed_run,
                        run.run_id,
                        {"run_id": run.run_id, "reason": decision.reason},
                    )
                )
            uow.commit()
            return True

    def _reopen_reviewed_phase(
        self: OrchestratorHost,
        uow: UnitOfWork,
        run: Run,
        plan: PlanRevision,
        decision: GateDecision,
    ) -> None:
        if _review_rework_uses_planner(uow, run.run_id):
            if _review_rework_has_active_work(uow, run.run_id):
                # queue_ready_attempts will persist the Planner pause after
                # existing actors settle; do not reopen or roll back the failure.
                return
            # The existing convergence/pause handler retains completed inputs.
            # Policy validation and its cumulative budget belong to the
            # coordinator, not to a blanket Phase-node reopening operation.
            raise OrchestrationError(
                "Reviewer Gate rejected the candidate; configured Planner adjustment "
                "must determine the affected rework scope"
            )
        phase = next(
            (item for item in plan.phases if item.reviewer_node_id == decision.plan_node_id), None
        )
        if phase is None:
            raise OrchestrationError(
                "Rejected Reviewer has no Phase rework route; Planner input is required"
            )
        phase_nodes = set(phase.node_ids)
        affected = {
            node_id
            for node_id in phase.rework_node_ids
            if (branch := _branch_containing(plan, node_id)) is None
            or branch.status is not BranchStatus.PRUNED
        }
        if not affected:
            raise OrchestrationError("The Phase rework route has no retained implementation target")
        pending = list(affected)
        while pending:
            source = pending.pop()
            for edge in plan.edges:
                if (
                    edge.source_node_id == source
                    and edge.target_node_id in phase_nodes
                    and edge.target_node_id not in affected
                ):
                    affected.add(edge.target_node_id)
                    pending.append(edge.target_node_id)
        if phase.reviewer_node_id not in affected:
            raise OrchestrationError("Phase rework targets do not lead to the rejected Reviewer")
        if any(
            attempt.plan_node_id in affected
            and attempt.status in {AttemptStatus.RUNNING, AttemptStatus.PENDING}
            for attempt in uow.states.list_attempts(run.run_id)
        ):
            raise OrchestrationError("Phase rework must first quiesce affected live Attempts")
        evaluators = tuple(
            node
            for node in plan.nodes
            if node.plan_node_id in affected and node.kind is PlanNodeKind.EVALUATOR
        )
        invalid_groups = {
            (branch.fork_node_id, branch.merge_node_id)
            for evaluator in evaluators
            for branch in _evaluator_branches(plan, evaluator)
        }
        reopened = tuple(
            node.reopen_for_rework() for node in plan.nodes if node.plan_node_id in affected
        )
        node_replacements = {node.plan_node_id: node for node in reopened}
        invalidated = tuple(
            branch
            for branch in plan.branches
            if (branch.fork_node_id, branch.merge_node_id) in invalid_groups
            and branch.status is not BranchStatus.ACTIVE
        )
        branch_replacements = {
            branch.branch_id: branch.reopen_selection() for branch in invalidated
        }
        revised = _rehydrate_plan(
            plan,
            nodes=tuple(node_replacements.get(node.plan_node_id, node) for node in plan.nodes),
            branches=tuple(
                branch_replacements.get(branch.branch_id, branch) for branch in plan.branches
            ),
        )
        # Compute the whole transition before writing any rework facts.
        uow.states.put_execution_plan(run.run_id, revised)
        for branch in invalidated:
            uow.events.append(
                self._event(
                    EventType.BRANCH_SELECTION_INVALIDATED,
                    run,
                    branch.branch_id,
                    {
                        "branch_id": branch.branch_id,
                        "fork_node_id": branch.fork_node_id,
                        "merge_node_id": branch.merge_node_id,
                        "phase_id": phase.phase_id,
                        "gate_id": decision.gate_id,
                        "review_attempt_id": decision.attempt_id,
                        "reason": "candidate dependencies require rework",
                    },
                )
            )
        for node in reopened:
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_REOPENED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "phase_id": phase.phase_id,
                        "reviewer_node_id": phase.reviewer_node_id,
                        "gate_id": decision.gate_id,
                        "review_attempt_id": decision.attempt_id,
                        "reason": "repair the implementation against the unchanged Phase Gate",
                    },
                )
            )

    @staticmethod
    def _current_verification(
        uow: UnitOfWork,
        run: Run,
        node: PlanNode,
        attempt_id: ID,
        adoption_id: ID | None = None,
    ) -> bool:
        attempts = tuple(
            item
            for item in uow.states.list_attempts(run.run_id)
            if item.plan_node_id == node.plan_node_id
        )
        if adoption_id is not None:
            try:
                normalized_adoption_id = normalize_id(adoption_id)
                adoption = uow.states.get_result_adoption(normalized_adoption_id)
                attempt = uow.states.get_attempt(normalize_id(attempt_id))
                plan = uow.states.get_execution_plan(run.run_id)
                if adoption is None or attempt is None or plan is None:
                    return False
                if adoption.adoption_id != normalized_adoption_id:
                    return False
                current_node = _required_node(plan, node.plan_node_id)
                AdoptionMixin._validate_adopted_scope(
                    uow, run, plan, current_node, attempt, adoption
                )
                AdoptionMixin._adopted_artifacts(uow, attempt, adoption)
            except (OrchestrationError, ValueError):
                return False
            return (
                run.status is RunStatus.RUNNING
                and current_node == node
                and node.status is PlanNodeStatus.VERIFYING
                and not attempts
            )
        return (
            run.status is RunStatus.RUNNING
            and node.status is PlanNodeStatus.VERIFYING
            and bool(attempts)
            and attempts[-1].attempt_id == attempt_id
            and attempts[-1].status is AttemptStatus.SUCCEEDED
        )


def _rebind_check_run(persisted: CheckRun, executed: CheckRun) -> CheckRun:
    """Attach a CheckRunner outcome to the already persisted running CheckRun ID."""
    if (
        persisted.run_id,
        persisted.plan_node_id,
        persisted.attempt_id,
        persisted.adoption_id,
        persisted.check_id,
    ) != (
        executed.run_id,
        executed.plan_node_id,
        executed.attempt_id,
        executed.adoption_id,
        executed.check_id,
    ):
        raise OrchestrationError("CheckRunner outcome belongs to a different verification subject")
    ended_at = executed.ended_at
    if ended_at is None:
        raise OrchestrationError(
            f"CheckRunner returned non-terminal CheckRun {executed.check_run_id}"
        )
    if executed.status is CheckRunStatus.COMPLETED and executed.result is not None:
        source = executed.result
        result = CheckResult(
            check_id=persisted.check_id,
            check_run_id=persisted.check_run_id,
            run_id=persisted.run_id,
            plan_node_id=persisted.plan_node_id,
            attempt_id=persisted.attempt_id,
            adoption_id=persisted.adoption_id,
            passed=source.passed,
            evaluated_at=source.evaluated_at,
            evidence_artifact_ids=source.evidence_artifact_ids,
            output=source.output,
            failure_reason=source.failure_reason,
        )
        return persisted.complete(result, at=ended_at)
    if executed.status is CheckRunStatus.TIMED_OUT:
        return persisted.time_out(
            executed.failure_reason or "Check adapter timed out",
            at=ended_at,
        )
    if executed.status is CheckRunStatus.FAILED:
        return persisted.fail(
            executed.failure_reason or "Check adapter failed",
            at=ended_at,
        )
    raise OrchestrationError(
        f"CheckRunner returned unsupported status {executed.status.value} for "
        f"Check {persisted.check_id}"
    )


def _selected_branch_mapping(plan: PlanRevision) -> dict[ID, ID]:
    return {
        branch.fork_node_id: branch.branch_id
        for branch in plan.branches
        if branch.status is BranchStatus.SELECTED
    }


def _is_final_completion(plan: PlanRevision, completed_node: PlanNode) -> bool:
    if any(edge.source_node_id == completed_node.plan_node_id for edge in plan.edges):
        return False
    if len(plan.nodes) == 1:
        return True
    pruned_branch_node_ids = {
        node_id
        for branch in plan.branches
        if branch.status is BranchStatus.PRUNED
        for node_id in branch.node_ids
    }
    return all(
        node.plan_node_id == completed_node.plan_node_id
        or node.status in {PlanNodeStatus.COMPLETED, PlanNodeStatus.PRUNED}
        or (node.plan_node_id in pruned_branch_node_ids and node.status is PlanNodeStatus.FAILED)
        for node in plan.nodes
    )
