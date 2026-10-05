"""Thin async client for the Google Calendar REST API.

Its one job beyond HTTP is translating every way a request can fail into a
`CalendarError` whose `retryable` flag is correct.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .auth import TokenManager
from .errors import CalendarError, auth_required

API_BASE = "https://www.googleapis.com/calendar/v3"

RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
DEFAULT_RATE_LIMIT_WAIT_S = 30


def _google_error(resp: httpx.Response) -> tuple[str, str]:
    """Pull (reason, message) out of Google's error envelope, tolerating junk."""
    try:
        err = resp.json().get("error", {})
        if not isinstance(err, dict):
            return "", str(err)
        reasons = err.get("errors") or [{}]
        return reasons[0].get("reason", ""), err.get("message", "")
    except (ValueError, AttributeError, IndexError):
        return "", resp.text[:200]


def _retry_after(resp: httpx.Response) -> int:
    try:
        return max(1, int(resp.headers.get("Retry-After", "")))
    except ValueError:
        return DEFAULT_RATE_LIMIT_WAIT_S


def _error_for(resp: httpx.Response, not_found_hint: str | None) -> CalendarError:
    status = resp.status_code
    reason, message = _google_error(resp)
    detail = message or f"HTTP {status}"

    if status == 429 or (status == 403 and reason in RATE_LIMIT_REASONS):
        wait = _retry_after(resp)
        return CalendarError(
            "rate_limited",
            f"Google Calendar rate limit hit; retry after {wait}s.",
            retryable=True,
            retry_after_s=wait,
        )
    if status == 403:
        if reason in {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
            return auth_required("The cached token lacks a scope this tool needs.")
        return CalendarError(
            "forbidden",
            f"Google refused the request: {detail}",
            retryable=False,
            hint=(
                "The signed-in user lacks permission on this calendar. Writing needs "
                "access_role 'writer' or 'owner'; check it with list_calendars."
            ),
        )
    if status == 404:
        return CalendarError("not_found", detail, retryable=False, hint=not_found_hint)
    if status == 410:
        return CalendarError(
            "gone",
            "That event has already been deleted.",
            retryable=False,
            hint="Nothing left to do for this event_id; call search_events for current events.",
        )
    if status >= 500:
        return CalendarError(
            "upstream_unavailable",
            f"Google Calendar returned HTTP {status}.",
            retryable=True,
            retry_after_s=10,
        )
    return CalendarError(
        "invalid_request",
        f"Google rejected the request: {detail}",
        retryable=False,
        hint="Fix the arguments before calling again; the same call will fail the same way.",
    )


class CalendarClient:
    def __init__(self, tokens: TokenManager, http: httpx.AsyncClient | None = None) -> None:
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(timeout=20)

    @staticmethod
    def calendar_path(calendar_id: str) -> str:
        # Calendar IDs are often email-shaped or contain '#'; they must be escaped.
        return f"/calendars/{quote(calendar_id, safe='')}"

    @classmethod
    def event_path(cls, calendar_id: str, event_id: str) -> str:
        return f"{cls.calendar_path(calendar_id)}/events/{quote(event_id, safe='')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        not_found_hint: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._send(method, path, params, json, force_refresh=False)
        if resp.status_code == 401:
            # The access token died early (revoked, clock skew). One silent refresh,
            # then give up with an actionable error rather than looping.
            resp = await self._send(method, path, params, json, force_refresh=True)
            if resp.status_code == 401:
                raise auth_required("Google rejected the access token even after a refresh.")
        if resp.status_code >= 400:
            raise _error_for(resp, not_found_hint)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        *,
        force_refresh: bool,
    ) -> httpx.Response:
        access_token = await self._tokens.get_access_token(force_refresh=force_refresh)
        try:
            return await self._http.request(
                method,
                API_BASE + path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.TimeoutException as exc:
            raise CalendarError(
                "network_error",
                "Timed out talking to Google Calendar.",
                retryable=True,
                retry_after_s=5,
            ) from exc
        except httpx.HTTPError as exc:
            raise CalendarError(
                "network_error",
                f"Network error talking to Google Calendar ({type(exc).__name__}).",
                retryable=True,
                retry_after_s=5,
            ) from exc
