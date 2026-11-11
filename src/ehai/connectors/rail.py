"""Read-only catbus 12306 adapter; no shell, retries, login or booking."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from pydantic import ValidationError

from ehai.application.connector_models import ConnectorAction, ConnectorClaim, ConnectorManifest
from ehai.connectors.bridge import ConnectorOutcome
from ehai.connectors.rail_contract import (
    PriceInput,
    PricesOutput,
    RailModel,
    RouteInput,
    RouteOutput,
    StationSearchInput,
    StationsOutput,
    TicketSearchInput,
    TicketsOutput,
    TransferSearchInput,
    TransfersOutput,
)

CONTRACTS: dict[str, tuple[type[RailModel], type[RailModel], str]] = {
    "rail.stations.search": (StationSearchInput, StationsOutput, "Search stations by name or code"),
    "rail.tickets.search": (
        TicketSearchInput,
        TicketsOutput,
        "Query direct trains and remaining seats",
    ),
    "rail.tickets.price": (
        PriceInput,
        PricesOutput,
        "Query ticket prices by train and station numbers",
    ),
    "rail.transfers.search": (TransferSearchInput, TransfersOutput, "Search transfer itineraries"),
    "rail.route.get": (RouteInput, RouteOutput, "Query a train's station stops"),
}


def rail_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_type="rail_12306",
        version="1",
        actions=[
            ConnectorAction(
                name=name,
                version="1",
                description=description,
                read_only=True,
                input_schema=inputs.model_json_schema(),
                output_schema=output.model_json_schema(),
            )
            for name, (inputs, output, description) in CONTRACTS.items()
        ],
    )


class CatbusError(ValueError):
    """Short, public failure reason."""


class CatbusClient:
    def __init__(self, path: str) -> None:
        self.path = path
        self.last_finished: float | None = None

    def call(self, arguments: list[str]) -> Any:
        if self.last_finished is not None:
            time.sleep(max(0, 0.5 - (time.monotonic() - self.last_finished)))
        try:
            result = subprocess.run(
                [self.path, *arguments, "-o", "json"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CatbusError("catbus timed out after 30 s") from exc
        except OSError as exc:
            raise CatbusError("catbus could not be started") from exc
        finally:
            self.last_finished = time.monotonic()
        try:
            envelope = json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            reason = (
                "catbus exited without JSON"
                if result.returncode
                else "catbus returned invalid JSON"
            )
            raise CatbusError(reason) from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise CatbusError("catbus returned an invalid response")
        if envelope["ok"] is False:
            error = envelope.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise CatbusError(
                message[:200] if isinstance(message, str) and message else "catbus query failed"
            )
        if result.returncode:
            raise CatbusError("catbus exited unsuccessfully")
        return envelope.get("data")


def _arguments(request: RailModel) -> list[str]:
    if isinstance(request, StationSearchInput):
        return ["station", "search", request.keyword]
    if isinstance(request, TicketSearchInput):
        args = ["ticket", "search", request.from_, request.to, "--date", request.date]
        if request.train_types:
            args += ["--type", ",".join(request.train_types)]
        if request.available_only:
            args += ["--available"]
        return args
    if isinstance(request, PriceInput):
        return [
            "ticket",
            "price",
            request.train_no,
            "--from-no",
            request.from_no,
            "--to-no",
            request.to_no,
            "--seat-types",
            request.seat_types,
            "--date",
            request.date,
        ]
    if isinstance(request, TransferSearchInput):
        args = ["transfer", "search", request.from_, request.to, "--date", request.date]
        if request.via is not None:
            args += ["--via", request.via]
        return [*args, "--limit", str(request.limit)]
    if isinstance(request, RouteInput):
        return [
            "route",
            "get",
            request.train_no,
            request.from_,
            request.to,
            "--date",
            request.date,
        ]
    raise ValueError("Unsupported rail action")


def _seats(row: dict[str, Any]) -> list[dict[str, Any]]:
    remaining = row.get("remaining", {})
    prices = row.get("prices", {})
    # Retain catbus seat order, including classes only present in remaining/prices.
    classes = dict.fromkeys([*row.get("seats", {}), *remaining, *prices])
    seats = []
    for name in classes:
        count = remaining.get(name)
        at_least = isinstance(count, dict)
        if at_least:
            count = count["min"]
        price = prices.get(name)
        seats.append(
            {
                "class": name,
                "remaining": count,
                "remaining_at_least": at_least,
                "price_cny": price["amount"] if price is not None else None,
            }
        )
    return seats


def normalize_output(action: str, data: Any) -> RailModel:
    """Normalize the documented catbus envelopes, validating before delivery."""
    if action == "rail.stations.search":
        return StationsOutput.model_validate(
            {
                "stations": [
                    {key: row[key] for key in ("name", "code", "city")} for row in data[:20]
                ],
                "truncated": len(data) > 20,
            }
        )
    if action == "rail.tickets.search":
        trains = []
        for row in data[:60]:
            train = {
                key: row[key]
                for key in (
                    "train_no",
                    "from",
                    "to",
                    "date",
                    "depart",
                    "arrive",
                    "duration",
                    "bookable",
                    "seat_types",
                    "url",
                )
            }
            train.update(train=row["id"], seats=_seats(row))
            trains.append(train)
        return TicketsOutput.model_validate(
            {
                "trains": trains,
                "total": len(data),
                "truncated": len(data) > 60,
            }
        )
    if action == "rail.tickets.price":
        return PricesOutput.model_validate(
            {
                "train_no": data["train_no"],
                "date": data["date"],
                "prices": [
                    {"class": name, "price_cny": value["amount"]}
                    for name, value in data["prices"].items()
                ],
            }
        )
    if action == "rail.transfers.search":
        plans = []
        for row in data[:20]:
            plan = {
                key: row[key]
                for key in (
                    "via",
                    "kind",
                    "same_station",
                    "depart",
                    "arrive",
                    "duration_minutes",
                    "wait",
                )
            }
            plan["legs"] = [
                {
                    **{
                        key: leg[key]
                        for key in (
                            "train_no",
                            "from",
                            "to",
                            "depart",
                            "arrive",
                            "duration",
                        )
                    },
                    "train": leg["id"],
                    "seats": _seats(leg),
                }
                for leg in row["legs"]
            ]
            plans.append(plan)
        return TransfersOutput.model_validate({"plans": plans, "truncated": len(data) > 20})
    if action == "rail.route.get":
        return RouteOutput.model_validate(
            {
                "stops": [
                    {
                        "no": row["id"],
                        "station": row["station"],
                        **{
                            key: None if row[key] == "----" else row[key]
                            for key in ("arrive", "depart", "stopover")
                        },
                    }
                    for row in data
                ]
            }
        )
    raise ValueError("Unsupported rail action")


class RailConnectorAdapter:
    def __init__(self, client: CatbusClient) -> None:
        self.client = client

    def execute(self, claim: ConnectorClaim) -> ConnectorOutcome:
        if (
            claim.connection.manifest != rail_manifest()
            or claim.call.action not in claim.connection.manifest.actions
            or claim.connection.configuration != {"catbus_path": self.client.path}
        ):
            return ConnectorOutcome("failed", error="Rail contract does not match this connection")
        name = claim.call.action.name
        try:
            request = CONTRACTS[name][0].model_validate(claim.call.inputs)
        except ValidationError:
            return ConnectorOutcome("failed", error=f"Invalid {name} inputs")
        try:
            data = self.client.call(["12306", *_arguments(request), "-q"])
            output = normalize_output(name, data)
            return ConnectorOutcome(
                "completed", output=output.model_dump(mode="json", by_alias=True)
            )
        except CatbusError as error:
            return ConnectorOutcome("failed", error=str(error))
        except Exception:
            return ConnectorOutcome("failed", error="catbus returned invalid rail data")
