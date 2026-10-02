"""Normal CLI backend discovery, isolated from legacy model/Run assembly."""

from __future__ import annotations


def resolve_planner_kind(kind: str | None, *, pi_configured: bool, model: str | None) -> str:
    """Explicit model/backend intent must not fall through to a scripted Planner."""
    if kind is not None:
        return kind
    return "pi" if pi_configured or model is not None else "single"
