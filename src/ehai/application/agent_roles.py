"""Backend-neutral role prompts, context and authorized host tool composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ehai import ID, JsonValue, new_id, normalize_id
from ehai.application.agent_contracts import (
    ModelMessage,
    ToolDefinition,
    ToolExecutor,
    ToolHandler,
    ToolSet,
)
from ehai.application.agent_trace import AgentTrace, AgentTraceEvent

ContextBuilder = Callable[[Mapping[str, JsonValue]], Mapping[str, JsonValue]]
BeforeStepMessages = Callable[[ID], tuple[ModelMessage, ...]]


class AgentRole(StrEnum):
    PLANNER = "planner"
    WORKER = "worker"
    EVALUATOR = "evaluator"
    MERGE = "merge"
    VISUALIZER = "visualizer"
    REVIEWER = "reviewer"
    ASSISTANCE = "assistance"


def _identity_context(context: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return context


@dataclass(frozen=True, slots=True)
class AgentRoleConfig:
    """All behavior that may vary between internal model Roles."""

    role: AgentRole
    system_prompt: str
    tool_profile: str
    tool_names: tuple[str, ...]
    finish_tool: str
    permissions: frozenset[str] = frozenset()
    context_builder: ContextBuilder = _identity_context
    final_tool_requires_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise TypeError("role must be a AgentRole")
        for value, name in (
            (self.system_prompt, "system_prompt"),
            (self.tool_profile, "tool_profile"),
            (self.finish_tool, "finish_tool"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
        names = tuple(self.tool_names)
        if not names or len(set(names)) != len(names):
            raise ValueError("tool_names must be non-empty and unique")
        if self.finish_tool not in names:
            raise ValueError("finish_tool must belong to the Role Tool Profile")
        if not isinstance(self.final_tool_requires_only, bool):
            raise ValueError("final_tool_requires_only must be a boolean")
        object.__setattr__(self, "tool_names", names)
        object.__setattr__(self, "permissions", frozenset(self.permissions))


class ToolRegistry:
    """Immutable Tool capability catalog selected by one Role profile."""

    def __init__(
        self,
        definitions: tuple[ToolDefinition, ...],
        handlers: Mapping[str, ToolHandler],
    ) -> None:
        catalog = ToolSet(definitions)
        if set(handlers) != {definition.name for definition in catalog.definitions}:
            raise ValueError("ToolRegistry handlers must exactly match its definitions")
        self._definitions = {definition.name: definition for definition in catalog.definitions}
        self._handlers = dict(handlers)

    def freeze(self, config: AgentRoleConfig) -> tuple[ToolSet, ToolExecutor]:
        try:
            definitions = tuple(self._definitions[name] for name in config.tool_names)
        except KeyError as error:
            raise ValueError(f"Tool Profile references unknown Tool {error.args[0]!r}") from error
        finish = next(item for item in definitions if item.name == config.finish_tool)
        if not finish.ends_turn:
            raise ValueError("Role finish_tool must be marked ends_turn")
        tool_set = ToolSet(definitions)
        handlers = {name: self._handlers[name] for name in config.tool_names}
        return tool_set, ToolExecutor(tool_set, handlers)


@dataclass(frozen=True, slots=True)
class AgentRoleExecution:
    """Internal Role execution identity not owned by a Worker Attempt."""

    agent_session_ref_id: ID
    attempt_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_session_ref_id", normalize_id(self.agent_session_ref_id))
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))


class MemoryAgentTraceStore:
    """Process-local store used when no application SQLite store is configured."""

    def __init__(self) -> None:
        self._events: dict[ID, tuple[AgentTraceEvent, ...]] = {}

    def create(self, agent_session_ref_id: ID | None = None) -> AgentTrace:
        session_id = normalize_id(agent_session_ref_id or new_id())
        self._events.setdefault(session_id, ())
        return self.load(session_id)

    def load(self, agent_session_ref_id: ID) -> AgentTrace:
        session_id = normalize_id(agent_session_ref_id)
        return AgentTrace(session_id, self._events.get(session_id, ()))

    def append(
        self,
        agent_session_ref_id: ID,
        expected_sequence: int,
        events: tuple[AgentTraceEvent, ...],
    ) -> None:
        session_id = normalize_id(agent_session_ref_id)
        current = self._events.get(session_id, ())
        if len(current) != expected_sequence:
            raise RuntimeError("Agent trace optimistic sequence mismatch")
        self._events[session_id] = current + tuple(events)
