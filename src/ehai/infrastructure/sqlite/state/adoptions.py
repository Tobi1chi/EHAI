"""Result adoptions carried across Runs."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from ehai import ID, format_utc_datetime, json_dumps, json_loads
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind, CheckRunStatus
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    BranchStatus,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevisionStatus,
)
from ehai.infrastructure.sqlite.state.common import (
    PersistenceConflictError,
    _required_plan_node,
    _row_string,
)
from ehai.infrastructure.sqlite.state.host import StateRepositoryHost


class AdoptionStateMixin:
    """Result adoptions carried across Runs."""

    def get_result_adoption(self: StateRepositoryHost, adoption_id: ID) -> ResultAdoption | None:
        row = self._connection.execute(
            """
            SELECT adoption_id, target_run_id, target_plan_revision_id, target_plan_node_id,
                source_run_id, source_plan_revision_id, source_process_revision_id,
                source_plan_node_id, source_attempt_id, created_at, snapshot_json
            FROM result_adoptions WHERE adoption_id = ?
            """,
            (adoption_id,),
        ).fetchone()
        return None if row is None else self._decode_result_adoption_row(row)

    def list_result_adoptions(
        self: StateRepositoryHost, target_run_id: ID
    ) -> tuple[ResultAdoption, ...]:
        return tuple(
            self._decode_result_adoption_row(row)
            for row in self._connection.execute(
                """
                SELECT adoption_id, target_run_id, target_plan_revision_id, target_plan_node_id,
                source_run_id, source_plan_revision_id, source_process_revision_id,
                source_plan_node_id, source_attempt_id, created_at, snapshot_json
            FROM result_adoptions
                WHERE target_run_id = ? ORDER BY created_at, adoption_id
                """,
                (target_run_id,),
            ).fetchall()
        )

    def put_result_adoption(self: StateRepositoryHost, record: ResultAdoption) -> None:
        if not isinstance(record, ResultAdoption):
            raise TypeError("record must be a ResultAdoption")

        existing = self.get_result_adoption(record.adoption_id)
        if existing is not None:
            if existing != record:
                raise PersistenceConflictError(f"ResultAdoption {record.adoption_id} is immutable")
            return

        collision = self._connection.execute(
            """
            SELECT adoption_id FROM result_adoptions
            WHERE target_run_id = ? AND target_plan_node_id = ?
            """,
            (record.target_run_id, record.target_plan_node_id),
        ).fetchone()
        if collision is not None:
            raise PersistenceConflictError(
                "Target Run and PlanNode already have a different ResultAdoption"
            )

        self._validate_result_adoption(record)
        snapshot = json_dumps(record.to_dict())
        try:
            self._connection.execute(
                """
                INSERT INTO result_adoptions(
                    adoption_id, target_run_id, target_plan_revision_id, target_plan_node_id,
                    source_run_id, source_plan_revision_id, source_process_revision_id,
                    source_plan_node_id, source_attempt_id, created_at, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.adoption_id,
                    record.target_run_id,
                    record.target_plan_revision_id,
                    record.target_plan_node_id,
                    record.source_run_id,
                    record.source_plan_revision_id,
                    record.source_process_revision_id,
                    record.source_plan_node_id,
                    record.source_attempt_id,
                    format_utc_datetime(record.created_at),
                    snapshot,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PersistenceConflictError(
                f"ResultAdoption {record.adoption_id} cannot be persisted"
            ) from error

    def _adoption_verification(
        self: StateRepositoryHost, run: Run, node_id: ID, attempt: Attempt, adoption_id: ID
    ) -> ResultAdoption:
        """Resolve a retained target binding without relabelling its producer."""
        record = self.get_result_adoption(adoption_id)
        if record is None or (
            record.target_run_id != run.run_id
            or record.target_plan_revision_id != run.plan_revision_id
            or record.target_plan_node_id != node_id
            or record.source_run_id != run.predecessor_run_id
            or record.source_run_id != attempt.run_id
            or record.source_plan_node_id != attempt.plan_node_id
            or record.source_attempt_id != attempt.attempt_id
            or attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise PersistenceConflictError("Verification has no matching adopted producer")
        approved = self._required_plan_revision(run.plan_revision_id)
        original_node = _required_plan_node(approved, node_id)
        if original_node.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            raise PersistenceConflictError("Only implementation nodes can accept prior results")
        current_node = _required_plan_node(self._required_execution_plan(run.run_id), node_id)
        if replace(current_node, status=original_node.status) != original_node:
            raise PersistenceConflictError("Adopted target node changed after its acceptance")
        if any(item.plan_node_id == node_id for item in self.list_attempts(run.run_id)):
            raise PersistenceConflictError("Target execution has superseded the adopted result")
        if {item.artifact_id for item in record.evidence} != set(attempt.artifact_ids):
            raise PersistenceConflictError("Adopted producer evidence no longer matches")
        for evidence in record.evidence:
            artifact = self.get_artifact(evidence.artifact_id)
            if artifact is None or (
                artifact.run_id != attempt.run_id
                or artifact.plan_node_id != attempt.plan_node_id
                or artifact.attempt_id != attempt.attempt_id
                or artifact.sha256 != evidence.sha256
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            ):
                raise PersistenceConflictError("Adopted verification evidence is inconsistent")
        return record

    def _validate_result_adoption(self: StateRepositoryHost, record: ResultAdoption) -> None:
        target_run = self._required_run(record.target_run_id)
        if target_run.predecessor_run_id != record.source_run_id:
            raise PersistenceConflictError("ResultAdoption source is not the bound predecessor Run")
        if target_run.status is not RunStatus.PENDING:
            raise PersistenceConflictError(
                f"ResultAdoption target Run {target_run.run_id} must be pending"
            )
        target_plan = self._required_plan_revision(record.target_plan_revision_id)
        if target_plan.status is not PlanRevisionStatus.APPROVED:
            raise PersistenceConflictError(
                f"ResultAdoption target PlanRevision {target_plan.plan_revision_id} "
                "must be approved"
            )
        if (
            target_run.plan_revision_id != record.target_plan_revision_id
            or target_plan.goal_id != target_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption target Run and PlanRevision do not match"
            )
        target_execution_plan = self._required_execution_plan(target_run.run_id)
        if (
            target_execution_plan.plan_revision_id != record.target_plan_revision_id
            or target_execution_plan.goal_id != target_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption target execution plan does not match its approved PlanRevision"
            )
        target_node = _required_plan_node(target_execution_plan, record.target_plan_node_id)
        if target_node.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            raise PersistenceConflictError("ResultAdoption target must be an implementation node")
        if target_node.status is not PlanNodeStatus.PENDING:
            raise PersistenceConflictError(
                f"ResultAdoption target PlanNode {target_node.plan_node_id} must be pending"
            )

        source_run = self._required_run(record.source_run_id)
        if source_run.status is not RunStatus.PAUSED:
            raise PersistenceConflictError(
                f"ResultAdoption source Run {source_run.run_id} must be paused"
            )
        if source_run.goal_id != target_run.goal_id:
            raise PersistenceConflictError(
                "ResultAdoption source and target Runs must belong to the same Goal"
            )
        source_plan = self._required_plan_revision(record.source_plan_revision_id)
        if (
            source_plan.status is not PlanRevisionStatus.APPROVED
            or source_run.plan_revision_id != record.source_plan_revision_id
            or source_plan.goal_id != source_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption source Run and PlanRevision do not match"
            )
        source_execution_plan = self._required_execution_plan(source_run.run_id)
        if (
            source_execution_plan.plan_revision_id != record.source_plan_revision_id
            or source_execution_plan.goal_id != source_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption source execution plan does not match its PlanRevision"
            )
        active_process = self._required_active_process_revision(source_run.run_id)
        if active_process.process_revision_id != record.source_process_revision_id:
            raise PersistenceConflictError(
                "ResultAdoption source ProcessRevision is not the active revision"
            )
        source_node = _required_plan_node(source_execution_plan, record.source_plan_node_id)
        if source_node.status is not PlanNodeStatus.COMPLETED:
            raise PersistenceConflictError(
                f"ResultAdoption source PlanNode {source_node.plan_node_id} must be completed"
            )

        attempts = self.list_attempts(source_run.run_id)
        if any(
            attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING} for attempt in attempts
        ):
            raise PersistenceConflictError(
                f"ResultAdoption source Run {source_run.run_id} has unresolved Attempts"
            )
        successful_attempts = tuple(
            attempt
            for attempt in attempts
            if attempt.plan_node_id == source_node.plan_node_id
            and attempt.status is AttemptStatus.SUCCEEDED
        )
        if (
            not successful_attempts
            or successful_attempts[-1].attempt_id != record.source_attempt_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption source Attempt is not the latest succeeded Attempt for its node"
            )
        source_attempt = successful_attempts[-1]
        if (
            source_attempt.run_id != source_run.run_id
            or source_attempt.plan_node_id != source_node.plan_node_id
            or source_attempt.attempt_id != record.source_attempt_id
        ):
            raise PersistenceConflictError("ResultAdoption source Attempt ownership does not match")

        check_specs = {
            spec.check_id: spec for spec in self.list_check_specs(source_plan.plan_revision_id)
        }
        for check in self.list_check_runs(source_run.run_id):
            if check.status not in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}:
                continue
            check_spec = check_specs.get(check.check_id)
            if check_spec is None or check_spec.kind is not CheckKind.HUMAN:
                raise PersistenceConflictError(
                    f"ResultAdoption source Run {source_run.run_id} has an unresolved "
                    "automatic Check"
                )

        source_branch = next(
            (
                branch
                for branch in source_execution_plan.branches
                if source_node.plan_node_id in branch.node_ids
            ),
            None,
        )
        if source_branch is not None and source_branch.status is not BranchStatus.SELECTED:
            raise PersistenceConflictError(
                f"ResultAdoption source PlanNode {source_node.plan_node_id} belongs to "
                f"unselected Branch {source_branch.branch_id}"
            )

        evidence_ids = tuple(item.artifact_id for item in record.evidence)
        if len(evidence_ids) != len(source_attempt.artifact_ids) or set(evidence_ids) != set(
            source_attempt.artifact_ids
        ):
            raise PersistenceConflictError(
                "ResultAdoption evidence must exactly match the source Attempt Artifacts"
            )
        for evidence in record.evidence:
            artifact = self.get_artifact(evidence.artifact_id)
            if artifact is None:
                raise PersistenceConflictError(
                    f"ResultAdoption evidence Artifact {evidence.artifact_id} is not persisted"
                )
            if (
                artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or artifact.run_id != source_run.run_id
                or artifact.plan_node_id != source_node.plan_node_id
                or artifact.attempt_id != source_attempt.attempt_id
                or artifact.sha256 != evidence.sha256
            ):
                raise PersistenceConflictError(
                    f"ResultAdoption evidence Artifact {evidence.artifact_id} does not match "
                    "the source Attempt"
                )

    def _decode_result_adoption_row(self: StateRepositoryHost, row: sqlite3.Row) -> ResultAdoption:
        try:
            decoded = json_loads(_row_string(row, "snapshot_json"))
            if not isinstance(decoded, dict):
                raise ValueError("snapshot is not a JSON object")
            record = ResultAdoption.from_mapping(decoded)
        except (TypeError, ValueError) as error:
            raise PersistenceConflictError("ResultAdoption snapshot is invalid") from error

        for field_name in (
            "adoption_id",
            "target_run_id",
            "target_plan_revision_id",
            "target_plan_node_id",
            "source_run_id",
            "source_plan_revision_id",
            "source_process_revision_id",
            "source_plan_node_id",
            "source_attempt_id",
        ):
            if _row_string(row, field_name) != str(getattr(record, field_name)):
                raise PersistenceConflictError(
                    f"ResultAdoption {record.adoption_id} projection does not match its snapshot"
                )
        if _row_string(row, "created_at") != format_utc_datetime(record.created_at):
            raise PersistenceConflictError(
                f"ResultAdoption {record.adoption_id} projection does not match its snapshot"
            )
        return record
