"""Project, Goal, and versioned completion-contract domain models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from ehai import ID, new_id, normalize_id, utc_now

if TYPE_CHECKING:
    from ehai.domain.checking import GateDecision

_CONTROLLED_STATE = object()


class GoalInvariantError(ValueError):
    """Raised when an operation would violate a Goal invariant."""


class GoalStatus(StrEnum):
    """P1 Goal lifecycle states."""

    OPEN = "open"
    SATISFIED = "satisfied"


@dataclass(frozen=True, slots=True)
class Project:
    """A long-lived workspace that owns Goals."""

    project_id: ID
    name: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validated_id(self.project_id, "Project", "project_id")
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, f"Project {self.project_id}"))
        if not self.name.strip():
            raise GoalInvariantError(f"Project {self.project_id} must have a name")

    @classmethod
    def create(
        cls,
        name: str,
        *,
        project_id: ID | None = None,
        created_at: datetime | None = None,
    ) -> Self:
        """Create a Project with generated identity and UTC timestamp defaults."""
        return cls(
            project_id=project_id or new_id(),
            name=name.strip(),
            created_at=created_at or utc_now(),
        )


@dataclass(frozen=True, slots=True)
class CompletionContract:
    """An immutable, explicitly versioned definition of Goal completion."""

    completion_contract_id: ID
    goal_id: ID
    version: int
    criteria: tuple[str, ...]
    required_check_ids: tuple[ID, ...]
    created_at: datetime
    confirmed_at: datetime | None = None
    supersedes_completion_contract_id: ID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "criteria", tuple(self.criteria))
        object.__setattr__(
            self,
            "completion_contract_id",
            _validated_id(
                self.completion_contract_id, "CompletionContract", "completion_contract_id"
            ),
        )
        object.__setattr__(
            self,
            "goal_id",
            _validated_id(
                self.goal_id, f"CompletionContract {self.completion_contract_id}", "goal_id"
            ),
        )
        owner = f"CompletionContract {self.completion_contract_id} for Goal {self.goal_id}"
        object.__setattr__(
            self,
            "required_check_ids",
            tuple(
                _validated_id(check_id, owner, "required_check_id")
                for check_id in self.required_check_ids
            ),
        )
        if self.supersedes_completion_contract_id is not None:
            object.__setattr__(
                self,
                "supersedes_completion_contract_id",
                _validated_id(
                    self.supersedes_completion_contract_id,
                    owner,
                    "supersedes_completion_contract_id",
                ),
            )
        object.__setattr__(self, "created_at", _utc(self.created_at, owner))
        if self.confirmed_at is not None:
            object.__setattr__(self, "confirmed_at", _utc(self.confirmed_at, owner))
        if self.version < 1:
            raise GoalInvariantError(f"{owner} must have a positive version")
        if not self.criteria or any(not criterion.strip() for criterion in self.criteria):
            raise GoalInvariantError(f"{owner} must have non-empty criteria")
        if len(set(self.required_check_ids)) != len(self.required_check_ids):
            raise GoalInvariantError(f"{owner} contains duplicate required Check IDs")
        if not self.required_check_ids:
            raise GoalInvariantError(f"{owner} must require at least one Check")
        if self.version == 1 and self.supersedes_completion_contract_id is not None:
            raise GoalInvariantError(f"{owner} version 1 cannot supersede another contract")
        if self.version > 1 and self.supersedes_completion_contract_id is None:
            raise GoalInvariantError(
                f"{owner} version {self.version} must identify its predecessor"
            )
        if self.confirmed_at is not None and self.confirmed_at < self.created_at:
            raise GoalInvariantError(f"{owner} cannot be confirmed before it was created")

    @property
    def is_confirmed(self) -> bool:
        """Return whether a user has confirmed this exact contract version."""
        return self.confirmed_at is not None

    @classmethod
    def draft(
        cls,
        goal_id: ID,
        criteria: Iterable[str],
        required_check_ids: Iterable[ID],
        *,
        completion_contract_id: ID | None = None,
        created_at: datetime | None = None,
    ) -> Self:
        """Create the first unconfirmed CompletionContract version for a Goal."""
        return cls(
            completion_contract_id=completion_contract_id or new_id(),
            goal_id=goal_id,
            version=1,
            criteria=tuple(criterion.strip() for criterion in criteria),
            required_check_ids=tuple(required_check_ids),
            created_at=created_at or utc_now(),
        )

    def confirm(self, *, confirmed_at: datetime | None = None) -> Self:
        """Confirm this exact version without mutating the draft instance."""
        if self.is_confirmed:
            return self
        return replace(self, confirmed_at=confirmed_at or utc_now())

    def revise(
        self,
        criteria: Iterable[str],
        required_check_ids: Iterable[ID],
        *,
        completion_contract_id: ID | None = None,
        created_at: datetime | None = None,
    ) -> Self:
        """Create a new unconfirmed version while preserving predecessor identity."""
        return type(self)(
            completion_contract_id=completion_contract_id or new_id(),
            goal_id=self.goal_id,
            version=self.version + 1,
            criteria=tuple(criterion.strip() for criterion in criteria),
            required_check_ids=tuple(required_check_ids),
            created_at=created_at or utc_now(),
            supersedes_completion_contract_id=self.completion_contract_id,
        )


@dataclass(frozen=True, slots=True)
class Goal:
    """A desired outcome whose satisfaction is controlled by a final Gate."""

    goal_id: ID
    project_id: ID
    objective: str
    created_at: datetime
    completion_contract: CompletionContract | None = None
    status: GoalStatus = GoalStatus.OPEN
    final_gate_id: ID | None = None
    satisfied_at: datetime | None = None
    _state_token: InitVar[object | None] = None

    def __post_init__(self, _state_token: object | None) -> None:
        object.__setattr__(self, "status", GoalStatus(self.status))
        object.__setattr__(self, "goal_id", _validated_id(self.goal_id, "Goal", "goal_id"))
        object.__setattr__(
            self,
            "project_id",
            _validated_id(self.project_id, f"Goal {self.goal_id}", "project_id"),
        )
        owner = f"Goal {self.goal_id}"
        object.__setattr__(self, "created_at", _utc(self.created_at, owner))
        if self.final_gate_id is not None:
            object.__setattr__(
                self,
                "final_gate_id",
                _validated_id(self.final_gate_id, owner, "final_gate_id"),
            )
        if self.satisfied_at is not None:
            object.__setattr__(self, "satisfied_at", _utc(self.satisfied_at, owner))
        if not self.objective.strip():
            raise GoalInvariantError(f"{owner} must have an objective")
        if (
            self.completion_contract is not None
            and self.completion_contract.goal_id != self.goal_id
        ):
            raise GoalInvariantError(f"{owner} cannot use another Goal's CompletionContract")
        if self.status is GoalStatus.OPEN:
            if self.final_gate_id is not None or self.satisfied_at is not None:
                raise GoalInvariantError(f"{owner} cannot have satisfaction evidence while open")
        else:
            if _state_token is not _CONTROLLED_STATE:
                raise GoalInvariantError(
                    f"{owner} satisfied state requires satisfy() or rehydrate()"
                )
            if self.final_gate_id is None or self.satisfied_at is None:
                raise GoalInvariantError(
                    f"{owner} satisfaction requires a final Gate and timestamp"
                )
            if self.completion_contract is None or not self.completion_contract.is_confirmed:
                raise GoalInvariantError(
                    f"{owner} satisfaction requires a confirmed CompletionContract"
                )
        if self.satisfied_at is not None and self.satisfied_at < self.created_at:
            raise GoalInvariantError(f"{owner} cannot be satisfied before creation")

    @classmethod
    def create(
        cls,
        project_id: ID,
        objective: str,
        *,
        goal_id: ID | None = None,
        created_at: datetime | None = None,
    ) -> Self:
        """Create an open Goal without silently inventing its completion criteria."""
        return cls(
            goal_id=goal_id or new_id(),
            project_id=project_id,
            objective=objective.strip(),
            created_at=created_at or utc_now(),
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        goal_id: ID,
        project_id: ID,
        objective: str,
        created_at: datetime,
        completion_contract: CompletionContract | None,
        status: GoalStatus,
        final_gate_id: ID | None,
        satisfied_at: datetime | None,
    ) -> Self:
        """Restore a validated Goal snapshot from trusted persistence data."""
        return cls(
            goal_id=goal_id,
            project_id=project_id,
            objective=objective,
            created_at=created_at,
            completion_contract=completion_contract,
            status=status,
            final_gate_id=final_gate_id,
            satisfied_at=satisfied_at,
            _state_token=_CONTROLLED_STATE,
        )

    def use_completion_contract(self, contract: CompletionContract) -> Self:
        """Attach an initial contract or move explicitly to its next version."""
        owner = f"Goal {self.goal_id}"
        if self.status is GoalStatus.SATISFIED:
            raise GoalInvariantError(f"{owner} cannot change contract after satisfaction")
        if contract.goal_id != self.goal_id:
            raise GoalInvariantError(f"{owner} cannot use another Goal's CompletionContract")

        current = self.completion_contract
        if current is None:
            if contract.version != 1 or contract.supersedes_completion_contract_id is not None:
                raise GoalInvariantError(f"{owner} must start with CompletionContract version 1")
        elif contract == current:
            return self
        elif (
            contract.completion_contract_id == current.completion_contract_id
            and contract.version == current.version
        ):
            confirmed_snapshot = replace(current, confirmed_at=contract.confirmed_at)
            if (
                current.confirmed_at is None
                and contract.confirmed_at is not None
                and contract == confirmed_snapshot
            ):
                return replace(self, completion_contract=contract)
            raise GoalInvariantError(
                f"{owner} cannot change CompletionContract {current.completion_contract_id} "
                f"without a new version"
            )
        elif (
            contract.version != current.version + 1
            or contract.supersedes_completion_contract_id != current.completion_contract_id
        ):
            raise GoalInvariantError(
                f"{owner} requires an explicit next CompletionContract version "
                f"after {current.version}"
            )

        return replace(self, completion_contract=contract)

    def satisfy(self, decision: GateDecision) -> Self:
        """Mark the Goal satisfied only from a passing, evidenced final Gate decision."""
        owner = f"Goal {self.goal_id}"
        if self.status is GoalStatus.SATISFIED:
            raise GoalInvariantError(f"{owner} is already satisfied")
        contract = self.completion_contract
        if contract is None or not contract.is_confirmed:
            raise GoalInvariantError(f"{owner} requires a confirmed CompletionContract")
        if not decision.passed:
            raise GoalInvariantError(
                f"{owner} cannot be satisfied by failed Gate {decision.gate_id}"
            )
        if not decision.required_check_ids:
            raise GoalInvariantError(
                f"{owner} final Gate {decision.gate_id} has no required Checks"
            )
        if not decision.evidence_artifact_ids:
            raise GoalInvariantError(f"{owner} final Gate {decision.gate_id} has no evidence")
        if set(decision.required_check_ids) != set(contract.required_check_ids):
            raise GoalInvariantError(
                f"{owner} final Gate {decision.gate_id} does not cover CompletionContract "
                f"{contract.completion_contract_id}"
            )

        return replace(
            self,
            status=GoalStatus.SATISFIED,
            final_gate_id=decision.gate_id,
            satisfied_at=decision.evaluated_at,
            _state_token=_CONTROLLED_STATE,
        )


def _validated_id(value: ID, owner: str, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise GoalInvariantError(f"{owner} has invalid {field_name}: {error}") from error


def _utc(value: datetime, owner: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GoalInvariantError(f"{owner} timestamp must include a UTC offset")
    return value.astimezone(UTC)
