"""SQLite persistence (stdlib sqlite3, WAL). Tables: drafts, tickets, conversations.

One connection per request/call — sqlite3 connections are not safe to share across threads
or concurrent async tasks, and WAL makes short-lived connections cheap.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DRAFT_TTL_HOURS = 24
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    token TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    priority TEXT NOT NULL,
    account_name TEXT,
    character_name TEXT,
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ticket_id TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    priority TEXT NOT NULL,
    account_name TEXT,
    character_name TEXT,
    conversation_id TEXT,
    status TEXT NOT NULL,
    discord_guild_id TEXT,
    discord_thread_id TEXT,
    discord_user_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    summary TEXT,
    transcript_json TEXT,
    received_at TEXT NOT NULL
);
"""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def db_path(data_dir: str) -> str:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    return os.path.join(data_dir, "support.db")


@contextlib.contextmanager
def get_connection(data_dir: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(data_dir), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def new_draft_token() -> str:
    """22+ chars, url-safe, 128 bits of randomness (SPEC §4)."""
    return secrets.token_urlsafe(16)


def new_ticket_id() -> str:
    """GB-<5 char Crockford base32>."""
    suffix = "".join(secrets.choice(_CROCKFORD_ALPHABET) for _ in range(5))
    return f"GB-{suffix}"


def create_draft(
    data_dir: str,
    *,
    category: str,
    summary: str,
    priority: str,
    account_name: str | None = None,
    character_name: str | None = None,
    conversation_id: str | None = None,
) -> str:
    token = new_draft_token()
    created_at = _now()
    expires_at = created_at + dt.timedelta(hours=DRAFT_TTL_HOURS)
    with get_connection(data_dir) as conn:
        conn.execute(
            """INSERT INTO drafts
               (token, category, summary, priority, account_name, character_name,
                conversation_id, created_at, expires_at, ticket_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                token,
                category,
                summary,
                priority,
                account_name,
                character_name,
                conversation_id,
                _iso(created_at),
                _iso(expires_at),
            ),
        )
    return token


def get_draft(data_dir: str, token: str) -> dict[str, Any] | None:
    with get_connection(data_dir) as conn:
        row = conn.execute("SELECT * FROM drafts WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    draft = dict(row)
    expires_at = dt.datetime.fromisoformat(draft["expires_at"])
    if _now() > expires_at:
        return None
    return draft


def mark_draft_consumed(data_dir: str, token: str, ticket_id: str) -> None:
    with get_connection(data_dir) as conn:
        conn.execute("UPDATE drafts SET ticket_id = ? WHERE token = ?", (ticket_id, token))


def create_ticket(
    data_dir: str,
    *,
    token: str,
    category: str,
    summary: str,
    priority: str,
    account_name: str | None,
    character_name: str | None,
    conversation_id: str | None,
    status: str,
) -> str:
    ticket_id = new_ticket_id()
    with get_connection(data_dir) as conn:
        conn.execute(
            """INSERT INTO tickets
               (ticket_id, token, category, summary, priority, account_name, character_name,
                conversation_id, status, discord_guild_id, discord_thread_id, discord_user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)""",
            (
                ticket_id,
                token,
                category,
                summary,
                priority,
                account_name,
                character_name,
                conversation_id,
                status,
                _iso(_now()),
            ),
        )
    mark_draft_consumed(data_dir, token, ticket_id)
    return ticket_id


def get_ticket(data_dir: str, ticket_id: str) -> dict[str, Any] | None:
    with get_connection(data_dir) as conn:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def get_ticket_by_token(data_dir: str, token: str) -> dict[str, Any] | None:
    with get_connection(data_dir) as conn:
        row = conn.execute("SELECT * FROM tickets WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def find_ticket_by_conversation(data_dir: str, conversation_id: str) -> dict[str, Any] | None:
    with get_connection(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    return dict(row) if row else None


def update_ticket_discord(
    data_dir: str,
    ticket_id: str,
    *,
    guild_id: str,
    thread_id: str,
    user_id: str,
    status: str = "open",
) -> None:
    with get_connection(data_dir) as conn:
        conn.execute(
            """UPDATE tickets
               SET discord_guild_id = ?, discord_thread_id = ?, discord_user_id = ?, status = ?
               WHERE ticket_id = ?""",
            (guild_id, thread_id, user_id, status, ticket_id),
        )


def store_conversation(data_dir: str, conversation_id: str, *, summary: str | None, transcript: list[dict[str, Any]]) -> None:
    with get_connection(data_dir) as conn:
        conn.execute(
            """INSERT INTO conversations (conversation_id, summary, transcript_json, received_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                   summary = excluded.summary,
                   transcript_json = excluded.transcript_json,
                   received_at = excluded.received_at""",
            (conversation_id, summary, json.dumps(transcript), _iso(_now())),
        )


def get_conversation(data_dir: str, conversation_id: str) -> dict[str, Any] | None:
    with get_connection(data_dir) as conn:
        row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["transcript"] = json.loads(result.pop("transcript_json") or "[]")
    return result
