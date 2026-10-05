"""Entry point: `gcal-mcp` serves over stdio; `gcal-mcp auth` signs in to Google."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# week2/.env holds the OAuth client; real environment variables take precedence.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .auth import (
    LoginError,
    OAuthConfig,
    TokenStore,
    revoke_and_forget,
    run_login_flow,
)
from .errors import CalendarError


def _auth(args: argparse.Namespace) -> int:
    try:
        config = OAuthConfig.from_env()
        token = run_login_flow(config, TokenStore(), open_browser=not args.no_browser)
    except CalendarError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        print("Copy week2/.env.example to week2/.env and fill it in (see week2/README.md).", file=sys.stderr)
        return 1
    except LoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    minutes = int((token.expires_at - time.time()) / 60)
    print(f"Signed in. Token cached at {TokenStore().path} (access token good for ~{minutes} min,")
    print("then refreshed automatically).")
    return 0


def _status(_: argparse.Namespace) -> int:
    store = TokenStore()
    token = store.load()
    if token is None:
        print(f"Not signed in (no token at {store.path}). Run: gcal-mcp auth")
        return 1
    remaining = int(token.expires_at - time.time())
    state = f"valid for {remaining}s" if remaining > 0 else "expired; will refresh on next call"
    print(f"Token at {store.path}: access token {state}.")
    print("Scopes: " + ", ".join(token.scope.split()))
    return 0


def _logout(_: argparse.Namespace) -> int:
    print(revoke_and_forget(TokenStore()))
    return 0


def _serve(_: argparse.Namespace) -> int:
    from .server import create_server

    # stdio transport: stdout belongs to the protocol, so nothing here may print to it.
    create_server().run(transport="stdio", show_banner=False)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="gcal-mcp", description=__doc__)
    parser.set_defaults(func=_serve)
    sub = parser.add_subparsers(title="commands")
    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(func=_serve)
    auth = sub.add_parser("auth", help="sign in to Google in a browser and cache the token")
    auth.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    auth.set_defaults(func=_auth)
    sub.add_parser("status", help="show whether a token is cached").set_defaults(func=_status)
    sub.add_parser("logout", help="revoke the grant and delete the cached token").set_defaults(
        func=_logout
    )
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
