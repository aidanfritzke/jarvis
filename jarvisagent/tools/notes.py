"""Notes second-brain: SQLite FTS5 search/read over the vault + frictionless inbox capture.

Why FTS5 and not embeddings: the vault is small (tens of notes), so full-text search finds
the right note instantly with zero extra RAM and no embedding model — and Commonplace already
owns the semantic index on the desktop. Jarvis writes ONLY under the inbox subdir.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime


def _is_md(name: str) -> bool:
    return name.lower().endswith(".md")


class NotesIndex:
    def __init__(self, vault_path: str, db_path: str, inbox_subdir: str = "inbox", todo_subdir: str = "to-do"):
        self.vault = os.path.realpath(vault_path)
        self.inbox_subdir = inbox_subdir
        self.todo_subdir = todo_subdir
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        with self._conn() as c:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(rel, content)")
            c.execute("CREATE TABLE IF NOT EXISTS files (rel TEXT PRIMARY KEY, mtime REAL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    # ---- path safety ----
    def _resolve(self, rel: str) -> str:
        target = os.path.realpath(os.path.join(self.vault, rel))
        if target != self.vault and not target.startswith(self.vault + os.sep):
            raise ValueError("refused: path is outside the vault")
        return target

    # ---- indexing ----
    def refresh(self) -> int:
        """Incrementally sync the FTS index with the vault (by mtime). Returns files re-indexed."""
        on_disk: dict[str, float] = {}
        for root, dirs, names in os.walk(self.vault):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for nm in names:
                if _is_md(nm):
                    full = os.path.join(root, nm)
                    rel = os.path.relpath(full, self.vault).replace("\\", "/")
                    try:
                        on_disk[rel] = os.path.getmtime(full)
                    except OSError:
                        pass
        changed = 0
        with self._conn() as c:
            known = {row[0]: row[1] for row in c.execute("SELECT rel, mtime FROM files")}
            # new or modified
            for rel, mtime in on_disk.items():
                if known.get(rel) == mtime:
                    continue
                try:
                    with open(self._resolve(rel), "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                c.execute("DELETE FROM notes_fts WHERE rel = ?", (rel,))
                c.execute("INSERT INTO notes_fts (rel, content) VALUES (?, ?)", (rel, content))
                c.execute(
                    "INSERT INTO files (rel, mtime) VALUES (?, ?) "
                    "ON CONFLICT(rel) DO UPDATE SET mtime = excluded.mtime",
                    (rel, mtime),
                )
                changed += 1
            # deletions
            for rel in set(known) - set(on_disk):
                c.execute("DELETE FROM notes_fts WHERE rel = ?", (rel,))
                c.execute("DELETE FROM files WHERE rel = ?", (rel,))
        return changed

    # ---- read tools ----
    def search(self, query: str, k: int = 5) -> list[dict]:
        self.refresh()
        with self._conn() as c:
            try:
                rows = c.execute(
                    "SELECT rel, snippet(notes_fts, 1, '«', '»', ' … ', 12) AS snip "
                    "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, k),
                ).fetchall()
            except sqlite3.OperationalError:
                # FTS MATCH syntax error on odd queries → fall back to a quoted phrase match.
                safe = '"' + query.replace('"', " ") + '"'
                rows = c.execute(
                    "SELECT rel, snippet(notes_fts, 1, '«', '»', ' … ', 12) AS snip "
                    "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (safe, k),
                ).fetchall()
        return [{"rel": r, "snippet": s} for r, s in rows]

    def read(self, rel: str) -> str:
        with open(self._resolve(rel), "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ---- write tool (inbox only) ----
    def capture(self, text: str) -> str:
        """Write a timestamped fleeting note into the inbox (mirrors Commonplace Ctrl+Shift+N)."""
        inbox = self._resolve(self.inbox_subdir)
        os.makedirs(inbox, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        # avoid clobbering a same-minute capture
        rel = f"{self.inbox_subdir}/{stamp}.md"
        target = self._resolve(rel)
        n = 1
        while os.path.exists(target):
            rel = f"{self.inbox_subdir}/{stamp}-{n}.md"
            target = self._resolve(rel)
            n += 1
        with open(target, "w", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
        return rel

    # ---- write tool (todo list) ----
    def add_todo(self, text: str) -> str:
        """Append a to-do item to the living list (different folder from inbox)."""
        todo_dir = self._resolve(self.todo_subdir)
        os.makedirs(todo_dir, exist_ok=True)
        rel = f"{self.todo_subdir}/todo.md"
        target = self._resolve(rel)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"- [ ] {text.rstrip()} ({stamp})\n"
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)
        return rel
    
    # ---- read tool (todo list) ----
    def list_todos(self) -> list[dict]:
        """List open to-dos with their line index (for use with complete_todo)."""
        rel = f"{self.todo_subdir}/todo.md"
        target = self._resolve(rel)
        if not os.path.exists(target):
            return []

        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()

        return [
            {"index": i, "text": ln[len("- [ ] "):].rstrip("\n")}
            for i, ln in enumerate(lines)
            if ln.startswith("- [ ] ")
        ]
    
    # ---- write tool (todo list) ----
    def complete_todo(self, index: int) -> str:
        """Remove the to-do at the given line index (in place, same file)."""
        rel = f"{self.todo_subdir}/todo.md"
        target = self._resolve(rel)
        if not os.path.exists(target):
            raise ValueError("no to-do list yet")

        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if not (0 <= index < len(lines)):
            raise IndexError(f"index {index} out of range (file has {len(lines)} lines)")
        if not lines[index].startswith("- [ ] "):
            raise ValueError(f"line {index} is not an open to-do")

        del lines[index]

        with open(target, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return rel