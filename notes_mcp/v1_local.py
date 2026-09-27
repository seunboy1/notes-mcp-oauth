"""Stage 1 - a local MCP server over stdio.

The client (Claude Desktop, Cursor, VS Code, ...) launches this file as a
subprocess and speaks JSON-RPC over stdin/stdout. Nothing is listening on a
port, so there is no network attack surface and no need for authentication -
the OS process boundary *is* the security model.

Run it yourself:
    uv run notes_mcp/v1_local.py

Or let a client run it - see clients/cursor.local.json.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notes_mcp import notes_db  # noqa: E402
from notes_mcp.notes_db import LOCAL_OWNER, NoteNotFound  # noqa: E402

mcp = FastMCP(
    name="notes-local",
    instructions=(
        "A personal notes store. Use list_notes to see what exists before "
        "adding or deleting, and always echo the note id back to the user so "
        "they can refer to it later."
    ),
)


@mcp.tool
def list_notes(
    limit: Annotated[int, Field(ge=1, le=500, description="Max notes to return")] = 50,
) -> dict[str, Any]:
    """List the saved notes, newest first.

    Returns a count plus the note objects, each with an id, content, and
    creation timestamp.
    """
    notes = notes_db.list_notes(LOCAL_OWNER, limit=limit)
    return {"count": len(notes), "notes": notes}


@mcp.tool
def add_note(
    content: Annotated[str, Field(min_length=1, max_length=10_000)],
) -> dict[str, Any]:
    """Save a new note and return the created note, including its new id."""
    try:
        note = notes_db.add_note(content, LOCAL_OWNER)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "note": note}


@mcp.tool
def delete_note(
    note_id: Annotated[int, Field(ge=1, description="The id returned by list_notes")],
) -> dict[str, Any]:
    """Delete a note by id.

    Destructive: confirm the id with the user (or with list_notes) first.
    """
    try:
        deleted = notes_db.delete_note(note_id, LOCAL_OWNER)
    except NoteNotFound:
        deleted = False
    if not deleted:
        return {"ok": False, "error": f"no note with id {note_id}"}
    return {"ok": True, "deleted_id": note_id}


if __name__ == "__main__":
    notes_db.init_db()
    # stdio is the default transport: the client owns this process's lifetime.
    mcp.run()
