"""Authorization for a remote MCP server.

This module is deliberately provider-agnostic. An MCP server is an OAuth 2.1
*resource server*: it never renders a login screen and never sees a password.
It only ever does three things.

1. Advertise where the authorization server lives
   (RFC 9728 ``/.well-known/oauth-protected-resource``, plus a
   ``WWW-Authenticate`` header on every 401).
2. Verify the bearer token on each request (signature, issuer, audience,
   expiry) against the authorization server's JWKS.
3. Enforce scopes and derive the caller's identity from the ``sub`` claim.

Point ``OAUTH_ISSUER`` (or ``DESCOPE_CONFIG_URL``) at any compliant provider -
Descope, Auth0, WorkOS, Keycloak, or the bundled dev authorization server in
``notes_mcp/dev_auth_server.py`` - and the code below does not change.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
import jwt
from jwt import PyJWKClient

# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #
NOTES_READ = "notes:read"
NOTES_WRITE = "notes:write"
SUPPORTED_SCOPES = [NOTES_READ, NOTES_WRITE]


class AuthError(Exception):
    """Token missing, malformed, expired, or issued for someone else."""


class ForbiddenError(Exception):
    """Token is valid but lacks the scope this tool requires."""

    def __init__(self, required: str, held: Iterable[str]):
        self.required = required
        self.held = sorted(held)
        super().__init__(
            f"missing required scope '{required}'. Token carries: "
            f"{', '.join(self.held) or '(none)'}"
        )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _strip_wellknown(url: str) -> str:
    """Turn any ``.../.well-known/...`` URL into its bare issuer."""
    url = url.strip().rstrip("/")
    for marker in (
        "/.well-known/openid-configuration",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        if url.endswith(marker):
            return url[: -len(marker)]
    return url


@dataclass
class AuthSettings:
    """Everything the resource server needs, resolved from the environment."""

    issuer: str
    resource_url: str
    required_scopes: list[str] = field(default_factory=lambda: list(SUPPORTED_SCOPES))
    audience: str | None = None
    verify_audience: bool = True

    @classmethod
    def from_env(cls) -> "AuthSettings":
        raw_issuer = (
            os.getenv("OAUTH_ISSUER")
            or os.getenv("DESCOPE_CONFIG_URL")
            or os.getenv("DESCOPE_ISSUER")
            or ""
        )
        if not raw_issuer:
            raise AuthError(
                "No authorization server configured. Set OAUTH_ISSUER (or "
                "DESCOPE_CONFIG_URL) in your .env - see .env.example."
            )

        server_url = (
            os.getenv("MCP_SERVER_URL") or "http://localhost:8000/mcp"
        ).strip().rstrip("/")
        # The canonical resource identifier is the MCP endpoint itself.
        if not server_url.endswith("/mcp"):
            server_url = f"{server_url}/mcp"

        scopes = [
            s.strip()
            for s in os.getenv("MCP_SCOPES", ",".join(SUPPORTED_SCOPES)).split(",")
            if s.strip()
        ]

        audience = os.getenv("OAUTH_AUDIENCE") or server_url
        verify_audience = os.getenv("OAUTH_VERIFY_AUDIENCE", "true").lower() not in (
            "0",
            "false",
            "no",
        )

        return cls(
            issuer=_strip_wellknown(raw_issuer),
            resource_url=server_url,
            required_scopes=scopes,
            audience=audience,
            verify_audience=verify_audience,
        )

    # -- derived URLs ------------------------------------------------------- #
    @property
    def metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    @property
    def oauth_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-authorization-server"

    @property
    def base_url(self) -> str:
        """The server's own base URL, without the ``/mcp`` path."""
        return self.resource_url[: -len("/mcp")]


# --------------------------------------------------------------------------- #
# Authorization-server discovery
# --------------------------------------------------------------------------- #
_metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_METADATA_TTL = 600.0


def discover_metadata(settings: AuthSettings, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch (and cache) the authorization server's metadata document.

    Tries OpenID Connect discovery first, then the OAuth 2.0 variant, because
    providers differ on which one they publish.
    """
    cached = _metadata_cache.get(settings.issuer)
    if cached and cached[0] > time.time():
        return cached[1]

    errors: list[str] = []
    for url in (settings.metadata_url, settings.oauth_metadata_url):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            if "jwks_uri" not in data:
                errors.append(f"{url}: no jwks_uri in document")
                continue
            _metadata_cache[settings.issuer] = (time.time() + _METADATA_TTL, data)
            return data
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(f"{url}: {exc}")

    raise AuthError(
        "Could not discover the authorization server. Tried:\n  - "
        + "\n  - ".join(errors)
    )


_jwk_clients: dict[str, PyJWKClient] = {}


def _jwk_client(jwks_uri: str) -> PyJWKClient:
    client = _jwk_clients.get(jwks_uri)
    if client is None:
        client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=600)
        _jwk_clients[jwks_uri] = client
    return client


# --------------------------------------------------------------------------- #
# Token verification
# --------------------------------------------------------------------------- #
@dataclass
class Principal:
    """The authenticated caller, distilled from a verified access token."""

    user_id: str
    client_id: str | None
    scopes: list[str]
    expires_at: int | None
    claims: dict[str, Any] = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise ForbiddenError(scope, self.scopes)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "client_id": self.client_id,
            "scopes": self.scopes,
            "email": self.claims.get("email"),
            "expires_at": self.expires_at,
        }


def _parse_scopes(claims: dict[str, Any]) -> list[str]:
    """Read scopes from whichever claim the provider decided to use."""
    raw: Any = (
        claims.get("scope")
        or claims.get("scp")
        or claims.get("scopes")
        or claims.get("permissions")
        or []
    )
    if isinstance(raw, str):
        return [s for s in raw.replace(",", " ").split() if s]
    if isinstance(raw, (list, tuple)):
        return [str(s) for s in raw]
    return []


def verify_token(token: str, settings: AuthSettings) -> Principal:
    """Validate a bearer token and return the caller it represents.

    Raises :class:`AuthError` on anything suspicious. Notably this checks the
    **audience**: a token minted for a different resource server must not be
    replayable against this one (the "confused deputy" problem the MCP spec
    calls out explicitly).
    """
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise AuthError("no bearer token presented")

    metadata = discover_metadata(settings)
    jwks_uri = metadata["jwks_uri"]

    try:
        signing_key = _jwk_client(jwks_uri).get_signing_key_from_jwt(token)
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"could not resolve signing key: {exc}") from exc

    algorithms = metadata.get(
        "id_token_signing_alg_values_supported", ["RS256", "ES256"]
    )
    algorithms = [a for a in algorithms if a != "none"] or ["RS256"]

    options = {"require": ["exp", "sub"], "verify_aud": settings.verify_audience}
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=algorithms,
            issuer=metadata.get("issuer", settings.issuer),
            audience=settings.audience if settings.verify_audience else None,
            options=options,
            leeway=30,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("access token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError(
            f"token audience does not include this server ({settings.audience})"
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("token was issued by an unexpected authorization server") from exc
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid access token: {exc}") from exc

    return Principal(
        user_id=str(claims["sub"]),
        client_id=claims.get("azp") or claims.get("client_id") or claims.get("cid"),
        scopes=_parse_scopes(claims),
        expires_at=claims.get("exp"),
        claims=claims,
    )


# --------------------------------------------------------------------------- #
# RFC 9728 protected-resource metadata
# --------------------------------------------------------------------------- #
def protected_resource_metadata(settings: AuthSettings) -> dict[str, Any]:
    """The document a client fetches after a 401 to learn where to log in."""
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer],
        "scopes_supported": settings.required_scopes,
        "bearer_methods_supported": ["header"],
        "resource_name": os.getenv("MCP_SERVER_NAME", "Notes MCP"),
        "resource_documentation": f"{settings.base_url}/docs",
    }


def www_authenticate_header(settings: AuthSettings, error: str = "invalid_token") -> str:
    """The header that turns an opaque 401 into a self-service login prompt."""
    return (
        f'Bearer error="{error}", '
        f'resource_metadata="{settings.base_url}/.well-known/oauth-protected-resource"'
    )
