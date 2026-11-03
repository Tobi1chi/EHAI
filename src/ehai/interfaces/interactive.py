"""Interactive terminal session over the local engine's thread interface.

``ehai`` without arguments opens it. Each command calls the same manager and core HTTP
operations the web workbench uses; the session only remembers which thread is selected.
Business state stays in the core: every view is re-read before it is shown.
"""

from __future__ import annotations

import getpass
import shlex
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TextIO
from uuid import uuid4

import httpx

_PLANNING_TIMEOUT_SECONDS = 1800.0
_WATCH_INTERVAL_SECONDS = 3.0
_DIFF_LINES = 400


class SessionError(Exception):
    """A request failed; the message says whether its outcome is known."""


@dataclass
class _Thread:
    workspace_id: str
    goal_id: str
    objective: str


class Engine:
    """The local manager: its own API and each workspace core behind ``/workspaces/<id>``."""

    def __init__(self, base_url: str, client: httpx.Client) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    def request(self, method: str, path: str, body: object = None, *, timeout: float = 60.0) -> Any:
        try:
            response = self._client.request(method, self._base + path, json=body, timeout=timeout)
        except httpx.HTTPError as error:
            if method == "GET":
                raise SessionError(f"{path}: {error}") from error
            raise SessionError(
                "The request did not complete; its outcome is unknown. "
                "Use show to read the thread before repeating it."
            ) from error
        try:
            document = response.json()
        except ValueError:
            document = None
        if not response.is_success:
            detail = document.get("error") if isinstance(document, dict) else None
            if isinstance(detail, dict):
                raise SessionError(f"{detail.get('code')}: {detail.get('message')}")
            raise SessionError(f"HTTP {response.status_code}")
        return document.get("data") if isinstance(document, dict) else None

    def core(
        self, workspace_id: str, method: str, path: str, body: object = None, **kw: Any
    ) -> Any:
        return self.request(method, f"/workspaces/{workspace_id}/api/v1{path}", body, **kw)


def _key(kind: str) -> str:
    return f"terminal-{kind}-{uuid4()}"


def _short(text: object, limit: int = 72) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _clock(at: object) -> str:
    value = str(at or "")
    return value[5:16].replace("T", " ") if len(value) >= 16 else value


class Session:
    def __init__(self, engine: Engine, *, out: TextIO, read_line: Callable[[str], str]) -> None:
        self._engine = engine
        self._out = out
        self._read_line = read_line
        self._listed: list[_Thread] = []
        # The (kind, request_id) behind each number todo printed, so a number keeps naming
        # the item the user saw even after other items are resolved.
        self._numbered: list[tuple[str, str]] = []
        self._thread: _Thread | None = None
        self._actor = getpass.getuser() or "terminal"
        self._commands: dict[str, tuple[Callable[[list[str]], None], str]] = {
            "threads": (self._threads, "list threads in every workspace"),
            "open": (self._open, "open <number|goal-id>: select a thread and show it"),
            "new": (self._new, "start a thread: choose a workspace, give the goal"),
            "show": (self._show, "the selected thread's state and recent timeline"),
            "say": (self._say, "say <message>: continue the discussion with the Planner"),
            "plan": (self._plan, "the latest plan revision of the selected thread"),
            "approve": (self._approve, "approve the draft plan and start its Run"),
            "watch": (self._watch, "follow the Run until it needs you or ends (Ctrl-C stops)"),
            "todo": (self._todo, "items waiting for you in the selected thread"),
            "pass": (self._pass, "pass <number> [comment]: accept a human check"),
            "fail": (self._fail, "fail <number> [comment]: reject a human check"),
            "reply": (self._reply, "reply <number> <message>: answer an intervention"),
            "integrate": (self._integrate, "integrate the completed Run's results"),
            "diff": (self._diff, "the latest integration's diff"),
            "branch": (self._branch, "branch <name>: put the integration on a new branch"),
            "help": (self._help, "this list"),
        }

    def print(self, text: str = "") -> None:
        self._out.write(text + "\n")
        self._out.flush()

    def run(self) -> int:
        self.print("EHAI terminal session. Type help for commands, quit to leave.")
        while True:
            try:
                line = self._read_line(self._prompt())
            except EOFError:
                self.print()
                return 0
            except KeyboardInterrupt:
                self.print()
                continue
            try:
                words = shlex.split(line)
            except ValueError as error:
                self.print(f"Error: {error}")
                continue
            if not words:
                continue
            name, arguments = words[0].lower(), words[1:]
            if name in {"quit", "exit"}:
                return 0
            command = self._commands.get(name)
            if command is None:
                self.print(f"Unknown command {name!r}; type help.")
                continue
            try:
                command[0](arguments)
            except SessionError as error:
                self.print(f"Error: {error}")
            except KeyboardInterrupt:
                self.print("\nStopped waiting; anything already sent keeps running on the host.")

    def _prompt(self) -> str:
        if self._thread is None:
            return "ehai> "
        return f"ehai {self._thread.workspace_id}: {_short(self._thread.objective, 24)}> "

    def _help(self, arguments: list[str]) -> None:
        del arguments
        for name, (_, text) in self._commands.items():
            self.print(f"  {name:<10} {text}")
        self.print(f"  {'quit':<10} leave the session")

    def _selected(self) -> _Thread:
        if self._thread is None:
            raise SessionError("No thread selected; use threads and open <number>, or new")
        return self._thread

    def _summary(self) -> dict[str, Any]:
        thread = self._selected()
        summary: dict[str, Any] = self._engine.core(
            thread.workspace_id, "GET", f"/threads/{thread.goal_id}"
        )
        return summary

    # Threads -------------------------------------------------------------------------

    def _threads(self, arguments: list[str]) -> None:
        del arguments
        listing = self._engine.request("GET", "/api/v1/threads")
        self._listed = []
        for entry in listing["workspaces"]:
            workspace_id = entry["workspace"]["workspace_id"]
            if not entry["available"]:
                error = entry.get("error") or {}
                self.print(f"  {workspace_id}: unavailable ({error.get('message', 'no detail')})")
                continue
            for row in entry["threads"]["threads"]:
                self._listed.append(_Thread(workspace_id, row["goal_id"], row["objective"]))
                attention = f" !{row['attention_count']}" if row["attention_count"] else ""
                self.print(
                    f"  {len(self._listed):>2}  {row['phase']:<17}{attention:<4} "
                    f"{workspace_id}/{_short(row['project_name'], 20)}  "
                    f"{_short(row['objective'], 48)}"
                )
        if not self._listed:
            self.print("  No threads yet; use new to start one.")

    def _open(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise SessionError("Usage: open <number|goal-id>")
        if not self._listed:
            self._threads([])
        choice = arguments[0]
        if choice.isdigit() and 1 <= int(choice) <= len(self._listed):
            self._thread = self._listed[int(choice) - 1]
        else:
            matches = [t for t in self._listed if t.goal_id.startswith(choice.lower())]
            if len(matches) != 1:
                raise SessionError(f"No single thread matches {choice!r}; use threads")
            self._thread = matches[0]
        self._show([])

    def _show(self, arguments: list[str]) -> None:
        del arguments
        thread = self._selected()
        summary = self._summary()
        items = self._timeline(thread)
        self.print(f"{summary['objective']}")
        self.print(
            f"  {thread.workspace_id}/{summary['project_name']} · phase {summary['phase']}"
            + (f" · Run {summary['run_status']}" if summary["run_id"] else "")
            + f" · next: {_next_hint(summary['next_action']['kind'])}"
        )
        for item in items[-15:]:
            self.print(f"  {_clock(item['at'])}  {item['kind']:<22} {_short(item['summary'])}")

    def _timeline(self, thread: _Thread) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after: int | None = 0
        while after is not None:
            page = self._engine.core(
                thread.workspace_id,
                "GET",
                f"/threads/{thread.goal_id}/timeline?limit=1000&after={after}",
            )
            items.extend(page["items"])
            after = page["next_after"]
        return items

    def _new(self, arguments: list[str]) -> None:
        overview = self._engine.request("GET", "/api/v1/threads")
        available = [
            e["workspace"]["workspace_id"] for e in overview["workspaces"] if e["available"]
        ]
        if not available:
            raise SessionError("No running workspace; add one with ehai add <path>")
        workspace_id = arguments[0] if arguments else available[0]
        if workspace_id not in available:
            raise SessionError(f"Workspace {workspace_id!r} is not running: {available}")
        if not arguments and len(available) > 1:
            self.print("Workspaces: " + ", ".join(available))
            workspace_id = self._ask(f"Workspace [{workspace_id}]") or workspace_id
            if workspace_id not in available:
                raise SessionError(f"Workspace {workspace_id!r} is not running")
        objective = self._ask("Goal (a concrete, checkable result)")
        if not objective:
            raise SessionError("A thread needs a goal")
        message = self._ask("Notes for the Planner (optional)") or objective
        projects = self._engine.core(workspace_id, "GET", "/projects")["projects"]
        if projects:
            project_id = projects[0]["project_id"]
        else:
            project_id = self._engine.core(
                workspace_id,
                "POST",
                "/projects",
                {"idempotency_key": _key("project"), "name": workspace_id},
            )["project_id"]
        goal = self._engine.core(
            workspace_id,
            "POST",
            "/goals",
            {"idempotency_key": _key("goal"), "project_id": project_id, "objective": objective},
        )
        self._thread = _Thread(workspace_id, goal["goal_id"], objective)
        self.print(f"Thread {goal['goal_id']} created.")
        self._discuss(message)

    def _ask(self, label: str) -> str:
        try:
            return self._read_line(f"{label}: ").strip()
        except EOFError:
            return ""

    # Planning ------------------------------------------------------------------------

    def _say(self, arguments: list[str]) -> None:
        message = " ".join(arguments).strip()
        if not message:
            raise SessionError("Usage: say <message>")
        self._discuss(message)

    def _discuss(self, message: str) -> None:
        thread = self._selected()
        conversations = [
            item["refs"]["conversation_id"]
            for item in self._timeline(thread)
            if item["kind"] == "discussion_message" and item["refs"].get("conversation_id")
        ]
        self.print("Planner is working; this can take minutes (Ctrl-C stops waiting only).")
        conversation = self._engine.core(
            thread.workspace_id,
            "POST",
            "/planning/discuss",
            {
                "idempotency_key": _key("discuss"),
                "goal_id": thread.goal_id,
                "message": message,
                "conversation_id": conversations[-1] if conversations else None,
            },
            timeout=_PLANNING_TIMEOUT_SECONDS,
        )
        turn = conversation["turns"][-1]
        if turn.get("reply"):
            self.print(f"Planner: {turn['reply']}")
        if turn.get("error"):
            self.print(f"Planner error: {turn['error']}")
        if turn.get("plan_revision_id"):
            self.print("A draft plan is ready: plan shows it, approve starts it.")

    def _plan(self, arguments: list[str]) -> None:
        del arguments
        thread, summary = self._selected(), self._summary()
        if summary["plan_revision_id"] is None:
            raise SessionError("This thread has no plan yet; say what you need first")
        plan = self._engine.core(
            thread.workspace_id, "GET", f"/plans/{summary['plan_revision_id']}"
        )
        self.print(
            f"Plan version {plan['version']} ({plan['status']}), {len(plan['nodes'])} tasks:"
        )
        titles = {node["plan_node_id"]: node["title"] for node in plan["nodes"]}
        for node in plan["nodes"]:
            after = [titles.get(i, i) for i in node["required_dependency_ids"]]
            self.print(
                f"  - {node['title']} [{node['kind']}]"
                + (f" after {', '.join(after)}" if after else "")
            )
            self.print(f"      {_short(node['instruction'], 100)}")

    def _approve(self, arguments: list[str]) -> None:
        del arguments
        thread, summary = self._selected(), self._summary()
        if summary["plan_revision_id"] is None:
            raise SessionError("This thread has no plan to approve")
        plan = self._engine.core(
            thread.workspace_id, "GET", f"/plans/{summary['plan_revision_id']}"
        )
        config = self._engine.request(
            "GET", f"/api/v1/workspaces/{thread.workspace_id}/execution-config"
        )["execution_config"]
        if config is None:
            self.print("This workspace runs its own configured Worker (no model Worker).")
        else:
            commands = [" ".join(argv) for argv in config["allowed_commands"]] or ["none"]
            self.print(f"  workspace    {config['workspace']}")
            effort = f" · {config['reasoning_effort']}" if config["reasoning_effort"] else ""
            self.print(f"  model        {config['model']}{effort}")
            self.print(f"  capacity     {config['capacity']}")
            self.print(f"  commands     {'; '.join(commands)}")
            self.print(f"  git          {', '.join(config['git_permissions']) or 'none'}")
        verb = "Approve plan version {} and start".format(plan["version"])
        if plan["status"] != "draft":
            verb = f"Start plan version {plan['version']}"
        if self._ask(f"{verb} with this configuration? [y/N]").lower() not in {"y", "yes"}:
            self.print("Nothing changed.")
            return
        if plan["status"] == "draft":
            self._engine.core(
                thread.workspace_id,
                "POST",
                "/plans/approve",
                {
                    "idempotency_key": _key("approve"),
                    "plan_revision_id": plan["plan_revision_id"],
                    "completion_contract_id": plan["completion_contract_id"],
                },
            )
        body: dict[str, Any] = {
            "idempotency_key": _key("start"),
            "plan_revision_id": plan["plan_revision_id"],
        }
        if config is not None:
            body["execution_config"] = config
        run = self._engine.core(thread.workspace_id, "POST", "/runs/start", body)
        self.print(f"Run {run['run_id']} started; watch follows it.")

    # Execution -----------------------------------------------------------------------

    def _watch(self, arguments: list[str]) -> None:
        del arguments
        thread = self._selected()
        seen: dict[str, str] = {}
        while True:
            summary = self._summary()
            if summary["run_id"] is None:
                raise SessionError("This thread has no Run yet")
            plan = self._engine.core(thread.workspace_id, "GET", f"/runs/{summary['run_id']}/plan")
            for node in plan["nodes"]:
                if seen.get(node["plan_node_id"]) != node["status"]:
                    seen[node["plan_node_id"]] = node["status"]
                    self.print(f"  {node['status']:<12} {node['title']}")
            if summary["phase"] not in {"running"}:
                self.print(
                    f"Run {summary['run_status']} · phase {summary['phase']}"
                    f" · next: {_next_hint(summary['next_action']['kind'])}"
                )
                return
            time.sleep(_WATCH_INTERVAL_SECONDS)

    def _inbox(self) -> list[dict[str, Any]]:
        thread = self._selected()
        inbox = self._engine.core(thread.workspace_id, "GET", "/inbox")
        return [item for item in inbox["items"] if item["owner"]["goal_id"] == thread.goal_id]

    def _todo(self, arguments: list[str]) -> None:
        del arguments
        items = self._inbox()
        self._numbered = [(item["kind"], item["request_id"]) for item in items]
        if not items:
            self.print("  Nothing is waiting for you in this thread.")
        for number, item in enumerate(items, 1):
            node = f" · {item['owner']['node_title']}" if item["owner"]["node_title"] else ""
            self.print(f"  {number}. [{item['kind']}{node}] {item['question']}")
            if item["evidence_summary"]:
                self.print(f"     {_short(item['evidence_summary'], 100)}")
            if not item["actionable"]:
                self.print(f"     not actionable: {item['unavailable_reason']}")
                continue
            hints = {
                "human_check": "pass / fail <number> [comment]",
                "intervention": "reply <number> <message>",
            }
            self.print(
                "     "
                + hints.get(
                    item["kind"],
                    "handle in the web workbench or with the ehai CLI: "
                    + ", ".join(sorted({a["operation"] for a in item["actions"]})),
                )
            )

    def _item(self, arguments: list[str], kind: str) -> tuple[dict[str, Any], list[str]]:
        if not arguments or not arguments[0].isdigit():
            raise SessionError("Give the item number from todo")
        index = int(arguments[0])
        if not 1 <= index <= len(self._numbered):
            raise SessionError("No such item; run todo again")
        identity = self._numbered[index - 1]
        # Act on the item that number showed, with its current request token.
        item = next((i for i in self._inbox() if (i["kind"], i["request_id"]) == identity), None)
        if item is None:
            raise SessionError(f"Item {index} is no longer waiting; run todo again")
        if item["kind"] != kind or not item["actionable"]:
            raise SessionError(f"Item {index} is not an actionable {kind}")
        return item, arguments[1:]

    def _decide(self, arguments: list[str], passed: bool) -> None:
        item, rest = self._item(arguments, "human_check")
        action = next(
            (
                a
                for a in item["actions"]
                if a["arguments"].get("passed") is passed and not a["arguments"].get("hold")
            ),
            None,
        )
        if action is None:
            raise SessionError("The core does not offer this decision for the item")
        thread = self._selected()
        self._engine.core(
            thread.workspace_id,
            "POST",
            f"/check-runs/{action['arguments']['check_run_id']}/decision",
            {
                "idempotency_key": _key("check"),
                "request_token": action["arguments"]["request_token"],
                "passed": passed,
                "actor": self._actor,
                "comment": " ".join(rest) or action["label"],
            },
        )
        self.print("Decision recorded.")

    def _pass(self, arguments: list[str]) -> None:
        self._decide(arguments, True)

    def _fail(self, arguments: list[str]) -> None:
        self._decide(arguments, False)

    def _reply(self, arguments: list[str]) -> None:
        item, rest = self._item(arguments, "intervention")
        if not rest:
            raise SessionError("Usage: reply <number> <message>")
        action = next(a for a in item["actions"] if a["operation"] == "reply-intervention")
        thread = self._selected()
        self._engine.core(
            thread.workspace_id,
            "POST",
            f"/interventions/{action['arguments']['intervention_id']}/reply",
            {
                "idempotency_key": _key("reply"),
                "request_token": action["arguments"]["request_token"],
                "actor": self._actor,
                "message": " ".join(rest),
            },
        )
        self.print("Reply recorded.")

    # Results -------------------------------------------------------------------------

    def _run_id(self) -> str:
        summary = self._summary()
        if summary["run_id"] is None:
            raise SessionError("This thread has no Run yet")
        return str(summary["run_id"])

    def _integrate(self, arguments: list[str]) -> None:
        del arguments
        thread, run_id = self._selected(), self._run_id()
        plan = self._engine.core(thread.workspace_id, "GET", f"/runs/{run_id}/plan")
        integration = self._engine.core(
            thread.workspace_id,
            "POST",
            f"/runs/{run_id}/integrate",
            {"expected_process_revision_id": plan["process_revision_id"]},
        )
        self.print(f"Integration {integration['status']}: commit {integration['commit']}")
        for conflict in integration.get("conflicts") or []:
            self.print(f"  conflict: {conflict}")
        if integration["status"] == "integrated":
            self.print("diff shows the change; branch <name> puts it on a new branch.")

    def _latest_integration(self) -> tuple[str, dict[str, Any]]:
        thread, run_id = self._selected(), self._run_id()
        integrations = self._engine.core(thread.workspace_id, "GET", f"/runs/{run_id}/integrations")
        if not integrations:
            raise SessionError("No integration yet; run integrate first")
        return run_id, integrations[-1]

    def _diff(self, arguments: list[str]) -> None:
        del arguments
        run_id, integration = self._latest_integration()
        diff = self._engine.core(
            self._selected().workspace_id,
            "GET",
            f"/runs/{run_id}/integrations/{integration['integration_id']}/diff",
        )
        lines = diff["content"].splitlines()
        for line in lines[:_DIFF_LINES]:
            self.print(line)
        if len(lines) > _DIFF_LINES or diff["truncated"]:
            self.print(f"… diff shortened; read the full patch at {integration['diff_path']}")

    def _branch(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise SessionError("Usage: branch <name>")
        run_id, integration = self._latest_integration()
        branch = self._engine.core(
            self._selected().workspace_id,
            "POST",
            f"/runs/{run_id}/integrations/{integration['integration_id']}/branch",
            {"branch": arguments[0]},
        )
        state = "created" if branch["created"] else "already pointed there"
        self.print(f"Branch {branch['branch']} → {branch['commit']} ({state}); not checked out.")


_NEXT_HINTS = {
    "start_discussion": "say what you need",
    "continue_discussion": "say more to the Planner",
    "review_plan": "plan, then approve",
    "handle_inbox": "todo",
    "watch_run": "watch",
    "resume_run": "the Run is paused; resume it in the web workbench or with ehai resume-run",
    "review_result": "integrate, diff, branch <name>",
    "none": "nothing",
}


def _next_hint(kind: str) -> str:
    return _NEXT_HINTS.get(kind, kind)


def _lines(stream: TextIO) -> Iterator[str]:
    yield from (line.rstrip("\n") for line in stream)


def run_interactive(
    base_url: str, *, stdin: TextIO | None = None, out: TextIO | None = None
) -> int:
    """Run the session; a non-terminal stdin is read line by line without prompts."""
    source = stdin or sys.stdin
    output = out or sys.stdout
    if source.isatty():

        def read_line(prompt: str) -> str:
            return input(prompt)

    else:
        lines = _lines(source)

        def read_line(prompt: str) -> str:
            del prompt
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

    with httpx.Client(trust_env=False) as client:
        return Session(Engine(base_url, client), out=output, read_line=read_line).run()
