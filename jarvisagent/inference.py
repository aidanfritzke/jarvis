"""The Jarvis agent: Pydantic AI wired to the local Ollama model, with tools.

Security-relevant design:
  - Consequential actions (send email, update/delete events) are NEVER executed by a tool.
    The tool only *proposes*; the proposal is parked in PendingStore and must be confirmed by
    the user via POST /confirm. So a prompt-injected model cannot send/delete on its own.
  - Safe actions (read notes, capture to inbox, search web/email, draft email, create event)
    run directly. Every call is written to the audit log.

Each tool also reports a short progress string via ctx.deps.progress(...) so the UI can show
what Jarvis is doing while the (slow, CPU-bound) model works.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from .audit import AuditLog
from .config import Config, load_config
from .pending import PendingStore
from .reminders import ReminderStore
from .tools.notes import NotesIndex
from .tools.web import WebSearch

# The user's timezone — used to give the model an accurate "now" each turn so it can
# compute calendar times correctly. Change if the server/user moves.
LOCAL_TZ = "America/Phoenix"


def _noop(_msg: str) -> None:
    pass


@dataclass
class JarvisDeps:
    notes: NotesIndex
    web: WebSearch | None
    audit: AuditLog
    pending: PendingStore
    reminders: ReminderStore
    session_id: str
    progress: Callable[[str], None] = field(default=_noop)


@dataclass
class Runtime:
    cfg: Config
    agent: Agent
    notes: NotesIndex
    web: WebSearch | None
    audit: AuditLog
    pending: PendingStore
    reminders: ReminderStore


_runtime: Runtime | None = None


# ----------------------------- tools -----------------------------
# All tools take RunContext[JarvisDeps]. Read/safe-write tools act directly; mutating tools
# (send/update/delete) only enqueue a PendingAction for human confirmation. Each reports a
# short progress line for the UI.

def search_notes(ctx: RunContext[JarvisDeps], query: str) -> str:
    """Search the user's personal notes (the vault) for a query. Returns matching notes with snippets."""
    ctx.deps.progress("Searching your notes…")
    ctx.deps.audit.record("notes.search", session=ctx.deps.session_id, query=query)
    hits = ctx.deps.notes.search(query, k=5)
    if not hits:
        return "(no matching notes)"
    return "\n\n".join(f"[{h['rel']}] {h['snippet']}" for h in hits)


def read_note(ctx: RunContext[JarvisDeps], rel: str) -> str:
    """Read a note's full text by its vault-relative path (e.g. 'ideas/foo.md')."""
    ctx.deps.progress("Reading a note…")
    ctx.deps.audit.record("notes.read", session=ctx.deps.session_id, rel=rel)
    try:
        return ctx.deps.notes.read(rel)
    except (OSError, ValueError) as e:
        return f"error: {e}"


def capture_note(ctx: RunContext[JarvisDeps], text: str) -> str:
    """Save a quick fleeting note into the inbox (timestamped). Use for 'jot this down' requests."""
    ctx.deps.progress("Saving to your inbox…")
    rel = ctx.deps.notes.capture(text)
    ctx.deps.audit.record("notes.capture", session=ctx.deps.session_id, rel=rel)
    return f"Saved to {rel}."


def web_search(ctx: RunContext[JarvisDeps], query: str) -> str:
    """Search the web for current information. Returns titles, URLs, and snippets."""
    ctx.deps.progress("Searching the web…")
    if ctx.deps.web is None:
        return "Web search is not configured."
    ctx.deps.audit.record("web.search", session=ctx.deps.session_id, query=query)
    try:
        hits = ctx.deps.web.search(query, k=5)
    except Exception as e:  # noqa: BLE001 - surface backend errors to the model as text
        return f"web search error: {e}"
    if not hits:
        return "(no results)"
    return "\n\n".join(f"{h['title']}\n{h['url']}\n{h['content']}" for h in hits)
    
def get_todo_list(ctx: RunContext[JarvisDeps]) -> str:
    """Show the user's to-do list (a simple text checklist). This is NOT the calendar —
    use this for 'what's on my to-do list', 'what do I need to do', not for events or dates.
    Returns each item with its index (needed to remove one)."""
    ctx.deps.progress("Checking your to-do list…")
    ctx.deps.audit.record("notes.list_todos", session=ctx.deps.session_id)
    items = ctx.deps.notes.list_todos()
    if not items:
        return "(no open to-dos)"
    return "\n".join(f"[{i['index']}] {i['text']}" for i in items)


def add_todo(ctx: RunContext[JarvisDeps], text: str) -> str:
    """Add an item to the to-do checklist. This is NOT a calendar event — use for
    'add X to my to-do list'."""
    ctx.deps.progress("Adding to your to-do list…")
    rel = ctx.deps.notes.add_todo(text)
    ctx.deps.audit.record("notes.add_todo", session=ctx.deps.session_id, rel=rel, text=text)
    return f"Added to {rel}."


def remove_todo(ctx: RunContext[JarvisDeps], index: int) -> str:
    """Remove an item from the to-do checklist by its index. Call get_todo_list first to
    get the correct current index. This does NOT touch the calendar."""
    ctx.deps.progress("Marking that done…")
    try:
        rel = ctx.deps.notes.complete_todo(index)
    except (OSError, ValueError, IndexError) as e:
        return f"error: {e}"
    ctx.deps.audit.record("notes.complete_todo", session=ctx.deps.session_id, rel=rel, index=index)
    return f"Removed from {rel}."


def set_reminder(ctx: RunContext[JarvisDeps], text: str, in_minutes: int = 0, at_iso: str = "") -> str:
    """Remind the user later with a phone notification. Give EITHER in_minutes (relative, e.g. 60 for
    'in an hour', 30 for 'in half an hour') OR at_iso (an absolute ISO 8601 time)."""
    ctx.deps.progress("Setting a reminder…")
    if at_iso:
        try:
            due = datetime.fromisoformat(at_iso)
        except ValueError:
            return "I couldn't parse that time — try 'in 30 minutes' or a specific time like 3pm."
        if due.tzinfo is None:
            due = due.replace(tzinfo=ZoneInfo(LOCAL_TZ))
    else:
        try:
            mins = int(in_minutes)
        except (TypeError, ValueError):
            mins = 0
        if mins <= 0:
            return "Tell me when — e.g. 'in 30 minutes' or 'at 3pm'."
        due = datetime.now(timezone.utc) + timedelta(minutes=mins)
    due_utc = due.astimezone(timezone.utc)
    ctx.deps.reminders.add(text, due_utc)
    ctx.deps.audit.record("reminder.set", session=ctx.deps.session_id, text=text, due=due_utc.isoformat())
    local = due_utc.astimezone(ZoneInfo(LOCAL_TZ))
    return f"Reminder set for {local:%A %-I:%M %p}: {text}"


_TOOLS = [
    search_notes, read_note, capture_note, 
    web_search,
    add_todo, remove_todo, get_todo_list,
    set_reminder,
]


# ----------------------------- build -----------------------------

def get_runtime(cfg: Config | None = None) -> Runtime:
    global _runtime
    if _runtime is not None:
        return _runtime
    cfg = cfg or load_config()

    model = OpenAIChatModel(
        cfg.ollama.chat_model,
        provider=OpenAIProvider(base_url=cfg.ollama.base_url, api_key=cfg.ollama.api_key),
    )
    agent = Agent(
        model=model,
        deps_type=JarvisDeps,
        system_prompt=cfg.agent.system_prompt,
        # Low temperature = more reliable tool calls; max_tokens caps rambling (big CPU-time saver).
        model_settings=ModelSettings(temperature=0.2, max_tokens=400),
    )

    # Dynamic system prompt: inject the real current date/time every turn. A 3B model
    # cannot know "today", so without this it hallucinates calendar timestamps.
    @agent.system_prompt
    def _now() -> str:
        now = datetime.now(ZoneInfo(LOCAL_TZ))
        return (
            f"The current date and time is {now:%A, %Y-%m-%d %H:%M %Z}. "
            f"When creating calendar events, express start/end as ISO 8601 relative to this."
        )

    for fn in _TOOLS:
        agent.tool(fn)

    notes = NotesIndex(cfg.notes.path, cfg.notes.db_path, cfg.notes.inbox_subdir)
    web = WebSearch(cfg.searxng.base_url) if cfg.searxng.enabled else None
    audit = AuditLog(cfg.audit.log_path)
    pending = PendingStore()
    reminders = ReminderStore("/data/reminders.db")

    _runtime = Runtime(
        cfg=cfg, agent=agent, notes=notes, web=web,
        audit=audit, pending=pending, reminders=reminders,
    )
    return _runtime


async def chat(
    message: str,
    session_id: str,
    history: list[ModelMessage] | None = None,
    progress: Callable[[str], None] | None = None,
):
    """Run one turn. Returns the Pydantic AI result (use .output and .all_messages())."""
    rt = get_runtime()
    deps = JarvisDeps(
        notes=rt.notes, web=rt.web,
        audit=rt.audit, pending=rt.pending, reminders=rt.reminders, session_id=session_id,
        progress=progress or _noop,
    )
    return await rt.agent.run(message, message_history=history or [], deps=deps)
