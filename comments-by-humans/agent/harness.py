"""The comments-by-humans agent: a coding loop for any model, with the gate in its tool layer.

The model can act only through the tools defined here, and every tool runs the same gate
checks as the Claude Code hooks (scripts/gate.py): writes must carry a placeholder, a new
chunk locks the gate, a locked gate denies every write and command, and only the human's
typed /pause lifts it. The model has no tool for pausing, so pausing is human-only by
construction rather than by instruction.
"""

import fnmatch
import html
import json
import os
import re
import shlex
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import gate as engine  # noqa: E402

MAX_RESULT = 30000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target"}


def _tool(name, description, properties, required, primary=None):
    return {"name": name, "description": description, "primary": primary,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


S = {"type": "string"}
TOOLS = [
    _tool("read_file", "Read a text file in the project. Returns numbered lines.",
          {"path": S, "offset": {"type": "integer", "description": "first line, 1-based"},
           "limit": {"type": "integer", "description": "number of lines"}}, ["path"], "path"),
    _tool("list_files", "List files under a directory, optionally filtered by a glob pattern.",
          {"path": S, "pattern": S}, [], "path"),
    _tool("search", "Search file contents with a regular expression. Returns file:line: text.",
          {"pattern": S, "path": S}, ["pattern"], "pattern"),
    _tool("write_file", "Create or overwrite a file with the full content. New code needs an empty "
          "EXPLAIN(human) placeholder above it.", {"path": S, "content": S}, ["path", "content"], "content"),
    _tool("edit_file", "Replace exact text in a file. old_string must appear exactly once unless "
          "replace_all is true.", {"path": S, "old_string": S, "new_string": S,
                                   "replace_all": {"type": "boolean"}},
          ["path", "old_string", "new_string"]),
    _tool("run_command", "Run a shell command in the project root. Denied while the gate is locked.",
          {"command": S}, ["command"], "command"),
    _tool("gate", "The comments-by-humans gate: status, show [id], followup <id>, approve <id>, next, "
          "finding <id> \"text\", next-id.", {"command": S}, ["command"], "command"),
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}

TEXT_PROTOCOL = """
## How to call tools

This conversation has no built-in tool calling. To call a tool, write a tool block. You may write
a short sentence before it. Write nothing after your last tool block: the agent runs the blocks in
order and answers with their results. Without a tool block, your reply ends your turn.

<tool name="read_file" path="src/app.py"/>

<tool name="list_files" path="src" pattern="*.py"/>

<tool name="search" pattern="def retry" path="src"/>

<tool name="write_file" path="src/app.py">
the complete file content, exactly as it should be on disk
</tool>

<tool name="edit_file" path="src/app.py">
<old_string>the exact text to replace</old_string>
<new_string>the replacement text</new_string>
</tool>

<tool name="run_command">python3 -m pytest -q</tool>

<tool name="gate">approve c01</tool>

Attribute values cannot contain double quotes; put such text in the block body instead.
"""

_TOOL_RE = re.compile(r'<tool\s+name="([\w-]+)"((?:\s+[\w-]+="[^"]*")*)\s*(?:/>|>(.*?)</tool>)', re.S)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def _trim_one(text, leading_only=False):
    if text.startswith("\r\n"):
        text = text[2:]
    elif text.startswith("\n"):
        text = text[1:]
    if not leading_only and text.endswith("\n"):
        text = text[:-1]
    return text


def parse_text_calls(text):
    """Tool blocks in a reply -> (calls, the reply's text without them)."""
    calls = []
    for k, m in enumerate(_TOOL_RE.finditer(text)):
        name, attrs, body = m.group(1), m.group(2), m.group(3)
        args = {key: html.unescape(value) for key, value in _ATTR_RE.findall(attrs or "")}
        spec = TOOLS_BY_NAME.get(name)
        if body is not None and spec:
            stripped = body.strip()
            parsed = None
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    parsed = None
            if isinstance(parsed, dict) and name not in ("write_file",):
                args.update(parsed)
            else:
                found = False
                for param in spec["parameters"]["properties"]:
                    sub = re.search(r"<%s>(.*?)</%s>" % (param, param), body, re.S)
                    if sub:
                        args[param] = _trim_one(sub.group(1))
                        found = True
                if not found and spec["primary"]:
                    args[spec["primary"]] = _trim_one(body, leading_only=spec["primary"] == "content")
        for key in ("offset", "limit"):
            if isinstance(args.get(key), str) and args[key].isdigit():
                args[key] = int(args[key])
        if isinstance(args.get("replace_all"), str):
            args["replace_all"] = args["replace_all"].lower() == "true"
        calls.append({"id": "text_%d" % k, "name": name, "args": args})
    visible = _TOOL_RE.sub("", text)
    visible = re.sub(r"```[a-zA-Z]*\s*```", "", visible).strip()
    return calls, visible


def plain_messages(messages):
    """The conversation as plain user/assistant text, for models without native tool calls."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            text = "[result of %s%s]\n%s" % (m["name"], ", error" if m.get("error") else "", m["content"])
            role = "user"
        else:
            text, role = m["content"], m["role"]
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    return out


HELP = """Commands: /build <task> · /review <path or diff range> [--inline] · /pause · /status · /help · /quit
Write your explanations in the source file (or the review report) in your own editor, save, then send any message."""


class Agent:
    def __init__(self, project, provider, tool_mode=None, approve_command=None, max_steps=40, on_event=None):
        self.root = engine.api_root(os.path.abspath(project))
        self.provider = provider
        self.tool_mode = tool_mode or ("native" if provider.native_tools else "text")
        if self.tool_mode == "native" and not provider.native_tools:
            raise ValueError("provider %s has no native tool calling; use tool_mode='text'" % provider.label)
        self.approve_command = approve_command or (lambda command: False)
        self.max_steps = max_steps
        self.on_event = on_event or (lambda kind, data: None)
        self.messages = []
        self.events = []
        self.pending = []
        with open(os.path.join(HERE, "prompt.md"), encoding="utf-8") as f:
            self.system = f.read().strip() + "\n\nThe project root is %s." % self.root
        if self.tool_mode == "text":
            self.system += "\n" + TEXT_PROTOCOL

    # human side -------------------------------------------------------------

    def send(self, text):
        """One thing the human typed. Returns everything the assistant said in reply."""
        text = text.strip()
        if text.startswith("/"):
            return self._command(text)
        context = engine.api_prompt(self.root, text, uuid.uuid4().hex)
        notes = self.pending + ([context] if context else [])
        self.pending = []
        return self._turn("".join("[gate] %s\n\n" % n for n in notes) + text)

    def _command(self, text):
        name, _, args = text[1:].partition(" ")
        args = args.strip()
        if name in ("quit", "exit"):
            return ""
        if name == "help":
            return HELP
        if name == "status":
            return engine.api_cli(self.root, ["status"])[1]
        if name in ("build", "review", "pause"):
            context, ok = engine.api_command(self.root, name, args)
            self._event("command", {"command": name, "args": args, "ok": ok, "result": context})
            if not ok:
                return context
            if name == "pause":
                self.pending.append(context)
                return context
            follow = ("Task: %s" % args) if name == "build" and args else (
                "Continue the build." if name == "build" else "Present the current chunk.")
            return self._turn("[gate] %s\n\n%s" % (context, follow))
        return "Unknown command /%s.\n%s" % (name, HELP)

    # model side -------------------------------------------------------------

    def _call(self):
        if self.tool_mode == "native":
            return self.provider.chat(self.system, self.messages, TOOLS_PUBLIC)
        return self.provider.chat(self.system, plain_messages(self.messages), None)

    def _turn(self, content):
        self.messages.append({"role": "user", "content": content})
        self._event("human", content)
        said = []
        for _ in range(self.max_steps):
            reply = self._call()
            if self.tool_mode == "text":
                calls, visible = parse_text_calls(reply.text)
            else:
                calls, visible = reply.calls, reply.text.strip()
            self.messages.append({"role": "assistant", "content": reply.text, "calls": calls,
                                  "raw": reply.raw, "raw_kind": self.provider.kind})
            if visible:
                said.append(visible)
                self._event("assistant", visible)
            if not calls:
                break
            for call in calls:
                result, error = self._run(call)
                self.messages.append({"role": "tool", "id": call["id"], "name": call["name"],
                                      "content": result, "error": error})
        else:
            said.append("[The agent stopped after %d model calls in one turn.]" % self.max_steps)
        return "\n\n".join(said)

    def _event(self, kind, data):
        self.events.append({"kind": kind, "data": data})
        self.on_event(kind, data)

    # tools ------------------------------------------------------------------

    def _run(self, call):
        name, args = call["name"], dict(call.get("args") or {})
        if "__invalid__" in args:
            result = ("The arguments for %s were not valid JSON; call it again with valid arguments."
                      % name, True)
        elif name not in TOOLS_BY_NAME:
            result = ("There is no tool named %r. Tools: %s." % (name, ", ".join(TOOLS_BY_NAME)), True)
        else:
            spec = TOOLS_BY_NAME[name]["parameters"]
            unknown = [k for k in args if k not in spec["properties"]]
            missing = [k for k in spec["required"] if k not in args]
            if unknown or missing:
                result = ("%s: %s%s" % (name, "missing %s. " % ", ".join(missing) if missing else "",
                                        "unknown %s." % ", ".join(unknown) if unknown else ""), True)
            else:
                try:
                    result = getattr(self, "_tool_" + name)(**args)
                except Exception as e:
                    result = ("%s failed: %s: %s" % (name, type(e).__name__, e), True)
        text, error = result
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + "\n[truncated: %d more characters]" % (len(text) - MAX_RESULT)
        self._event("tool", {"name": name, "args": args, "result": text, "error": error})
        return text, error

    def _path(self, path):
        full = os.path.realpath(os.path.join(self.root, path or "."))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise PermissionError("%s is outside the project root" % path)
        return full

    def _rel(self, full):
        return os.path.relpath(full, self.root)

    def _gate_off(self):
        state = engine.api_state(self.root)
        if not state or state["mode"] == "off":
            return ("comments-by-humans: the gate is off, so nothing may be written yet. Ask the human to "
                    "start it with /build <task> or /review <target>.", True)
        return None

    def _tool_read_file(self, path, offset=1, limit=2000):
        full = self._path(path)
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        start = max(1, int(offset))
        chunk = lines[start - 1:start - 1 + int(limit)]
        out = "\n".join("%6d\t%s" % (start + k, line) for k, line in enumerate(chunk))
        return out or "(empty file)", False

    def _walk(self, base):
        for d, dirs, names in os.walk(base):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            for n in sorted(names):
                yield os.path.join(d, n)

    def _tool_list_files(self, path=".", pattern=None):
        base = self._path(path)
        found = []
        for full in self._walk(base):
            rel = self._rel(full)
            if pattern and not (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(rel), pattern)):
                continue
            found.append(rel)
            if len(found) >= 500:
                found.append("[stopped at 500 files]")
                break
        return "\n".join(found) or "(no files)", False

    def _tool_search(self, pattern, path="."):
        rx = re.compile(pattern)
        base = self._path(path)
        hits = []
        files = [base] if os.path.isfile(base) else self._walk(base)
        for full in files:
            try:
                with open(full, encoding="utf-8") as f:
                    for k, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append("%s:%d: %s" % (self._rel(full), k, line.rstrip()[:300]))
                            if len(hits) >= 200:
                                return "\n".join(hits + ["[stopped at 200 matches]"]), False
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) or "(no matches)", False

    def _write_through_gate(self, tool, full, tool_input, apply):
        off = self._gate_off()
        if off:
            return off
        denied = engine.api_pre_tool(self.root, tool, tool_input)
        if denied:
            return "DENIED. " + denied, True
        apply()
        note = engine.api_post_tool(self.root, tool, tool_input)
        done = "%s %s." % ("Wrote" if tool == "Write" else "Edited", self._rel(full))
        return done + ("\n\n" + note if note else ""), False

    def _tool_write_file(self, path, content):
        full = self._path(path)

        def apply():
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        return self._write_through_gate("Write", full, {"file_path": full, "content": content}, apply)

    def _tool_edit_file(self, path, old_string, new_string, replace_all=False):
        full = self._path(path)
        if not os.path.isfile(full):
            return "%s does not exist; use write_file to create it." % path, True
        with open(full, encoding="utf-8", newline="") as f:
            text = f.read()
        count = text.count(old_string) if old_string else 0
        if count == 0:
            return "old_string was not found in %s. Read the file and copy the text exactly." % path, True
        if count > 1 and not replace_all:
            return ("old_string appears %d times in %s; include more surrounding lines, or set "
                    "replace_all." % (count, path)), True

        def apply():
            new = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(new)
        return self._write_through_gate("Edit", full, {"file_path": full, "old_string": old_string,
                                                       "new_string": new_string,
                                                       "replace_all": bool(replace_all)}, apply)

    def _tool_run_command(self, command):
        if re.match(r"^\s*gate(\s|$)", command):
            return self._tool_gate(command.strip()[4:].strip())
        off = self._gate_off()
        if off:
            return off
        denied = engine.api_pre_tool(self.root, "Bash", {"command": command})
        if denied:
            return "DENIED. " + denied, True
        if not self.approve_command(command):
            return "The human declined to run this command.", True
        try:
            out = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True,
                                 timeout=300, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return "The command timed out after 300 seconds.", True
        text = (out.stdout + (("\n" + out.stderr) if out.stderr else "")).strip()
        return "exit code %d\n%s" % (out.returncode, text), out.returncode != 0

    def _tool_gate(self, command):
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return "Could not parse the gate command: %s" % e, True
        if argv and argv[0].lstrip("/") in ("pause", "build", "review"):
            return ("Only the human can %s the gate, by typing /%s themselves."
                    % (argv[0].lstrip("/"), argv[0].lstrip("/"))), True
        code, out = engine.api_cli(self.root, argv or ["help"])
        return out or "(no output)", code != 0


# The schema the model sees: no internal fields.
TOOLS_PUBLIC = [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
                for t in TOOLS]
