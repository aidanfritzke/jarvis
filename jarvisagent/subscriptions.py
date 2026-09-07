"""Store of Web Push subscriptions (one per browser/device that opted in).

A subscription is the JSON object the browser hands back from pushManager.subscribe():
{endpoint, keys:{p256dh, auth}}. We store it keyed by endpoint and hand the set to pywebpush.
"""
from __future__ import annotations

import json
import os
import sqlite3


class SubscriptionStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db = db_path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS subs (endpoint TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db)

    def add(self, sub: dict) -> None:
        endpoint = sub.get("endpoint")
        if not endpoint:
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO subs (endpoint, data) VALUES (?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET data = excluded.data",
                (endpoint, json.dumps(sub)),
            )

    def all(self) -> list[dict]:
        with self._conn() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM subs")]

    def remove(self, endpoint: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM subs WHERE endpoint = ?", (endpoint,))
