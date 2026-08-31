"""Shared identifier conventions for the execution plane."""

from typing import NewType
from uuid import UUID, uuid4

ID = NewType("ID", str)
"""A canonical, lowercase UUID string."""


def new_id() -> ID:
    """Create a new random identifier in canonical UUID form."""
    return ID(str(uuid4()))


def normalize_id(value: str) -> ID:
    """Validate and normalize a UUID string used at a system boundary.

    The all-zero UUID is reserved as an invalid sentinel and is not a valid EHAI
    identifier.
    """
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"invalid identifier: {value!r}") from error

    if parsed.int == 0:
        raise ValueError("the nil UUID is not a valid identifier")

    return ID(str(parsed))
