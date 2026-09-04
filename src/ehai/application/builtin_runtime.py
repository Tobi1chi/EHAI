"""Shared Role and Tool composition for every internal Built-in Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, cast

from ehai import ID, JsonValue, new_id, normalize_id
from ehai.application.builtin_agent import (
    DEFAULT_AGENT_BUDGET,
    AgentBudget,
    BuiltinAgent,
    BuiltinAgentLoop,
    BuiltinExecution,
    BuiltinSession,
    BuiltinSessionEvent,
    BuiltinSessionStore,
    CancellationToken,
    ExecutionScope,
    ModelClient,
    ModelMessage,
    PromptBuilder,
    ToolDefinition,
    ToolExecutor,
    ToolHandler,
    ToolSet,
)

ContextBuilder = Callable[[Mapping[str, JsonValue]], Mapping[str, JsonValue]]
BeforeStepMessages = Callable[[ID], tuple[ModelMessage, ...]]


class BuiltinRole(StrEnum):
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
class BuiltinRoleConfig:
    """All behavior that may vary between internal model Roles."""

    role: BuiltinRole
    system_prompt: str
    tool_profile: str
    tool_names: tuple[str, ...]
    finish_tool: str
    permissions: frozenset[str] = frozenset()
    context_builder: ContextBuilder = _identity_context
    tool_choice: Literal["auto", "required"] = "required"
    final_tool_requires_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.role, BuiltinRole):
            raise TypeError("role must be a BuiltinRole")
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
        if self.tool_choice not in {"auto", "required"}:
            raise ValueError("tool_choice must be 'auto' or 'required'")
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

    def freeze(self, config: BuiltinRoleConfig) -> tuple[ToolSet, ToolExecutor]:
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
class BuiltinRoleExecution:
    """Internal Role execution identity not owned by a Worker Attempt."""

    agent_session_ref_id: ID
    attempt_id: ID = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_session_ref_id", normalize_id(self.agent_session_ref_id))
        object.__setattr__(self, "attempt_id", normalize_id(self.attempt_id))


class BuiltinAgentRuntime:
    """Construct and run the single Built-in Agent Loop for any internal Role."""

    def __init__(
        self,
        session_store: BuiltinSessionStore,
        *,
        budget: AgentBudget = DEFAULT_AGENT_BUDGET,
    ) -> None:
        self.session_store = session_store
        self.budget = budget

    def create_session(self, agent_session_ref_id: ID | None = None) -> BuiltinSession:
        create = getattr(self.session_store, "create", None)
        if not callable(create):
            raise RuntimeError("Builtin Session Store cannot create internal Role sessions")
        return cast(BuiltinSession, create(agent_session_ref_id))

    async def run(
        self,
        *,
        config: BuiltinRoleConfig,
        registry: ToolRegistry,
        model_client: ModelClient,
        session: BuiltinSession,
        execution: BuiltinExecution,
        instruction: str,
        context: Mapping[str, JsonValue],
        cancellation: CancellationToken | None = None,
        before_step_messages: BeforeStepMessages | None = None,
    ) -> str:
        tool_set, executor = registry.freeze(config)
        runtime_facts: dict[str, JsonValue] = {
            "role": config.role.value,
            "tool_profile": config.tool_profile,
            "finish_tool": config.finish_tool,
            "tool_names": cast(list[JsonValue], list(config.tool_names)),
            "permissions": cast(list[JsonValue], sorted(config.permissions)),
        }
        loop = BuiltinAgentLoop(
            model_client=model_client,
            prompt_builder=PromptBuilder(config.system_prompt),
            tool_set=tool_set,
            session_store=self.session_store,
            budget=self.budget,
            tool_choice=config.tool_choice,
            runtime_facts=runtime_facts,
            final_tool_requires_only=config.final_tool_requires_only,
            before_step_messages=before_step_messages,
        )
        agent = BuiltinAgent(loop)
        token = cancellation or CancellationToken()
        scope = ExecutionScope(
            agent=agent,
            session=session,
            tool_executor=executor,
            cancellation=token,
        )
        async with scope:
            return await agent.run(
                scope,
                execution,
                instruction,
                config.context_builder(context),
            )


class MemoryBuiltinSessionStore:
    """Process-local store used when no application SQLite store is configured."""

    def __init__(self) -> None:
        self._events: dict[ID, tuple[BuiltinSessionEvent, ...]] = {}

    def create(self, agent_session_ref_id: ID | None = None) -> BuiltinSession:
        session_id = normalize_id(agent_session_ref_id or new_id())
        self._events.setdefault(session_id, ())
        return BuiltinSession(session_id)

    def load(self, agent_session_ref_id: ID) -> BuiltinSession:
        session_id = normalize_id(agent_session_ref_id)
        return BuiltinSession(session_id, self._events.get(session_id, ()))

    def append(
        self,
        agent_session_ref_id: ID,
        expected_sequence: int,
        events: tuple[BuiltinSessionEvent, ...],
    ) -> None:
        session_id = normalize_id(agent_session_ref_id)
        current = self._events.get(session_id, ())
        if len(current) != expected_sequence:
            raise RuntimeError("Builtin Session optimistic sequence mismatch")
        self._events[session_id] = current + tuple(events)
