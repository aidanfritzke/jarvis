"""Append-only audit log.

Every tool call and every consequential outbound action (email send, calendar write,
web search) is recorded here as one JSON object per line. This is the artifact that
*proves* the security controls hold — review it, and check it during the red-team pass.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone


class AuditLog:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()

    def record(self, action: str, **fields) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False)
        # Append-only; one line per event. Lock guards concurrent requests in-process.
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
