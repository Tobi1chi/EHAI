"""Any Codex CLI speaking experimental app-server; measured CLI versions are recorded only.

Only Codex reads its private credential store. This layer uses account/read and keeps
its own state-key/thread mapping separately from Codex's native history.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from ehai import JsonValue, json_dumps, json_loads
from ehai.hub.adapters import HarnessFailure, HarnessRequestError, McpEndpoint
from ehai.hub.adapters.codex.process import TIMEOUT, AppServer
from ehai.hub.adapters.codex.session import CodexSession
from ehai.hub.protocol import ResolvedHarness, StartSession

KIND = "codex"
VERSION = "app-server"
_TOKEN_ENV = "EHAI_CODEX_MCP_TOKEN"
_SETTINGS = {"executable", "codex_home", "environment_names", "cli_version"}
_BLOCKED_ENV = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TMP",
    "TEMP",
    "TMPDIR",
    "SHELL",
    "ENV",
    "BASH_ENV",
    "ZDOTDIR",
    "CDPATH",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "RUSTUP_HOME",
    "CARGO_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
}
_BLOCKED_PREFIXES = (
    "CODEX_",
    "OPENAI_",
    "EHAI_",
    "LD_",
    "DYLD_",
    "GIT_CONFIG_",
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCLOUD_",
)
# `codex features list` at implementation time, default-on features that add tools,
# MCP servers, agents or hooks beyond the native shell and file edits the core approves.
# A ChatGPT login otherwise loads the `codex_apps` connector server (mail, GitHub,
# Drive and more) and plugin servers; the launch check still requires `ehai` alone.
# `code_mode_host` stays on: this version exposes every tool, MCP included, to the model
# only through its `exec` tool, so disabling it leaves the model with no usable tools.
_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "plugin_discovery_prefetch",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "computer_use",
    "multi_agent",
    "image_generation",
    "hooks",
    "goals",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_suggest",
    "workspace_dependencies",
    "in_app_local_automation",
)
_CAPABILITIES: dict[str, JsonValue] = {
    "session.resume": True,
    "session.fork": False,
    "requests.interactive": False,
    "tools.native.restrictable": True,
    "tools.native.kept": True,
    "tools.native.approval": True,
    "tools.mcp": True,
    "usage.reported": True,
    "cancel.confirmed": False,
    "schema.unique_items": False,
}


def _environment(executable: Path, home: Path, names: list[str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"}
    }
    # Native commands run with this PATH, so approved commands such as `uv run pytest`
    # resolve as they do for HostTools; each command still needs the core's approval.
    system = (
        [os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")]
        if sys.platform == "win32"
        else ["/usr/bin", "/bin"]
    )
    inherited = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    environment.update(
        {
            "PATH": os.pathsep.join(dict.fromkeys([str(executable.parent), *inherited, *system])),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CODEX_HOME": str(home),
        }
    )
    for name in names:
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"Codex requires configured environment variable {name}")
        environment[name] = value
    return environment


def _version(executable: Path) -> str:
    try:
        with TemporaryDirectory(prefix="ehai-codex-version-") as directory:
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                timeout=TIMEOUT,
                env=_environment(executable, Path(directory), []),
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Codex did not report its version") from error
    match = re.fullmatch(rb"codex-cli ([^\s]+)", result.stdout.strip())
    if result.returncode != 0 or match is None:
        raise ValueError("Codex did not report its version")
    return match[1].decode("utf-8")


def _settings(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if set(value) - _SETTINGS or not (_SETTINGS - {"cli_version"}).issubset(value):
        raise ValueError(
            "Codex settings take executable, codex_home, environment_names and optional cli_version"
        )
    paths = []
    for name in ("executable", "codex_home"):
        entry = value[name]
        if not isinstance(entry, str) or not Path(entry).is_absolute():
            raise ValueError(f"Codex {name} must be an absolute path")
        paths.append(Path(entry).resolve(strict=True))
    executable, home = paths
    if not executable.is_file() or not home.is_dir():
        raise ValueError("Codex executable/codex_home paths have the wrong kind")
    if executable.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("Codex executable must be the native binary, not a shell shim")
    if home.is_relative_to((Path.home() / ".codex").resolve()):
        raise ValueError("Codex codex_home must be private to EHAI, outside the user's ~/.codex")
    names = value["environment_names"]
    if not isinstance(names, list) or any(
        not isinstance(n, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", n) for n in names
    ):
        raise ValueError("Codex environment_names must be uppercase variable names")
    environment_names = cast(list[str], names)
    if len(set(environment_names)) != len(names):
        raise ValueError("Codex environment_names must be unique")
    if any(n in _BLOCKED_ENV or n.startswith(_BLOCKED_PREFIXES) for n in environment_names):
        raise ValueError(
            "Codex environment_names cannot replace process controls or credential sources"
        )
    version = _version(executable)
    return {
        "executable": str(executable),
        "codex_home": str(home),
        "environment_names": names,
        "cli_version": version,
    }


def _command(executable: Path, overrides: dict[str, JsonValue]) -> list[str]:
    # -c values use TOML; these scalar JSON values also have valid TOML spelling.
    command = [str(executable), "app-server"]
    for key, value in overrides.items():
        command.extend(["-c", key + "=" + json_dumps(value)])
    return command


def _login_command(settings: dict[str, JsonValue]) -> str:
    return (
        f"CODEX_HOME={shlex.quote(str(settings['codex_home']))} "
        f"{shlex.quote(str(settings['executable']))} "
        "-c 'cli_auth_credentials_store=\"file\"' login"
    )


def _process(
    settings: dict[str, JsonValue], cwd: Path, mcp: McpEndpoint | None = None
) -> AppServer:
    executable, home = Path(str(settings["executable"])), Path(str(settings["codex_home"]))
    environment = _environment(executable, home, cast(list[str], settings["environment_names"]))
    overrides: dict[str, JsonValue] = {
        "cli_auth_credentials_store": "file",
        "forced_login_method": "chatgpt",
        "model_provider": "openai",
        "web_search": "disabled",
        "sandbox_workspace_write.network_access": False,
        "sandbox_workspace_write.writable_roots": [],
        "sandbox_workspace_write.exclude_slash_tmp": True,
        "sandbox_workspace_write.exclude_tmpdir_env_var": True,
        "sandbox_read_only.network_access": False,
        **{f"features.{name}": False for name in _DISABLED_FEATURES},
    }
    if mcp is not None:
        environment[_TOKEN_ENV] = mcp.token
        overrides.update(
            {"mcp_servers.ehai.url": mcp.url, "mcp_servers.ehai.bearer_token_env_var": _TOKEN_ENV}
        )
    return AppServer(_command(executable, overrides), cwd, environment)


async def _account(process: AppServer) -> bool:
    # v2/GetAccount{Params,Response}.json. Return only login presence, no account PII.
    result = await process.call("account/read", {"refreshToken": False})
    account = result.get("account")
    return isinstance(account, dict) and account.get("type") == "chatgpt"


class CodexAdapter:
    kind = KIND
    version = VERSION

    @property
    def capabilities(self) -> dict[str, JsonValue]:
        return dict(_CAPABILITIES)

    def resolve(self, settings: dict[str, JsonValue], pin_directory: str | None) -> ResolvedHarness:
        # Native history and credentials remain owned by Codex, never copied or read.
        del pin_directory
        try:
            canonical = _settings(settings)
        except (OSError, ValueError) as error:
            raise HarnessRequestError(str(error)) from error
        executable = Path(str(canonical["executable"]))
        apps = [parent for parent in executable.parents if parent.suffix.lower() == ".app"]
        # CLI auto-updates must not change workspace authorization or session fingerprints.
        fingerprint_settings = {
            key: value for key, value in canonical.items() if key != "cli_version"
        }
        return ResolvedHarness(
            kind=self.kind,
            version=self.version,
            settings=canonical,
            fingerprint=hashlib.sha256(
                json_dumps(fingerprint_settings).encode("utf-8")
            ).hexdigest(),
            environment_names=cast(list[str], canonical["environment_names"]),
            protected_paths=[str(apps[-1] if apps else executable), str(canonical["codex_home"])],
            capabilities=self.capabilities,
        )

    async def inspect(self, settings: dict[str, JsonValue]) -> dict[str, JsonValue]:
        canonical = (await asyncio.to_thread(self.resolve, settings, None)).settings
        try:
            process = _process(canonical, Path(str(canonical["codex_home"])))
        except ValueError as error:
            raise HarnessRequestError(str(error)) from error
        await process.start()
        try:
            await process.initialize()
            logged_in = await _account(process)
        finally:
            await process.close()
        return {
            "backend": KIND,
            "version": canonical["cli_version"],
            "executable": canonical["executable"],
            "codex_home": canonical["codex_home"],
            "logged_in": logged_in,
            "login_command": None if logged_in else _login_command(canonical),
            "protocol": "app-server",
            "control_channel_verified": True,
            "model_execution_verified": False,
            "model_requests": 0,
        }

    async def launch(self, request: StartSession, mcp: McpEndpoint) -> CodexSession:
        try:
            canonical = await asyncio.to_thread(_settings, request.harness.settings)
            if request.native_policy is None:
                raise ValueError("Codex requires native_policy; no sandbox may be inferred")
            if request.reasoning_effort is not None and not request.reasoning_effort.strip():
                raise ValueError("Codex reasoning_effort must be a nonempty advertised effort")
            workspace = Path(request.workspace)
            if not workspace.is_absolute() or not workspace.is_dir():
                raise ValueError("Codex workspace must be an existing absolute directory")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", request.state_key) or request.state_key in {
                ".",
                "..",
            }:
                raise ValueError("Codex state_key must be a plain directory name")
            state = Path(str(canonical["codex_home"])) / "ehai-adapter" / request.state_key
            reference = state / "thread.json"
            if request.fresh and reference.exists():
                raise ValueError("Untracked Codex history cannot become a fresh EHAI session")
            thread_id = None
            if not request.fresh:
                if not reference.is_file():
                    raise ValueError("Codex thread mapping is missing; refusing a fresh thread")
                saved = json_loads(reference.read_text(encoding="utf-8"))
                if (
                    not isinstance(saved, dict)
                    or saved.get("native_session_id") != request.native_session_id
                ):
                    raise ValueError("Codex native session identity differs from its mapping")
                thread_id = saved.get("thread_id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise ValueError("Codex thread mapping has no thread_id")
            process = _process(canonical, workspace, mcp)
        except (OSError, ValueError) as error:
            raise HarnessRequestError(str(error)) from error
        await process.start()
        try:
            await process.initialize()
            if not await _account(process):
                raise HarnessRequestError(
                    "Codex private CODEX_HOME is not logged in to ChatGPT; run "
                    + _login_command(canonical)
                )
            await self._effort(process, request.model, request.reasoning_effort)
            # v2/Thread{Start,Resume}{Params,Response}.json. baseInstructions replaces
            # Codex's default coding instructions with the EHAI role, rather than
            # adding a second role via developerInstructions. Repeat on resume.
            params: dict[str, JsonValue] = {
                "cwd": str(workspace),
                "sandbox": request.native_policy.sandbox,
                "approvalPolicy": "untrusted",
                "approvalsReviewer": "user",
                "baseInstructions": request.system_prompt,
                "model": request.model,
                "modelProvider": "openai",
            }
            if request.reasoning_effort is not None:
                params["config"] = {"model_reasoning_effort": request.reasoning_effort}
            if thread_id is None:
                params["allowProviderModelFallback"] = False
            else:
                params["threadId"] = thread_id
            result = await process.call(
                "thread/start" if thread_id is None else "thread/resume", params
            )
            thread = result.get("thread")
            actual_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(actual_id, str) or (thread_id is not None and actual_id != thread_id):
                raise HarnessFailure("Codex omitted or changed thread identity")
            self._verify(result, request)
            await self._await_tools(process, actual_id, {tool.name for tool in request.tools})
            state.mkdir(parents=True, exist_ok=True)
            # Only this Hub-side reference is read/written; never Codex's auth/history.
            reference.write_text(
                json_dumps(
                    {"native_session_id": request.native_session_id, "thread_id": actual_id}
                ),
                encoding="utf-8",
            )
        except BaseException as error:
            await process.close(abort=True)
            if isinstance(error, OSError):
                raise HarnessFailure(
                    "Could not persist Codex thread reference; outcome unknown"
                ) from error
            raise
        return CodexSession(
            process, actual_id, {tool.name for tool in request.tools}, request.reasoning_effort
        )

    @staticmethod
    def _verify(result: dict[str, JsonValue], request: StartSession) -> None:
        assert request.native_policy is not None
        sandbox = result.get("sandbox")
        expected = "readOnly" if request.native_policy.sandbox == "read-only" else "workspaceWrite"
        if (
            result.get("model") != request.model
            or result.get("approvalPolicy") != "untrusted"
            or result.get("approvalsReviewer") != "user"
            or result.get("cwd") != request.workspace
            or not isinstance(sandbox, dict)
            or sandbox.get("type") != expected
            or sandbox.get("networkAccess", False) is not False
        ):
            raise HarnessFailure(
                "Codex did not apply the requested model, cwd, sandbox or approvals"
            )
        if expected == "workspaceWrite" and (
            sandbox.get("writableRoots") != []
            or sandbox.get("excludeSlashTmp") is not True
            or sandbox.get("excludeTmpdirEnvVar") is not True
        ):
            raise HarnessFailure("Codex exposed writable roots outside the workspace")
        if (
            request.reasoning_effort is not None
            and result.get("reasoningEffort") != request.reasoning_effort
        ):
            raise HarnessRequestError("Codex did not apply the requested reasoning_effort")

    @staticmethod
    async def _effort(process: AppServer, model: str, effort: str | None) -> None:
        if effort is None:
            return
        # v2/ModelList{Params,Response}.json / ReasoningEffortOption. The effort
        # schema is open-ended; verify identity mapping against this model's catalog.
        cursor: JsonValue = None
        while True:
            result = await process.call("model/list", {"includeHidden": True, "cursor": cursor})
            data = result.get("data")
            for entry in data if isinstance(data, list) else []:
                if isinstance(entry, dict) and model in (entry.get("id"), entry.get("model")):
                    options = entry.get("supportedReasoningEfforts")
                    supported = (
                        {item.get("reasoningEffort") for item in options if isinstance(item, dict)}
                        if isinstance(options, list)
                        else set()
                    )
                    if effort not in supported:
                        raise HarnessRequestError(
                            f"Codex model {model} does not advertise effort {effort}"
                        )
                    return
            cursor = result.get("nextCursor")
            if cursor is None:
                raise HarnessRequestError(f"Codex cannot verify reasoning_effort for model {model}")

    @staticmethod
    async def _await_tools(process: AppServer, thread_id: str, expected: set[str]) -> None:
        # v2/ListMcpServerStatus{Params,Response}.json; inspect the whole inventory,
        # not a serverName-filtered subset that could hide extra configured servers.
        try:
            async with asyncio.timeout(TIMEOUT):
                while True:
                    servers: list[JsonValue] = []
                    cursor: JsonValue = None
                    while True:
                        result = await process.call(
                            "mcpServerStatus/list",
                            {"threadId": thread_id, "detail": "full", "cursor": cursor},
                        )
                        data = result.get("data")
                        if not isinstance(data, list):
                            raise HarnessFailure("Codex MCP inventory is missing")
                        servers.extend(data)
                        cursor = result.get("nextCursor")
                        if cursor is None:
                            break
                    if (
                        len(servers) != 1
                        or not isinstance(servers[0], dict)
                        or servers[0].get("name") != "ehai"
                    ):
                        raise HarnessFailure("Codex loaded MCP servers outside this session")
                    server = servers[0]
                    if server.get("runtimeStatus") == "connected":
                        tools = server.get("tools")
                        names = (
                            {item.get("name") for item in tools.values() if isinstance(item, dict)}
                            if isinstance(tools, dict)
                            else set()
                        )
                        if names != expected or server.get("toolsError") is not None:
                            raise HarnessFailure(
                                "Codex did not list exactly the session's MCP tools"
                            )
                        return
                    if server.get("runtimeStatus") not in ("notStarted", "starting"):
                        raise HarnessFailure("Codex could not connect to the session's MCP tools")
                    await asyncio.sleep(0.2)
        except TimeoutError as error:
            raise HarnessFailure("Codex timed out connecting to the session's MCP tools") from error
