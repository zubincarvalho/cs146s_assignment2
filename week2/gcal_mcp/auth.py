"""Google OAuth 2.0: authorization-code + PKCE login, token cache, silent refresh.

Two entry points with deliberately different powers:

* `run_login_flow` is interactive. It opens a browser and listens on a loopback
  port. Only the `gcal-mcp auth` CLI command calls it.
* `TokenManager.get_access_token` is what the running server uses. It reads the
  cache and refreshes silently, and when that fails it raises `auth_required`
  instead of ever opening a browser from inside a tool call.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .errors import CalendarError, auth_required

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Each scope maps to a tool; see writeup.md Part III for the justification.
SCOPES = (
    # list_calendars: read the calendar list, nothing else about the calendars.
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    # search_events / get_event / create_event / delete_event: events only,
    # no access to calendar settings, ACLs, or sharing.
    "https://www.googleapis.com/auth/calendar.events",
)

# Refresh this many seconds before the stated expiry so a token never dies in flight.
EXPIRY_SKEW_S = 60
LOGIN_TIMEOUT_S = 300


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> OAuthConfig:
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise CalendarError(
                "not_configured",
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set.",
                retryable=False,
                hint=(
                    "Ask the user to copy week2/.env.example to week2/.env and fill in "
                    "the OAuth client from Google Cloud Console (see week2/README.md)."
                ),
            )
        return cls(client_id=client_id, client_secret=client_secret)


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""

    def is_fresh(self, now: float | None = None) -> bool:
        return (now or time.time()) < self.expires_at - EXPIRY_SKEW_S


def default_token_path() -> Path:
    """Outside the repo by default, so the cache cannot be committed by accident."""
    override = os.environ.get("GCAL_MCP_TOKEN_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "gcal-mcp" / "token.json"


class TokenStore:
    """The on-disk token cache: one JSON file, mode 0600, written atomically."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_token_path()

    def load(self) -> Token | None:
        try:
            return Token(**json.loads(self.path.read_text()))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            # Unreadable or corrupt cache is the same as no cache: re-auth fixes it.
            return None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".token-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(token), f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


def _token_from_response(body: dict, *, previous_refresh_token: str = "") -> Token:
    return Token(
        access_token=body["access_token"],
        # Google omits refresh_token on refresh responses; keep the one we have.
        refresh_token=body.get("refresh_token") or previous_refresh_token,
        expires_at=time.time() + int(body.get("expires_in", 3600)),
        scope=body.get("scope", ""),
    )


class TokenManager:
    """Hands the server a valid access token, refreshing silently when needed."""

    def __init__(
        self,
        store: TokenStore | None = None,
        http: httpx.AsyncClient | None = None,
        config: OAuthConfig | None = None,
    ) -> None:
        self.store = store or TokenStore()
        self._http = http
        self._config = config
        self._lock = asyncio.Lock()

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token or raise `auth_required`. Never interactive."""
        # One refresh at a time: concurrent tool calls must not race to spend the
        # same refresh token or interleave writes to the cache file.
        async with self._lock:
            # Re-read the cache on every call so a token written by `gcal-mcp auth`
            # in another process is picked up without restarting the server.
            token = self.store.load()
            if token is None:
                raise auth_required("No cached Google token: the user has not signed in yet.")
            if token.is_fresh() and not force_refresh:
                return token.access_token
            if not token.refresh_token:
                raise auth_required("The cached Google token expired and has no refresh token.")
            refreshed = await self._refresh(token)
            self.store.save(refreshed)
            return refreshed.access_token

    async def _refresh(self, token: Token) -> Token:
        config = self._config or OAuthConfig.from_env()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }
        try:
            if self._http is not None:
                resp = await self._http.post(TOKEN_URL, data=data)
            else:
                async with httpx.AsyncClient(timeout=15) as http:
                    resp = await http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise CalendarError(
                "network_error",
                f"Could not reach Google to refresh the token ({type(exc).__name__}).",
                retryable=True,
                retry_after_s=5,
            ) from exc

        if resp.status_code == 200:
            return _token_from_response(resp.json(), previous_refresh_token=token.refresh_token)
        if resp.status_code >= 500:
            raise CalendarError(
                "upstream_unavailable",
                f"Google's token endpoint returned HTTP {resp.status_code}.",
                retryable=True,
                retry_after_s=10,
            )
        # 400/401 here means invalid_grant: the refresh token was revoked, expired
        # (7 days for an OAuth app in "Testing" mode), or the client changed.
        try:
            reason = resp.json().get("error", "unknown_error")
        except ValueError:
            reason = "unknown_error"
        raise auth_required(f"Google rejected the refresh token ({reason}); it was revoked or expired.")


# --- Interactive login (CLI only) -------------------------------------------------


class LoginError(Exception):
    pass


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        if "code" not in params and "error" not in params:
            # Favicon and other stray requests: keep waiting for the real redirect.
            self.send_response(404)
            self.end_headers()
            return
        self.server.oauth_params = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]
        ok = "code" in params
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        text = "Signed in. You can close this tab." if ok else "Sign-in failed. Check the terminal."
        self.wfile.write(f"<html><body><h3>gcal-mcp</h3><p>{text}</p></body></html>".encode())

    def log_message(self, *args) -> None:
        pass


def run_login_flow(config: OAuthConfig, store: TokenStore, *, open_browser: bool = True) -> Token:
    """Authorization-code + PKCE against a loopback redirect. Blocks until done."""
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.oauth_params = None  # type: ignore[attr-defined]
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()

    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # offline + consent is what makes Google issue a refresh token.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    print(f"Open this URL to sign in to Google:\n\n  {url}\n")
    if open_browser:
        webbrowser.open(url)

    deadline = time.time() + LOGIN_TIMEOUT_S
    try:
        while server.oauth_params is None:  # type: ignore[attr-defined]
            remaining = deadline - time.time()
            if remaining <= 0:
                raise LoginError(f"Timed out after {LOGIN_TIMEOUT_S}s waiting for the redirect.")
            server.timeout = min(remaining, 1.0)
            server.handle_request()
        params: dict = server.oauth_params  # type: ignore[attr-defined]
    finally:
        server.server_close()

    if "error" in params:
        raise LoginError(f"Google returned an error: {params['error']}")
    if not secrets.compare_digest(params.get("state", ""), state):
        raise LoginError("State mismatch on the redirect; refusing the authorization code.")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise LoginError(f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}")

    token = _token_from_response(resp.json())
    if not token.refresh_token:
        raise LoginError("Google did not return a refresh token; cannot keep the session alive.")
    # Google's consent screen lets the user untick individual scopes.
    missing = set(SCOPES) - set(token.scope.split())
    if missing:
        raise LoginError(
            "These permissions were not granted, so some tools would fail: "
            + ", ".join(sorted(missing))
            + ". Run the command again and leave every box ticked."
        )
    store.save(token)
    return token


def revoke_and_forget(store: TokenStore) -> str:
    """Revoke the grant at Google and delete the local cache."""
    token = store.load()
    if token is None:
        return "No cached token."
    revoked = False
    try:
        resp = httpx.post(
            REVOKE_URL, data={"token": token.refresh_token or token.access_token}, timeout=15
        )
        revoked = resp.status_code == 200
    except httpx.HTTPError:
        pass
    store.delete()
    if revoked:
        return f"Revoked the grant at Google and deleted {store.path}."
    return (
        f"Deleted {store.path}, but could not revoke at Google. "
        "Remove the app at https://myaccount.google.com/connections."
    )
