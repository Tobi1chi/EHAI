"""Check specs, Check runs, checkpoints and Artifacts."""

from __future__ import annotations

from ehai import ID, format_utc_datetime
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, Checkpoint, CheckRun, CheckRunStatus, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    PlanNode,
    PlanRevisionStatus,
)
from ehai.domain.process import (
    ProcessRevisionSource,
)
from ehai.domain.process import (
    process_graph_definition as _plan_structure,
)
from ehai.infrastructure.sqlite.codec import (
    decode_artifact,
    decode_check_run,
    decode_check_spec,
    decode_checkpoint,
    encode_artifact,
    encode_check_run,
    encode_check_spec,
    encode_checkpoint,
    encode_execution_plan,
    encode_run,
)
from ehai.infrastructure.sqlite.state.common import (
    _CHECK_RUN_STATUS_TRANSITIONS,
    PersistenceConflictError,
    _required_plan_node,
    _row_index_integer,
    _row_index_string,
    _row_string,
)
from ehai.infrastructure.sqlite.state.host import StateRepositoryHost


class CheckStateMixin:
    """Check specs, Check runs, checkpoints and Artifacts."""

    def put_check_spec(
        self: StateRepositoryHost, plan_revision_id: ID, check_spec: CheckSpec
    ) -> None:
        plan = self._required_plan_revision(plan_revision_id)
        existing_row = self._connection.execute(
            "SELECT plan_revision_id, snapshot_json FROM check_specs WHERE check_id = ?",
            (check_spec.check_id,),
        ).fetchone()
        snapshot = encode_check_spec(check_spec)
        if existing_row is not None:
            owner_id = _row_index_string(existing_row, 0)
            stored_snapshot = _row_index_string(existing_row, 1)
            if owner_id != plan.plan_revision_id or stored_snapshot != snapshot:
                raise PersistenceConflictError(f"CheckSpec {check_spec.check_id} is immutable")
            return

        referenced_ids = {check_id for node in plan.nodes for check_id in node.required_check_ids}
        contract = self.get_completion_contract(plan.completion_contract_id)
        if contract is None:
            raise PersistenceConflictError(
                f"PlanRevision {plan.plan_revision_id} CompletionContract is not persisted"
            )
        if check_spec.required and check_spec.check_id not in referenced_ids:
            raise PersistenceConflictError(
                f"required CheckSpec {check_spec.check_id} is not referenced by PlanRevision "
                f"{plan.plan_revision_id}"
            )
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sort_index), -1) + 1 FROM check_specs WHERE plan_revision_id = ?",
            (plan.plan_revision_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate always returns a row
            raise RuntimeError("SQLite did not assign a CheckSpec order")
        sort_index = _row_index_integer(row, 0)
        self._connection.execute(
            """
            INSERT INTO check_specs(check_id, plan_revision_id, sort_index, kind, snapshot_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                check_spec.check_id,
                plan.plan_revision_id,
                sort_index,
                check_spec.kind.value,
                snapshot,
            ),
        )

    def get_check_spec(self: StateRepositoryHost, check_id: ID) -> CheckSpec | None:
        snapshot = self._snapshot("check_specs", "check_id", check_id)
        return None if snapshot is None else decode_check_spec(snapshot)

    def list_check_specs(self: StateRepositoryHost, plan_revision_id: ID) -> tuple[CheckSpec, ...]:
        return tuple(
            decode_check_spec(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM check_specs
                WHERE plan_revision_id = ? ORDER BY sort_index
                """,
                (plan_revision_id,),
            )
        )

    def put_check_run(self: StateRepositoryHost, check_run: CheckRun) -> None:
        attempt = self._required_attempt(check_run.attempt_id)
        run = self._required_run(check_run.run_id)
        if check_run.adoption_id is None:
            if attempt.run_id != check_run.run_id or attempt.plan_node_id != check_run.plan_node_id:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} does not match Attempt {attempt.attempt_id}"
                )
            plan = self._attempt_plan(attempt)
        else:
            adoption = self._adoption_verification(
                run, check_run.plan_node_id, attempt, check_run.adoption_id
            )
            plan = self._required_execution_plan(run.run_id)
            if check_run.result is not None and not set(
                check_run.result.evidence_artifact_ids
            ).issubset(item.artifact_id for item in adoption.evidence):
                raise PersistenceConflictError("Adopted Check result has unrelated evidence")
        node = _required_plan_node(plan, check_run.plan_node_id)
        if check_run.check_id not in node.required_check_ids:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} Check is not required by PlanNode "
                f"{node.plan_node_id}"
            )
        if check_run.human_request is not None or check_run.human_decision is not None:
            check_spec = self.get_check_spec(check_run.check_id)
            if check_spec is None:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} CheckSpec {check_run.check_id} "
                    "is not persisted"
                )
            if check_spec.kind is not CheckKind.HUMAN:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} has human metadata for a non-human Check"
                )
            self._validate_human_check_run(check_run, run, node, attempt)
        existing = self.get_check_run(check_run.check_run_id)
        if existing is not None:
            if _check_run_identity(existing) != _check_run_identity(check_run):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} identity changed"
                )
            if existing.human_request is not None and (
                existing.human_request != check_run.human_request
            ):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human request is immutable"
                )
            if existing.human_decision is not None and (
                existing.human_decision != check_run.human_decision
                or existing.result != check_run.result
            ):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human decision is immutable"
                )
            if check_run.status not in _CHECK_RUN_STATUS_TRANSITIONS[existing.status]:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} status transition is illegal"
                )
        self._connection.execute(
            """
            INSERT INTO check_runs(
                check_run_id, run_id, plan_node_id, attempt_id,
                check_id, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_run_id) DO UPDATE SET snapshot_json = excluded.snapshot_json
            """,
            (
                check_run.check_run_id,
                check_run.run_id,
                check_run.plan_node_id,
                check_run.attempt_id,
                check_run.check_id,
                format_utc_datetime(check_run.created_at),
                encode_check_run(check_run),
            ),
        )

    def _validate_human_check_run(
        self: StateRepositoryHost,
        check_run: CheckRun,
        run: Run,
        node: PlanNode,
        attempt: Attempt,
    ) -> None:
        request = check_run.human_request
        if request is None:
            if check_run.human_decision is not None:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human decision has no request"
                )
            return
        if request.plan_revision_id != run.plan_revision_id:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request PlanRevision does not match Run"
            )
        plan = (
            self._attempt_plan(attempt)
            if check_run.adoption_id is None
            else self._required_execution_plan(run.run_id)
        )
        if plan.status is not PlanRevisionStatus.APPROVED:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request requires an approved PlanRevision"
            )
        if (
            request.completion_contract_id != plan.completion_contract_id
            or request.completion_contract_version != plan.completion_contract_version
        ):
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request CompletionContract does not "
                "match its approved PlanRevision"
            )
        contract = self.get_completion_contract(request.completion_contract_id)
        if contract is None:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request CompletionContract is missing"
            )
        if (
            contract.goal_id != run.goal_id
            or contract.completion_contract_id != request.completion_contract_id
            or contract.version != request.completion_contract_version
            or not contract.is_confirmed
        ):
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request CompletionContract is not "
                "the current Run contract"
            )
        if attempt.status is not AttemptStatus.SUCCEEDED:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request requires a succeeded Attempt"
            )
        if (
            check_run.human_decision is not None
            and check_run.status is not CheckRunStatus.COMPLETED
        ):
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human decision requires a completed CheckRun"
            )

        attempt_artifact_ids = set(attempt.artifact_ids)
        request_artifact_ids = {evidence.artifact_id for evidence in request.evidence}
        if request_artifact_ids != attempt_artifact_ids:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human evidence must cover the complete "
                f"Attempt {attempt.attempt_id} candidate/patch snapshot"
            )
        for evidence in request.evidence:
            if evidence.artifact_id not in attempt_artifact_ids:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human evidence Artifact "
                    f"{evidence.artifact_id} is not on Attempt {attempt.attempt_id}"
                )
            artifact = self.get_artifact(evidence.artifact_id)
            if artifact is None:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human evidence Artifact "
                    f"{evidence.artifact_id} is missing"
                )
            if (
                artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or artifact.run_id != attempt.run_id
                or artifact.plan_node_id != attempt.plan_node_id
                or artifact.attempt_id != attempt.attempt_id
                or artifact.sha256 != evidence.sha256
            ):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human evidence Artifact "
                    f"{evidence.artifact_id} is not the current candidate/patch version"
                )

    def get_check_run(self: StateRepositoryHost, check_run_id: ID) -> CheckRun | None:
        snapshot = self._snapshot("check_runs", "check_run_id", check_run_id)
        return None if snapshot is None else decode_check_run(snapshot)

    def list_check_runs(self: StateRepositoryHost, run_id: ID) -> tuple[CheckRun, ...]:
        return tuple(
            decode_check_run(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM check_runs
                WHERE run_id = ? ORDER BY created_at, check_run_id
                """,
                (run_id,),
            )
        )

    def put_checkpoint(self: StateRepositoryHost, checkpoint: Checkpoint) -> None:
        existing = self.get_checkpoint(checkpoint.checkpoint_id)
        if existing is not None:
            if existing != checkpoint:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} is immutable"
                )
            return
        self._validate_checkpoint_references(checkpoint)
        snapshot = encode_checkpoint(checkpoint)
        self._connection.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, plan_revision_id, run_id, event_offset, gate_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.plan_revision_id,
                checkpoint.run_id,
                checkpoint.event_offset,
                checkpoint.gate_decision.gate_id,
                snapshot,
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO checkpoint_branch_selections(
                checkpoint_id, fork_node_id, branch_id
            ) VALUES (?, ?, ?)
            """,
            (
                (checkpoint.checkpoint_id, fork_id, branch_id)
                for fork_id, branch_id in checkpoint.branch_selections.items()
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO checkpoint_artifacts(checkpoint_id, artifact_id)
            VALUES (?, ?)
            """,
            ((checkpoint.checkpoint_id, artifact_id) for artifact_id in checkpoint.artifact_refs),
        )

    def get_checkpoint(self: StateRepositoryHost, checkpoint_id: ID) -> Checkpoint | None:
        snapshot = self._snapshot("checkpoints", "checkpoint_id", checkpoint_id)
        return None if snapshot is None else decode_checkpoint(snapshot)

    def list_checkpoints(self: StateRepositoryHost, run_id: ID) -> tuple[Checkpoint, ...]:
        return tuple(
            decode_checkpoint(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM checkpoints
                WHERE run_id = ? ORDER BY event_offset, checkpoint_id
                """,
                (run_id,),
            )
        )

    def restore_checkpoint_state(
        self: StateRepositoryHost, checkpoint: Checkpoint, restored_run: Run
    ) -> None:
        """Restore an audited Checkpoint without weakening normal transition checks."""
        stored_checkpoint = self.get_checkpoint(checkpoint.checkpoint_id)
        if stored_checkpoint != checkpoint:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} is not the persisted recovery snapshot"
            )
        latest_row = self._connection.execute(
            """
            SELECT checkpoint_id FROM checkpoints
            WHERE run_id = ? ORDER BY event_offset DESC, checkpoint_id DESC LIMIT 1
            """,
            (checkpoint.run_id,),
        ).fetchone()
        if latest_row is None or _row_index_string(latest_row, 0) != checkpoint.checkpoint_id:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} is superseded and cannot be restored"
            )
        current_run = self._required_run(checkpoint.run_id)
        current_plan = self._required_execution_plan(checkpoint.run_id)
        process = self._required_active_process_revision(checkpoint.run_id)
        legacy_baseline = (
            checkpoint.process_revision_id is None
            and process.version == 1
            and process.source is ProcessRevisionSource.LEGACY_SNAPSHOT
        )
        if checkpoint.process_revision_id != process.process_revision_id and not legacy_baseline:
            raise PersistenceConflictError("Checkpoint belongs to a different ProcessRevision")
        if current_run.status is not RunStatus.PAUSED:
            raise PersistenceConflictError(
                f"Run {current_run.run_id} must be paused before Checkpoint restore"
            )
        if (
            restored_run.run_id != checkpoint.run_id
            or restored_run.goal_id != checkpoint.run.goal_id
            or restored_run.plan_revision_id != checkpoint.plan_revision_id
            or restored_run.status is not RunStatus.PAUSED
        ):
            raise PersistenceConflictError(
                f"Run {restored_run.run_id} is not a paused snapshot of Checkpoint "
                f"{checkpoint.checkpoint_id}"
            )
        expected_run = (
            checkpoint.run.pause() if checkpoint.run.status is RunStatus.RUNNING else checkpoint.run
        )
        if restored_run != expected_run:
            raise PersistenceConflictError(
                f"Run {restored_run.run_id} differs from Checkpoint {checkpoint.checkpoint_id}"
            )
        if _plan_structure(current_plan) != _plan_structure(checkpoint.plan_revision):
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} PlanRevision structure changed"
            )

        self._connection.execute(
            "UPDATE run_execution_plans SET snapshot_json = ? WHERE run_id = ?",
            (encode_execution_plan(checkpoint.plan_revision), checkpoint.run_id),
        )
        self._connection.execute(
            "UPDATE runs SET snapshot_json = ? WHERE run_id = ?",
            (encode_run(restored_run), restored_run.run_id),
        )

    def put_artifact(self: StateRepositoryHost, artifact: Artifact) -> None:
        if artifact.run_id is not None:
            run = self._required_run(artifact.run_id)
            plan_revision = (
                self._required_execution_plan(run.run_id)
                if artifact.attempt_id is None
                else self._attempt_plan(self._required_attempt(artifact.attempt_id))
            )
            if artifact.plan_node_id is not None:
                _required_plan_node(plan_revision, artifact.plan_node_id)
            if artifact.attempt_id is not None:
                attempt = self._required_attempt(artifact.attempt_id)
                if (
                    attempt.run_id != artifact.run_id
                    or attempt.plan_node_id != artifact.plan_node_id
                ):
                    raise PersistenceConflictError(
                        f"Artifact {artifact.artifact_id} does not match Attempt "
                        f"{artifact.attempt_id}"
                    )
        snapshot = encode_artifact(artifact)
        existing = self._snapshot("artifacts", "artifact_id", artifact.artifact_id)
        if existing is not None:
            if existing != snapshot:
                raise PersistenceConflictError(f"Artifact {artifact.artifact_id} is immutable")
            return
        self._connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, run_id, plan_node_id, attempt_id, sha256,
                relative_path, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                artifact.run_id,
                artifact.plan_node_id,
                artifact.attempt_id,
                artifact.sha256,
                artifact.relative_path,
                format_utc_datetime(artifact.created_at),
                snapshot,
            ),
        )

    def get_artifact(self: StateRepositoryHost, artifact_id: ID) -> Artifact | None:
        snapshot = self._snapshot("artifacts", "artifact_id", artifact_id)
        return None if snapshot is None else decode_artifact(snapshot)

    def list_artifacts_for_run(self: StateRepositoryHost, run_id: ID) -> tuple[Artifact, ...]:
        return tuple(
            decode_artifact(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM artifacts
                WHERE run_id = ? ORDER BY created_at, artifact_id
                """,
                (run_id,),
            )
        )

    def _validate_checkpoint_references(self: StateRepositoryHost, checkpoint: Checkpoint) -> None:
        process = self._required_active_process_revision(checkpoint.run_id)
        if checkpoint.process_revision_id != process.process_revision_id:
            raise PersistenceConflictError("New Checkpoint must bind the active ProcessRevision")
        persisted_plan = self._required_execution_plan(checkpoint.run_id)
        if persisted_plan != checkpoint.plan_revision:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} PlanRevision snapshot is stale"
            )
        persisted_run = self._required_run(checkpoint.run_id)
        if persisted_run != checkpoint.run:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Run snapshot is stale"
            )
        row = self._connection.execute(
            "SELECT event_json FROM event_log WHERE event_offset = ?",
            (checkpoint.event_offset,),
        ).fetchone()
        if row is None:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Event offset is not persisted"
            )
        event = Event.from_json(_row_string(row, "event_json"))
        gate_id = event.payload.get("gate_id")
        if (
            event.type is not EventType.GATE_PASSED
            or event.run_id != checkpoint.run_id
            or gate_id != checkpoint.gate_decision.gate_id
        ):
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} does not reference its GatePassed Event"
            )

        decision = checkpoint.gate_decision
        node = _required_plan_node(persisted_plan, decision.plan_node_id)
        if not set(node.required_check_ids).issubset(decision.required_check_ids):
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Gate omits a required PlanNode Check"
            )
        attempt = self._required_attempt(decision.attempt_id)
        if decision.adoption_id is None:
            if attempt.run_id != decision.run_id or attempt.plan_node_id != decision.plan_node_id:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} Gate Attempt ownership does not match"
                )
        else:
            adoption = self._adoption_verification(
                persisted_run, decision.plan_node_id, attempt, decision.adoption_id
            )
            if not set(decision.evidence_artifact_ids).issubset(
                item.artifact_id for item in adoption.evidence
            ):
                raise PersistenceConflictError(
                    "Adopted Gate has evidence outside its accepted result"
                )

        decision_evidence = set(decision.evidence_artifact_ids)
        for check_id in decision.required_check_ids:
            candidates = tuple(
                decode_check_run(snapshot)
                for snapshot in self._snapshots(
                    """
                    SELECT snapshot_json FROM check_runs
                    WHERE run_id = ? AND plan_node_id = ?
                        AND attempt_id = ? AND check_id = ?
                    ORDER BY created_at, check_run_id
                    """,
                    (decision.run_id, decision.plan_node_id, decision.attempt_id, check_id),
                )
            )
            valid_results = tuple(
                check_run.result
                for check_run in candidates
                if check_run.status is CheckRunStatus.COMPLETED
                and check_run.adoption_id == decision.adoption_id
                and check_run.result is not None
                and check_run.result.passed
                and check_run.result.evidence_artifact_ids
            )
            if not valid_results or not any(
                set(result.evidence_artifact_ids).issubset(decision_evidence)
                for result in valid_results
            ):
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} lacks a passing evidenced "
                    f"CheckRun for Check {check_id}"
                )

        for artifact_id in decision.evidence_artifact_ids:
            artifact = self.get_artifact(artifact_id)
            if artifact is None:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} evidence Artifact "
                    f"{artifact_id} is not persisted"
                )
            if (
                artifact.run_id != attempt.run_id
                or artifact.plan_node_id != attempt.plan_node_id
                or artifact.attempt_id != decision.attempt_id
            ):
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} evidence Artifact "
                    f"{artifact_id} belongs to another execution scope"
                )


def _check_run_identity(check_run: CheckRun) -> tuple[object, ...]:
    return (
        check_run.check_run_id,
        check_run.run_id,
        check_run.plan_node_id,
        check_run.attempt_id,
        check_run.adoption_id,
        check_run.check_id,
        check_run.created_at,
    )
