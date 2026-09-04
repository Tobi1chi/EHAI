"""Bounded SKILL.md loading that never grants Agent Tool permissions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ehai import JsonValue
from ehai.application.builtin_agent import (
    CancellationToken,
    RecoverableToolError,
    ToolDefinition,
    ToolHandler,
)

_MAX_SKILL_BYTES = 256 * 1024
_MAX_RESOURCE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    name: str
    instructions: str
    required_tools: tuple[str, ...]
    resources: Mapping[str, str]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "instructions": self.instructions,
            "required_tools": list(self.required_tools),
            "resources": dict(self.resources),
        }


class SkillLoader:
    """Load only configured Skill directories and explicitly named resources."""

    def __init__(self, skills: Mapping[str, Path]) -> None:
        if not skills:
            raise ValueError("SkillLoader requires at least one configured Skill")
        self._skills = {name: path.resolve(strict=True) for name, path in skills.items()}
        if any(not name.strip() or not path.is_dir() for name, path in self._skills.items()):
            raise ValueError("configured Skills require names and directories")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))

    def load(self, name: str, authorized_tools: frozenset[str]) -> LoadedSkill:
        root = self._skills.get(name)
        if root is None:
            raise RecoverableToolError("skill_not_found", "Skill is not configured")
        try:
            path = (root / "SKILL.md").resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise RecoverableToolError("invalid_skill", "SKILL.md escapes its configured root")
            if path.stat().st_size > _MAX_SKILL_BYTES:
                raise RecoverableToolError("output_limit", "SKILL.md exceeds the size limit")
            text = path.read_text(encoding="utf-8")
        except RecoverableToolError:
            raise
        except (OSError, UnicodeDecodeError) as error:
            raise RecoverableToolError("skill_read_failed", "SKILL.md could not be read") from error
        metadata, instructions = _frontmatter(text)
        required_tools = _string_list(metadata.get("required_tools"), "required_tools")
        missing = sorted(set(required_tools) - authorized_tools)
        if missing:
            raise RecoverableToolError(
                "skill_permission_denied",
                f"Skill requires unauthorized tools: {', '.join(missing)}",
            )
        resource_names = _string_list(metadata.get("resources"), "resources")
        resources: dict[str, str] = {}
        for resource_name in resource_names:
            resource = (root / resource_name).resolve(strict=True)
            if not resource.is_relative_to(root) or not resource.is_file():
                raise RecoverableToolError("invalid_resource", "Skill resource escapes its root")
            try:
                if resource.stat().st_size > _MAX_RESOURCE_BYTES:
                    raise RecoverableToolError("output_limit", "Skill resource exceeds size limit")
                resources[resource_name] = resource.read_text(encoding="utf-8")
            except RecoverableToolError:
                raise
            except (OSError, UnicodeDecodeError) as error:
                raise RecoverableToolError(
                    "resource_read_failed", "Skill resource could not be read"
                ) from error
        return LoadedSkill(name, instructions, required_tools, resources)


class SkillToolProvider:
    def __init__(self, loader: SkillLoader, *, authorized_tools: frozenset[str]) -> None:
        self._loader = loader
        self._authorized_tools = frozenset(authorized_tools)
        self.definitions = (
            ToolDefinition(
                "skill_load",
                "Load one configured Skill's instructions and explicit resources",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": list(loader.names)}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
        )
        self.handlers: Mapping[str, ToolHandler] = {"skill_load": self._load}

    async def _load(
        self, arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        name = arguments.get("name")
        if not isinstance(name, str):
            raise RecoverableToolError("invalid_arguments", "Skill name must be text")
        return self._loader.load(name, self._authorized_tools).to_json()


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RecoverableToolError("invalid_skill", "SKILL.md frontmatter is not closed")
    metadata: dict[str, object] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                metadata[key.strip()] = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise RecoverableToolError(
                    "invalid_skill", "SKILL.md list metadata is invalid"
                ) from error
        else:
            metadata[key.strip()] = stripped
    return metadata, text[end + 5 :].strip()


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RecoverableToolError("invalid_skill", f"{name} must be a string array")
    return tuple(value)
