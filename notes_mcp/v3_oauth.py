"""Stage 3 - remote MCP server with OAuth 2.1, scopes, and per-user data.

Three things changed versus ``v2_remote.py``:

1. The server is an OAuth 2.1 *resource server*. It publishes
   ``/.well-known/oauth-protected-resource``, rejects unauthenticated calls
   with a 401 that points at the authorization server, and verifies every
   bearer token against that server's JWKS.
2. Tools declare the scope they need. ``notes:read`` cannot delete.
3. Every query is filtered by the ``sub`` claim of the caller's token, so two
   users on the same deployment never see each other's notes.

The authorization server is pluggable:

* ``DESCOPE_CONFIG_URL``  -> Descope (hosted login, consent, DCR)
* ``OAUTH_ISSUER``        -> any OIDC/OAuth 2.1 provider (Auth0, WorkOS,
                             Keycloak, Clerk, Supabase, ...)
* neither, with ``ALLOW_DEV_AUTH=true`` -> the bundled dev authorization
                             server in ``notes_mcp/dev_auth_server.py``

Run it:
    uv run notes_mcp/v3_oauth.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider, require_scopes
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.exceptions import ToolError
from pydantic import Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notes_mcp import notes_db  # noqa: E402
from notes_mcp.notes_db import NoteNotFound  # noqa: E402

load_dotenv(ROOT / ".env")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
NOTES_READ = "notes:read"
NOTES_WRITE = "notes:write"
SCOPES = [NOTES_READ, NOTES_WRITE]

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
SERVER_URL = (os.getenv("MCP_SERVER_URL") or f"http://{HOST}:{PORT}/mcp").rstrip("/")
BASE_URL = SERVER_URL[: -len("/mcp")] if SERVER_URL.endswith("/mcp") else SERVER_URL

DESCOPE_CONFIG_URL = (os.getenv("DESCOPE_CONFIG_URL") or "").strip()
OAUTH_ISSUER = (os.getenv("OAUTH_ISSUER") or "").strip()
DEV_AUTH_URL = (os.getenv("DEV_AUTH_URL") or "http://127.0.0.1:9000").rstrip("/")
ALLOW_DEV_AUTH = os.getenv("ALLOW_DEV_AUTH", "false").lower() in ("1", "true", "yes")
VERIFY_AUDIENCE = os.getenv("OAUTH_VERIFY_AUDIENCE", "true").lower() not in (
    "0",
    "false",
    "no",
)


def _strip_wellknown(url: str) -> str:
    """Accept a full discovery URL or a bare issuer; always return the issuer."""
    url = url.strip().rstrip("/")
    for marker in (
        "/.well-known/openid-configuration",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        if url.endswith(marker):
            return url[: -len(marker)]
    return url


def build_auth():
    """Pick an authorization server and wire up token verification.

    The MCP server itself never learns a password. It only ever validates a
    signature, an issuer, an audience, an expiry, and a scope list - which is
    exactly why the provider is swappable in a few lines of config.
    """
    audience = os.getenv("OAUTH_AUDIENCE") or SERVER_URL

    # 1. Descope - hosted login + consent + dynamic client registration.
    if DESCOPE_CONFIG_URL:
        from fastmcp.server.auth.providers.descope import DescopeProvider

        print(f"[auth] Descope  <- {DESCOPE_CONFIG_URL}", flush=True)
        return DescopeProvider(
            config_url=DESCOPE_CONFIG_URL,
            base_url=BASE_URL,
            resource_base_url=BASE_URL,
            required_scopes=[],          # enforced per-tool instead of globally
            scopes_supported=SCOPES,     # advertised to clients during consent
            resource_name=os.getenv("MCP_SERVER_NAME", "Notes MCP"),
        )

    # 2. Any other OIDC / OAuth 2.1 provider.
    issuer = _strip_wellknown(OAUTH_ISSUER) if OAUTH_ISSUER else ""
    if not issuer and ALLOW_DEV_AUTH:
        issuer = DEV_AUTH_URL
        print(
            "[auth] DEV authorization server - do not use in production",
            flush=True,
        )

    if not issuer:
        raise SystemExit(
            "No authorization server configured.\n"
            "  Set DESCOPE_CONFIG_URL, or OAUTH_ISSUER, or ALLOW_DEV_AUTH=true.\n"
            "  See .env.example."
        )

    print(f"[auth] issuer  <- {issuer}", flush=True)
    verifier = JWTVerifier(
        jwks_uri=f"{issuer}/.well-known/jwks.json",
        issuer=issuer,
        audience=audience if VERIFY_AUDIENCE else None,
        required_scopes=[],  # per-tool enforcement, see below
        base_url=BASE_URL,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[issuer],  # type: ignore[list-item]
        base_url=BASE_URL,
        resource_base_url=BASE_URL,
        scopes_supported=SCOPES,
        resource_name=os.getenv("MCP_SERVER_NAME", "Notes MCP"),
    )


mcp = FastMCP(
    name=os.getenv("MCP_SERVER_NAME", "notes"),
    instructions=(
        "A multi-tenant notes store. Notes are private to the signed-in user. "
        "Reading requires the notes:read scope; adding or deleting requires "
        "notes:write. Call whoami if you need to tell the user which identity "
        "they are acting as."
    ),
    auth=build_auth(),
)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def current_principal() -> dict[str, Any]:
    """Distil the verified token into the caller's identity.

    ``sub`` is the only claim that matters for data isolation: it is the
    stable, provider-issued user id. Never key rows on an email address - users
    change those, and some providers let them be unverified.
    """
    token = get_access_token()
    if token is None:
        # Should be unreachable: the transport rejects unauthenticated calls.
        raise ToolError("not authenticated")

    claims = getattr(token, "claims", None) or {}
    user_id = (
        getattr(token, "subject", None)
        or claims.get("sub")
        or getattr(token, "client_id", None)
    )
    if not user_id:
        raise ToolError("token carries no subject - cannot scope data to a user")

    return {
        "user_id": str(user_id),
        "client_id": getattr(token, "client_id", None),
        "scopes": sorted(getattr(token, "scopes", []) or []),
        "email": claims.get("email"),
        "name": claims.get("name") or claims.get("given_name"),
        "expires_at": getattr(token, "expires_at", None),
    }


def require_scope(scope: str) -> dict[str, Any]:
    """Return the caller, or raise if their token lacks ``scope``.

    Belt-and-braces: ``require_scopes()`` on the decorator already hides the
    tool from under-privileged callers, but a second check inside the body
    means the data access itself is never reachable without the grant.
    """
    principal = current_principal()
    if scope not in principal["scopes"]:
        raise ToolError(
            f"missing required scope '{scope}'. Your token grants: "
            f"{', '.join(principal['scopes']) or '(none)'}"
        )
    return principal


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool
def whoami() -> dict[str, Any]:
    """Report the identity, client, and scopes of the current caller.

    Useful for debugging an authorization problem: it answers "who does the
    server think I am?" without touching any data.
    """
    principal = current_principal()
    return {
        **principal,
        "resource": SERVER_URL,
        "storage_backend": notes_db.backend_name(),
        "notes_owned": notes_db.count_notes(principal["user_id"]),
    }


@mcp.tool(auth=require_scopes(NOTES_READ))
def list_notes(
    limit: Annotated[int, Field(ge=1, le=500, description="Max notes to return")] = 50,
) -> dict[str, Any]:
    """List the signed-in user's notes, newest first.

    Only ever returns notes owned by the caller. Requires scope: notes:read.
    """
    principal = require_scope(NOTES_READ)
    notes = notes_db.list_notes(principal["user_id"], limit=limit)
    return {"count": len(notes), "notes": notes, "owner": principal["user_id"]}


@mcp.tool(auth=require_scopes(NOTES_READ))
def get_note(
    note_id: Annotated[int, Field(ge=1, description="The id returned by list_notes")],
) -> dict[str, Any]:
    """Fetch one of the signed-in user's notes by id. Requires notes:read."""
    principal = require_scope(NOTES_READ)
    try:
        return {"ok": True, "note": notes_db.get_note(note_id, principal["user_id"])}
    except NoteNotFound:
        # Deliberately indistinguishable from "exists but belongs to someone
        # else" - do not leak the existence of other users' rows.
        return {"ok": False, "error": f"no note with id {note_id}"}


@mcp.tool(auth=require_scopes(NOTES_WRITE))
def add_note(
    content: Annotated[
        str, Field(min_length=1, max_length=10_000, description="The note text")
    ],
) -> dict[str, Any]:
    """Save a new note for the signed-in user. Requires scope: notes:write."""
    principal = require_scope(NOTES_WRITE)
    try:
        note = notes_db.add_note(content, principal["user_id"])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "note": note}


@mcp.tool(auth=require_scopes(NOTES_WRITE))
def delete_note(
    note_id: Annotated[int, Field(ge=1, description="The id returned by list_notes")],
) -> dict[str, Any]:
    """Permanently delete one of the signed-in user's notes.

    Destructive. Confirm the id with the user first. Requires notes:write, and
    silently refuses ids the caller does not own.
    """
    principal = require_scope(NOTES_WRITE)
    if not notes_db.delete_note(note_id, principal["user_id"]):
        return {"ok": False, "error": f"no note with id {note_id}"}
    return {"ok": True, "deleted_id": note_id}


if __name__ == "__main__":
    notes_db.init_db()
    print(f"[mcp ] resource  = {SERVER_URL}", flush=True)
    print(f"[mcp ] metadata  = {BASE_URL}/.well-known/oauth-protected-resource", flush=True)
    print(f"[mcp ] scopes    = {', '.join(SCOPES)}", flush=True)
    print(f"[mcp ] storage   = {notes_db.backend_name()}", flush=True)
    mcp.run(transport="http", host=HOST, port=PORT)
