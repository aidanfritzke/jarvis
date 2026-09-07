"""Conversation memory: persists Pydantic AI message history per session in SQLite.

History is stored as a JSON blob per session_id using Pydantic AI's message serialization,
so it round-trips cleanly back into agent.run(message_history=...).
"""
from __future__ import annotations

import os
import sqlite3

import pydantic_core
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, UserPromptPart

# How many recent ModelMessages to feed the model. The model re-reads the whole history every
# turn, so on a small CPU model an unbounded history means ever-slower, ever-dumber replies.
# We keep a sliding window (older turns fall off). ~16 entries ≈ the last handful of exchanges.
HISTORY_MAX = 16


def tail_history(messages: list[ModelMessage], max_msgs: int = HISTORY_MAX) -> list[ModelMessage]:
    """Return the most recent messages, trimmed to start cleanly at a user turn (so we never
    begin with a dangling tool result or half a request/response pair)."""
    if len(messages) <= max_msgs:
        return messages
    tail = messages[-max_msgs:]
    for i, msg in enumerate(tail):
        if isinstance(msg, ModelRequest) and any(isinstance(p, UserPromptPart) for p in msg.parts):
            return tail[i:]
    return tail  # fallback: no clean boundary found


class History:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "session_id TEXT PRIMARY KEY, "
                "messages TEXT NOT NULL, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def load(self, session_id: str) -> list[ModelMessage]:
        with self._conn() as c:
            row = c.execute(
                "SELECT messages FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return []
        return ModelMessagesTypeAdapter.validate_json(row[0])

    def save(self, session_id: str, messages: list[ModelMessage]) -> None:
        blob = pydantic_core.to_json(
            pydantic_core.to_jsonable_python(messages)
        ).decode("utf-8")
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions (session_id, messages, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "messages = excluded.messages, updated_at = excluded.updated_at",
                (session_id, blob),
            )

    def clear(self, session_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
