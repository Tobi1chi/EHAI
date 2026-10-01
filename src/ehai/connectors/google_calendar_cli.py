"""Standalone Google Calendar process: the core is accessed exclusively over HTTP."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ehai.connectors.bridge import _api_base, _initialize, _lock, _read, _run_once, _write
from ehai.connectors.google_calendar import GoogleCalendarAdapter
from ehai.connectors.google_calendar_contract import google_calendar_manifest

_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
_GOOGLE_API = "https://www.googleapis.com/calendar/v3/"


def _authorize(directory: Path, client_config: Path) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

    config = _read(client_config)
    installed = config.get("installed", {})
    if (
        installed.get("token_uri") != "https://oauth2.googleapis.com/token"
        or urlsplit(installed.get("auth_uri", "")).hostname != "accounts.google.com"
    ):
        raise ValueError("Use a Google OAuth Desktop client configuration")
    flow = InstalledAppFlow.from_client_config(config, _SCOPES, autogenerate_code_verifier=True)
    credentials = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=300)
    _write(directory / "google-token.json", json.loads(credentials.to_json()))
    print("Google Calendar authorization saved in the connector state directory")


def _token_provider(directory: Path) -> Callable[[], str]:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = directory / "google-token.json"
    document = _read(token_path)
    if document.get("token_uri") != "https://oauth2.googleapis.com/token":
        raise ValueError("Invalid Google OAuth token endpoint")
    credentials = Credentials.from_authorized_user_info(document, scopes=_SCOPES)  # type: ignore[no-untyped-call]

    def token() -> str:
        if not credentials.valid:
            credentials.refresh(Request())
            _write(token_path, json.loads(credentials.to_json()))
        return str(credentials.token)

    return token


def _run(directory: Path, *, once: bool, test_provider_url: str | None) -> None:
    config = _read(directory / "bridge.json")
    base = _api_base(config["api_url"])
    bridge_token = _read(directory / "bridge-token.json")["token"]
    if test_provider_url is not None:
        parsed = urlsplit(test_provider_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Protocol trials require an explicit 127.0.0.1 HTTP provider")
        provider_url = test_provider_url.rstrip("/") + "/"
        token = lambda: "local-protocol-trial"  # noqa: E731
    else:
        provider_url = _GOOGLE_API
        token = _token_provider(directory)
    with (
        httpx.Client(
            base_url=base,
            headers={"X-EHAI-Connector-Token": bridge_token},
            timeout=30,
            trust_env=False,
        ) as core,
        httpx.Client(
            base_url=provider_url, timeout=30, trust_env=test_provider_url is None
        ) as provider,
    ):
        adapter = GoogleCalendarAdapter(provider, token)
        while True:
            _run_once(directory, core, adapter, config)
            if once:
                return
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent Google Calendar connector")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="register with the core and create private connector state"
    )
    init.add_argument("--api-url", required=True)
    init.add_argument("--project-id", required=True)
    init.add_argument("--calendar-id", action="append", default=None)
    auth = commands.add_parser("authorize", help="open Google OAuth login for this connector")
    auth.add_argument("--client-config", type=Path, required=True)
    run = commands.add_parser("run", help="claim actions and deliver durable results/events")
    run.add_argument("--once", action="store_true")
    run.add_argument(
        "--test-provider-url", help="loopback-only protocol trial; never loads Google credentials"
    )
    for command in (init, auth, run):
        command.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with _lock(args.state_dir):
            if args.command == "init":
                _initialize(
                    args.state_dir,
                    args.api_url,
                    args.project_id,
                    google_calendar_manifest(),
                    {"allowed_calendar_ids": args.calendar_id or ["primary"]},
                    "Google Calendar",
                )
            elif args.command == "authorize":
                _authorize(args.state_dir, args.client_config)
            else:
                _run(args.state_dir, once=args.once, test_provider_url=args.test_provider_url)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # OAuth/library exceptions may contain secret request parameters.
        print(
            f"Connector stopped ({type(error).__name__}); state retained for retry. "
            "Check configuration, authorization and service availability.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
