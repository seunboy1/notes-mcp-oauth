#!/usr/bin/env python
"""Verify stage 1 (stdio) and stage 2 (remote HTTP) with a real MCP client.

Also demonstrates the point of stage 3 by showing that stage 2 answers
tool calls from an anonymous caller.

Usage:
    uv run scripts/verify_basic.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
_results: list[bool] = []


def check(ok: bool, label: str) -> bool:
    _results.append(ok)
    print(f"{PASS if ok else FAIL}  {label}")
    return ok


def unwrap(result) -> dict:
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        try:
            return json.loads(content[0].text)
        except Exception:
            return {"text": content[0].text}
    return {}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def exercise(client: Client, label: str) -> None:
    tools = sorted(t.name for t in await client.list_tools())
    check(
        tools == ["add_note", "delete_note", "list_notes"],
        f"{label}: tools discovered -> {tools}",
    )

    created = unwrap(await client.call_tool("add_note", {"content": f"hello from {label}"}))
    note_id = (created.get("note") or {}).get("id")
    check(created.get("ok") is True and bool(note_id), f"{label}: add_note -> id={note_id}")

    listed = unwrap(await client.call_tool("list_notes", {"limit": 10}))
    check(
        any(n["id"] == note_id for n in listed.get("notes", [])),
        f"{label}: list_notes contains the new note (count={listed.get('count')})",
    )

    deleted = unwrap(await client.call_tool("delete_note", {"note_id": note_id}))
    check(deleted.get("ok") is True, f"{label}: delete_note -> ok")

    missing = unwrap(await client.call_tool("delete_note", {"note_id": 10_000_000}))
    check(missing.get("ok") is False, f"{label}: deleting a bogus id fails cleanly")


async def main() -> int:
    env = {**os.environ, "SQLITE_PATH": str(ROOT / "notes-verify.db")}

    print("\n\033[1mStage 1 - local stdio server\033[0m")
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        args=[str(ROOT / "notes_mcp" / "v1_local.py")],
        env=env,
    )
    async with Client(transport) as client:
        check(True, "stdio: client connected to the subprocess (no port, no token)")
        await exercise(client, "stdio")

    print("\n\033[1mStage 2 - remote streamable HTTP, no auth\033[0m")
    port = free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "notes_mcp" / "v2_remote.py"),
        env={**env, "HOST": "127.0.0.1", "PORT": str(port)},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(40):
            await asyncio.sleep(0.25)
            try:
                async with Client(url) as probe:
                    await probe.list_tools()
                break
            except Exception:
                continue

        async with Client(url) as client:
            check(True, f"http: connected to {url} with NO credentials at all")
            await exercise(client, "http")
            print("        ^ that is the problem stage 3 fixes.")
    finally:
        proc.terminate()
        await proc.wait()

    (ROOT / "notes-verify.db").unlink(missing_ok=True)

    passed = sum(_results)
    print(f"\n\033[1m{passed}/{len(_results)} checks passed\033[0m\n")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
