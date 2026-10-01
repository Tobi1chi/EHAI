"""Google REST adapter. It has no core database, service or orchestration imports."""

from collections.abc import Callable
from hashlib import sha256
from typing import cast
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from ehai import JsonValue, json_dumps, parse_utc_datetime
from ehai.application.connector_models import ConnectorClaim
from ehai.connectors.bridge import ConnectorOutcome
from ehai.connectors.google_calendar_contract import (
    CalendarCreateInput,
    CalendarEvent,
    CalendarEventInput,
    CalendarEventOutput,
    CalendarEventsInput,
    CalendarEventsOutput,
    CalendarInfo,
    CalendarListInput,
    CalendarListOutput,
    CalendarMoment,
    google_calendar_manifest,
)


class _ProviderFailure(Exception):
    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


class GoogleCalendarAdapter:
    def __init__(self, client: httpx.Client, token: Callable[[], str]) -> None:
        self.client = client
        self.token = token

    def execute(self, claim: ConnectorClaim) -> ConnectorOutcome:
        call = claim.call
        recovering_write = (
            call.action.name == "calendar.events.create" and claim.mode == "reconcile"
        )
        if claim.connection.manifest != google_calendar_manifest():
            return ConnectorOutcome(
                "unknown" if recovering_write else "failed",
                error="Installed Google Calendar contract does not match registration",
            )
        configured = claim.connection.configuration.get("allowed_calendar_ids")
        if (
            not isinstance(configured, list)
            or not configured
            or not all(isinstance(c, str) and c for c in configured)
        ):
            return ConnectorOutcome(
                "unknown" if recovering_write else "failed",
                error="Connection requires allowed_calendar_ids",
            )
        allowed = cast(list[str], configured)
        try:
            name = call.action.name
            if name == "calendar.list":
                request = CalendarListInput.model_validate(call.inputs)
                params = {"maxResults": "100"}
                if request.page_token is not None:
                    params["pageToken"] = request.page_token
                raw = self._get("users/me/calendarList", params=params)
                items = [
                    CalendarInfo(
                        calendar_id=str(c["id"]),
                        title=str(c.get("summary", "")),
                        time_zone=_optional_text(c.get("timeZone")),
                        access_role=str(c.get("accessRole", "")),
                        primary=c.get("primary") is True,
                    )
                    for c in _items(raw)
                    if c.get("id") in allowed or ("primary" in allowed and c.get("primary") is True)
                ]
                output = CalendarListOutput(
                    items=items, next_page_token=_optional_text(raw.get("nextPageToken"))
                )
                return ConnectorOutcome("completed", output=output.model_dump(mode="json"))
            if name == "calendar.events.list":
                query = CalendarEventsInput.model_validate(call.inputs)
                self._allowed(query.calendar_id, allowed)
                params = {
                    "timeMin": query.time_min.isoformat(),
                    "timeMax": query.time_max.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": str(query.limit),
                }
                if query.page_token is not None:
                    params["pageToken"] = query.page_token
                raw = self._get(_calendar_path(query.calendar_id), params=params)
                result = CalendarEventsOutput(
                    items=[_event(query.calendar_id, e) for e in _items(raw)],
                    next_page_token=_optional_text(raw.get("nextPageToken")),
                )
                return ConnectorOutcome("completed", output=result.model_dump(mode="json"))
            if name == "calendar.events.get":
                get = CalendarEventInput.model_validate(call.inputs)
                self._allowed(get.calendar_id, allowed)
                result_event = self._get(
                    _calendar_path(get.calendar_id) + "/" + quote(get.event_id, safe="")
                )
                return ConnectorOutcome(
                    "completed",
                    output=CalendarEventOutput(
                        event=_event(get.calendar_id, result_event)
                    ).model_dump(mode="json"),
                    external_operation_ref=get.event_id,
                )
            if name == "calendar.events.create":
                create = CalendarCreateInput.model_validate(call.inputs)
                self._allowed(create.calendar_id, allowed)
                if not call.write_authorized:
                    raise _ProviderFailure("Calendar create requires write authorization")
                return self._create(claim, create)
            raise _ProviderFailure("Unsupported Google Calendar action")
        except ValidationError:
            return ConnectorOutcome(
                "unknown" if recovering_write else "failed",
                error="Invalid Google Calendar action inputs",
            )
        except _ProviderFailure as error:
            return ConnectorOutcome(
                "unknown" if error.unknown or recovering_write else "failed",
                error=str(error),
                external_operation_ref=call.call_id.replace("-", "")
                if name == "calendar.events.create"
                else None,
            )
        except Exception:
            # Provider bodies/tokens/URLs must not escape through public errors.
            return ConnectorOutcome(
                "unknown" if name == "calendar.events.create" else "failed",
                error="Google Calendar adapter could not establish a valid result",
            )

    @staticmethod
    def _allowed(calendar_id: str, allowed: list[str]) -> None:
        if calendar_id not in allowed:
            raise _ProviderFailure("Calendar is outside this connection's allowed_calendar_ids")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, JsonValue] | None = None,
    ) -> httpx.Response:
        try:
            token = self.token()
        except Exception as error:
            raise _ProviderFailure(
                "Google OAuth authorization is unavailable; authorize the connector"
            ) from error
        try:
            response = self.client.request(
                method, path, params=params, json=body, headers={"Authorization": "Bearer " + token}
            )
        except httpx.HTTPError as error:
            raise _ProviderFailure(
                "Google Calendar request failed without a confirmed response",
                unknown=method != "GET",
            ) from error
        if response.status_code >= 500:
            raise _ProviderFailure(
                "Google Calendar service could not confirm the operation", unknown=method != "GET"
            )
        return response

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, JsonValue]:
        response = self._request("GET", path, params=params)
        if response.status_code != 200:
            raise _ProviderFailure(
                f"Google Calendar read rejected with HTTP {response.status_code}"
            )
        return _object(response)

    def _create(self, claim: ConnectorClaim, request: CalendarCreateInput) -> ConnectorOutcome:
        event_id = claim.call.call_id.replace("-", "")
        path = _calendar_path(request.calendar_id)
        fingerprint = sha256(json_dumps(request.model_dump(mode="json")).encode()).hexdigest()
        existing = self._request("GET", path + "/" + event_id)
        if existing.status_code == 200:
            return self._created(request, _object(existing), claim.call.call_id, fingerprint)
        if existing.status_code != 404:
            raise _ProviderFailure(
                f"Unable to reconcile calendar event: HTTP {existing.status_code}",
                unknown=claim.mode == "reconcile",
            )
        if claim.mode == "reconcile":
            return ConnectorOutcome(
                "unknown",
                error="Previously claimed create has no confirmed remote result; not replayed",
                external_operation_ref=event_id,
            )
        body: dict[str, JsonValue] = {
            "id": event_id,
            "summary": request.title,
            "description": request.description,
            "location": request.location,
            "start": {"dateTime": request.start.isoformat()},
            "end": {"dateTime": request.end.isoformat()},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": minute} for minute in request.reminder_minutes
                ],
            },
            "extendedProperties": {
                "private": {"ehai_call_id": claim.call.call_id, "ehai_input_sha256": fingerprint}
            },
        }
        try:
            response = self._request("POST", path, body=body)
            if response.status_code in {200, 201}:
                return self._created(request, _object(response), claim.call.call_id, fingerprint)
            if response.status_code == 409:
                raise _ProviderFailure(
                    "Calendar create needs reconciliation after a conflict", unknown=True
                )
            raise _ProviderFailure(
                f"Google Calendar create returned HTTP {response.status_code}",
                unknown=response.status_code not in {400, 401, 403, 404, 405, 410, 422, 429},
            )
        except _ProviderFailure as error:
            if not error.unknown:
                raise
            try:
                remote = self._get(path + "/" + event_id)
                return self._created(request, remote, claim.call.call_id, fingerprint)
            except _ProviderFailure:
                return ConnectorOutcome(
                    "unknown",
                    error="Calendar create outcome is unknown; reconcile the saved event ID",
                    external_operation_ref=event_id,
                )

    @staticmethod
    def _created(
        request: CalendarCreateInput, raw: dict[str, JsonValue], call_id: str, fingerprint: str
    ) -> ConnectorOutcome:
        extended = raw.get("extendedProperties")
        private = extended.get("private") if isinstance(extended, dict) else None
        if (
            not isinstance(private, dict)
            or private.get("ehai_call_id") != call_id
            or private.get("ehai_input_sha256") != fingerprint
            or raw.get("id") != call_id.replace("-", "")
        ):
            raise _ProviderFailure("Remote event does not match this invocation", unknown=True)
        if raw.get("status") == "cancelled":
            raise _ProviderFailure(
                "Previously created event has been cancelled; not recreated", unknown=True
            )
        event = _event(request.calendar_id, raw)
        if (
            event.title != request.title
            or event.description != request.description
            or event.location != request.location
            or event.start is None
            or event.end is None
            or event.start.date_time is None
            or event.end.date_time is None
            or parse_utc_datetime(event.start.date_time) != request.start
            or parse_utc_datetime(event.end.date_time) != request.end
            or event.use_default_reminders
            or sorted(event.reminder_minutes) != sorted(request.reminder_minutes)
        ):
            raise _ProviderFailure(
                "Remote event content differs from the authorized request", unknown=True
            )
        return ConnectorOutcome(
            "completed",
            output=CalendarEventOutput(event=event).model_dump(mode="json"),
            external_operation_ref=event.event_id,
            event_type="calendar.event.created",
        )


def _calendar_path(calendar_id: str) -> str:
    return "calendars/" + quote(calendar_id, safe="") + "/events"


def _object(response: httpx.Response) -> dict[str, JsonValue]:
    value = response.json()
    if not isinstance(value, dict):
        raise _ProviderFailure("Google Calendar returned an invalid object")
    return cast(dict[str, JsonValue], value)


def _items(value: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    items = value.get("items", [])
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise _ProviderFailure("Google Calendar returned invalid items")
    return cast(list[dict[str, JsonValue]], items)


def _optional_text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _event(calendar_id: str, raw: dict[str, JsonValue]) -> CalendarEvent:
    def moment(value: JsonValue) -> CalendarMoment | None:
        if not isinstance(value, dict):
            return None
        return CalendarMoment(
            date_time=_optional_text(value.get("dateTime")),
            date=_optional_text(value.get("date")),
            time_zone=_optional_text(value.get("timeZone")),
        )

    reminders = raw.get("reminders")
    overrides = reminders.get("overrides", []) if isinstance(reminders, dict) else []
    minutes = (
        [
            cast(int, r["minutes"])
            for r in overrides
            if isinstance(r, dict)
            and r.get("method") == "popup"
            and isinstance(r.get("minutes"), int)
        ]
        if isinstance(overrides, list)
        else []
    )
    return CalendarEvent(
        calendar_id=calendar_id,
        event_id=str(raw["id"]),
        title=str(raw.get("summary", "")),
        description=str(raw.get("description", "")),
        location=str(raw.get("location", "")),
        start=moment(raw.get("start")),
        end=moment(raw.get("end")),
        status=str(raw.get("status", "confirmed")),
        etag=_optional_text(raw.get("etag")),
        url=_optional_text(raw.get("htmlLink")),
        updated_at=_optional_text(raw.get("updated")),
        use_default_reminders=isinstance(reminders, dict) and reminders.get("useDefault") is True,
        reminder_minutes=minutes,
    )
