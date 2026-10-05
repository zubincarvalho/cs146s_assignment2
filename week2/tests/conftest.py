"""A fake Google (Calendar API + token endpoint) behind httpx.MockTransport.

The server under test is real and is driven through a real MCP client session;
only the outbound HTTP to Google is replaced.
"""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from fastmcp import Client

from gcal_mcp.auth import TOKEN_URL, OAuthConfig, Token, TokenManager, TokenStore
from gcal_mcp.client import API_BASE, CalendarClient
from gcal_mcp.server import create_server

RAW_EVENT = {
    # Everything Google sends, including the fields the server is meant to drop.
    "kind": "calendar#event",
    "etag": '"3181161784712000"',
    "id": "evt_standup_1",
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=abc",
    "created": "2026-09-01T10:00:00.000Z",
    "updated": "2026-09-02T10:00:00.000Z",
    "summary": "Team standup",
    "description": "Daily sync",
    "location": "Room 4",
    "creator": {"email": "me@example.com", "self": True},
    "organizer": {"email": "me@example.com", "self": True},
    "start": {"dateTime": "2026-10-05T09:00:00-07:00", "timeZone": "America/Los_Angeles"},
    "end": {"dateTime": "2026-10-05T09:15:00-07:00", "timeZone": "America/Los_Angeles"},
    "iCalUID": "evt_standup_1@google.com",
    "sequence": 0,
    "attendees": [
        {"email": "me@example.com", "responseStatus": "accepted", "self": True},
        {"email": "sam@example.com", "responseStatus": "needsAction"},
    ],
    "reminders": {"useDefault": True},
    "eventType": "default",
}


class FakeGoogle:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {RAW_EVENT["id"]: dict(RAW_EVENT)}
        self.valid_access_tokens = {"access-good"}
        self.refresh_ok = True
        self.refresh_calls = 0
        self.fail_next: httpx.Response | None = None
        self.calls: list[tuple[str, str]] = []

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            return self._token(request)
        path = unquote(url[len(API_BASE) :].split("?")[0])
        self.calls.append((request.method, path))

        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_access_tokens:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        if self.fail_next is not None:
            resp, self.fail_next = self.fail_next, None
            return resp

        if path == "/users/me/calendarList":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "me@example.com",
                            "summary": "Me",
                            "primary": True,
                            "accessRole": "owner",
                            "timeZone": "America/Los_Angeles",
                            "etag": "x",
                            "colorId": "14",
                        }
                    ]
                },
            )
        parts = path.strip("/").split("/")  # calendars/<id>/events[/<event_id>]
        if parts[1] not in {"primary", "me@example.com"}:
            return self._not_found()
        if len(parts) == 3:
            if request.method == "POST":
                body = json.loads(request.content)
                event = {"id": f"evt_new_{len(self.events)}", "status": "confirmed", **body}
                self.events[event["id"]] = event
                return httpx.Response(200, json=event)
            return httpx.Response(200, json={"items": list(self.events.values())})
        event = self.events.get(parts[3])
        if event is None:
            return self._not_found()
        if request.method == "DELETE":
            del self.events[parts[3]]
            return httpx.Response(204)
        return httpx.Response(200, json=event)

    @staticmethod
    def _not_found() -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}},
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.refresh_calls += 1
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        if not self.refresh_ok:
            return httpx.Response(400, json={"error": "invalid_grant"})
        new = f"access-refreshed-{self.refresh_calls}"
        self.valid_access_tokens.add(new)
        return httpx.Response(200, json={"access_token": new, "expires_in": 3600})


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def store(tmp_path) -> TokenStore:
    store = TokenStore(tmp_path / "token.json")
    store.save(Token("access-good", "refresh-good", time.time() + 3600, "scope"))
    return store


@pytest.fixture
async def mcp_client(google: FakeGoogle, store: TokenStore, monkeypatch):
    # The server must never try to open a browser; make any attempt fail loudly.
    def _no_browser(*args, **kwargs):
        raise AssertionError("server tried to open a browser during a tool call")

    monkeypatch.setattr("webbrowser.open", _no_browser)

    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    tokens = TokenManager(store=store, http=http, config=OAuthConfig("client-id", "client-secret"))
    server = create_server(CalendarClient(tokens, http))
    async with Client(server) as client:
        yield client


def error_of(result) -> dict:
    """Decode the structured error out of an isError=true tool result."""
    assert result.is_error, "expected the tool call to fail"
    return json.loads(result.content[0].text)["error"]
