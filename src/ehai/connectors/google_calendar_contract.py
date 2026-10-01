"""Google Calendar's bounded v1 actions and provider-independent calendar data."""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from ehai.application.connector_models import (
    ConnectorAction,
    ConnectorEventType,
    ConnectorManifest,
    ConnectorModel,
)

Text = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
PageToken = Annotated[str, StringConstraints(max_length=4096)]


class CalendarListInput(ConnectorModel):
    page_token: PageToken | None = None


class CalendarEventsInput(ConnectorModel):
    calendar_id: Text = "primary"
    time_min: AwareDatetime
    time_max: AwareDatetime
    page_token: PageToken | None = None
    limit: int = Field(default=50, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> CalendarEventsInput:
        if self.time_max <= self.time_min:
            raise ValueError("time_max must be after time_min")
        return self


class CalendarEventInput(ConnectorModel):
    calendar_id: Text = "primary"
    event_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{5,1024}$")]


class CalendarCreateInput(ConnectorModel):
    calendar_id: Text = "primary"
    title: Text
    start: AwareDatetime
    end: AwareDatetime
    description: Annotated[str, StringConstraints(max_length=10000)] = ""
    location: Annotated[str, StringConstraints(max_length=1000)] = ""
    reminder_minutes: list[Annotated[int, Field(ge=0, le=40320, strict=True)]] = Field(
        default_factory=list, max_length=5
    )

    @model_validator(mode="after")
    def ordered(self) -> CalendarCreateInput:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class CalendarInfo(ConnectorModel):
    calendar_id: str
    title: str
    time_zone: str | None
    access_role: str
    primary: bool


class CalendarListOutput(ConnectorModel):
    items: list[CalendarInfo]
    next_page_token: str | None


class CalendarMoment(ConnectorModel):
    date_time: str | None = None
    date: str | None = None
    time_zone: str | None = None


class CalendarEvent(ConnectorModel):
    calendar_id: str
    event_id: str
    title: str
    description: str
    location: str
    start: CalendarMoment | None
    end: CalendarMoment | None
    status: str
    etag: str | None
    url: str | None
    updated_at: str | None
    use_default_reminders: bool
    reminder_minutes: list[int]


class CalendarEventOutput(ConnectorModel):
    event: CalendarEvent


class CalendarEventsOutput(ConnectorModel):
    items: list[CalendarEvent]
    next_page_token: str | None


def google_calendar_manifest() -> ConnectorManifest:
    definitions = (
        ("calendar.list", True, "List allowed calendars", CalendarListInput, CalendarListOutput),
        (
            "calendar.events.list",
            True,
            "Query events in a bounded time range",
            CalendarEventsInput,
            CalendarEventsOutput,
        ),
        (
            "calendar.events.get",
            True,
            "Read a calendar event",
            CalendarEventInput,
            CalendarEventOutput,
        ),
        (
            "calendar.events.create",
            False,
            "Create one personal event with optional popup reminders",
            CalendarCreateInput,
            CalendarEventOutput,
        ),
    )
    return ConnectorManifest(
        connector_type="google_calendar",
        version="1",
        actions=[
            ConnectorAction(
                name=name,
                version="1",
                description=description,
                read_only=read_only,
                input_schema=input_model.model_json_schema(),
                output_schema=output_model.model_json_schema(),
            )
            for name, read_only, description, input_model, output_model in definitions
        ],
        events=[
            ConnectorEventType(
                name="calendar.event.created",
                version="1",
                data_schema=CalendarEventOutput.model_json_schema(),
            )
        ],
    )
