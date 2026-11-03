"""Harness settings as the core carries them: checked by the Hub, held as plain values.

The core never reads a harness's native configuration itself. The Hub's compatibility layer
validates the settings on the machine that will run the harness and returns the canonical
settings, their fingerprint and the capability manifest the core matches roles against.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ehai import JsonValue, json_loads
from ehai.hub.client import HubClient, default_hub
from ehai.hub.protocol import InspectHarness, ResolvedHarness, ResolveHarness


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    kind: str
    version: str
    settings: dict[str, JsonValue] = field(hash=False)
    fingerprint: str
    environment_names: tuple[str, ...]
    protected_paths: tuple[Path, ...]
    capabilities: dict[str, JsonValue] = field(hash=False, compare=False)

    @classmethod
    def from_resolved(cls, resolved: ResolvedHarness) -> HarnessConfig:
        return cls(
            kind=resolved.kind,
            version=resolved.version,
            settings=dict(resolved.settings),
            fingerprint=resolved.fingerprint,
            environment_names=tuple(resolved.environment_names),
            protected_paths=tuple(Path(path) for path in resolved.protected_paths),
            capabilities=dict(resolved.capabilities),
        )

    def to_document(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "version": self.version, "settings": dict(self.settings)}

    def supports(self, capability: str) -> bool:
        return self.capabilities.get(capability) is True


async def resolve_harness_async(
    kind: str,
    settings: Mapping[str, JsonValue],
    *,
    version: str | None = None,
    pin_directory: Path | None = None,
    hub: HubClient | None = None,
) -> HarnessConfig:
    """Validate settings through the Hub; ``pin_directory`` copies native files there first."""
    async with (hub or default_hub()).connect() as connection:
        resolved = await connection.resolve(
            ResolveHarness(
                kind=kind,
                settings=dict(settings),
                pin_directory=None if pin_directory is None else str(pin_directory),
            )
        )
    if resolved.kind != kind:
        raise ValueError("Hub resolved a different harness kind")
    if version is not None and resolved.version != version:
        raise ValueError(
            f"Harness {kind} {version} is not installed in the Hub (it has {resolved.version})"
        )
    return HarnessConfig.from_resolved(resolved)


def resolve_harness(
    kind: str,
    settings: Mapping[str, JsonValue],
    *,
    version: str | None = None,
    pin_directory: Path | None = None,
    hub: HubClient | None = None,
) -> HarnessConfig:
    return _blocking(
        lambda: resolve_harness_async(
            kind, settings, version=version, pin_directory=pin_directory, hub=hub
        )
    )


def load_harness_file(path: Path) -> HarnessConfig:
    """Resolve a private settings file.

    The file is either a harness document ``{kind, version, settings}`` (``version``
    optional) or, as written by ``ehai init``, the settings of a Pi harness alone.
    """
    document = json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Harness settings file must contain an object")
    if "kind" not in document:
        return resolve_harness("pi", document)
    kind, version, settings = document["kind"], document.get("version"), document.get("settings")
    if (
        set(document) - {"kind", "version", "settings"}
        or not isinstance(kind, str)
        or not isinstance(settings, dict)
        or (version is not None and not isinstance(version, str))
    ):
        raise ValueError("A harness document takes kind, settings and optionally version")
    return resolve_harness(kind, settings, version=version)


def inspect_harness(
    kind: str, settings: Mapping[str, JsonValue], *, hub: HubClient | None = None
) -> dict[str, JsonValue]:
    """Check an installation without credentials or model requests."""

    async def inspect() -> dict[str, JsonValue]:
        async with (hub or default_hub()).connect() as connection:
            report = await connection.inspect_harness(
                InspectHarness(kind=kind, settings=dict(settings))
            )
        return report.report

    return _blocking(inspect)


def _blocking[Result](factory: Callable[[], Awaitable[Result]]) -> Result:
    async def run() -> Result:
        return await factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    # Called from synchronous code inside an event loop (host assembly in a child process).
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run()).result()
