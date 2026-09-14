"""Explicit, fingerprinted native Pi configuration; credentials stay in the environment."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ehai import JsonValue, json_dumps, json_loads

PI_VERSION = "0.85.1"


@dataclass(frozen=True, slots=True)
class PiBackendConfig:
    node: Path
    cli: Path
    agent_dir: Path
    provider: str
    environment_names: tuple[str, ...]
    configuration_hash: str

    @classmethod
    def from_path(cls, path: Path) -> PiBackendConfig:
        value = json_loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Pi backend configuration must be an object")
        return cls.from_document(value)

    @classmethod
    def from_document(cls, value: Mapping[str, JsonValue]) -> PiBackendConfig:
        required = {"node", "cli", "agent_dir", "provider", "environment_names"}
        if set(value) - required - {"configuration_hash"} or not required.issubset(value):
            raise ValueError("Pi config requires node, cli, agent_dir, provider, environment_names")
        paths: list[Path] = []
        for name in ("node", "cli", "agent_dir"):
            entry = value[name]
            if not isinstance(entry, str) or not Path(entry).is_absolute():
                raise ValueError(f"Pi {name} must be an absolute path")
            paths.append(Path(entry).resolve(strict=True))
        provider, names = value["provider"], value["environment_names"]
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("Pi provider must not be blank")
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            for name in names
        ):
            raise ValueError("Pi environment_names must be uppercase environment variable names")
        blocked = {"PATH", "HOME", "USERPROFILE", "NODE_OPTIONS", "NODE_PATH", "COMSPEC"}
        if any(name in blocked or str(name).startswith("PI_") for name in names):
            raise ValueError("Pi environment_names cannot replace process controls")
        if len(set(cast(list[str], names))) != len(names):
            raise ValueError("Pi environment_names must be unique")
        node, cli, agent_dir = paths
        if not node.is_file() or not cli.is_file() or not agent_dir.is_dir():
            raise ValueError("Pi executable/configuration paths have the wrong kind")
        validate_pi_cli(cli)
        if node.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError("Pi node must be a native executable, not a shell script")
        native = _native_documents(agent_dir)
        _validate_native(native, tuple(cast(list[str], names)))
        fingerprint = hashlib.sha256(json_dumps(native).encode("utf-8")).hexdigest()
        expected = value.get("configuration_hash")
        if expected is None:
            expected = fingerprint
        if expected != fingerprint:
            raise ValueError("Native Pi settings/models changed since execution authorization")
        return cls(node, cli, agent_dir, provider, tuple(cast(list[str], names)), fingerprint)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "node": str(self.node),
            "cli": str(self.cli),
            "agent_dir": str(self.agent_dir),
            "provider": self.provider,
            "environment_names": list(self.environment_names),
            "configuration_hash": self.configuration_hash,
        }

    def native_documents(self) -> dict[str, JsonValue]:
        documents = _native_documents(self.agent_dir)
        if (
            hashlib.sha256(json_dumps(documents).encode("utf-8")).hexdigest()
            != self.configuration_hash
        ):
            raise ValueError(
                "Native Pi settings/models no longer match the authorized configuration"
            )
        _validate_native(documents, self.environment_names)
        return documents

    def environment(self, isolated_home: Path) -> dict[str, str]:
        environment = isolated_pi_environment(isolated_home, self.node)
        for name in self.environment_names:
            value = os.environ.get(name)
            if value is None or not value:
                raise ValueError(f"Pi requires configured environment variable {name}")
            environment[name] = value
        return environment


def _native_documents(directory: Path) -> dict[str, JsonValue]:
    documents: dict[str, JsonValue] = {}
    for name in ("settings.json", "models.json"):
        path = directory / name
        if not path.is_file():
            raise ValueError(f"Pi agent_dir requires explicit {name}")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError(f"Pi {name} exceeds 1 MiB")
        document = json_loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"Pi {name} must contain an object")
        documents[name] = document
    return documents


def _validate_native(documents: Mapping[str, JsonValue], names: tuple[str, ...]) -> None:
    settings = documents["settings.json"]
    assert isinstance(settings, dict)
    # Only host-installed business extensions are loaded. Native settings cannot
    # install executable packages or resolve credentials through shell commands.
    if any(settings.get(key) for key in ("packages", "extensions", "skills", "prompts", "themes")):
        raise ValueError("Pi resource discovery is not authorized; use EHAI role tool profiles")
    retry = settings.get("retry")
    if not isinstance(retry, dict) or retry.get("enabled") is not False:
        raise ValueError("Initial Pi integration requires retry.enabled=false")
    provider_retry = retry.get("provider", {})
    if not isinstance(provider_retry, dict) or provider_retry.get("maxRetries", 0) != 0:
        raise ValueError("Initial Pi integration requires retry.provider.maxRetries=0")

    def walk(value: JsonValue) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"apikey", "authorization", "password", "secret"} and (
                    not isinstance(child, str) or child not in {f"${name}" for name in names}
                ):
                    raise ValueError(
                        "Pi credentials must reference an allowed environment variable"
                    )
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and value.startswith("!"):
            raise ValueError("Shell-resolved values are not allowed in Pi backend configuration")

    for document in documents.values():
        walk(document)


def isolated_pi_environment(root: Path, node: Path) -> dict[str, str]:
    # Do not inherit provider keys, NODE_OPTIONS, proxy credentials, HOME settings,
    # or extension discovery. Everything writable belongs to this temporary probe.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}
    }
    environment.update(
        {
            "PATH": str(node.parent),
            "HOME": str(root),
            "USERPROFILE": str(root),
            "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "localappdata"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "TEMP": str(root),
            "TMP": str(root),
            "PI_CODING_AGENT_DIR": str(root / "pi"),
            "PI_CODING_AGENT_SESSION_DIR": str(root / "sessions"),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "NO_COLOR": "1",
        }
    )
    return environment


def validate_pi_cli(cli: Path) -> None:
    package_file = cli.parent.parent.parent / "package.json"
    if (
        not cli.is_file()
        or cli.parts[-3:] != ("dist", "bundle", "cli.js")
        or not package_file.is_file()
    ):
        raise ValueError("Pi cli must point to the installed Pi dist/bundle/cli.js")
    package = json_loads(package_file.read_text(encoding="utf-8"))
    if not isinstance(package, dict) or (
        package.get("name") != "@earendil-works/pi-coding-agent"
        or package.get("version") != PI_VERSION
    ):
        raise ValueError(f"This connector requires upstream Pi {PI_VERSION}")


async def check_node_version(node: Path, environment: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        str(node),
        "--version",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10.0):
            stdout, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    version = stdout.decode("ascii").strip()
    try:
        major, minor, patch = (int(part) for part in version.removeprefix("v").split("."))
    except ValueError as error:
        raise ValueError("Could not identify the Node version") from error
    if process.returncode != 0 or (major, minor, patch) < (22, 19, 0):
        raise ValueError(f"Pi {PI_VERSION} requires Node >=22.19.0; selected {version}")
    return version
