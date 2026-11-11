"""Independent rail connector CLI using the durable HTTP bridge."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import httpx

from ehai.connectors.bridge import _api_base, _initialize, _lock, _read, _run_once
from ehai.connectors.rail import CatbusClient, CatbusError, RailConnectorAdapter, rail_manifest


def _catbus_path(value: str | None) -> str:
    candidate = value if value is not None else shutil.which("catbus")
    if not candidate or not Path(candidate).is_file():
        raise CatbusError("catbus was not found; install it or pass --catbus PATH")
    return str(Path(candidate).resolve())


def _run(directory: Path, *, once: bool) -> None:
    config = _read(directory / "bridge.json")
    token = _read(directory / "bridge-token.json")["token"]
    registration = _read(directory / "registration.json")
    path = registration["request"]["configuration"]["catbus_path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise CatbusError("Registered catbus_path must be an absolute path")
    adapter = RailConnectorAdapter(CatbusClient(path))
    with httpx.Client(
        base_url=_api_base(config["api_url"]),
        headers={"X-EHAI-Connector-Token": token},
        timeout=30,
        trust_env=False,
    ) as core:
        while True:
            _run_once(directory, core, adapter, config)
            if once:
                return
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only catbus 12306 connector")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--api-url", required=True)
    init.add_argument("--project-id", required=True)
    init.add_argument("--catbus")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    for command in (init, run):
        command.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with _lock(args.state_dir):
            if args.command == "init":
                path = _catbus_path(args.catbus)
                CatbusClient(path).call(["version"])
                _initialize(
                    args.state_dir,
                    args.api_url,
                    args.project_id,
                    rail_manifest(),
                    {"catbus_path": path},
                    "12306 rail queries",
                )
            else:
                _run(args.state_dir, once=args.once)
    except KeyboardInterrupt:
        return 130
    except CatbusError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"Rail connector stopped ({type(error).__name__}); private state retained",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
