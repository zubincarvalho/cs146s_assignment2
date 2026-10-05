"""Launch the server the way an MCP host does: as a subprocess speaking stdio."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

WEEK2 = Path(__file__).resolve().parent.parent


async def test_real_subprocess_serves_tools_and_fails_cleanly_when_signed_out(tmp_path):
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "gcal_mcp"],
        cwd=str(WEEK2),
        env={
            # No token at this path, so the tool call must report auth_required
            # without touching the network or the developer's real token cache.
            "GCAL_MCP_TOKEN_PATH": str(tmp_path / "missing.json"),
            "GOOGLE_CLIENT_ID": "test-client-id",
            "GOOGLE_CLIENT_SECRET": "test-client-secret",
        },
    )
    async with Client(transport) as client:
        assert "search_events" in client.instructions

        names = {t.name for t in await client.list_tools()}
        assert names == {
            "list_calendars", "search_events", "get_event", "create_event", "delete_event",
        }  # fmt: skip

        result = await client.call_tool("search_events", {}, raise_on_error=False)
        assert result.is_error
        error = json.loads(result.content[0].text)["error"]
        assert error["code"] == "auth_required"
        assert error["retryable"] is False
        assert "gcal-mcp auth" in error["hint"]
