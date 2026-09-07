"""In-memory job store for async /chat processing (job + poll).

The model is slow on CPU, and mobile browsers kill a long-held request the moment you
background the app. So /chat returns a job ticket immediately, runs the agent in the
background, and the client polls /chat/status/<id>. Backgrounding is safe — polling
resumes when you return and the finished (or in-progress) result is right there.

Single-process, in-memory: uvicorn must run ONE worker (it does by default).
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Job:
    id: str
    status: str = "working"        # working | done | error
    progress: str = "Thinking…"    # human-readable current activity
    reply: str | None = None
    pending: dict | None = None
    error: str | None = None
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class JobStore:
    def __init__(self, max_jobs: int = 100):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max = max_jobs

    def create(self) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _prune(self) -> None:
        # Drop oldest jobs once we exceed the cap (single-user: this is generous).
        excess = len(self._jobs) - self._max
        if excess <= 0:
            return
        oldest = sorted(self._jobs, key=lambda j: self._jobs[j].created)[:excess]
        for jid in oldest:
            self._jobs.pop(jid, None)
