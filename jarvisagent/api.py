"""FastAPI service exposing the Jarvis agent (async job + poll model).

Because the model is slow on CPU and phones kill long requests when backgrounded, /chat
returns a job ticket immediately and runs the agent in the background; the client polls
/chat/status/<id> and shows live progress. Backgrounding the app is safe.

Endpoints:
  POST /chat              — start a turn; returns {job_id, status}
  GET  /chat/status/{id}  — poll a job: {status, progress, reply, pending, error}
  POST /confirm           — execute the session's pending action (human-in-the-loop gate)
  POST /capture           — frictionless quick-capture to inbox/ (NO LLM; mirrors Ctrl+Shift+N)
  GET  /health            — liveness
  GET  /                  — the mobile chat PWA

Run:  uvicorn jarvisagent.api:app --host 0.0.0.0 --port 8765   (ONE worker; jobs are in-memory)
"""
from __future__ import annotations

import asyncio
import pathlib
import re
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import load_config
from .inference import chat, get_runtime
from .jobs import JobStore
from .memory import History, tail_history
from .subscriptions import SubscriptionStore
from .webpush import WebPush

cfg = load_config()
rt = get_runtime(cfg)          # builds the agent + tools + singletons (fails fast on bad config)
history_store = History(cfg.memory.db_path)
jobs = JobStore()
subs = SubscriptionStore("/data/subs.db")
pusher = WebPush(cfg.vapid, subs) if cfg.vapid.enabled else None

app = FastAPI(title="Jarvis", version="0.3.0")

# Minimal mobile chat UI, served at / so the phone/desktop browser works over Tailscale.
_UI = (pathlib.Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _UI


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Jarvis",
            "short_name": "Jarvis",
            "display": "standalone",
            "start_url": "/",
            "background_color": "#16171a",
            "theme_color": "#16171a",
            "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}],
        },
        media_type="application/manifest+json",
    )


@app.get("/icon.png")
def icon() -> FileResponse:
    return FileResponse(pathlib.Path(__file__).parent / "icon.png", media_type="image/png")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Service-Worker-Allowed: / lets a worker served at /sw.js control the whole app scope.
    return FileResponse(
        pathlib.Path(__file__).parent / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/push/vapid")
def push_vapid() -> dict:
    """The Application Server Key the browser needs to subscribe."""
    return {"key": cfg.vapid.public_key}


@app.post("/push/subscribe")
def push_subscribe(sub: dict = Body(...)) -> dict:
    """Store a browser's push subscription (from pushManager.subscribe())."""
    subs.add(sub)
    rt.audit.record("push.subscribe", endpoint=str(sub.get("endpoint", ""))[:60])
    return {"ok": True}


@app.post("/push/test")
def push_test() -> dict:
    """Send a test push to all subscribed devices (verify the pipeline)."""
    if pusher is None:
        raise HTTPException(status_code=400, detail="push not configured (vapid.enabled=false)")
    sent = pusher.send_to_all("Jarvis", "Test notification 🎉")
    rt.audit.record("push.test", sent=sent)
    return {"ok": True, "sent": sent}


class NotifyIn(BaseModel):
    token: str
    message: str
    title: str = "Jarvis"


@app.post("/notify")
def post_notify(inp: NotifyIn) -> dict:
    """Generic notification webhook: an authenticated external trigger → a push to your devices.
    Gated by a shared token so only your own scripts/devices can fire it, even though it's reachable
    over the tailnet. Use for any 'ping my phone when X happens' automation (backups, cron jobs,
    home sensors, etc.). POST {"token": "...", "message": "Backup finished", "title": "Server"}."""
    import hmac
    if not cfg.notify_token or not hmac.compare_digest(inp.token, cfg.notify_token):
        raise HTTPException(status_code=403, detail="bad token")
    if pusher is None:
        raise HTTPException(status_code=400, detail="push not configured")
    sent = pusher.send_to_all(inp.title, inp.message)
    rt.audit.record("notify", title=inp.title, message=inp.message, sent=sent)
    return {"ok": True, "sent": sent}


@app.on_event("startup")
async def _start_background_loops() -> None:
    asyncio.create_task(_reminder_loop())


async def _reminder_loop() -> None:
    """Every 30s, fire any due reminders as a push. Reminders that came due while the app was
    down fire on the next check (they persist in SQLite)."""
    while True:
        try:
            if pusher is not None:
                now = datetime.now(timezone.utc)
                for rid, text in rt.reminders.due(now):
                    pusher.send_to_all("Reminder", text)
                    rt.reminders.mark_fired(rid)
                    rt.audit.record("reminder.fired", text=text)
        except Exception as e:  # noqa: BLE001 - never let the loop die
            rt.audit.record("reminder.loop.error", error=str(e))
        await asyncio.sleep(30)


class ChatIn(BaseModel):
    message: str
    session_id: str = "default"


class CaptureIn(BaseModel):
    text: str


class ConfirmIn(BaseModel):
    session_id: str = "default"


_CONFIRM_WORDS = {"confirm", "confirm send", "send it", "yes send it", "yes, send it"}


async def _run_job(job, message: str, session_id: str) -> None:
    """Background worker for one chat turn."""
    try:
        # Type-to-confirm: if a consequential action is queued and the user typed "confirm",
        # run it directly (no model needed). Anything else falls through to the agent, which
        # sees the prior draft in history and can revise it ("propose a change").
        if message.strip().lower() in _CONFIRM_WORDS:
            action = rt.pending.take(session_id)
            if action is None:
                job.reply = "Nothing to confirm right now."
            else:
                job.progress = "Working…"
                job.reply = action.execute()
            job.status = "done"
            return


        job.progress = "Thinking…"
        history = tail_history(history_store.load(session_id))  # sliding window; keeps it fast

        def _progress(msg: str) -> None:
            job.progress = msg

        result = await chat(message, session_id, history, progress=_progress)
        history_store.save(session_id, result.all_messages())

        pa = rt.pending.peek(session_id)
        if pa is not None and pa.kind == "gmail.send":
            # Guarantee the identical draft-confirmation format every time, no matter how the
            # model phrases its own reply.
            job.reply = pa.detail
            job.pending = None
        else:
            job.reply = result.output
            job.pending = {"id": pa.id, "kind": pa.kind, "summary": pa.summary} if pa else None
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.progress = "Cancelled"
        raise
    except Exception as e:  # noqa: BLE001 - report failures back through the job
        rt.audit.record("chat.error", session=session_id, error=str(e))
        job.error = str(e)
        job.status = "error"


@app.get("/health")
def health() -> dict:
    return {"ok": True}


_tasks: dict[str, asyncio.Task] = {}


@app.post("/chat")
async def post_chat(inp: ChatIn) -> dict:
    """Start a turn. Returns a job ticket immediately; poll /chat/status/<id>."""
    for jid in [k for k, t in _tasks.items() if t.done()]:
        _tasks.pop(jid, None)  # drop finished task handles
    job = jobs.create()
    _tasks[job.id] = asyncio.create_task(_run_job(job, inp.message, inp.session_id))
    return {"job_id": job.id, "status": job.status}


@app.post("/chat/cancel/{job_id}")
def chat_cancel(job_id: str) -> dict:
    """Cancel an in-progress job (the phone's Cancel button)."""
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    job = jobs.get(job_id)
    if job is not None and job.status == "working":
        job.status = "cancelled"
        job.progress = "Cancelled"
    return {"ok": True}


@app.get("/chat/status/{job_id}")
def chat_status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown or expired job")
    return {
        "status": job.status,
        "progress": job.progress,
        "reply": job.reply,
        "pending": job.pending,
        "error": job.error,
    }


@app.post("/confirm")
def post_confirm(inp: ConfirmIn) -> dict:
    """Execute the pending action for this session (send email / delete event / ...)."""
    action = rt.pending.take(inp.session_id)
    if action is None:
        raise HTTPException(status_code=404, detail="no pending action for this session")
    try:
        result = action.execute()
    except Exception as e:  # noqa: BLE001
        rt.audit.record("confirm.error", session=inp.session_id, kind=action.kind, error=str(e))
        raise HTTPException(status_code=502, detail=f"action failed: {e}") from e
    return {"ok": True, "kind": action.kind, "result": result}


@app.post("/chat/reset")
def post_reset(inp: ConfirmIn) -> dict:
    """Start a fresh conversation: clear this session's history and any queued action."""
    history_store.clear(inp.session_id)
    rt.pending.take(inp.session_id)  # discard any pending send/delete
    return {"ok": True}


@app.post("/capture")
def post_capture(inp: CaptureIn) -> dict:
    """Quick-capture a fleeting note to inbox/. Deterministic — no model involved."""
    if not inp.text.strip():
        raise HTTPException(status_code=400, detail="empty note")
    rel = rt.notes.capture(inp.text)
    rt.audit.record("notes.capture", session="capture-endpoint", rel=rel)
    return {"ok": True, "rel": rel}
