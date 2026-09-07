"""Persistent reminder store (SQLite).

A reminder is text + a due time. A background loop (in api.py) polls `due()` and fires a push.
Persisted so reminders survive restarts; anything that came due while the app was down fires on
the next check. Times are stored as UTC ISO strings (lexicographic order == chronological).
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime


class ReminderStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db = db_path
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS reminders ("
                "id TEXT PRIMARY KEY, text TEXT NOT NULL, due_at TEXT NOT NULL, fired INTEGER DEFAULT 0)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db)

    def add(self, text: str, due_at_utc: datetime) -> str:
        rid = uuid.uuid4().hex[:8]
        with self._conn() as c:
            c.execute(
                "INSERT INTO reminders (id, text, due_at, fired) VALUES (?, ?, ?, 0)",
                (rid, text, due_at_utc.isoformat()),
            )
        return rid

    def due(self, now_utc: datetime) -> list[tuple[str, str]]:
        """Return [(id, text)] for unfired reminders whose time has passed."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, text FROM reminders WHERE fired = 0 AND due_at <= ?",
                (now_utc.isoformat(),),
            ).fetchall()
        return rows

    def mark_fired(self, rid: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (rid,))
