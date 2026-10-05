# gcal-mcp

An MCP server that exposes Google Calendar to a coding agent: list calendars, search
events, read one event, create an event, delete an event. Runs over stdio.

## Run it

From the repo root:

```bash
uv run --directory week2 gcal-mcp
```

That starts the server on stdio. An MCP host (Claude Code, Cursor) runs this command
for you once the server is registered; see step 3.

## One-time setup

### 1. Create a Google OAuth client

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. **APIs & Services > Library**: enable **Google Calendar API**.
3. **APIs & Services > OAuth consent screen**: user type **External**, and add your own
   Google account under **Test users**.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**, application
   type **Desktop app**.
5. Copy the client ID and secret into `week2/.env`:

```bash
cp week2/.env.example week2/.env   # then edit it
```

`week2/.env` is gitignored. Real environment variables override it.

### 2. Sign in

```bash
uv run --directory week2 gcal-mcp auth
```

This opens a browser, runs the authorization-code flow with PKCE against a loopback
redirect, and caches the token at `~/.config/gcal-mcp/token.json` (mode 0600, outside
the repo). After that the server refreshes the access token on its own.

### 3. Register with Claude Code

```bash
cp week2/.mcp.json.example .mcp.json   # at the repo root; gitignored
```

Start `claude` from the repo root and approve the `gcal` server. `/mcp` shows its status.

## Commands

| Command | What it does |
|---|---|
| `gcal-mcp` | Serve over stdio (default) |
| `gcal-mcp auth` | Sign in to Google and cache the token |
| `gcal-mcp status` | Show whether a token is cached and its scopes |
| `gcal-mcp logout` | Revoke the grant at Google and delete the cached token |

Prefix each with `uv run --directory week2`.

## Tests

```bash
uv run --directory week2 pytest
```

The tests need no Google account. They drive the server through an MCP client session
against a fake Google, plus one test that launches the real server as a stdio subprocess.

## Layout

| File | Role |
|---|---|
| `gcal_mcp/server.py` | The five tools, their schemas, output shapes, and instructions |
| `gcal_mcp/auth.py` | OAuth login flow, token cache, silent refresh |
| `gcal_mcp/client.py` | HTTP to Google and the mapping from HTTP failures to structured errors |
| `gcal_mcp/errors.py` | The error shape the agent sees |
| `gcal_mcp/__main__.py` | CLI entry point |
