"""Structured errors: every failure an agent can see is one of these, never a traceback."""

from __future__ import annotations

import json
from typing import Literal

ErrorCode = Literal[
    "auth_required",
    "not_configured",
    "invalid_request",
    "forbidden",
    "not_found",
    "gone",
    "rate_limited",
    "upstream_unavailable",
    "network_error",
    "internal_error",
]

AUTH_COMMAND = "uv run --directory week2 gcal-mcp auth"


class CalendarError(Exception):
    """A failure the agent can act on.

    `retryable` is the field the agent branches on: True means the same call may
    succeed later (rate limit, 5xx, network), False means the arguments or the
    account state have to change first (bad ID, missing auth, no permission).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool,
        retry_after_s: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.hint = hint

    def to_payload(self) -> dict:
        error: dict = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_s is not None:
            error["retry_after_s"] = self.retry_after_s
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}

    def to_json(self) -> str:
        return json.dumps(self.to_payload())


def auth_required(reason: str) -> CalendarError:
    """The cached token is missing or dead. Only a human in a terminal can fix that."""
    return CalendarError(
        "auth_required",
        reason,
        retryable=False,
        hint=(
            f"Ask the user to run `{AUTH_COMMAND}` in a terminal from the repo root. "
            "It opens a browser to sign in. The server picks up the new token on the "
            "next tool call; no restart is needed. Do not retry until they confirm."
        ),
    )
