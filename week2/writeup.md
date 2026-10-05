# Week 2 Write-up

## Part I: The Server

**API chosen**, and why:
> Google Calendar. I use it daily, so I can judge whether a tool returns what I would
> actually want. It also fits the OAuth requirements honestly: access tokens expire
> after an hour, so silent refresh is exercised in any real session, and the API has
> granular scopes, so "minimal scopes" is a real choice rather than a formality.

**How to run it** (one command):
```
uv run --directory week2 gcal-mcp
```
(From the repo root. One-time setup, including `gcal-mcp auth`, is in `week2/README.md`.)

| Tool | What it does | Read/Write | Composes with |
|---|---|---|---|
| `list_calendars` | Lists the user's calendars with id, time zone, and access role | Read | Its `id` is the `calendar_id` every other tool takes; its `time_zone` is how the agent resolves "tomorrow at 3pm" |
| `search_events` | Finds events in one calendar by text and/or time window; returns compact summaries | Read | Its `id` is the `event_id` for `get_event` and `delete_event`; `next_page_token` feeds its own `page_token` |
| `get_event` | Returns one event in full: description, attendees and responses, organizer, links | Read | Takes `calendar_id` + `event_id` from `search_events` or from a committed `create_event` |
| `create_event` | Creates a timed event; previews (with conflicts) unless `dry_run=false` | Write | Returns the new event's `id`, usable with `get_event` and `delete_event` |
| `delete_event` | Permanently deletes one event; previews unless `dry_run=false` | Write (destructive) | Takes `event_id` from `search_events`; undoes a `create_event` |


## Part II: Agent Ergonomics

For each, point at the code (`file:line`) and say what it buys. Paths are relative to `week2/`.

| Decision | Where | Why |
|---|---|---|
| Schema-level constraint | `gcal_mcp/server.py:306` (`order_by: Literal["start_time", "updated"]`), `:309` (`max_results` bounded 1 to 50), `:389` (`start`/`end` as `AwareDatetime`), `:400` (`attendees: list[EmailStr]`) | The agent sees an `enum`, `minimum`/`maximum`, and `format: date-time` in the JSON schema, so it cannot send `order_by="date"` or a naive datetime without getting a validation message that names the allowed values. `AwareDatetime` rejects datetimes with no UTC offset, which removes the "which time zone did the agent mean" class of bug. The shared `CalendarId`/`EventId` types (`:229`, `:236`) give the same parameter the same schema and description in every tool. |
| Output shaping (fields kept vs. dropped) | `gcal_mcp/server.py:78` (`EventSummary`), `:94` (`Event`), `:157`-`:195` (shaping), `:48`-`:52` (`fields` masks sent to Google) | Search returns 8 fields per event; `get_event` adds description, attendees, organizer and links. Dropped: `etag`, `kind`, `iCalUID`, `sequence`, `reminders`, `creator`, `created`/`updated`, and per-attendee flags. Google's `{dateTime, timeZone}` / `{date}` union is flattened to a `start` string plus an `all_day` bool. Descriptions are capped at 2000 chars and attendees at 25, each with an explicit truncation signal (`description_truncated`, `attendee_count`), so one bloated invite cannot flood the context. The `fields` mask means the dropped data is never downloaded either. |
| Structured errors (retry vs. don't-retry) | `gcal_mcp/errors.py:24` (shape), `gcal_mcp/client.py:42` (HTTP to error mapping), `gcal_mcp/server.py:202` (boundary decorator) | Every failure is `{"error": {"code", "message", "retryable", "retry_after_s"?, "hint"?}}` returned with `isError: true`. `retryable` is the one field the agent branches on: 429, Google's 403 `rateLimitExceeded`, 5xx and network failures are `true` with a wait; 404, 400, 403 permission and auth failures are `false` with a hint saying what to change. The decorator also catches unexpected exceptions and reports them as `internal_error`, so a bug in the server never reaches the agent as a traceback. |
| Docstring that chains tools together | `gcal_mcp/server.py:21` (`INSTRUCTIONS`), `:315` (`search_events` docstring), `:229`/`:236` (parameter descriptions), `:55` (`EVENT_ID_HINT`) | Each ID parameter says where its value comes from ("An event `id` from search_events, for this same calendar_id"), and each producing tool says where its output goes. The cross-tool workflow (`list_calendars` then one search per calendar, search then get, preview then commit, how to read errors) lives in `FastMCP(instructions=...)`. The same provenance sentence is reused as the `hint` on a 404, so the agent is told how to recover at the moment it guessed an ID. |
| Brake on the write tool | `gcal_mcp/server.py:406` and `:534` (`dry_run` defaults to `True`), `:439` (create preview with conflict check), `:552` (delete preview), `:377`/`:518` (annotations) | Both write tools preview unless the agent explicitly passes `dry_run=false`, so the careless call is the safe one. The create preview does a real read, returning overlapping events and proving the calendar exists and the token works before the user is asked to approve. The delete preview returns the full event so the user can confirm it is the right one. Readers carry `readOnlyHint: true`; `delete_event` carries `destructiveHint: true`. `notify_attendees` defaults to `False` on both, so no email leaves the account unless asked. A create that fails with a network error gets a hint to search before retrying (`:506`), because a blind retry could duplicate the event. |

**One thing you changed after watching the agent misuse a tool:**
> **What the agent did.** Prompt: "What's the next thing on my calendar in the coming 7
> days, and who else is invited to it?" Its first calendar call was
> `search_events(calendar_id="primary", ...)`. It did what my instructions told it to:
> step 1 said to use `"primary"` and to call `list_calendars` "only when they mean
> another one". On my account that is wrong for this question, because my classes live on
> four other calendars (engr108, cs221, cs146s, ms&e203). The agent only went on to
> `list_calendars` and the other four because primary happened to come back empty. Had
> primary held one event, it would have reported that as "the next thing" and never
> looked at the rest.
>
> **What I changed.** Three pieces of prose, no schema change:
> `INSTRUCTIONS` step 1 (`gcal_mcp/server.py:25`) now says `search_events` reads one
> calendar per call and that an open question about the schedule starts with
> `list_calendars`; the `list_calendars` docstring (`:257`) says the same from its side
> instead of "you do not need it for the main calendar"; and the `search_events`
> docstring (`:321`) says an empty result means this one calendar is empty, not that the
> user is free.
>
> **After.** Same prompt, fresh session: `list_calendars` fired first, then one
> `search_events` per calendar (all five), before answering.


## Part III: OAuth

**Flow**: how a token is obtained, cached, and refreshed:
> **Obtained:** `gcal-mcp auth` (`gcal_mcp/auth.py:242`) runs the authorization-code flow
> with PKCE (S256). It binds a loopback listener on `127.0.0.1` at a random free port,
> opens the browser to Google's consent screen with `access_type=offline` and
> `prompt=consent` so a refresh token is issued, checks the returned `state`, and
> exchanges the code plus the PKCE verifier at Google's token endpoint. It then verifies
> that every requested scope was actually granted (`:304`), since Google's consent screen
> lets the user untick individual scopes.
>
> **Cached:** the access token, refresh token and expiry go to
> `~/.config/gcal-mcp/token.json`, written atomically with mode 0600 (`:106`). The path is
> outside the repo by default.
>
> **Refreshed:** before each API call, `TokenManager.get_access_token` (`:150`) re-reads
> the cache; if the access token is within 60 seconds of expiry it exchanges the refresh
> token for a new one and rewrites the cache, with no user involvement. An `asyncio.Lock`
> keeps concurrent tool calls from racing on the refresh. If Google returns 401 on a token
> that still looked fresh, the client forces one refresh and retries once
> (`gcal_mcp/client.py:115`).

**Scopes requested**, and why each is necessary:
> Two scopes (`gcal_mcp/auth.py:37`):
>
> - `calendar.calendarlist.readonly`: needed by `list_calendars` to read the list of
>   calendars and their time zones. Read-only, and it grants nothing about events.
> - `calendar.events`: needed by `search_events` and `get_event` to read events, and by
>   `create_event` and `delete_event` to write them. A read-only event scope would not
>   cover the two write tools.
>
> Deliberately not requested: the full `calendar` scope, which would also allow changing
> calendar settings, sharing/ACLs, and creating or deleting whole calendars. No tool
> needs any of that.

**Secrets**: what's in env, what's gitignored:
> `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are read from the environment
> (`gcal_mcp/auth.py:56`), optionally loaded from `week2/.env`. Nothing secret is in the
> source. Committed: `.env.example` and `.mcp.json.example`, both with placeholders only.
> Gitignored: `.env`, `.mcp.json` (repo root and `week2/`), and `token.json`. The token
> cache lives in `~/.config/gcal-mcp/` by default, so it is outside the repo regardless.

**Token dies mid-session**: what the agent sees:
> The server never opens a browser: the interactive flow is only reachable from the
> `gcal-mcp auth` CLI command, and the test fixture makes `webbrowser.open` raise to
> prove it. When the refresh token is rejected (revoked, or expired after 7 days for an
> OAuth app in Testing mode), the tool call returns:
>
> ```json
> {"error": {"code": "auth_required",
>            "message": "Google rejected the refresh token (invalid_grant); it was revoked or expired.",
>            "retryable": false,
>            "hint": "Ask the user to run `uv run --directory week2 gcal-mcp auth` in a terminal from the repo root. It opens a browser to sign in. The server picks up the new token on the next tool call; no restart is needed. Do not retry until they confirm."}}
> ```
>
> Because the server re-reads the cache on every call, signing in again from a terminal
> fixes the running session without restarting it
> (`tests/test_protocol.py::test_server_picks_up_a_token_written_by_another_process`).


## Part IV: Integration

**Registration config** (`.mcp.json.example`) and the client you used:
> Claude Code. `week2/.mcp.json.example` is copied to `.mcp.json` at the repo root
> (gitignored) and registers the server as `gcal`:
>
> ```json
> {
>   "mcpServers": {
>     "gcal": {
>       "type": "stdio",
>       "command": "uv",
>       "args": ["run", "--directory", "week2", "gcal-mcp"]
>     }
>   }
> }
> ```
>
> The OAuth client ID and secret are not in this file; the server loads them from
> `week2/.env`.

**End-to-end transcript**: the prompt, the tools that fired with their arguments, the result:

Real session against my Google account, run headless (`claude -p --mcp-config .mcp.json`)
with the five `gcal` tools pre-approved. Two turns of one session. Results are trimmed
to the fields that matter; long calendar IDs are shortened.
```
USER: Block 30 minutes on my main calendar tomorrow (Mon Oct 5) at 3pm called
      'CS146S week 2 MCP test'. No guests.

CALL  list_calendars {}
  ->  {"calendars": [{"id": "zubincarvalho2@gmail.com", "name": "Zubin", "primary": true,
        "access_role": "owner", "time_zone": "America/Los_Angeles"},
        {"id": "50ce47...@group.calendar.google.com", "name": "engr108", ...},
        ... 3 more]}

CALL  create_event {"summary": "CS146S week 2 MCP test",
        "start": "2026-10-05T15:00:00-07:00", "end": "2026-10-05T15:30:00-07:00",
        "calendar_id": "primary", "dry_run": true}
  ->  {"committed": false, "event": null,
       "preview": {"calendar_id": "primary", "summary": "CS146S week 2 MCP test",
         "start": "2026-10-05T15:00:00-07:00", "end": "2026-10-05T15:30:00-07:00",
         "attendees": [], "notify_attendees": false},
       "conflicts": [],
       "next_step": "Nothing was created. Show the user this preview, then call
         create_event again with the same arguments and dry_run=false."}

CALL  create_event {... same arguments ..., "dry_run": false}
  ->  {"committed": true,
       "event": {"id": "e8qbhs6psmirg3e5saaf5s2vcg", "calendar_id": "primary",
         "summary": "CS146S week 2 MCP test", "start": "2026-10-05T15:00:00-07:00",
         "end": "2026-10-05T15:30:00-07:00", "organizer": "zubincarvalho2@gmail.com",
         "attendees": [], "attendee_count": 0, ...},
       "next_step": "Created. Its event_id is e8qbhs6psmirg3e5saaf5s2vcg;
         delete_event can undo this."}

AGENT: "CS146S week 2 MCP test" is on your main calendar for Mon Oct 5, 3:00-3:30pm
       Pacific, with no guests. The preview showed no conflicts on that calendar; I
       didn't check your class calendars (engr108, cs221, cs146s, ms&e203) for overlaps.

USER: Thanks. Who's the organizer on it, and can you get rid of it now? It was only a test.

CALL  delete_event {"calendar_id": "primary",
        "event_id": "e8qbhs6psmirg3e5saaf5s2vcg", "dry_run": true}
  ->  {"committed": false,
       "event": {"id": "e8qbhs6psmirg3e5saaf5s2vcg", "summary": "CS146S week 2 MCP test",
         "organizer": "zubincarvalho2@gmail.com", "attendee_count": 0, ...},
       "next_step": "Nothing was deleted. Confirm with the user that this is the right
         event, then call delete_event again with dry_run=false."}

CALL  delete_event {"calendar_id": "primary",
        "event_id": "e8qbhs6psmirg3e5saaf5s2vcg", "dry_run": false}
  ->  {"committed": true, "event": {...}, "next_step": "Deleted. This event_id is no
       longer valid."}

AGENT: The organizer was you, zubincarvalho2@gmail.com. The event is now deleted, and
       since it had no guests, nobody was notified.
```
The chain: `list_calendars` supplied the time zone used to turn "tomorrow at 3pm" into
`15:00:00-07:00`; the `id` returned by the committed `create_event` was the `event_id`
passed to `delete_event`; and the delete preview's full event answered the organizer
question without a separate `get_event`. A later search of that day returned no events,
confirming the delete.

One honest limit: in both turns the agent previewed and then committed without pausing
for me, because the request was explicit and the tools were pre-approved in headless
mode. `dry_run` guarantees a preview happens and is visible; it does not by itself force
a human confirmation.

**A failure, handled**: what you provoked, what the agent saw, what it did next:

Provoked with an event ID that was never returned by `search_events` (real session,
read-only tools only).
```
USER: Pull up the details of calendar event id 'standup-2026-10-05' on my primary
      calendar. If that doesn't work, figure out what I probably meant.

CALL  get_event {"calendar_id": "primary", "event_id": "standup-2026-10-05"}
  ->  isError: true
      {"error": {"code": "not_found", "message": "Not Found", "retryable": false,
        "hint": "event_id must be an `id` returned by search_events for this same
          calendar_id. Do not guess or construct IDs; call search_events again to get
          a current one. calendar_id must be \"primary\" or an `id` returned by
          list_calendars."}}

CALL  search_events {"calendar_id": "primary", "query": "standup",
        "time_min": "2026-10-04T00:00:00Z", "time_max": "2026-10-07T00:00:00Z",
        "max_results": 25}
  ->  {"events": [], "next_page_token": null}

CALL  search_events {"calendar_id": "primary",
        "time_min": "2026-10-04T00:00:00Z", "time_max": "2026-10-07T00:00:00Z",
        "max_results": 50}
  ->  {"events": [], "next_page_token": null}

... wider searches ("stand-up", a six-month window), then list_calendars and a
    search of each other calendar; nothing resembling a standup anywhere ...

AGENT: There is no event with id `standup-2026-10-05` on your primary calendar, and I
       couldn't find a real event it could plausibly refer to. The lookup returned
       `not_found`, and the ID doesn't look like one Google would generate. [...]
```
What the error design did: `retryable: false` meant the agent never repeated the
`get_event` call or tried variations of the ID, and the hint named the tool to use
instead, so its very next call was `search_events`. It ended with a correct "this does
not exist" rather than a stalled turn or an invented event.

**Protocol-level test**: what it covers and how to run it:
> `uv run --directory week2 pytest` (15 tests, no Google account needed).
>
> - `tests/test_stdio.py` launches the real server as a subprocess and speaks MCP to it
>   over stdio, the way a host does: `initialize` (checks the instructions arrive),
>   `tools/list` (all five tools), and a `tools/call` that must come back as a structured
>   `auth_required` error because no token is cached.
> - `tests/test_protocol.py` drives the server through an MCP client session with only
>   the outbound HTTP to Google replaced by a fake. It covers: the advertised schemas and
>   annotations; `search_events` output feeding `get_event`; output shaping; both dry-run
>   brakes (no POST or DELETE reaches Google until `dry_run=false`); 404 as don't-retry
>   and 429 / 403-rate-limit as retry; silent refresh of an expired token and re-caching;
>   a forced refresh after an early 401; and the dead-refresh-token path.


## Submission
1. `Command (⌘) + F` for `TODO`. No results means you're done.
2. Confirm no tokens, client secrets, cached token file, or real `.mcp.json` are committed.
3. Push all changes to your remote repository and submit via Gradescope.
4. Clean up (optional): remove the server from your agent config, delete your cached token, and revoke the OAuth app's access.
