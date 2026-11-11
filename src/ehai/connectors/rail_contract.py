"""Strict v1 inputs and normalized outputs for the read-only rail connector."""

from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from ehai.application.connector_models import ConnectorModel

Text = Annotated[str, StringConstraints(min_length=1)]
Station = Annotated[str, StringConstraints(min_length=1, max_length=40)]
Date = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class RailModel(ConnectorModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StationSearchInput(RailModel):
    keyword: Station


class TicketSearchInput(RailModel):
    from_: Station = Field(alias="from")
    to: Station
    date: Date
    train_types: list[Annotated[str, StringConstraints(pattern=r"^[A-Z]$")]] = Field(max_length=10)
    available_only: bool


class PriceInput(RailModel):
    train_no: Text
    from_no: Text
    to_no: Text
    seat_types: Text
    date: Date


class TransferSearchInput(RailModel):
    from_: Station = Field(alias="from")
    to: Station
    date: Date
    via: Station | None
    limit: int = Field(ge=1, le=20)


class RouteInput(RailModel):
    train_no: Text
    from_: Station = Field(alias="from")
    to: Station
    date: Date


class StationResult(RailModel):
    name: str
    code: str
    city: str


class StationsOutput(RailModel):
    stations: list[StationResult] = Field(max_length=20)
    truncated: bool


class Seat(RailModel):
    class_: str = Field(alias="class")
    remaining: int | None
    remaining_at_least: bool
    price_cny: float | None = Field(allow_inf_nan=False)


class TrainStation(RailModel):
    name: str
    code: str
    no: str


class Train(RailModel):
    train: str
    train_no: str
    from_: TrainStation = Field(alias="from")
    to: TrainStation
    date: str
    depart: str
    arrive: str
    duration: str
    bookable: bool
    seats: list[Seat]
    seat_types: str
    url: str | None


class TicketsOutput(RailModel):
    trains: list[Train] = Field(max_length=60)
    total: int
    truncated: bool


class Price(RailModel):
    class_: str = Field(alias="class")
    price_cny: float = Field(allow_inf_nan=False)


class PricesOutput(RailModel):
    train_no: str
    date: str
    prices: list[Price]


class Leg(RailModel):
    train: str
    train_no: str
    from_: str = Field(alias="from")
    to: str
    depart: str
    arrive: str
    duration: str
    seats: list[Seat]


class Plan(RailModel):
    via: str
    kind: str
    same_station: bool
    depart: str
    arrive: str
    duration_minutes: int
    wait: str
    legs: list[Leg]


class TransfersOutput(RailModel):
    plans: list[Plan] = Field(max_length=20)
    truncated: bool


class Stop(RailModel):
    no: str
    station: str
    arrive: str | None
    depart: str | None
    stopover: str | None


class RouteOutput(RailModel):
    stops: list[Stop]
