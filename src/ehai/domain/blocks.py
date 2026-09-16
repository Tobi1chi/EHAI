"""Host-authored block lineage; it is neither approval nor a Git revert recipe."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ehai import ID, JsonValue, normalize_id


class BlockChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class BlockChange:
    """One logical block's transition in a process revision.

    Version advances when execution identity changes. Removed entries retain
    their last version. Unchanged is eligibility for existing review, not proof
    that a result is valid or a permission to adopt a commit.
    """

    block_id: ID
    version: int
    previous_node_id: ID | None
    node_id: ID | None
    change: BlockChangeKind
    changed_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_id", normalize_id(self.block_id))
        for name in ("previous_node_id", "node_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, normalize_id(value))
        object.__setattr__(self, "change", BlockChangeKind(self.change))
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Block version must be a positive integer")
        fields = tuple(self.changed_fields)
        if any(not isinstance(item, str) or not item for item in fields):
            raise ValueError("Block changed_fields must contain nonempty strings")
        if len(set(fields)) != len(fields):
            raise ValueError("Block changed_fields must not repeat")
        object.__setattr__(self, "changed_fields", fields)
        old, new = self.previous_node_id, self.node_id
        valid = {
            BlockChangeKind.ADDED: old is None and new == self.block_id and self.version == 1,
            BlockChangeKind.REMOVED: old is not None and new is None,
            BlockChangeKind.UNCHANGED: old is not None and old == new,
            BlockChangeKind.MODIFIED: old is not None and new is not None and old != new,
        }
        if not valid[self.change] or (bool(fields) != (self.change is BlockChangeKind.MODIFIED)):
            raise ValueError("Block change does not match its node identities or changed fields")

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "block_id": self.block_id,
            "version": self.version,
            "previous_node_id": self.previous_node_id,
            "node_id": self.node_id,
            "change": self.change.value,
            "changed_fields": list(self.changed_fields),
        }
