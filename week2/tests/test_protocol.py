"""Every test here goes through an MCP client session (initialize, tools/list,
tools/call), not by calling the Python functions directly."""

from __future__ import annotations

import time

import httpx

from gcal_mcp.auth import Token

from .conftest import RAW_EVENT, error_of

READERS = {"list_calendars", "search_events", "get_event"}
WRITERS = {"create_event", "delete_event"}


async def test_tools_advertise_their_contract(mcp_client):
    tools = {t.name: t for t in await mcp_client.list_tools()}
    assert set(tools) == READERS | WRITERS

    for name in READERS:
        assert tools[name].annotations.read_only_hint is True
    for name in WRITERS:
        assert tools[name].annotations.read_only_hint is False
        # The brake is in the schema: a write that omits dry_run only previews.
        assert tools[name].input_schema["properties"]["dry_run"]["default"] is True
    assert tools["delete_event"].annotations.destructive_hint is True

    # Constraints live in the schema, not just the prose.
    props = tools["search_events"].input_schema["properties"]
    assert props["order_by"]["enum"] == ["start_time", "updated"]
    assert (props["max_results"]["minimum"], props["max_results"]["maximum"]) == (1, 50)


async def test_search_result_id_feeds_get_event(mcp_client):
    found = await mcp_client.call_tool("search_events", {"query": "standup"})
    summary = found.structured_content["events"][0]

    event = await mcp_client.call_tool(
        "get_event", {"calendar_id": summary["calendar_id"], "event_id": summary["id"]}
    )
    detail = event.structured_content
    assert detail["summary"] == "Team standup"
    assert detail["attendees"] == [
        {"email": "me@example.com", "response": "accepted"},
        {"email": "sam@example.com", "response": "needsAction"},
    ]


async def test_output_is_shaped_not_passed_through(mcp_client):
    found = await mcp_client.call_tool("search_events", {})
    summary = found.structured_content["events"][0]
    assert set(summary) == {
        "id", "calendar_id", "summary", "start", "end", "all_day", "location", "status",
    }  # fmt: skip
    assert summary["start"] == "2026-10-05T09:00:00-07:00"

    detail = (
        await mcp_client.call_tool("get_event", {"calendar_id": "primary", "event_id": summary["id"]})
    ).structured_content
    for dropped in ("etag", "kind", "iCalUID", "sequence", "reminders", "creator", "htmlLink"):
        assert dropped in RAW_EVENT and dropped not in detail


async def test_create_event_previews_until_told_to_commit(mcp_client, google):
    args = {
        "summary": "Dentist",
        "start": "2026-10-05T09:05:00-07:00",
        "end": "2026-10-05T10:00:00-07:00",
    }
    preview = (await mcp_client.call_tool("create_event", args)).structured_content
    assert preview["committed"] is False and preview["event"] is None
    assert preview["preview"]["summary"] == "Dentist"
    assert [c["id"] for c in preview["conflicts"]] == ["evt_standup_1"]
    assert "POST" not in google.methods()

    created = (
        await mcp_client.call_tool("create_event", {**args, "dry_run": False})
    ).structured_content
    assert created["committed"] is True
    assert google.methods().count("POST") == 1

    # The new id chains straight into the other tools.
    again = await mcp_client.call_tool(
        "get_event", {"calendar_id": "primary", "event_id": created["event"]["id"]}
    )
    assert again.structured_content["summary"] == "Dentist"


async def test_delete_event_previews_until_told_to_commit(mcp_client, google):
    args = {"calendar_id": "primary", "event_id": "evt_standup_1"}
    preview = (await mcp_client.call_tool("delete_event", args)).structured_content
    assert preview["committed"] is False
    assert preview["event"]["summary"] == "Team standup"
    assert "DELETE" not in google.methods() and "evt_standup_1" in google.events

    done = (await mcp_client.call_tool("delete_event", {**args, "dry_run": False})).structured_content
    assert done["committed"] is True and "evt_standup_1" not in google.events


async def test_bad_event_id_is_a_dont_retry_error(mcp_client):
    result = await mcp_client.call_tool(
        "get_event", {"calendar_id": "primary", "event_id": "made_up"}, raise_on_error=False
    )
    err = error_of(result)
    assert err["code"] == "not_found" and err["retryable"] is False
    assert "search_events" in err["hint"]


async def test_rate_limit_is_a_retry_error_with_a_wait(mcp_client, google):
    google.fail_next = httpx.Response(
        429, headers={"Retry-After": "42"}, json={"error": {"code": 429, "message": "slow down"}}
    )
    err = error_of(await mcp_client.call_tool("list_calendars", {}, raise_on_error=False))
    assert err == {
        "code": "rate_limited",
        "message": "Google Calendar rate limit hit; retry after 42s.",
        "retryable": True,
        "retry_after_s": 42,
    }


async def test_google_403_rate_limit_reason_is_also_retryable(mcp_client, google):
    google.fail_next = httpx.Response(
        403, json={"error": {"code": 403, "message": "x", "errors": [{"reason": "rateLimitExceeded"}]}}
    )
    err = error_of(await mcp_client.call_tool("list_calendars", {}, raise_on_error=False))
    assert err["code"] == "rate_limited" and err["retryable"] is True


async def test_cross_field_validation_is_structured_too(mcp_client, google):
    result = await mcp_client.call_tool(
        "create_event",
        {"summary": "x", "start": "2026-10-05T10:00:00-07:00", "end": "2026-10-05T09:00:00-07:00"},
        raise_on_error=False,
    )
    assert error_of(result)["code"] == "invalid_request"
    assert google.calls == []


async def test_schema_rejects_values_outside_the_enum(mcp_client, google):
    result = await mcp_client.call_tool("search_events", {"order_by": "stars"}, raise_on_error=False)
    assert result.is_error and "'start_time' or 'updated'" in result.content[0].text
    assert google.calls == []


async def test_expired_token_is_refreshed_silently_and_recached(mcp_client, google, store):
    store.save(Token("access-stale", "refresh-good", time.time() - 10, "scope"))

    result = await mcp_client.call_tool("list_calendars", {})
    assert result.structured_content["calendars"][0]["id"] == "me@example.com"
    assert google.refresh_calls == 1

    cached = store.load()
    assert cached.access_token == "access-refreshed-1"
    assert cached.refresh_token == "refresh-good"  # kept: Google omits it on refresh

    await mcp_client.call_tool("list_calendars", {})
    assert google.refresh_calls == 1  # the refreshed token is reused, not re-fetched


async def test_token_revoked_early_gets_one_refresh_then_succeeds(mcp_client, google):
    google.valid_access_tokens.clear()  # still "fresh" by the clock, but Google says 401
    result = await mcp_client.call_tool("list_calendars", {})
    assert not result.is_error and google.refresh_calls == 1


async def test_dead_refresh_token_mid_session_is_an_actionable_error(mcp_client, google, store):
    assert not (await mcp_client.call_tool("list_calendars", {})).is_error  # session is live

    store.save(Token("access-stale", "refresh-revoked", time.time() - 10, "scope"))
    google.refresh_ok = False

    err = error_of(await mcp_client.call_tool("list_calendars", {}, raise_on_error=False))
    assert err["code"] == "auth_required" and err["retryable"] is False
    assert "gcal-mcp auth" in err["hint"]
    # conftest makes webbrowser.open raise, so reaching here proves no browser was opened.


async def test_server_picks_up_a_token_written_by_another_process(mcp_client, store):
    store.delete()
    err = error_of(await mcp_client.call_tool("list_calendars", {}, raise_on_error=False))
    assert err["code"] == "auth_required"

    # What `gcal-mcp auth` does in a separate terminal, while the server keeps running.
    store.save(Token("access-good", "refresh-good", time.time() + 3600, "scope"))
    assert not (await mcp_client.call_tool("list_calendars", {})).is_error
