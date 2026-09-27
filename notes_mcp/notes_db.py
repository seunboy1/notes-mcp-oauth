"""Storage layer for the Notes MCP server.

Two backends behind one tiny API:

* SQLite (default) - zero setup, file on disk, great for local demos.
* PostgreSQL (optional) - set ``DATABASE_URL=postgresql://...`` and install
  the ``postgres`` extra.

Every row is owned by an ``owner_id``. In the unauthenticated demos that owner
is the literal string ``local``; in the OAuth demo it is the ``sub`` claim of
the caller's access token. That single column is what turns a toy server into a
multi-tenant one.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCAL_OWNER = "local"

_DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent.parent / "notes.db"
_lock = threading.Lock()


class NoteNotFound(Exception):
    """Raised when a note id does not exist for the requesting owner."""


def _database_url() -> str | None:
    url = os.getenv("DATABASE_URL", "").strip()
    return url or None


def _use_postgres() -> bool:
    url = _database_url()
    return bool(url and url.startswith(("postgres://", "postgresql://")))


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #
def _connect():
    """Open a connection to whichever backend is configured."""
    if _use_postgres():
        import psycopg  # imported lazily so SQLite users need no driver

        return psycopg.connect(_database_url())

    path = os.getenv("SQLITE_PATH") or str(_DEFAULT_SQLITE_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_owner_idx ON notes (owner_id);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notes_owner_idx ON notes (owner_id);
"""


def init_db() -> None:
    """Create the table if it is missing. Safe to call on every boot."""
    with _lock, _connect() as conn:
        if _use_postgres():
            with conn.cursor() as cur:
                cur.execute(_POSTGRES_SCHEMA)
            conn.commit()
        else:
            conn.executescript(_SQLITE_SCHEMA)
            conn.commit()


def backend_name() -> str:
    return "postgresql" if _use_postgres() else "sqlite"


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def add_note(content: str, owner_id: str = LOCAL_OWNER) -> dict[str, Any]:
    """Insert a note for ``owner_id`` and return the stored row."""
    content = (content or "").strip()
    if not content:
        raise ValueError("note content must not be empty")
    if len(content) > 10_000:
        raise ValueError("note content must be 10000 characters or fewer")

    created_at = datetime.now(timezone.utc).isoformat()

    with _lock, _connect() as conn:
        if _use_postgres():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notes (owner_id, content) VALUES (%s, %s)"
                    " RETURNING id, created_at",
                    (owner_id, content),
                )
                note_id, created = cur.fetchone()
            conn.commit()
            return {
                "id": int(note_id),
                "owner_id": owner_id,
                "content": content,
                "created_at": created.isoformat(),
            }

        cur = conn.execute(
            "INSERT INTO notes (owner_id, content, created_at) VALUES (?, ?, ?)",
            (owner_id, content, created_at),
        )
        conn.commit()
        return {
            "id": int(cur.lastrowid),
            "owner_id": owner_id,
            "content": content,
            "created_at": created_at,
        }


def list_notes(owner_id: str = LOCAL_OWNER, limit: int = 100) -> list[dict[str, Any]]:
    """Return the caller's notes, newest first."""
    limit = max(1, min(int(limit), 500))

    with _lock, _connect() as conn:
        if _use_postgres():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, owner_id, content, created_at FROM notes"
                    " WHERE owner_id = %s ORDER BY id DESC LIMIT %s",
                    (owner_id, limit),
                )
                rows = cur.fetchall()
            return [
                {
                    "id": int(r[0]),
                    "owner_id": r[1],
                    "content": r[2],
                    "created_at": r[3].isoformat(),
                }
                for r in rows
            ]

        rows = conn.execute(
            "SELECT id, owner_id, content, created_at FROM notes"
            " WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_note(note_id: int, owner_id: str = LOCAL_OWNER) -> dict[str, Any]:
    """Fetch one note the caller owns, or raise :class:`NoteNotFound`."""
    with _lock, _connect() as conn:
        if _use_postgres():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, owner_id, content, created_at FROM notes"
                    " WHERE id = %s AND owner_id = %s",
                    (note_id, owner_id),
                )
                row = cur.fetchone()
            if row is None:
                raise NoteNotFound(f"note {note_id} not found")
            return {
                "id": int(row[0]),
                "owner_id": row[1],
                "content": row[2],
                "created_at": row[3].isoformat(),
            }

        row = conn.execute(
            "SELECT id, owner_id, content, created_at FROM notes"
            " WHERE id = ? AND owner_id = ?",
            (note_id, owner_id),
        ).fetchone()
        if row is None:
            raise NoteNotFound(f"note {note_id} not found")
        return dict(row)


def delete_note(note_id: int, owner_id: str = LOCAL_OWNER) -> bool:
    """Delete a note the caller owns. Returns ``False`` if nothing matched.

    The ``owner_id`` predicate in the ``WHERE`` clause is the whole security
    story: without it, any authenticated user could delete anyone's note by
    guessing an id.
    """
    with _lock, _connect() as conn:
        if _use_postgres():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM notes WHERE id = %s AND owner_id = %s",
                    (note_id, owner_id),
                )
                deleted = cur.rowcount
            conn.commit()
            return deleted > 0

        cur = conn.execute(
            "DELETE FROM notes WHERE id = ? AND owner_id = ?",
            (note_id, owner_id),
        )
        conn.commit()
        return cur.rowcount > 0


def count_notes(owner_id: str = LOCAL_OWNER) -> int:
    with _lock, _connect() as conn:
        if _use_postgres():
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM notes WHERE owner_id = %s", (owner_id,))
                return int(cur.fetchone()[0])
        row = conn.execute(
            "SELECT count(*) AS n FROM notes WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        return int(row["n"])
