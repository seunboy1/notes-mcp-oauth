"""A minimal OAuth 2.1 + PKCE authorization server, for local development only.

This exists so you can exercise the *entire* flow - discovery, dynamic client
registration, authorize, consent, code exchange, JWKS, refresh - on your laptop
without signing up for anything. It implements the endpoints the MCP spec
requires and nothing else:

    GET  /.well-known/oauth-authorization-server   discovery (RFC 8414)
    GET  /.well-known/openid-configuration         discovery (OIDC)
    GET  /.well-known/jwks.json                    public keys
    POST /register                                 dynamic client registration (RFC 7591)
    GET  /authorize                                login + consent screen
    POST /authorize/consent                        the user's decision
    POST /token                                    code -> token, refresh -> token
    POST /revoke                                   token revocation

It is NOT production software. Keys live in memory, "login" is a name in a
text box, and there is no rate limiting, no storage, and no audit log. In
production you delegate this entire file to Descope, Auth0, WorkOS, Keycloak,
or similar - which is the point the article makes.

Run it:
    uv run notes_mcp/dev_auth_server.py     # -> http://127.0.0.1:9000
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

HOST = os.getenv("DEV_AUTH_HOST", "127.0.0.1")
PORT = int(os.getenv("DEV_AUTH_PORT", "9000"))
ISSUER = (os.getenv("DEV_AUTH_URL") or f"http://{HOST}:{PORT}").rstrip("/")

SUPPORTED_SCOPES = ["notes:read", "notes:write", "openid", "profile", "email", "offline_access"]
ACCESS_TTL = int(os.getenv("DEV_AUTH_ACCESS_TTL", "3600"))
REFRESH_TTL = int(os.getenv("DEV_AUTH_REFRESH_TTL", "2592000"))

# --------------------------------------------------------------------------- #
# Signing key (ephemeral - regenerated on every boot)
# --------------------------------------------------------------------------- #
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_kid = secrets.token_hex(8)
_private_pem = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwks() -> dict[str, Any]:
    numbers = _key.public_key().public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _kid,
                "n": _b64u(n),
                "e": _b64u(e),
            }
        ]
    }


# --------------------------------------------------------------------------- #
# In-memory state
# --------------------------------------------------------------------------- #
@dataclass
class Client:
    client_id: str
    client_name: str
    redirect_uris: list[str]
    client_secret: str | None = None


@dataclass
class PendingAuth:
    client_id: str
    redirect_uri: str
    state: str | None
    code_challenge: str
    code_challenge_method: str
    scopes: list[str]
    resource: str | None


@dataclass
class AuthCode(PendingAuth):
    subject: str = ""
    email: str = ""
    created_at: float = field(default_factory=time.time)


CLIENTS: dict[str, Client] = {}
PENDING: dict[str, PendingAuth] = {}
CODES: dict[str, AuthCode] = {}
REFRESH: dict[str, dict[str, Any]] = {}
REVOKED: set[str] = set()


def _err(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _metadata() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "revocation_endpoint": f"{ISSUER}/revoke",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "scopes_supported": SUPPORTED_SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "subject_types_supported": ["public"],
    }


async def metadata(request: Request) -> Response:
    return JSONResponse(_metadata())


async def jwks(request: Request) -> Response:
    return JSONResponse(_jwks())


# --------------------------------------------------------------------------- #
# Dynamic client registration (RFC 7591)
# --------------------------------------------------------------------------- #
async def register(request: Request) -> Response:
    """Let an MCP client enrol itself, with no human in the loop.

    This is what lets a user paste a URL into Claude or Cursor and just log in:
    the client registers, gets a client_id, and starts the code flow.
    """
    try:
        body = await request.json()
    except Exception:
        return _err("invalid_request", "body must be JSON")

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _err("invalid_redirect_uri", "redirect_uris is required")

    client = Client(
        client_id=f"dev_{uuid.uuid4().hex}",
        client_name=body.get("client_name") or "Unnamed MCP client",
        redirect_uris=[str(u) for u in redirect_uris],
    )
    CLIENTS[client.client_id] = client
    print(f"[dev-auth] registered client {client.client_name} ({client.client_id})")

    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(SUPPORTED_SCOPES),
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# Authorize + consent
# --------------------------------------------------------------------------- #
_CONSENT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in - Notes MCP (dev)</title>
<style>
 body{{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background:#0f1117;color:#e6e6e6;display:grid;place-items:center;
      min-height:100vh;margin:0}}
 .card{{background:#181b24;border:1px solid #262a36;border-radius:14px;
      padding:32px;width:min(440px,92vw)}}
 h1{{font-size:20px;margin:0 0 4px}}
 p.sub{{color:#9aa4b2;margin:0 0 24px;font-size:14px}}
 label{{display:block;font-size:13px;color:#9aa4b2;margin:16px 0 6px}}
 input[type=text]{{width:100%;padding:10px 12px;border-radius:8px;
      border:1px solid #2d3341;background:#0f1117;color:#e6e6e6;font-size:15px;
      box-sizing:border-box}}
 .scope{{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;
      border:1px solid #262a36;border-radius:8px;margin-bottom:8px}}
 .scope code{{color:#7dd3fc;font-size:13px}}
 .scope small{{display:block;color:#9aa4b2;font-size:12px}}
 .row{{display:flex;gap:10px;margin-top:24px}}
 button{{flex:1;padding:11px;border-radius:8px;border:0;font-size:15px;
      font-weight:600;cursor:pointer}}
 .allow{{background:#2563eb;color:#fff}}
 .deny{{background:#242835;color:#e6e6e6}}
 .warn{{margin-top:20px;font-size:12px;color:#f59e0b}}
 .client{{background:#0f1117;border:1px solid #262a36;border-radius:8px;
      padding:10px 12px;font-size:13px;color:#9aa4b2}}
</style></head><body>
<div class="card">
  <h1>Authorize access</h1>
  <p class="sub">A client wants to act on your behalf in <b>Notes MCP</b>.</p>
  <div class="client"><b style="color:#e6e6e6">{client_name}</b><br>{client_id}</div>
  <form method="post" action="/authorize/consent">
    <input type="hidden" name="pending" value="{pending}">
    <label for="username">Sign in as</label>
    <input id="username" type="text" name="username" value="alice" autofocus>
    <label>This client is requesting</label>
    {scope_html}
    <div class="row">
      <button class="deny" type="submit" name="decision" value="deny">Deny</button>
      <button class="allow" type="submit" name="decision" value="allow">Authorize</button>
    </div>
  </form>
  <p class="warn">Development authorization server - any username is accepted,
  no password is checked. Do not use in production.</p>
</div></body></html>
"""

_SCOPE_DESCRIPTIONS = {
    "notes:read": "Read your notes",
    "notes:write": "Create and delete your notes",
    "openid": "Confirm your identity",
    "profile": "See your basic profile",
    "email": "See your email address",
    "offline_access": "Stay signed in when you are away",
}


async def authorize(request: Request) -> Response:
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    response_type = q.get("response_type", "")
    challenge = q.get("code_challenge", "")
    method = q.get("code_challenge_method", "")

    client = CLIENTS.get(client_id)
    if client is None:
        return _err("invalid_client", f"unknown client_id {client_id!r}")
    if redirect_uri not in client.redirect_uris:
        # Never redirect to an unregistered URI - that is an open redirect.
        return _err("invalid_request", "redirect_uri was not registered by this client")
    if response_type != "code":
        return _err("unsupported_response_type", "only response_type=code is supported")
    if not challenge or method != "S256":
        # PKCE is mandatory in OAuth 2.1 and in the MCP spec.
        return _err("invalid_request", "PKCE with code_challenge_method=S256 is required")

    requested = [s for s in (q.get("scope") or "notes:read").split() if s]
    unknown = [s for s in requested if s not in SUPPORTED_SCOPES]
    if unknown:
        return _err("invalid_scope", f"unsupported scope(s): {', '.join(unknown)}")

    pending_id = secrets.token_urlsafe(24)
    PENDING[pending_id] = PendingAuth(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=q.get("state"),
        code_challenge=challenge,
        code_challenge_method=method,
        scopes=requested,
        resource=q.get("resource"),
    )

    scope_html = "".join(
        f'<div class="scope"><input type="checkbox" name="scope" value="{s}" checked>'
        f"<span><code>{s}</code>"
        f'<small>{_SCOPE_DESCRIPTIONS.get(s, "")}</small></span></div>'
        for s in requested
    )
    return HTMLResponse(
        _CONSENT_PAGE.format(
            client_name=client.client_name,
            client_id=client_id,
            pending=pending_id,
            scope_html=scope_html,
        )
    )


async def consent(request: Request) -> Response:
    form = await request.form()
    pending_id = str(form.get("pending", ""))
    pending = PENDING.pop(pending_id, None)
    if pending is None:
        return _err("invalid_request", "this authorization request expired")

    sep = "&" if "?" in pending.redirect_uri else "?"

    if form.get("decision") != "allow":
        target = f"{pending.redirect_uri}{sep}error=access_denied"
        if pending.state:
            target += f"&state={pending.state}"
        return RedirectResponse(target, status_code=302)

    # The user may untick scopes: grant the intersection, never more.
    granted = [s for s in form.getlist("scope") if s in pending.scopes]
    username = (str(form.get("username") or "alice")).strip() or "alice"
    subject = f"user_{hashlib.sha256(username.encode()).hexdigest()[:16]}"

    code = secrets.token_urlsafe(32)
    CODES[code] = AuthCode(
        client_id=pending.client_id,
        redirect_uri=pending.redirect_uri,
        state=pending.state,
        code_challenge=pending.code_challenge,
        code_challenge_method=pending.code_challenge_method,
        scopes=granted,
        resource=pending.resource,
        subject=subject,
        email=f"{username}@example.dev",
    )
    print(f"[dev-auth] {username} -> {subject} granted {granted or ['(none)']}")

    target = f"{pending.redirect_uri}{sep}code={code}"
    if pending.state:
        target += f"&state={pending.state}"
    return RedirectResponse(target, status_code=302)


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #
def _mint_access_token(
    subject: str, email: str, client_id: str, scopes: list[str], audience: str | None
) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience or f"{ISSUER}/mcp",
        "azp": client_id,
        "client_id": client_id,
        "iat": now,
        "nbf": now,
        "exp": now + ACCESS_TTL,
        "jti": secrets.token_hex(12),
        "scope": " ".join(scopes),
        "email": email,
        "name": email.split("@")[0],
    }
    return jwt.encode(claims, _private_pem, algorithm="RS256", headers={"kid": _kid})


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return secrets.compare_digest(_b64u(digest), challenge)


async def token(request: Request) -> Response:
    form = await request.form()
    grant_type = str(form.get("grant_type", ""))

    if grant_type == "authorization_code":
        code = str(form.get("code", ""))
        record = CODES.pop(code, None)  # single use
        if record is None:
            return _err("invalid_grant", "authorization code is unknown or already used")
        if time.time() - record.created_at > 300:
            return _err("invalid_grant", "authorization code expired")
        if str(form.get("client_id", "")) != record.client_id:
            return _err("invalid_grant", "client_id does not match the code")
        if str(form.get("redirect_uri", "")) != record.redirect_uri:
            return _err("invalid_grant", "redirect_uri does not match the code")

        verifier = str(form.get("code_verifier", ""))
        if not verifier or not _verify_pkce(verifier, record.code_challenge):
            return _err("invalid_grant", "PKCE verification failed")

        audience = str(form.get("resource") or record.resource or "") or None
        access = _mint_access_token(
            record.subject, record.email, record.client_id, record.scopes, audience
        )
        body: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "scope": " ".join(record.scopes),
        }
        if "offline_access" in record.scopes or True:
            refresh = secrets.token_urlsafe(40)
            REFRESH[refresh] = {
                "subject": record.subject,
                "email": record.email,
                "client_id": record.client_id,
                "scopes": record.scopes,
                "resource": audience,
                "expires_at": time.time() + REFRESH_TTL,
            }
            body["refresh_token"] = refresh
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    if grant_type == "refresh_token":
        presented = str(form.get("refresh_token", ""))
        record = REFRESH.pop(presented, None)  # rotate on use
        if record is None or record["expires_at"] < time.time():
            return _err("invalid_grant", "refresh token is unknown or expired")

        # A refresh may narrow scope, never widen it.
        requested = [s for s in str(form.get("scope") or "").split() if s]
        scopes = [s for s in requested if s in record["scopes"]] or record["scopes"]

        access = _mint_access_token(
            record["subject"], record["email"], record["client_id"], scopes, record["resource"]
        )
        rotated = secrets.token_urlsafe(40)
        REFRESH[rotated] = {**record, "scopes": scopes, "expires_at": time.time() + REFRESH_TTL}
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL,
                "refresh_token": rotated,
                "scope": " ".join(scopes),
            },
            headers={"Cache-Control": "no-store"},
        )

    return _err("unsupported_grant_type", f"grant_type {grant_type!r} is not supported")


async def revoke(request: Request) -> Response:
    form = await request.form()
    presented = str(form.get("token", ""))
    REFRESH.pop(presented, None)
    REVOKED.add(presented)
    return Response(status_code=200)


# --------------------------------------------------------------------------- #
# Test helper: mint a token without a browser
# --------------------------------------------------------------------------- #
async def dev_token(request: Request) -> Response:
    """Shortcut for scripted tests: ``/dev/token?sub=alice&scope=notes:read``.

    Present only because this is a dev server. A real authorization server has
    no such endpoint.
    """
    q = request.query_params
    username = q.get("sub") or "alice"
    scopes = [s for s in (q.get("scope") or "notes:read notes:write").split() if s]
    audience = q.get("resource") or f"http://127.0.0.1:8000/mcp"
    subject = f"user_{hashlib.sha256(username.encode()).hexdigest()[:16]}"
    return JSONResponse(
        {
            "access_token": _mint_access_token(
                subject, f"{username}@example.dev", "dev-cli", scopes, audience
            ),
            "token_type": "Bearer",
            "subject": subject,
            "scope": " ".join(scopes),
            "expires_in": ACCESS_TTL,
        }
    )


async def home(request: Request) -> Response:
    return JSONResponse(
        {
            "name": "Notes MCP dev authorization server",
            "issuer": ISSUER,
            "warning": "development only - ephemeral keys, no password check",
            "discovery": f"{ISSUER}/.well-known/oauth-authorization-server",
            "endpoints": sorted(
                [
                    "/register",
                    "/authorize",
                    "/token",
                    "/revoke",
                    "/dev/token",
                    "/.well-known/jwks.json",
                ]
            ),
        }
    )


app = Starlette(
    routes=[
        Route("/", home),
        Route("/.well-known/oauth-authorization-server", metadata),
        Route("/.well-known/openid-configuration", metadata),
        Route("/.well-known/jwks.json", jwks),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize),
        Route("/authorize/consent", consent, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
        Route("/revoke", revoke, methods=["POST"]),
        Route("/dev/token", dev_token),
    ]
)

if __name__ == "__main__":
    print(f"[dev-auth] issuer = {ISSUER}")
    print(f"[dev-auth] DEVELOPMENT ONLY - ephemeral RSA key, kid={_kid}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
