"""The MCP surface: five Google Calendar tools designed for an agent to chain.

The agent sees the schemas, the docstrings, and `INSTRUCTIONS` below, and nothing
else, so everything it needs to pick and call a tool correctly has to live there.
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel, EmailStr, Field

from .auth import TokenManager
from .client import CalendarClient
from .errors import CalendarError

INSTRUCTIONS = """\
Google Calendar for the signed-in user.

Workflow:
1. Events live in calendars, and search_events reads one calendar per call. People
   often keep events on several (classes, work, shared), so for an open question
   such as "what's next?" or "am I free?", call list_calendars first and search each
   calendar. Use calendar_id "primary" alone when creating an event, or when the
   user names their main calendar.
2. Find events with search_events. It returns compact summaries, each with an `id`.
3. Pass that `id` as event_id to get_event (full details) or delete_event.
4. create_event and delete_event default to dry_run=true, which previews and changes
   nothing. Show the user the preview, then call again with dry_run=false to commit.

Times are RFC 3339 with a UTC offset, e.g. 2026-10-05T14:00:00-07:00. To resolve
"tomorrow at 3pm", use the time_zone that list_calendars reports for the calendar.

Errors come back as JSON: {"error": {"code", "message", "retryable", "retry_after_s"?,
"hint"?}}. If retryable is true, wait retry_after_s and repeat the same call. If it is
false, do not repeat it: fix the arguments or follow the hint. On code "auth_required",
stop and ask the user to run the command in the hint; you cannot sign in for them.
"""

MAX_DESCRIPTION_CHARS = 2000
MAX_ATTENDEES = 25

# Ask Google for only the fields we surface, so we never download what we'd drop.
_EVENT_FIELDS = (
    "id,status,htmlLink,summary,description,location,start,end,recurringEventId,"
    "hangoutLink,organizer(email),attendees(email,responseStatus)"
)
_SUMMARY_FIELDS = "id,status,summary,location,start,end"

CALENDAR_ID_HINT = 'calendar_id must be "primary" or an `id` returned by list_calendars.'
EVENT_ID_HINT = (
    "event_id must be an `id` returned by search_events for this same calendar_id. "
    "Do not guess or construct IDs; call search_events again to get a current one."
)


# --- Output shapes ----------------------------------------------------------------


class Calendar(BaseModel):
    id: str = Field(description="Pass as calendar_id to the event tools.")
    name: str
    primary: bool = Field(description='True for the calendar that "primary" refers to.')
    access_role: Literal["owner", "writer", "reader", "freeBusyReader"] = Field(
        description="create_event and delete_event need 'owner' or 'writer'."
    )
    time_zone: str = Field(description="IANA zone, e.g. America/Los_Angeles.")


class CalendarList(BaseModel):
    calendars: list[Calendar]


class EventSummary(BaseModel):
    id: str = Field(description="Pass as event_id to get_event or delete_event.")
    calendar_id: str
    summary: str
    start: str = Field(description="RFC 3339 datetime, or YYYY-MM-DD if all_day.")
    end: str = Field(description="Exclusive end; RFC 3339 datetime, or YYYY-MM-DD if all_day.")
    all_day: bool
    location: str | None = None
    status: Literal["confirmed", "tentative", "cancelled"]


class Attendee(BaseModel):
    email: str
    response: Literal["needsAction", "declined", "tentative", "accepted"]


class Event(EventSummary):
    description: str | None = None
    description_truncated: bool = Field(
        default=False, description=f"True if description was cut at {MAX_DESCRIPTION_CHARS} chars."
    )
    organizer: str | None = Field(default=None, description="Organizer's email.")
    attendees: list[Attendee] = Field(default_factory=list)
    attendee_count: int = Field(
        default=0, description=f"Total attendees; the list shows at most {MAX_ATTENDEES}."
    )
    recurring: bool = Field(default=False, description="True if one instance of a series.")
    meet_link: str | None = None
    html_link: str | None = Field(default=None, description="Link to open in Google Calendar.")


class EventSearchResult(BaseModel):
    events: list[EventSummary]
    next_page_token: str | None = Field(
        default=None,
        description="If set, more events match; pass it as page_token with the same arguments.",
    )


class EventDraft(BaseModel):
    calendar_id: str
    summary: str
    start: str
    end: str
    location: str | None = None
    description: str | None = None
    attendees: list[str] = Field(default_factory=list)
    notify_attendees: bool = False


class CreateEventResult(BaseModel):
    committed: bool = Field(description="False for a dry run: nothing was created.")
    event: Event | None = Field(default=None, description="The created event, if committed.")
    preview: EventDraft | None = Field(
        default=None, description="What would be created, if a dry run."
    )
    conflicts: list[EventSummary] = Field(
        default_factory=list,
        description="Existing events overlapping the requested time (dry run only).",
    )
    next_step: str


class DeleteEventResult(BaseModel):
    committed: bool = Field(description="False for a dry run: nothing was deleted.")
    event: Event = Field(description="The event that was, or would be, deleted.")
    next_step: str


# --- Shaping raw Google payloads ----------------------------------------------------


def _when(raw: dict[str, Any] | None) -> tuple[str, bool]:
    raw = raw or {}
    if "dateTime" in raw:
        return raw["dateTime"], False
    return raw.get("date", ""), True


def _summary_fields(raw: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    start, all_day = _when(raw.get("start"))
    end, _ = _when(raw.get("end"))
    return {
        "id": raw["id"],
        "calendar_id": calendar_id,
        "summary": raw.get("summary") or "(no title)",
        "start": start,
        "end": end,
        "all_day": all_day,
        "location": raw.get("location"),
        "status": raw.get("status", "confirmed"),
    }


def shape_summary(raw: dict[str, Any], calendar_id: str) -> EventSummary:
    return EventSummary(**_summary_fields(raw, calendar_id))


def shape_event(raw: dict[str, Any], calendar_id: str) -> Event:
    description = raw.get("description")
    truncated = bool(description) and len(description) > MAX_DESCRIPTION_CHARS
    if truncated:
        description = description[:MAX_DESCRIPTION_CHARS]
    attendees = raw.get("attendees") or []
    return Event(
        **_summary_fields(raw, calendar_id),
        description=description,
        description_truncated=truncated,
        organizer=(raw.get("organizer") or {}).get("email"),
        attendees=[
            Attendee(email=a.get("email", ""), response=a.get("responseStatus", "needsAction"))
            for a in attendees[:MAX_ATTENDEES]
        ],
        attendee_count=len(attendees),
        recurring="recurringEventId" in raw,
        meet_link=raw.get("hangoutLink"),
        html_link=raw.get("htmlLink"),
    )


def _rfc3339(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _errors_as_data(fn):
    """Turn every failure into a structured, isError=true tool result.

    The agent gets JSON it can branch on; a raw exception would surface as an
    opaque internal error and tell it nothing about whether to retry.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except CalendarError as exc:
            raise ToolError(exc.to_json()) from exc
        except Exception as exc:
            err = CalendarError(
                "internal_error",
                f"Unexpected {type(exc).__name__} inside the server.",
                retryable=False,
                hint="This is a bug in the gcal MCP server, not in your arguments. Tell the user.",
            )
            raise ToolError(err.to_json()) from exc

    return wrapper


# --- Parameter types, shared so the same idea has the same schema everywhere ---------

CalendarId = Annotated[
    str,
    Field(
        min_length=1,
        description='"primary" for the user\'s main calendar, or an `id` from list_calendars.',
    ),
]
EventId = Annotated[
    str,
    Field(
        min_length=1,
        description="An event `id` from search_events, for this same calendar_id.",
    ),
]

READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}


def create_server(calendar: CalendarClient | None = None) -> FastMCP:
    """Build the server. Tests pass a `calendar` wired to a fake Google."""
    api = calendar or CalendarClient(TokenManager())
    mcp = FastMCP("gcal", instructions=INSTRUCTIONS, mask_error_details=True)

    @mcp.tool(annotations={"title": "List calendars", **READ_ONLY})
    @_errors_as_data
    async def list_calendars() -> CalendarList:
        """List the calendars the user can see, with their IDs, time zones and access.

        Call this before answering a question about the user's schedule as a whole:
        each `id` here is a calendar_id to pass to search_events, which reads only one
        calendar per call. Also use it to learn a calendar's time zone or to check
        write access before create_event. To create on the main calendar you can
        skip it and pass calendar_id "primary" directly.
        """
        raw = await api.request(
            "GET",
            "/users/me/calendarList",
            params={
                "maxResults": 250,
                "fields": "items(id,summary,summaryOverride,primary,accessRole,timeZone)",
            },
        )
        return CalendarList(
            calendars=[
                Calendar(
                    id=c["id"],
                    name=c.get("summaryOverride") or c.get("summary") or c["id"],
                    primary=bool(c.get("primary")),
                    access_role=c.get("accessRole", "reader"),
                    time_zone=c.get("timeZone", "UTC"),
                )
                for c in raw.get("items", [])
            ]
        )

    @mcp.tool(annotations={"title": "Search events", **READ_ONLY})
    @_errors_as_data
    async def search_events(
        calendar_id: CalendarId = "primary",
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Free-text match on title, description, location and attendees. "
                    "Omit to list every event in the time window."
                )
            ),
        ] = None,
        time_min: Annotated[
            AwareDatetime | None,
            Field(description="Only events ending after this. Defaults to now."),
        ] = None,
        time_max: Annotated[
            AwareDatetime | None,
            Field(description="Only events starting before this. Defaults to no upper bound."),
        ] = None,
        order_by: Annotated[
            Literal["start_time", "updated"],
            Field(description="start_time: soonest first. updated: least recently edited first."),
        ] = "start_time",
        max_results: Annotated[int, Field(ge=1, le=50)] = 10,
        page_token: Annotated[
            str | None,
            Field(description="next_page_token from a previous call with the same arguments."),
        ] = None,
    ) -> EventSearchResult:
        """Find events in one calendar by text and/or time window.

        Returns compact summaries (no description or attendees). Each has an `id`:
        pass it as event_id to get_event for full details, or to delete_event.
        Recurring events are expanded, so each occurrence is its own result with
        its own id. Searches upcoming events unless you set time_min in the past.
        An empty result means this one calendar has nothing in the window, not that
        the user is free: the other calendars from list_calendars are not searched.
        """
        start = time_min or datetime.now(timezone.utc)
        if time_max is not None and time_max <= start:
            raise CalendarError(
                "invalid_request",
                "time_max must be later than time_min (which defaults to now).",
                retryable=False,
                hint="To search the past, set time_min explicitly to an earlier datetime.",
            )
        params: dict[str, Any] = {
            "timeMin": _rfc3339(start),
            "singleEvents": "true",
            "orderBy": "startTime" if order_by == "start_time" else "updated",
            "maxResults": max_results,
            "fields": f"nextPageToken,items({_SUMMARY_FIELDS})",
        }
        if time_max is not None:
            params["timeMax"] = _rfc3339(time_max)
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        raw = await api.request(
            "GET",
            f"{api.calendar_path(calendar_id)}/events",
            params=params,
            not_found_hint=CALENDAR_ID_HINT,
        )
        return EventSearchResult(
            events=[shape_summary(e, calendar_id) for e in raw.get("items", [])],
            next_page_token=raw.get("nextPageToken"),
        )

    async def _fetch_event(calendar_id: str, event_id: str) -> Event:
        raw = await api.request(
            "GET",
            api.event_path(calendar_id, event_id),
            params={"fields": _EVENT_FIELDS},
            not_found_hint=f"{EVENT_ID_HINT} {CALENDAR_ID_HINT}",
        )
        return shape_event(raw, calendar_id)

    @mcp.tool(annotations={"title": "Get event details", **READ_ONLY})
    @_errors_as_data
    async def get_event(calendar_id: CalendarId, event_id: EventId) -> Event:
        """Get one event's full details: description, attendees and their responses,
        organizer, video-call link, and a link to open it in Google Calendar.

        event_id is the `id` of a result from search_events, and calendar_id must be
        the same calendar that search was run against.
        """
        return await _fetch_event(calendar_id, event_id)

    @mcp.tool(
        annotations={
            "title": "Create event",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @_errors_as_data
    async def create_event(
        summary: Annotated[str, Field(min_length=1, max_length=300, description="Event title.")],
        start: Annotated[
            AwareDatetime,
            Field(description="Start, RFC 3339 with UTC offset, e.g. 2026-10-05T14:00:00-07:00."),
        ],
        end: Annotated[
            AwareDatetime,
            Field(description="End, RFC 3339 with UTC offset. Must be after start."),
        ],
        calendar_id: CalendarId = "primary",
        description: Annotated[str | None, Field(max_length=8000)] = None,
        location: Annotated[str | None, Field(max_length=500)] = None,
        attendees: Annotated[
            list[EmailStr], Field(max_length=MAX_ATTENDEES, description="Guest email addresses.")
        ] = [],  # noqa: B006 - pydantic copies defaults
        notify_attendees: Annotated[
            bool,
            Field(description="Email invitations to attendees. Only applies when committing."),
        ] = False,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "true (default): validate and preview only, create nothing. "
                    "false: actually create the event."
                )
            ),
        ] = True,
    ) -> CreateEventResult:
        """Create a timed event on a calendar. Previews by default.

        With dry_run=true (the default) nothing is created: you get the event as it
        would be created plus any existing events that overlap the slot. Show that
        to the user, then call again with the same arguments and dry_run=false.
        A committed result includes the new event's `id`, usable with get_event
        and delete_event. All-day events are not supported; give start and end times.
        """
        if end <= start:
            raise CalendarError(
                "invalid_request",
                "end must be after start.",
                retryable=False,
                hint="Check the date and the UTC offset on both datetimes.",
            )
        if end - start > timedelta(days=30):
            raise CalendarError(
                "invalid_request",
                "An event may span at most 30 days.",
                retryable=False,
                hint="Check the year and month on start and end.",
            )

        if dry_run:
            # The preview does a real read, so it also proves the calendar exists
            # and the token works before the user is asked to approve anything.
            raw = await api.request(
                "GET",
                f"{api.calendar_path(calendar_id)}/events",
                params={
                    "timeMin": _rfc3339(start),
                    "timeMax": _rfc3339(end),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 10,
                    "fields": f"items({_SUMMARY_FIELDS})",
                },
                not_found_hint=CALENDAR_ID_HINT,
            )
            conflicts = [
                s
                for s in (shape_summary(e, calendar_id) for e in raw.get("items", []))
                if not s.all_day and s.status != "cancelled"
            ]
            return CreateEventResult(
                committed=False,
                preview=EventDraft(
                    calendar_id=calendar_id,
                    summary=summary,
                    start=_rfc3339(start),
                    end=_rfc3339(end),
                    location=location,
                    description=description,
                    attendees=list(attendees),
                    notify_attendees=notify_attendees,
                ),
                conflicts=conflicts,
                next_step=(
                    "Nothing was created. Show the user this preview"
                    + (" and the conflicts" if conflicts else "")
                    + ", then call create_event again with the same arguments and dry_run=false."
                ),
            )

        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": _rfc3339(start)},
            "end": {"dateTime": _rfc3339(end)},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        try:
            raw = await api.request(
                "POST",
                f"{api.calendar_path(calendar_id)}/events",
                params={
                    "sendUpdates": "all" if notify_attendees else "none",
                    "fields": _EVENT_FIELDS,
                },
                json=body,
                not_found_hint=CALENDAR_ID_HINT,
            )
        except CalendarError as exc:
            if exc.code in {"network_error", "upstream_unavailable"}:
                # A create that timed out may still have landed. Blind retry duplicates it.
                exc.hint = (
                    "The event may or may not have been created. Before retrying, call "
                    "search_events over this time window and check for it."
                )
            raise
        event = shape_event(raw, calendar_id)
        return CreateEventResult(
            committed=True,
            event=event,
            next_step=f"Created. Its event_id is {event.id}; delete_event can undo this.",
        )

    @mcp.tool(
        annotations={
            "title": "Delete event",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    @_errors_as_data
    async def delete_event(
        calendar_id: CalendarId,
        event_id: EventId,
        notify_attendees: Annotated[
            bool,
            Field(description="Email a cancellation to attendees. Only applies when committing."),
        ] = False,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "true (default): show the event that would be deleted, delete nothing. "
                    "false: permanently delete it."
                )
            ),
        ] = True,
    ) -> DeleteEventResult:
        """Permanently delete one event. Irreversible, so it previews by default.

        With dry_run=true (the default) nothing is deleted: you get the full event
        that would be removed. Confirm with the user that it is the right one, then
        call again with dry_run=false. event_id is an `id` from search_events; for a
        recurring series that id is a single occurrence, and only it is deleted.
        """
        event = await _fetch_event(calendar_id, event_id)
        if dry_run:
            return DeleteEventResult(
                committed=False,
                event=event,
                next_step=(
                    "Nothing was deleted. Confirm with the user that this is the right "
                    "event, then call delete_event again with dry_run=false."
                ),
            )
        await api.request(
            "DELETE",
            api.event_path(calendar_id, event_id),
            params={"sendUpdates": "all" if notify_attendees else "none"},
            not_found_hint=f"{EVENT_ID_HINT} {CALENDAR_ID_HINT}",
        )
        return DeleteEventResult(
            committed=True,
            event=event,
            next_step="Deleted. This event_id is no longer valid.",
        )

    return mcp
