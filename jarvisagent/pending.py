"""Pending-action store for human-in-the-loop confirmation.

Consequential actions (send email, update/delete calendar events) are NOT executed by
the agent directly. The agent can only *propose* them: the proposal is parked here keyed
by session, surfaced to the user, and executed only when the user POSTs /confirm. This
means even a prompt-injected model cannot send/delete on its own — a human must approve.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Callable


@dataclass
class PendingAction:
    id: str
    kind: str            # e.g. "gmail.send", "calendar.delete"
    summary: str         # short human-readable description (used by the pending banner)
    execute: Callable[[], str]  # performs the action, returns a result string
    detail: str = ""     # full text to show the user (e.g. the email draft)


class PendingStore:
    """One pending action per session (a new proposal supersedes an old one)."""

    def __init__(self) -> None:
        self._by_session: dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    def propose(
        self, session_id: str, kind: str, summary: str, execute: Callable[[], str], detail: str = ""
    ) -> PendingAction:
        action = PendingAction(
            id=uuid.uuid4().hex[:8], kind=kind, summary=summary, execute=execute, detail=detail
        )
        with self._lock:
            self._by_session[session_id] = action
        return action

    def peek(self, session_id: str) -> PendingAction | None:
        with self._lock:
            return self._by_session.get(session_id)

    def take(self, session_id: str) -> PendingAction | None:
        """Atomically remove and return the pending action (so it can run exactly once)."""
        with self._lock:
            return self._by_session.pop(session_id, None)
