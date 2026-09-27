"""Stage 2 - the same tools, exposed remotely over streamable HTTP.

One line changes versus stage 1: ``mcp.run(transport="http", ...)``. Now the
server has a URL and any MCP client on the network can reach it - which also
means any client on the network can reach it. Read the warning banner it prints
on boot, then go look at ``v3_oauth.py``.

Run it:
    uv run notes_mcp/v2_remote.py
    # -> http://127.0.0.1:8000/mcp
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notes_mcp import notes_db  # noqa: E402
from notes_mcp.notes_db import LOCAL_OWNER, NoteNotFound  # noqa: E402

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

mcp = FastMCP(
    name="notes-remote",
    instructions=(
        "A shared notes store reachable over HTTP. Call list_notes before "
        "mutating anything and report note ids back to the user."
    ),
)


@mcp.tool
def list_notes(
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """List the saved notes, newest first."""
    notes = notes_db.list_notes(LOCAL_OWNER, limit=limit)
    return {"count": len(notes), "notes": notes}


@mcp.tool
def add_note(
    content: Annotated[str, Field(min_length=1, max_length=10_000)],
) -> dict[str, Any]:
    """Save a new note and return it, including its new id."""
    try:
        note = notes_db.add_note(content, LOCAL_OWNER)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "note": note}


@mcp.tool
def delete_note(
    note_id: Annotated[int, Field(ge=1)],
) -> dict[str, Any]:
    """Delete a note by id. Destructive - confirm the id first."""
    try:
        deleted = notes_db.delete_note(note_id, LOCAL_OWNER)
    except NoteNotFound:
        deleted = False
    if not deleted:
        return {"ok": False, "error": f"no note with id {note_id}"}
    return {"ok": True, "deleted_id": note_id}


BANNER = """
==========================================================================
  notes-remote is running WITHOUT authentication.

  Every caller shares one notes bucket ("{owner}") and every tool -
  including delete_note - is reachable by anyone who can open the URL.

  Fine on 127.0.0.1. Never expose this to the internet.
  Use notes_mcp/v3_oauth.py for anything real.
==========================================================================
""".strip()

if __name__ == "__main__":
    notes_db.init_db()
    print(BANNER.format(owner=LOCAL_OWNER), flush=True)
    mcp.run(transport="http", host=HOST, port=PORT)
