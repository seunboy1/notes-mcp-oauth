#!/usr/bin/env python
"""End-to-end verification of the OAuth-protected MCP server.

Exercises the properties that actually matter and are easy to get wrong:

1. An unauthenticated call is rejected with 401 + a WWW-Authenticate header
   that points a client at the authorization server.
2. The protected-resource metadata document is well formed (RFC 9728).
3. A token minted for a *different* audience is rejected (confused deputy).
4. A read-only token cannot write.
5. Two different users cannot see each other's notes.

Usage (with the dev authorization server and v3 both running):
    uv run scripts/verify_oauth.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Default to the same value the server uses as its own identity. The audience in
# a minted token must equal the server's configured `resource` exactly, so a
# mismatch here (127.0.0.1 vs localhost) makes every *valid* token 401 and looks
# like a broken server. MCP_SERVER_URL is what v3_oauth.py reads, so read it too.
MCP_URL = os.getenv("MCP_URL") or os.getenv(
    "MCP_SERVER_URL", "http://localhost:8000/mcp"
)
BASE_URL = MCP_URL[: -len("/mcp")] if MCP_URL.endswith("/mcp") else MCP_URL
AUTH_URL = os.getenv("DEV_AUTH_URL", "http://127.0.0.1:9000")

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((ok, label))
    print(f"{PASS if ok else FAIL}  {label}")
    if detail and not ok:
        print(f"        {detail}")
    return ok


async def mint(client: httpx.AsyncClient, sub: str, scope: str, resource: str) -> str:
    q = urllib.parse.urlencode({"sub": sub, "scope": scope, "resource": resource})
    resp = await client.get(f"{AUTH_URL}/dev/token?{q}")
    resp.raise_for_status()
    return resp.json()["access_token"]


class Session:
    """The smallest possible streamable-HTTP MCP client."""

    def __init__(self, client: httpx.AsyncClient, token: str | None):
        self.client = client
        self.token = token
        self.session_id: str | None = None
        self._id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

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
        resp = await self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "verify-oauth", "version": "1.0"},
            },
        )
        self.session_id = resp.headers.get("mcp-session-id")
        if resp.status_code < 300:
            await self.client.post(
                MCP_URL,
                headers=self._headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        return resp

    async def list_tools(self) -> list[str]:
        resp = await self.rpc("tools/list")
        data = self._parse(resp.text)
        return [t["name"] for t in data.get("result", {}).get("tools", [])]

    async def call(self, name: str, args: dict | None = None) -> dict:
        resp = await self.rpc("tools/call", {"name": name, "arguments": args or {}})
        data = self._parse(resp.text)
        if "error" in data:
            return {"_error": data["error"]}
        result = data.get("result", {})
        if result.get("isError"):
            text = (result.get("content") or [{}])[0].get("text", "")
            return {"_error": {"message": text}}
        if "structuredContent" in result:
            return result["structuredContent"]
        text = (result.get("content") or [{}])[0].get("text", "")
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}


async def main() -> int:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        print("\n\033[1m1. Unauthenticated access is refused\033[0m")
        anon = Session(client, None)
        resp = await anon.initialize()
        check(resp.status_code == 401, f"initialize without a token -> 401 (got {resp.status_code})")
        challenge = resp.headers.get("www-authenticate", "")
        check(
            "resource_metadata=" in challenge,
            "401 carries WWW-Authenticate with resource_metadata",
            challenge or "(header absent)",
        )

        print("\n\033[1m2. Protected-resource metadata (RFC 9728)\033[0m")
        meta = None
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
        ):
            r = await client.get(f"{BASE_URL}{path}")
            if r.status_code == 200:
                meta = r.json()
                break
        if check(meta is not None, "metadata document is served"):
            assert meta is not None
            check(meta.get("resource", "").endswith("/mcp"), f"resource = {meta.get('resource')}")
            check(bool(meta.get("authorization_servers")),
                  f"authorization_servers = {meta.get('authorization_servers')}")
            check(
                set(meta.get("scopes_supported") or []) >= {"notes:read", "notes:write"},
                f"scopes_supported = {meta.get('scopes_supported')}",
            )

        print("\n\033[1m3. A token for another audience is rejected\033[0m")
        wrong = await mint(client, "alice", "notes:read notes:write", "https://someone-elses-server.example/mcp")
        resp = await Session(client, wrong).initialize()
        check(
            resp.status_code == 401,
            f"token with foreign audience -> 401 (got {resp.status_code})",
            "audience validation appears to be off - check OAUTH_VERIFY_AUDIENCE",
        )

        print("\n\033[1m4. Scopes gate the tools\033[0m")
        ro = Session(client, await mint(client, "alice", "notes:read", MCP_URL))
        resp = await ro.initialize()
        check(resp.status_code == 200, f"read-only token can connect (got {resp.status_code})")
        tools = await ro.list_tools()
        check("list_notes" in tools, f"list_notes visible to notes:read  {sorted(tools)}")
        check("add_note" not in tools, "add_note hidden from a notes:read-only token")
        out = await ro.call("add_note", {"content": "should not be written"})
        check("_error" in out, f"add_note refused for notes:read: {str(out)[:110]}")

        print("\n\033[1m5. Notes are private per user\033[0m")
        alice = Session(client, await mint(client, "alice", "notes:read notes:write", MCP_URL))
        await alice.initialize()
        me = await alice.call("whoami")
        check("user_id" in me, f"whoami -> {me.get('user_id')} scopes={me.get('scopes')}")

        created = await alice.call("add_note", {"content": "alice's private note"})
        note_id = (created.get("note") or {}).get("id")
        check(created.get("ok") is True and bool(note_id), f"alice wrote note id={note_id}")

        listed = await alice.call("list_notes")
        check(
            any(n["id"] == note_id for n in listed.get("notes", [])),
            f"alice sees her note (count={listed.get('count')})",
        )

        bob = Session(client, await mint(client, "bob", "notes:read notes:write", MCP_URL))
        await bob.initialize()
        bob_list = await bob.call("list_notes")
        bob_ids = {n["id"] for n in bob_list.get("notes", [])}
        check(note_id not in bob_ids, f"bob cannot see alice's note (bob has {len(bob_ids)})")

        stolen = await bob.call("delete_note", {"note_id": note_id})
        check(stolen.get("ok") is not True, f"bob cannot delete alice's note: {str(stolen)[:90]}")

        still = await alice.call("get_note", {"note_id": note_id})
        check(still.get("ok") is True, "alice's note survived bob's delete attempt")

        cleaned = await alice.call("delete_note", {"note_id": note_id})
        check(cleaned.get("ok") is True, "alice can delete her own note")

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"\n\033[1m{passed}/{total} checks passed\033[0m\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
