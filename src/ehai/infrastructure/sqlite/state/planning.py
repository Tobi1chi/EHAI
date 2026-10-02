"""Projects, Goals, completion contracts, plan revisions and execution plans."""

from __future__ import annotations

import sqlite3

from ehai import ID, format_utc_datetime
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import (
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.process import (
    process_graph_definition as _plan_structure,
)
from ehai.infrastructure.sqlite.codec import (
    decode_attempt,
    decode_completion_contract,
    decode_execution_plan,
    decode_goal,
    decode_project,
    encode_branch,
    encode_completion_contract,
    encode_edge,
    encode_execution_plan,
    encode_goal,
    encode_plan_node,
    encode_plan_revision,
    encode_project,
)
from ehai.infrastructure.sqlite.state.common import (
    _BRANCH_STATUS_TRANSITIONS,
    _PLAN_NODE_STATUS_TRANSITIONS,
    _PLAN_REVISION_STATUS_TRANSITIONS,
    PersistenceConflictError,
    _row_index_string,
    _row_string,
)
from ehai.infrastructure.sqlite.state.host import StateRepositoryHost


class PlanningStateMixin:
    """Projects, Goals, completion contracts, plan revisions and execution plans."""

    def put_project(self: StateRepositoryHost, project: Project) -> None:
        self._connection.execute(
            """
            INSERT INTO projects(project_id, created_at, snapshot_json)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                created_at = excluded.created_at,
                snapshot_json = excluded.snapshot_json
            """,
            (
                project.project_id,
                format_utc_datetime(project.created_at),
                encode_project(project),
            ),
        )

    def get_project(self: StateRepositoryHost, project_id: ID) -> Project | None:
        snapshot = self._snapshot("projects", "project_id", project_id)
        return None if snapshot is None else decode_project(snapshot)

    def list_projects(self: StateRepositoryHost) -> tuple[Project, ...]:
        return tuple(
            decode_project(snapshot)
            for snapshot in self._snapshots(
                "SELECT snapshot_json FROM projects ORDER BY created_at, project_id"
            )
        )

    def put_goal(self: StateRepositoryHost, goal: Goal) -> None:
        contract_id = (
            None
            if goal.completion_contract is None
            else goal.completion_contract.completion_contract_id
        )
        self._connection.execute(
            """
            INSERT INTO goals(
                goal_id, project_id, current_completion_contract_id, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(goal_id) DO UPDATE SET
                project_id = excluded.project_id,
                current_completion_contract_id = excluded.current_completion_contract_id,
                created_at = excluded.created_at,
                snapshot_json = excluded.snapshot_json
            """,
            (
                goal.goal_id,
                goal.project_id,
                contract_id,
                format_utc_datetime(goal.created_at),
                encode_goal(goal),
            ),
        )

    def get_goal(self: StateRepositoryHost, goal_id: ID) -> Goal | None:
        row = self._connection.execute(
            """
            SELECT snapshot_json, current_completion_contract_id
            FROM goals WHERE goal_id = ?
            """,
            (goal_id,),
        ).fetchone()
        if row is None:
            return None
        contract_id = _optional_row_string(row, "current_completion_contract_id")
        contract = None if contract_id is None else self.get_completion_contract(ID(contract_id))
        return decode_goal(_row_string(row, "snapshot_json"), contract)

    def list_goals(self: StateRepositoryHost, project_id: ID) -> tuple[Goal, ...]:
        return tuple(
            self._required_goal(ID(goal_id))
            for goal_id in self._strings(
                """
                SELECT goal_id FROM goals
                WHERE project_id = ? ORDER BY created_at, goal_id
                """,
                (project_id,),
            )
        )

    def put_completion_contract(self: StateRepositoryHost, contract: CompletionContract) -> None:
        existing = self.get_completion_contract(contract.completion_contract_id)
        if existing is not None:
            same_structure = _completion_contract_structure(existing) == (
                _completion_contract_structure(contract)
            )
            valid_confirmation = (
                same_structure
                and existing.confirmed_at is None
                and contract.confirmed_at is not None
            )
            if existing != contract and not valid_confirmation:
                raise PersistenceConflictError(
                    f"CompletionContract {contract.completion_contract_id} history changed"
                )
        self._connection.execute(
            """
            INSERT INTO completion_contracts(
                completion_contract_id, goal_id, version,
                supersedes_completion_contract_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(completion_contract_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json
            """,
            (
                contract.completion_contract_id,
                contract.goal_id,
                contract.version,
                contract.supersedes_completion_contract_id,
                encode_completion_contract(contract),
            ),
        )

    def get_completion_contract(
        self: StateRepositoryHost, completion_contract_id: ID
    ) -> CompletionContract | None:
        snapshot = self._snapshot(
            "completion_contracts",
            "completion_contract_id",
            completion_contract_id,
        )
        return None if snapshot is None else decode_completion_contract(snapshot)

    def list_completion_contracts(
        self: StateRepositoryHost, goal_id: ID
    ) -> tuple[CompletionContract, ...]:
        return tuple(
            decode_completion_contract(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM completion_contracts
                WHERE goal_id = ? ORDER BY version
                """,
                (goal_id,),
            )
        )

    def put_plan_revision(self: StateRepositoryHost, plan_revision: PlanRevision) -> None:
        existing = self.get_plan_revision(plan_revision.plan_revision_id)
        if existing is not None and existing.status is PlanRevisionStatus.APPROVED:
            if existing != plan_revision:
                raise PersistenceConflictError(
                    "Approved PlanRevision is immutable; update the Run execution plan instead"
                )
            return
        self._validate_plan_update(plan_revision)
        self._validate_plan_child_identity(plan_revision)
        self._connection.execute(
            """
            INSERT INTO plan_revisions(
                plan_revision_id, goal_id, completion_contract_id, version,
                supersedes_plan_revision_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_revision_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json
            """,
            (
                plan_revision.plan_revision_id,
                plan_revision.goal_id,
                plan_revision.completion_contract_id,
                plan_revision.version,
                plan_revision.supersedes_plan_revision_id,
                encode_plan_revision(plan_revision),
            ),
        )
        for index, node in enumerate(plan_revision.nodes):
            self._connection.execute(
                """
                INSERT INTO plan_nodes(
                    plan_node_id, plan_revision_id, sort_index, snapshot_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(plan_node_id) DO UPDATE SET
                    sort_index = excluded.sort_index,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    node.plan_node_id,
                    plan_revision.plan_revision_id,
                    index,
                    encode_plan_node(node),
                ),
            )
        for index, branch in enumerate(plan_revision.branches):
            self._connection.execute(
                """
                INSERT INTO branches(
                    branch_id, plan_revision_id, fork_node_id, merge_node_id,
                    sort_index, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id) DO UPDATE SET
                    sort_index = excluded.sort_index,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    branch.branch_id,
                    plan_revision.plan_revision_id,
                    branch.fork_node_id,
                    branch.merge_node_id,
                    index,
                    encode_branch(branch),
                ),
            )
        for index, edge in enumerate(plan_revision.edges):
            self._connection.execute(
                """
                INSERT INTO edges(
                    edge_id, plan_revision_id, source_node_id, target_node_id,
                    branch_id, sort_index, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    sort_index = excluded.sort_index,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    edge.edge_id,
                    plan_revision.plan_revision_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.branch_id,
                    index,
                    encode_edge(edge),
                ),
            )

    def get_plan_revision(self: StateRepositoryHost, plan_revision_id: ID) -> PlanRevision | None:
        snapshot = self._snapshot("plan_revisions", "plan_revision_id", plan_revision_id)
        return None if snapshot is None else decode_execution_plan(snapshot)

    def list_plan_revisions(self: StateRepositoryHost, goal_id: ID) -> tuple[PlanRevision, ...]:
        return tuple(
            self._required_plan_revision(ID(revision_id))
            for revision_id in self._strings(
                """
                SELECT plan_revision_id FROM plan_revisions
                WHERE goal_id = ? ORDER BY version
                """,
                (goal_id,),
            )
        )

    def get_execution_plan(self: StateRepositoryHost, run_id: ID) -> PlanRevision | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        snapshot = self._snapshot("run_execution_plans", "run_id", run_id)
        if snapshot is None:
            raise PersistenceConflictError(f"Run {run_id} has no execution plan")
        plan = decode_execution_plan(snapshot)
        if plan.plan_revision_id != run.plan_revision_id or plan.goal_id != run.goal_id:
            raise PersistenceConflictError(f"Run {run_id} execution plan ownership changed")
        return plan

    def put_execution_plan(
        self: StateRepositoryHost, run_id: ID, plan_revision: PlanRevision
    ) -> None:
        run = self._required_run(run_id)
        existing = self._required_execution_plan(run_id)
        if plan_revision.plan_revision_id != run.plan_revision_id:
            raise PersistenceConflictError(f"Run {run_id} execution plan approval changed")
        process = self._required_active_process_revision(run_id)
        if _plan_structure(process.graph) != _plan_structure(plan_revision):
            raise PersistenceConflictError(
                "Execution graph differs from its active ProcessRevision"
            )
        self._validate_graph_update(existing, plan_revision, run_id=run_id)
        self._connection.execute(
            "UPDATE run_execution_plans SET snapshot_json = ? WHERE run_id = ?",
            (encode_execution_plan(plan_revision), run_id),
        )

    def _validate_plan_update(self: StateRepositoryHost, plan_revision: PlanRevision) -> None:
        existing = self.get_plan_revision(plan_revision.plan_revision_id)
        if existing is None:
            return
        self._validate_graph_update(existing, plan_revision)

    def _validate_graph_update(
        self: StateRepositoryHost,
        existing: PlanRevision,
        plan_revision: PlanRevision,
        *,
        run_id: ID | None = None,
    ) -> None:
        if _plan_structure(existing) != _plan_structure(plan_revision):
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} structure changed"
            )
        if plan_revision.status not in _PLAN_REVISION_STATUS_TRANSITIONS[existing.status]:
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} status regressed"
            )
        if (
            existing.status is plan_revision.status
            and existing.approved_at != plan_revision.approved_at
        ):
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} approval timestamp changed"
            )
        for previous_node, current_node in zip(existing.nodes, plan_revision.nodes, strict=True):
            adopting_candidate = (
                previous_node.status is PlanNodeStatus.READY
                and current_node.status is PlanNodeStatus.CANDIDATE
            )
            adopting_intermediate = (
                previous_node.status is PlanNodeStatus.CANDIDATE
                and current_node.status is PlanNodeStatus.COMPLETED
                and not current_node.required_check_ids
                and any(
                    edge.source_node_id == current_node.plan_node_id for edge in plan_revision.edges
                )
            )
            if run_id is not None and (adopting_candidate or adopting_intermediate):
                adoption = next(
                    (
                        item
                        for item in self.list_result_adoptions(run_id)
                        if item.target_plan_node_id == current_node.plan_node_id
                    ),
                    None,
                )
                if adoption is not None and not any(
                    item.plan_node_id == current_node.plan_node_id
                    for item in self.list_attempts(run_id)
                ):
                    run = self._required_run(run_id)
                    if run.status is not RunStatus.RUNNING:
                        raise PersistenceConflictError("Adopted result requires a running target")
                    self._adoption_verification(
                        run,
                        current_node.plan_node_id,
                        self._required_attempt(adoption.source_attempt_id),
                        adoption.adoption_id,
                    )
                    continue
            if (
                previous_node.status is PlanNodeStatus.CANDIDATE
                and current_node.status is PlanNodeStatus.COMPLETED
                and not current_node.required_check_ids
                and any(
                    edge.source_node_id == current_node.plan_node_id for edge in plan_revision.edges
                )
            ):
                evidence = self._connection.execute(
                    """
                    SELECT a.snapshot_json FROM attempts a
                    WHERE a.plan_node_id = ? AND a.run_id = ?
                    ORDER BY a.sequence DESC LIMIT 1
                    """,
                    (current_node.plan_node_id, run_id),
                ).fetchone()
                if evidence is not None:
                    attempt = decode_attempt(_row_index_string(evidence, 0))
                    if attempt.status is AttemptStatus.SUCCEEDED and attempt.artifact_ids:
                        continue
            if current_node.status not in _PLAN_NODE_STATUS_TRANSITIONS[previous_node.status]:
                raise PersistenceConflictError(
                    f"PlanNode {current_node.plan_node_id} has an illegal persisted "
                    "status transition"
                )
        for previous_branch, current_branch in zip(
            existing.branches, plan_revision.branches, strict=True
        ):
            if current_branch.status not in _BRANCH_STATUS_TRANSITIONS[previous_branch.status]:
                raise PersistenceConflictError(
                    f"Branch {current_branch.branch_id} has an illegal persisted status transition"
                )

    def _validate_plan_child_identity(
        self: StateRepositoryHost, plan_revision: PlanRevision
    ) -> None:
        child_ids = {
            "plan_nodes": (
                "plan_node_id",
                tuple(node.plan_node_id for node in plan_revision.nodes),
            ),
            "edges": ("edge_id", tuple(edge.edge_id for edge in plan_revision.edges)),
            "branches": ("branch_id", tuple(branch.branch_id for branch in plan_revision.branches)),
        }
        for table, (id_column, entity_ids) in child_ids.items():
            for entity_id in entity_ids:
                owner = self._connection.execute(
                    f"SELECT plan_revision_id FROM {table} WHERE {id_column} = ?",
                    (entity_id,),
                ).fetchone()
                if (
                    owner is not None
                    and _row_index_string(owner, 0) != plan_revision.plan_revision_id
                ):
                    raise PersistenceConflictError(
                        f"{id_column} {entity_id} already belongs to another PlanRevision"
                    )

        existing = self._connection.execute(
            """
            SELECT goal_id, completion_contract_id, version, supersedes_plan_revision_id
            FROM plan_revisions WHERE plan_revision_id = ?
            """,
            (plan_revision.plan_revision_id,),
        ).fetchone()
        identity = (
            str(plan_revision.goal_id),
            str(plan_revision.completion_contract_id),
            plan_revision.version,
            (
                None
                if plan_revision.supersedes_plan_revision_id is None
                else str(plan_revision.supersedes_plan_revision_id)
            ),
        )
        if existing is None:
            return
        if _identity(existing, 4) != identity:
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} identity changed"
            )
        expected_children = {
            "plan_nodes": {str(node.plan_node_id) for node in plan_revision.nodes},
            "edges": {str(edge.edge_id) for edge in plan_revision.edges},
            "branches": {str(branch.branch_id) for branch in plan_revision.branches},
        }
        id_columns = {
            "plan_nodes": "plan_node_id",
            "edges": "edge_id",
            "branches": "branch_id",
        }
        for table, expected in expected_children.items():
            column = id_columns[table]
            actual = set(
                self._strings(
                    f"SELECT {column} FROM {table} WHERE plan_revision_id = ?",
                    (plan_revision.plan_revision_id,),
                )
            )
            if actual != expected:
                raise PersistenceConflictError(
                    f"PlanRevision {plan_revision.plan_revision_id} graph identity changed"
                )

    def _required_goal(self: StateRepositoryHost, goal_id: ID) -> Goal:
        goal = self.get_goal(goal_id)
        if goal is None:  # pragma: no cover - selected from the same transaction
            raise RuntimeError(f"Goal {goal_id} disappeared during query")
        return goal

    def _required_plan_revision(self: StateRepositoryHost, plan_revision_id: ID) -> PlanRevision:
        revision = self.get_plan_revision(plan_revision_id)
        if revision is None:  # pragma: no cover - selected from the same transaction
            raise RuntimeError(f"PlanRevision {plan_revision_id} disappeared during query")
        return revision

    def _required_execution_plan(self: StateRepositoryHost, run_id: ID) -> PlanRevision:
        plan = self.get_execution_plan(run_id)
        if plan is None:
            raise PersistenceConflictError(f"Run {run_id} has no execution plan")
        return plan


def _optional_row_string(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"SQLite column {column} is not text or null")
    return value


def _identity(row: sqlite3.Row, size: int) -> tuple[object, ...]:
    return tuple(row[index] for index in range(size))


def _completion_contract_structure(contract: CompletionContract) -> tuple[object, ...]:
    return (
        contract.completion_contract_id,
        contract.goal_id,
        contract.version,
        contract.criteria,
        contract.required_check_ids,
        contract.created_at,
        contract.supersedes_completion_contract_id,
    )
