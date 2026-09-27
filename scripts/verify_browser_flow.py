#!/usr/bin/env python
"""Drive the complete OAuth 2.1 + PKCE flow through a real browser.

Everything the earlier scripts skip happens here, in the order a real MCP
client does it:

    1. Call the MCP server with no token          -> 401 + WWW-Authenticate
    2. Fetch the protected-resource metadata      -> find the auth server
    3. Fetch the auth server metadata             -> find its endpoints
    4. POST /register                             -> dynamic client registration
    5. Generate a PKCE verifier + S256 challenge
    6. Open /authorize IN A BROWSER               -> Playwright logs in & consents
    7. Catch the redirect, extract ?code=
    8. POST /token with the code_verifier         -> access + refresh token
    9. Call the MCP server with the access token  -> tools work
   10. POST /token with grant_type=refresh_token  -> rotated token still works

It also checks the negative case: an authorize request without PKCE is
rejected, and a consent screen where the user unticks notes:write yields a
token that cannot write.

Usage (dev auth server + v3 both running):
    uv run scripts/verify_browser_flow.py
    HEADED=1 uv run scripts/verify_browser_flow.py   # watch it happen
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
BASE_URL = MCP_URL[: -len("/mcp")] if MCP_URL.endswith("/mcp") else MCP_URL
REDIRECT_URI = "http://127.0.0.1:7777/oauth/callback"
HEADED = os.getenv("HEADED", "").lower() in ("1", "true", "yes")

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
_results: list[bool] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append(ok)
    print(f"{PASS if ok else FAIL}  {label}")
    if detail and not ok:
        print(f"        {detail}")
    return ok


def step(n: int, text: str) -> None:
    print(f"\n\033[1m{n}. {text}\033[0m")


def pkce_pair() -> tuple[str, str]:
    """A fresh PKCE verifier and its S256 challenge (RFC 7636)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class McpProbe:
    """Just enough MCP client to prove a token works."""

    def __init__(self, client: httpx.AsyncClient, token: str | None = None):
        self.client = client
        self.token = token
        self.session_id: str | None = None
        self._id = 0

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(text: str) -> dict:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(text) if text.strip() else {}

    async def rpc(self, method: str, params: dict | None = None) -> httpx.Response:
        self._id += 1
        body: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        return await self.client.post(MCP_URL, headers=self._headers(), json=body)

    async def initialize(self) -> httpx.Response:
        resp = await self.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "playwright-oauth-probe", "version": "1.0"},
        })
        self.session_id = resp.headers.get("mcp-session-id")
        if resp.status_code < 300:
            await self.client.post(MCP_URL, headers=self._headers(),
                                   json={"jsonrpc": "2.0",
                                         "method": "notifications/initialized"})
        return resp

    async def call(self, name: str, args: dict | None = None) -> dict:
        resp = await self.rpc("tools/call", {"name": name, "arguments": args or {}})
        data = self._parse(resp.text)
        if "error" in data:
            return {"_error": data["error"]}
        result = data.get("result", {})
        if result.get("isError"):
            return {"_error": (result.get("content") or [{}])[0].get("text", "")}
        if "structuredContent" in result:
            return result["structuredContent"]
        text = (result.get("content") or [{}])[0].get("text", "")
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}

    async def tool_names(self) -> list[str]:
        data = self._parse((await self.rpc("tools/list")).text)
        return sorted(t["name"] for t in data.get("result", {}).get("tools", []))


class CallbackServer:
    """A one-shot loopback listener for the OAuth redirect.

    This is exactly what a desktop MCP client does: bind a port on 127.0.0.1,
    register it as the redirect_uri, and wait for the browser to deliver the
    authorization code to it.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7777):
        self.host, self.port = host, port
        self.received: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> "CallbackServer":
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            parts = line.decode("latin-1").split(" ")
            target = parts[1] if len(parts) > 1 else "/"
            if not self.received.done():
                self.received.set_result(f"http://{self.host}:{self.port}{target}")
            body = b"<h1>Authorized.</h1><p>You can close this window.</p>"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


async def consent_in_browser(
    authorize_url: str, username: str, untick: list[str] | None = None
) -> str:
    """Let Playwright be the human: log in, adjust scopes, click Authorize.

    Returns the redirect URL carrying the code (or an error).
    """
    from playwright.async_api import async_playwright

    async with CallbackServer() as callback:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not HEADED)
            page = await browser.new_page()
            try:
                await page.goto(authorize_url, wait_until="domcontentloaded")
                print(f"        browser is on: {(await page.title())!r}")

                await page.fill("#username", username)
                for scope in untick or []:
                    box = page.locator(f'input[name="scope"][value="{scope}"]')
                    if await box.count() and await box.is_checked():
                        await box.uncheck()
                        print(f"        user unticked scope: {scope}")

                await page.click('button[value="allow"]')
                redirect = await asyncio.wait_for(callback.received, timeout=20)
            finally:
                await browser.close()

    print(f"        redirect delivered to {REDIRECT_URI}")
    return redirect


async def register_client(client: httpx.AsyncClient, reg_endpoint: str, name: str) -> str:
    resp = await client.post(reg_endpoint, json={
        "client_name": name,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    resp.raise_for_status()
    return resp.json()["client_id"]


async def run_code_flow(
    client: httpx.AsyncClient,
    meta: dict,
    client_id: str,
    scopes: str,
    username: str,
    untick: list[str] | None = None,
) -> dict:
    verifier, challenge = pkce_pair()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scopes,
        "state": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": MCP_URL,  # RFC 8707 - bind the token to this server
    }
    url = f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
    redirect = await consent_in_browser(url, username, untick)

    q = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)
    if "code" not in q:
        raise RuntimeError(f"no code in redirect: {redirect}")
    check(q.get("state", [""])[0] == params["state"], "state parameter round-tripped intact")

    resp = await client.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code",
        "code": q["code"][0],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": MCP_URL,
    })
    resp.raise_for_status()
    return {**resp.json(), "_code": q["code"][0], "_verifier": verifier}


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        step(1, "Anonymous call to the MCP server")
        probe = McpProbe(client)
        resp = await probe.initialize()
        check(resp.status_code == 401, f"401 Unauthorized (got {resp.status_code})")
        challenge_header = resp.headers.get("www-authenticate", "")
        check("resource_metadata=" in challenge_header,
              "server told us where to find its metadata", challenge_header)

        step(2, "Discover the authorization server")
        meta_url = None
        for part in challenge_header.split(","):
            if "resource_metadata=" in part:
                meta_url = part.split("resource_metadata=", 1)[1].strip().strip('"')
        check(bool(meta_url), f"metadata URL parsed from the header: {meta_url}")
        assert meta_url
        pr = (await client.get(meta_url)).json()
        issuer = pr["authorization_servers"][0]
        check(bool(issuer), f"authorization server = {issuer}")

        as_meta = (await client.get(f"{issuer}/.well-known/oauth-authorization-server")).json()
        check("S256" in as_meta.get("code_challenge_methods_supported", []),
              "authorization server advertises PKCE S256")
        check(bool(as_meta.get("registration_endpoint")),
              "authorization server supports dynamic client registration")

        step(3, "Reject an authorize request that omits PKCE")
        bad_id = await register_client(client, as_meta["registration_endpoint"], "no-pkce-client")
        bad = await client.get(as_meta["authorization_endpoint"], params={
            "response_type": "code", "client_id": bad_id,
            "redirect_uri": REDIRECT_URI, "scope": "notes:read",
        })
        check(bad.status_code == 400, f"authorize without code_challenge -> 400 (got {bad.status_code})")

        step(4, "Register this client dynamically")
        client_id = await register_client(client, as_meta["registration_endpoint"], "Notes MCP demo client")
        check(bool(client_id), f"client_id issued without human setup: {client_id}")

        step(5, "Full code flow in a real browser (alice, both scopes)")
        tokens = await run_code_flow(
            client, as_meta, client_id, "notes:read notes:write offline_access", "alice"
        )
        check("access_token" in tokens, "code + verifier exchanged for an access token")
        check("refresh_token" in tokens, "refresh token issued")
        granted = set(tokens.get("scope", "").split())
        check({"notes:read", "notes:write"} <= granted, f"granted scopes = {sorted(granted)}")

        step(6, "The access token works against the MCP server")
        alice = McpProbe(client, tokens["access_token"])
        r = await alice.initialize()
        check(r.status_code == 200, f"initialize -> 200 (got {r.status_code})")
        names = await alice.tool_names()
        check("add_note" in names and "delete_note" in names, f"tools available: {names}")
        me = await alice.call("whoami")
        check(bool(me.get("user_id")), f"whoami -> {me.get('user_id')} / {me.get('email')}")
        made = await alice.call("add_note", {"content": "written through the browser OAuth flow"})
        note_id = (made.get("note") or {}).get("id")
        check(made.get("ok") is True, f"add_note -> id={note_id}")

        step(7, "Replaying the same authorization code fails")
        replay = await client.post(as_meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": tokens["_code"],
            "redirect_uri": REDIRECT_URI, "client_id": client_id,
            "code_verifier": tokens["_verifier"], "resource": MCP_URL,
        })
        check(replay.status_code == 400, f"reused code -> 400 (got {replay.status_code})")

        step(8, "Refresh rotates the token and the new one still works")
        refreshed = await client.post(as_meta["token_endpoint"], data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        })
        check(refreshed.status_code == 200, f"refresh -> 200 (got {refreshed.status_code})")
        new_tokens = refreshed.json()
        check(new_tokens["access_token"] != tokens["access_token"], "a new access token was issued")
        check(new_tokens.get("refresh_token") != tokens["refresh_token"],
              "the refresh token was rotated")
        rotated = McpProbe(client, new_tokens["access_token"])
        check((await rotated.initialize()).status_code == 200, "refreshed token is accepted")
        seen = await rotated.call("list_notes")
        check(any(n["id"] == note_id for n in seen.get("notes", [])),
              f"same user, same notes after refresh (count={seen.get('count')})")

        step(9, "User unticks notes:write at the consent screen")
        limited = await run_code_flow(
            client, as_meta, client_id,
            "notes:read notes:write", "carol", untick=["notes:write"],
        )
        lim_scopes = set(limited.get("scope", "").split())
        check("notes:write" not in lim_scopes, f"token granted only {sorted(lim_scopes)}")
        carol = McpProbe(client, limited["access_token"])
        await carol.initialize()
        carol_tools = await carol.tool_names()
        check("add_note" not in carol_tools, f"write tools hidden: {carol_tools}")
        denied = await carol.call("add_note", {"content": "nope"})
        check("_error" in denied, f"add_note refused: {str(denied)[:100]}")
        check(not (await carol.call("list_notes")).get("notes"),
              "carol sees none of alice's notes")

        step(10, "Cleanup")
        gone = await rotated.call("delete_note", {"note_id": note_id})
        check(gone.get("ok") is True, "alice deleted her own note")

    passed = sum(_results)
    print(f"\n\033[1m{passed}/{len(_results)} checks passed\033[0m\n")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
