#!/usr/bin/env python3
"""comments-by-humans gate: hook entry points, state machine and gate CLI.

Hooks call `gate.py hook <name>` with the hook payload on stdin. Claude calls
`gate.py <command>` through Bash. Everything that must not depend on Claude's
judgment lives here: the lock, placeholder rules, hashes, approval checks,
review ordering and the report.

Fail closed: any internal error exits 2, which blocks the action for the hook
events that can block.
"""

import json
import os
import re
import sys
import time

PLUGIN = "comments-by-humans"
STATE_DIR = ".comments-by-humans"
SCRIPT = os.path.realpath(os.path.abspath(__file__))
if os.path.dirname(SCRIPT) not in sys.path:
    sys.path.insert(0, os.path.dirname(SCRIPT))
PLUGIN_ROOT = os.path.dirname(os.path.dirname(SCRIPT))
GATE = 'python3 "%s"' % SCRIPT

CLAUDE_COMMANDS = ("status", "approve", "followup", "next", "finding", "show", "next-id", "help")
SKILLS = ("build", "review", "pause", "status")

DEFAULT_EXEMPT = [
    "**/*.lock", "**/package-lock.json", "**/npm-shrinkwrap.json", "**/pnpm-lock.yaml",
    "**/yarn.lock", "**/poetry.lock", "**/Pipfile.lock", "**/Cargo.lock", "**/Gemfile.lock",
    "**/composer.lock", "**/go.sum", "**/uv.lock", "**/bun.lockb",
    "dist/**", "build/**", "out/**", "target/**", ".next/**", "coverage/**", "node_modules/**",
    "**/__pycache__/**", "**/*.min.js", "**/*.min.css", "**/*.map",
    "**/*_pb2.py", "**/*_pb2_grpc.py", "**/*.pb.go", "**/*.generated.*", "**/*.g.dart",
    "**/*.json", "**/*.jsonc", "**/*.yaml", "**/*.yml", "**/*.toml", "**/*.ini", "**/*.cfg",
    "**/*.conf", "**/*.env", "**/.env*", "**/*.csv", "**/*.tsv", "**/*.xml", "**/*.md",
    "**/*.mdx", "**/*.txt", "**/*.rst", "**/*.svg", "**/.gitignore", "**/.gitattributes",
    "**/.editorconfig", "**/.dockerignore", "**/LICENSE*",
]
DEFAULT_CONFIG = {
    "depth": "strict",
    "target_chunk_lines": 40,
    "min_words": 5,
    "exempt": DEFAULT_EXEMPT,
    "review": {"inline": False},
}
DEPTHS = ("light", "normal", "strict")

RUBRIC = {
    "light": "What and Why",
    "normal": "What, Why, Connections and Catch",
    "strict": "What, Why, Connections and Catch, plus one follow-up question answered in chat",
}


class Refusal(Exception):
    """A CLI request the gate refuses. The message goes back to the assistant."""


class Denied(Exception):
    """A hook decision that blocks the action. The message explains why."""


# ---------------------------------------------------------------------------
# Small utilities


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_text(path):
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_text(path, text):
    tmp = path + ".cbh-tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except OSError:
        pass
    os.replace(tmp, path)


def glob_to_regex(pattern):
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def is_exempt(rel, cfg):
    rel = rel.replace(os.sep, "/")
    base = rel.rsplit("/", 1)[-1]
    for pattern in cfg.get("exempt", []):
        rx = glob_to_regex(pattern)
        if rx.match(rel) or ("/" not in pattern and rx.match(base)):
            return True
    return False


def find_root(start, create=False):
    """Directory holding .comments-by-humans/, searching upward from start."""
    d = os.path.realpath(start or os.getcwd())
    probe = d
    while True:
        if os.path.isfile(os.path.join(probe, STATE_DIR, "state.json")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not create:
        return None
    import subprocess
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
        if top.returncode == 0 and top.stdout.strip():
            return os.path.realpath(top.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return d


# ---------------------------------------------------------------------------
# State


class Gate:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, STATE_DIR)
        self.state_path = os.path.join(self.dir, "state.json")
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self.config_path = os.path.join(self.dir, "config.json")
        self._lock_fd = None
        self.warnings = []
        self._config = None

    # locking -------------------------------------------------------------

    def __enter__(self):
        os.makedirs(self.dir, exist_ok=True)
        self._lock_fd = open(os.path.join(self.dir, ".lock"), "a+")
        try:
            import fcntl
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        if self._lock_fd:
            self._lock_fd.close()
            self._lock_fd = None

    # state ---------------------------------------------------------------

    def exists(self):
        return os.path.isfile(self.state_path)

    def load(self):
        if not self.exists():
            return new_state()
        with open(self.state_path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or "mode" not in state:
            raise ValueError("state.json is not a comments-by-humans state file")
        for key, value in new_state().items():
            state.setdefault(key, value)
        return state

    def save(self, state):
        os.makedirs(self.dir, exist_ok=True)
        state["locked"] = is_locked(state)
        cur = current_chunk(state)
        state["current"] = cur["id"] if cur else None
        state["updated"] = now()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.state_path)

    def config(self):
        if self._config is None:
            self._config = self._read_config()
        return self._config

    def _read_config(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.isfile(self.config_path):
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    user = json.load(f)
                for key, value in user.items():
                    if key == "review" and isinstance(value, dict):
                        cfg["review"].update(value)
                    else:
                        cfg[key] = value
            except (OSError, ValueError) as e:
                self.warnings.append("config.json is unreadable (%s); using defaults" % e)
        if cfg.get("depth") not in DEPTHS:
            self.warnings.append("config depth %r is not one of %s; using strict"
                                 % (cfg.get("depth"), ", ".join(DEPTHS)))
            cfg["depth"] = "strict"
        for key in ("target_chunk_lines", "min_words"):
            if not isinstance(cfg.get(key), int) or cfg[key] < 0:
                self.warnings.append("config %s must be a whole number; using %s"
                                     % (key, DEFAULT_CONFIG[key]))
                cfg[key] = DEFAULT_CONFIG[key]
        if not isinstance(cfg.get("exempt"), list):
            cfg["exempt"] = list(DEFAULT_EXEMPT)
        return cfg

    def ensure_config(self):
        if not os.path.isfile(self.config_path):
            os.makedirs(self.dir, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
                f.write("\n")
            self._config = None

    def log(self, event, **fields):
        os.makedirs(self.dir, exist_ok=True)
        entry = {"event": event, "ts": now()}
        entry.update(fields)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    def rel(self, path):
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def abs(self, rel):
        return os.path.join(self.root, rel)


def new_state():
    return {
        "version": 1,
        "mode": "off",
        "paused_from": None,
        "resume_mode": None,
        "locked": False,
        "current": None,
        "queue": [],
        "chunks": {},
        "review": None,
        "task": None,
        "prompt_count": 0,
        "pauses": 0,
        "last_expansion_prompt_id": None,
        "gitignore_suggested": False,
        "updated": None,
    }


def is_locked(state):
    if state["mode"] not in ("build", "review"):
        return False
    if state["queue"]:
        return True
    return state["mode"] == "review" and review_current(state) is not None


def review_current(state):
    rv = state.get("review")
    if not rv or rv.get("done") or rv.get("current") is None:
        return None
    ch = rv["chunks"].get(rv["current"])
    return ch if ch and ch["status"] == "pending" else None


def current_chunk(state):
    if state["mode"] == "review":
        ch = review_current(state)
        if ch:
            return ch
    if state["mode"] in ("build", "review") and state["queue"]:
        return state["chunks"].get(state["queue"][0])
    return None


def get_chunk(state, cid):
    if cid in state["chunks"]:
        return state["chunks"][cid]
    rv = state.get("review")
    if rv and cid in rv["chunks"]:
        return rv["chunks"][cid]
    return None


def next_free_id(state, extra=()):
    nums = [int(cid[1:]) for cid in list(state["chunks"]) + list(extra) if cid.startswith("c")]
    return "c%02d" % (max(nums) + 1 if nums else 1)


def where(ch):
    return "%s:%d-%d" % (ch["file"], ch["lines"][0], ch["lines"][1])


# ---------------------------------------------------------------------------
# Reading a chunk's comment and code


class View:
    """Where a chunk's explanation and code are right now."""

    def __init__(self, found, body=None, comment_line=None, code=None, code_hash=None,
                 code_range=None, error=None):
        self.found = found
        self.body = body
        self.comment_line = comment_line
        self.code = code
        self.code_hash = code_hash
        self.code_range = code_range
        self.error = error


def _source_lines(gate, ch):
    import subprocess
    src = ch.get("source", "worktree")
    if src == "worktree":
        path = gate.abs(ch["file"])
        if not os.path.isfile(path):
            return None
        return read_text(path).split("\n")
    rev = src.split(":", 1)[1]
    out = subprocess.run(["git", "-C", gate.root, "show", "%s:%s" % (rev, ch["file"])],
                         capture_output=True, text=True, timeout=30)
    return out.stdout.split("\n") if out.returncode == 0 else None


def build_view(gate, ch):
    import comments
    path = gate.abs(ch["file"])
    if not os.path.isfile(path):
        return View(False, error="%s no longer exists" % ch["file"])
    lines = read_text(path).split("\n")
    lang = comments.language_for(ch["file"], "\n".join(lines[:1]))
    if not lang:
        return View(False, error="%s has an unknown file type" % ch["file"])
    markers = comments.find_markers(lines, lang)
    regions = comments.chunk_regions(lines, lang, markers=markers)
    if ch["id"] not in regions:
        return View(False, error="the EXPLAIN(human) %s marker is missing from %s"
                    % (ch["id"], ch["file"]))
    m, s, e = regions[ch["id"]]
    code = "\n".join(lines[s:e + 1]) if s is not None else ""
    rng = (s + 1, e + 1) if s is not None else None
    return View(True, body=m.body, comment_line=m.start + 1, code=code,
                code_hash=comments.region_hash(lines, s, e, markers), code_range=rng)


def locate_review_code(gate, ch):
    """Find a review chunk's code (it may have moved if inline placeholders were added)."""
    import comments
    lines = _source_lines(gate, ch)
    if lines is None:
        return None, None
    lang = comments.language_for(ch["file"], "\n".join(lines[:1])) or comments.HASH
    markers = comments.find_markers(lines, lang)
    span = ch["code_lines"][1] - ch["code_lines"][0]
    guess = ch["code_lines"][0] - 1
    candidates = [guess] + [i for i, line in enumerate(lines) if line == ch["anchor"] and i != guess]
    for s in candidates:
        e = s + span
        if 0 <= s and e < len(lines) and comments.region_hash(lines, s, e, markers) == ch["hash"]:
            return lines, (s, e)
    return lines, None


def review_view(gate, state, ch):
    import comments
    rv = state["review"]
    lines, rng = locate_review_code(gate, ch)
    if lines is None:
        return View(False, error="%s can no longer be read" % ch["file"])
    code_hash = ch["hash"] if rng else "changed"
    code = "\n".join(lines[rng[0]:rng[1] + 1]) if rng else None
    code_range = (rng[0] + 1, rng[1] + 1) if rng else None
    if rv["inline"]:
        lang = comments.language_for(ch["file"], "\n".join(lines[:1]))
        markers = {m.cid: m for m in comments.find_markers(lines, lang)} if lang else {}
        m = markers.get(ch["id"])
        if not m:
            return View(False, error="the EXPLAIN(human) %s marker is missing from %s"
                        % (ch["id"], ch["file"]))
        return View(True, body=m.body, comment_line=m.start + 1, code=code, code_hash=code_hash,
                    code_range=code_range)
    report = gate.abs(rv["report"])
    if not os.path.isfile(report):
        return View(False, error="the review report %s is missing" % rv["report"])
    found = report_block(read_text(report), ch["id"])
    if not found:
        return View(False, error="the EXPLAIN(human) %s block is missing from %s"
                    % (ch["id"], rv["report"]))
    body, line_no = found
    return View(True, body=body, comment_line=line_no, code=code, code_hash=code_hash,
                code_range=code_range)


def view_of(gate, state, ch):
    return review_view(gate, state, ch) if ch["id"].startswith("r") else build_view(gate, ch)


_REPORT_OPEN = re.compile(r"^<!-- (EXPLAIN|EXPLAINED)\(human\) (r\d+)\b.*-->\s*$")


def report_block(text, cid):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _REPORT_OPEN.match(line.strip())
        if m and m.group(2) == cid:
            body = []
            for j in range(i + 1, len(lines)):
                if lines[j].strip().startswith("<!-- /EXPLAIN %s" % cid):
                    return "\n".join(body).strip(), i + 2
                body.append(lines[j])
            return None
    return None


# ---------------------------------------------------------------------------
# Hook I/O


def hook_deny(reason):
    raise Denied(reason.rstrip())


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")


def tool_path(payload):
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(payload.get("cwd") or os.getcwd(), path)
    return os.path.realpath(path)


def protected(gate, path):
    """Paths Claude may never write while the gate exists."""
    rel = gate.rel(path)
    home = os.path.realpath(os.path.expanduser("~"))
    if rel == STATE_DIR or rel.startswith(STATE_DIR + "/"):
        return True
    if re.match(r"^\.claude/settings[^/]*\.json$", rel):
        return True
    if path == PLUGIN_ROOT or path.startswith(PLUGIN_ROOT + os.sep):
        return True
    claude_home = os.path.join(home, ".claude")
    if re.match(re.escape(claude_home) + r"/settings[^/]*\.json$", path):
        return True
    if path.startswith(os.path.join(claude_home, "plugins") + os.sep):
        return True
    return False


def new_text_for(tool, ti, old):
    if tool == "Write":
        return ti.get("content", "")
    if tool == "Edit":
        old_s, new_s = ti.get("old_string", ""), ti.get("new_string", "")
        if old_s == "":
            return new_s if old == "" else None
        if old_s not in old:
            return None
        return old.replace(old_s, new_s) if ti.get("replace_all") else old.replace(old_s, new_s, 1)
    if tool == "MultiEdit":
        text = old
        for edit in ti.get("edits") or []:
            text = new_text_for("Edit", edit, text)
            if text is None:
                return None
        return text
    return None


LOCKED_WRITE = (
    "comments-by-humans: the gate is LOCKED on {cid} ({where}). No file writes until the human's "
    "explanation passes. Stop and end your turn: ask the human to explain {cid} in their own words "
    "in its placeholder, save the file and send any message.")
LOCKED_BASH = (
    "comments-by-humans: the gate is LOCKED on {cid} ({where}). Only the gate CLI may run, as a "
    "single command with no pipes, redirects or `$`: {gate} <status|approve|followup|next|finding|show> ...")


def locked_message(template, state):
    ch = current_chunk(state)
    return template.format(cid=ch["id"], where=where(ch), gate=GATE)


# ---------------------------------------------------------------------------
# Hook: PreToolUse on Write, Edit, MultiEdit, NotebookEdit and mcp__ tools

_MCP_WRITE = re.compile(
    r"(write|edit|create|update|delete|remove|move|rename|patch|replace|insert|append|put|"
    r"upload|push|commit|apply|exec|run|shell|command|save|modify|set_|mkdir|copy)", re.I)
_MCP_FILE_WRITE = re.compile(r"(write|edit|create|patch|replace|insert|append|move|rename|delete|"
                             r"save|modify|copy).*(file|text|document|code|block)|"
                             r"(file|text|document|code|block).*(write|edit|create|patch|replace|"
                             r"insert|append|move|rename|delete|save|modify|copy)|edit_block", re.I)


def hook_pre_write(gate, state, payload):
    tool = payload.get("tool_name", "")
    mode = state["mode"]
    if tool.startswith("mcp__"):
        name = tool.split("__")[-1]
        if mode == "paused":
            return
        if (is_locked(state) or mode == "review") and _MCP_WRITE.search(name):
            if is_locked(state):
                hook_deny(locked_message(LOCKED_WRITE, state) + " (MCP write tools included.)")
            hook_deny("comments-by-humans: review mode — the assistant writes no code, so %s is denied." % tool)
        if mode == "build" and _MCP_FILE_WRITE.search(name):
            hook_deny("comments-by-humans: write code with Write or Edit, not %s, so the gate can check "
                      "its EXPLAIN(human) placeholder." % tool)
        return

    path = tool_path(payload)
    if path and protected(gate, path):
        hook_deny("comments-by-humans: %s is protected while the gate is on. The assistant may not edit "
                  "the gate's state, the plugin, or the agent's settings." % path)
    if mode == "paused":
        return
    if mode == "review":
        hook_deny("comments-by-humans: review mode — the assistant writes no code. Present chunks and grade "
                  "the human's explanations; the gate script writes the report.")
    if is_locked(state):
        hook_deny(locked_message(LOCKED_WRITE, state))
    if tool == "NotebookEdit" or not path:
        return
    rel = gate.rel(path)
    if rel.startswith("../"):
        return
    cfg = gate.config()
    if is_exempt(rel, cfg):
        return
    old = read_text(path) if os.path.isfile(path) else ""
    new = new_text_for(tool, payload.get("tool_input") or {}, old)
    if new is None:
        return  # the tool itself will fail
    import comments
    lang = comments.language_for(rel, new)
    if not lang:
        return
    problems = check_build_write(state, rel, lang, old, new)
    if problems:
        text = "comments-by-humans: this write breaks the placeholder rules:\n- " + "\n- ".join(problems)
        if any("comment for" not in p for p in problems):
            nid = next_free_id(state, [m.cid for m in comments.find_markers(new.split("\n"), lang)])
            example = "\n".join(lang.placeholder(nid))
            if lang.markup:
                example += ("\nInside <script> or <style>, use the embedded language's syntax:\n"
                            + "\n".join(lang.placeholder(nid, block=("/*", "*/"))))
            text += ("\n\nEvery new chunk of code needs exactly one EMPTY placeholder directly above "
                     "it, one chunk per write. Next free id: %s. Placeholder for this file type:\n%s\n"
                     "Changing code that has no placeholder? Put one above the whole function you are "
                     "changing; that function becomes the chunk." % (nid, example))
        hook_deny(text)


def check_build_write(state, rel, lang, old_text, new_text):
    import comments
    import difflib
    problems = []
    old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
    old_markers = {m.cid: m for m in comments.find_markers(old_lines, lang)}
    new_list = comments.find_markers(new_lines, lang)
    new_markers = {}
    for m in new_list:
        if m.cid in new_markers:
            problems.append("%s appears twice; every placeholder needs its own id" % m.cid)
        new_markers[m.cid] = m
    added = [m for m in new_list if m.cid not in old_markers]
    nid = next_free_id(state, list(old_markers) + list(new_markers))
    for m in added:
        if m.cid.startswith("r"):
            problems.append("%s is a review id; build chunks use c-ids such as %s" % (m.cid, nid))
        elif m.cid in state["chunks"]:
            problems.append("%s is already used (in %s); use %s"
                            % (m.cid, state["chunks"][m.cid]["file"], nid))
        if m.tag == "EXPLAINED":
            problems.append("%s is marked EXPLAINED; new placeholders say EXPLAIN(human) and only the "
                            "gate marks a chunk explained" % m.cid)
        if m.body:
            problems.append("the placeholder for %s is not empty; the assistant never writes the explanation, "
                            "the human does" % m.cid)
    if len(added) > 1:
        problems.append("this write adds %d placeholders (%s); write one chunk at a time"
                        % (len(added), ", ".join(m.cid for m in added)))

    old_regions = comments.chunk_regions(old_lines, lang)
    for cid, om in old_markers.items():
        nm = new_markers.get(cid)
        if nm is None:
            _, s, e = old_regions[cid]
            code = "\n".join(old_lines[s:e + 1]).strip() if s is not None else ""
            if code and code in new_text:
                problems.append("this removes the human's comment for %s but keeps its code" % cid)
            continue
        if nm.body != om.body or nm.tag != om.tag:
            problems.append("this changes the human's comment for %s; only the human edits it" % cid)

    info = comments.lex(new_lines, lang)
    free = comments.needs_no_chunk(new_lines, info, lang)
    regions = comments.chunk_regions(new_lines, lang, info, new_list)
    covered = set()
    for m, s, e in regions.values():
        covered.update(range(m.start, m.end + 1))
        if s is not None:
            covered.update(range(s, e + 1))
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    added_lines = []
    for op, _, _, j1, j2 in matcher.get_opcodes():
        if op in ("insert", "replace"):
            added_lines.extend(range(j1, j2))
    loose = [j for j in added_lines if not free[j] and j not in covered]
    if loose:
        problems.append("%s adds code outside any chunk (%s)"
                        % (rel, _ranges([j + 1 for j in loose])))
    for m in added:
        _, s, e = regions[m.cid]
        if s is None or all(free[k] for k in range(s, e + 1)):
            problems.append("the placeholder for %s has no code directly under it" % m.cid)
    return problems


def _ranges(nums):
    out, start, prev = [], None, None
    for n in sorted(nums):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(("line %d" % start) if start == prev else "lines %d-%d" % (start, prev))
            start = prev = n
    if start is not None:
        out.append(("line %d" % start) if start == prev else "lines %d-%d" % (start, prev))
    return ", ".join(out)


# ---------------------------------------------------------------------------
# Hook: PostToolUse on Write, Edit, MultiEdit, NotebookEdit


def hook_post_write(gate, state, payload):
    tool = payload.get("tool_name", "")
    path = tool_path(payload)
    mode = state["mode"]
    if not path or protected(gate, path):
        return
    rel = gate.rel(path)
    if rel.startswith("../"):
        return
    cfg = gate.config()
    if mode == "paused":
        if not is_exempt(rel, cfg):
            gate.log("ungated_write", file=rel, tool=tool, reason="paused")
        return
    if mode != "build":
        return
    import comments
    if is_exempt(rel, cfg) or not os.path.isfile(path):
        return
    text = read_text(path)
    lang = comments.language_for(rel, text)
    if tool == "NotebookEdit" or not lang:
        gate.log("ungated_write", file=rel, tool=tool, reason="unknown file type")
        return
    lines = text.split("\n")
    markers = comments.find_markers(lines, lang)
    regions = comments.chunk_regions(lines, lang, markers=markers)
    present = set()
    new_ids, regated = [], []
    rewrite = False
    for m in markers:
        present.add(m.cid)
        _, s, e = regions[m.cid]
        h = comments.region_hash(lines, s, e, markers)
        span = [m.start + 1, (e if e is not None else m.end) + 1]
        ch = state["chunks"].get(m.cid)
        if ch is None:
            if m.tag == "EXPLAIN" and m.cid.startswith("c"):
                state["chunks"][m.cid] = {
                    "id": m.cid, "file": rel, "lines": span, "hash": h, "status": "pending",
                    "attempts": 0, "attempts_total": 0, "regates": 0, "created": now(),
                    "locked_at": now(), "last_graded": None, "seen_comment": None,
                    "followup_prompt": None,
                }
                state["queue"].append(m.cid)
                new_ids.append(m.cid)
            continue
        if ch["file"] != rel or ch["status"] == "removed":
            continue
        ch["lines"] = span
        if h == ch["hash"]:
            continue
        ch["hash"] = h
        if ch["status"] == "passed":
            if m.tag == "EXPLAINED":
                lines = comments.set_marker_tag(lines, m, "EXPLAIN")
                rewrite = True
            ch.update(status="pending", regates=ch.get("regates", 0) + 1, attempts=0,
                      locked_at=now(), last_graded=None, seen_comment=None, followup_prompt=None)
            if m.cid not in state["queue"]:
                state["queue"].append(m.cid)
            regated.append(m.cid)
            gate.log("regated", chunk=m.cid, file=rel, lines=span)
    for cid, ch in state["chunks"].items():
        if ch["file"] == rel and ch["status"] != "removed" and cid not in present:
            ch["status"] = "removed"
            if cid in state["queue"]:
                state["queue"].remove(cid)
            gate.log("removed", chunk=cid, file=rel)
    if rewrite:
        write_text(path, "\n".join(lines))
    gate.save(state)
    if not new_ids and not regated:
        return
    parts = []
    for cid in new_ids:
        parts.append("New chunk %s registered at %s." % (cid, where(state["chunks"][cid])))
    for cid in regated:
        parts.append("Chunk %s (%s) changed, so its gate reopened: the tag is back to EXPLAIN(human) "
                     "and the human must update their comment to match the new code."
                     % (cid, where(state["chunks"][cid])))
    cur = current_chunk(state)
    msg = ("comments-by-humans: %s The gate is now LOCKED on %s. STOP: do not write files or run "
           "commands. End your turn now by asking the human to explain %s in their own words in the "
           "placeholder at %s:%d, then save the file and send any message. Do not explain the chunk, "
           "summarize it, or suggest wording." % (" ".join(parts), cur["id"], cur["id"], cur["file"],
                                                 cur["lines"][0]))
    return msg


# ---------------------------------------------------------------------------
# Hook: PreToolUse on Bash


_REDIRECT = re.compile(r"(?:^|[^<>&\d])(?:\d|&)?>>?\|?\s*([^\s;&|<>()]+)")
_TEE = re.compile(r"\btee\s+(?:-[a-z]+\s+)*([^\s;&|<>()]+)")
_INPLACE = re.compile(r"\b(?:sed|perl|ruby)\b[^|;&]*\s-[a-zA-Z]*i")
_COPY = re.compile(r"\b(?:cp|mv|install|rsync|ln)\b[^|;&]*?\s([^\s;&|<>()]+)\s*(?:$|[;&|])")
_PATCH = re.compile(r"\b(?:git\s+(?:apply|am)|patch)\b")
_PLUGIN_OFF = re.compile(r"\bclaude\s+plugins?\s+(?:disable|uninstall|remove|rm|update)\b")
_SETTINGS = re.compile(r"\.claude/settings[^\s'\"]*\.json")
_WRITE_VERB = re.compile(r">|\b(?:tee|sed|rm|mv|cp|ln|truncate|chmod|python3?|node|perl|ruby|jq|awk|dd|"
                         r"install|rsync|git|echo|printf|cat)\b")


def is_gate_cli(cmd):
    import shlex
    if "\n" in cmd or "$" in cmd or "`" in cmd:
        return False
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if len(tokens) < 3 or any(t and all(c in "();<>|&" for c in t) for t in tokens):
        return False
    if not re.match(r"^python3?(\.\d+)?$", os.path.basename(tokens[0])):
        return False
    target = os.path.realpath(os.path.expanduser(tokens[1]))
    return target == SCRIPT and tokens[2] in CLAUDE_COMMANDS


def _targets_code(gate, path, cfg):
    import comments
    if path.startswith("/dev/") or path.startswith("&"):
        return False
    rel = gate.rel(os.path.realpath(os.path.join(gate.root, path)))
    return comments.language_for(rel) is not None and not is_exempt(rel, cfg)


def bash_writes_code(gate, cmd, cfg):
    if _PATCH.search(cmd):
        return "applies a patch"
    for rx, what in ((_REDIRECT, "redirects into"), (_TEE, "tees into")):
        for m in rx.finditer(cmd):
            if _targets_code(gate, m.group(1).strip("'\""), cfg):
                return "%s %s" % (what, m.group(1))
    if _INPLACE.search(cmd):
        for token in re.findall(r"[^\s;&|<>()'\"]+", cmd):
            if _targets_code(gate, token, cfg):
                return "edits %s in place" % token
    for m in _COPY.finditer(cmd):
        if _targets_code(gate, m.group(1).strip("'\""), cfg):
            return "copies or moves onto %s" % m.group(1)
    return None


def hook_pre_bash(gate, state, payload):
    cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    if is_gate_cli(cmd):
        return
    if STATE_DIR in cmd or PLUGIN_ROOT in cmd or _PLUGIN_OFF.search(cmd) or (
            _SETTINGS.search(cmd) and _WRITE_VERB.search(cmd)):
        hook_deny("comments-by-humans: this command touches the gate's state, the plugin or the "
                  "agent's settings, which are protected while the gate is on. Use `%s status` to read "
                  "the gate." % GATE)
    mode = state["mode"]
    if mode == "paused":
        return
    if is_locked(state):
        hook_deny(locked_message(LOCKED_BASH, state))
    cfg = gate.config()
    reason = bash_writes_code(gate, cmd, cfg)
    if reason and mode == "review":
        hook_deny("comments-by-humans: review mode — the assistant writes no code, and this command %s." % reason)
    if reason and mode == "build":
        hook_deny("comments-by-humans: this command %s. Write code with Write or Edit so the gate can "
                  "check its EXPLAIN(human) placeholder." % reason)


# ---------------------------------------------------------------------------
# Hook: PreToolUse on Skill


def hook_pre_skill(root, payload):
    ti = payload.get("tool_input") or {}
    skill = ti.get("skill") or ti.get("skill_name") or ti.get("command") or ""
    name = skill.lstrip("/")
    if name.startswith(PLUGIN + ":"):
        name = name.split(":", 1)[1]
    elif name != "pause" or not root:
        return
    if name == "pause":
        if root and Gate(root).load()["mode"] != "off":
            hook_deny("comments-by-humans: only the human can pause the gate, by typing "
                      "/comments-by-humans:pause themselves. Keep going with the current chunk.")
        return
    if name in ("build", "review"):
        args = ti.get("args") or ti.get("skill_args") or ""
        if isinstance(args, list):
            args = " ".join(args)
        root = root or find_root(payload.get("cwd"), create=True)
        gate = Gate(root)
        with gate:
            state = gate.load()
            if name == "build":
                start_build(gate, state, args, by="claude")
            else:
                text, ok = start_review(gate, state, args, by="claude")
                if not ok:
                    hook_deny(text)
            gate.save(state)


# ---------------------------------------------------------------------------
# Hook: UserPromptExpansion (and a UserPromptSubmit fallback)


def run_command(gate, state, cmd, args, by):
    """Apply a typed /comments-by-humans:<cmd>. Returns (context, ok)."""
    if cmd == "build":
        return start_build(gate, state, args, by), True
    if cmd == "review":
        return start_review(gate, state, args, by)
    if cmd == "pause":
        return pause(gate, state), True
    return None, True


def hook_prompt_expansion(payload, start=None):
    name = payload.get("command_name") or ""
    if not name.startswith(PLUGIN + ":"):
        return
    cmd = name.split(":", 1)[1]
    if cmd not in SKILLS:
        return
    args = payload.get("command_args") or ""
    if isinstance(args, list):
        args = " ".join(str(a) for a in args)
    start = start or os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    root = find_root(start, create=cmd in ("build", "review"))
    if not root:
        return
    gate = Gate(root)
    with gate:
        state = gate.load()
        state["last_expansion_prompt_id"] = payload.get("prompt_id") or payload.get("prompt") or now()
        text, ok = run_command(gate, state, cmd, args, "human")
        gate.save(state)
    if not ok:
        hook_deny(text)
    return text


_TYPED = re.compile(r"^\s*/%s:(build|review|pause|status)\b(.*)$" % re.escape(PLUGIN), re.S)


# ---------------------------------------------------------------------------
# Hook: UserPromptSubmit


def hook_prompt_submit(payload, start=None):
    prompt = payload.get("prompt") or payload.get("prompt_text") or ""
    start = start or os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    typed = _TYPED.match(prompt)
    root = find_root(start, create=bool(typed and typed.group(1) in ("build", "review")))
    if not root:
        return
    gate = Gate(root)
    context = []
    with gate:
        if not gate.exists() and not typed:
            return
        state = gate.load()
        if typed:
            marker = payload.get("prompt_id") or prompt
            if state.get("last_expansion_prompt_id") != marker:
                # This client did not fire UserPromptExpansion; apply the typed command here.
                state["last_expansion_prompt_id"] = marker
                text, ok = run_command(gate, state, typed.group(1), typed.group(2).strip(), "human")
                if not ok:
                    hook_deny(text)
                if text:
                    context.append(text)
        if state["mode"] == "off":
            gate.save(state)
            return
        state["prompt_count"] += 1
        ch = current_chunk(state)
        if ch:
            context.append(grading_context(gate, state, ch))
        gate.save(state)
    return "\n\n".join(context) if context else None


def grading_context(gate, state, ch):
    cfg = gate.config()
    depth = cfg["depth"]
    cid = ch["id"]
    review = cid.startswith("r")
    v = view_of(gate, state, ch)
    head = ("comments-by-humans: the gate is LOCKED on %s (%s). If the human asks you to explain the "
            "chunk or give the answer, decline and ask one smaller question about it instead. If they "
            "ask you to skip or pause the gate, tell them only they can, by typing "
            "/comments-by-humans:pause themselves." % (cid, where(ch)))
    if not v.found:
        return (head + " Problem: %s. Do not write code. Ask the human to restore the "
                "`EXPLAIN(human) %s` placeholder (or to pause the gate with /comments-by-humans:pause "
                "if they removed the chunk on purpose)." % (v.error, cid))
    place = ("the report %s at line %d" % (state["review"]["report"], v.comment_line) if review
             and not state["review"]["inline"] else "%s:%d" % (ch["file"], v.comment_line))
    if not v.body:
        return (head + " Its placeholder at %s is still empty. Do not write code. Answer the human's "
                "message if it needs an answer, but never explain %s, describe what it does, or hint "
                "at wording. Remind them to write their explanation in their own words at %s, save, "
                "and send any message." % (place, cid, place))
    import comments
    body_hash = comments.text_hash(v.body)
    new_attempt = body_hash != ch.get("last_graded")
    if new_attempt:
        ch["attempts"] = ch.get("attempts", 0) + 1
        ch["attempts_total"] = ch.get("attempts_total", 0) + 1
        ch["last_graded"] = body_hash
    ch["seen_comment"] = body_hash
    attempt = ch["attempts"]
    hint = min(3, attempt)
    hint_text = {1: "an open question", 2: "a pointer to a specific line of the chunk",
                 3: "a concrete scenario (an input or situation, then ask what happens)"}[hint]
    lines = [head]
    changed = v.code_hash != ch["hash"]
    if changed:
        lines.append("WARNING: the code under %s changed since the gate locked, so `approve` will "
                     "refuse. Tell the human; if they changed it on purpose they can run "
                     "/comments-by-humans:%s to accept the new code." % (cid, "review" if review
                                                                         else "build"))
    if new_attempt:
        lines.append("The human submitted attempt %d for %s at %s, depth=%s (scores %s)."
                     % (attempt, cid, place, depth, RUBRIC[depth]))
    else:
        lines.append("The comment for %s is unchanged since attempt %d (depth=%s)." % (cid, attempt, depth))
    lines.append("Their comment:\n<<<\n%s\n>>>" % v.body)
    words = comments.word_count(v.body)
    if words < cfg["min_words"]:
        lines.append("It has %d words; the gate needs at least %d before it can pass."
                     % (words, cfg["min_words"]))
    followup = ch.get("followup_prompt")
    if not new_attempt and followup is not None:
        lines.append(
            "You asked a follow-up question; this message should hold the human's answer. If it shows "
            "they understand, run `%s approve %s`. If not, ask one more question (never the answer)."
            % (GATE, cid))
    else:
        steps = ["Read the chunk (%s) and grade the comment against the rubric in the %s skill. "
                 "A factually wrong claim fails it whatever else it covers. Length is not scored."
                 % (where(ch), "review" if review else "build"),
                 "If it falls short: ask exactly ONE question aimed at the weakest criterion, using "
                 "%s (hint level %d of 3). Never state the answer, never rewrite or dictate the "
                 "comment, and never explain the code." % (hint_text, hint)]
        if depth == "strict" and followup is None:
            steps.append("If it meets the rubric: run `%s followup %s` first, then end your turn "
                         "with ONE follow-up question about the chunk (for example, what breaks if a "
                         "given line changes) as the last thing in your message." % (GATE, cid))
        else:
            steps.append("If it meets the rubric: run `%s approve %s`." % (GATE, cid))
        if review:
            steps.append("Only after approval: share your own concerns about the chunk and let the "
                         "human choose findings to record with `%s finding %s \"...\"`; then run `%s "
                         "next`." % (GATE, cid, GATE))
        lines.extend("- " + s for s in steps)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Modes


def gitignore_note(gate, state):
    if state.get("gitignore_suggested"):
        return ""
    path = os.path.join(gate.root, ".gitignore")
    if os.path.isfile(path) and STATE_DIR in read_text(path):
        return ""
    state["gitignore_suggested"] = True
    return (" Suggest (once) that the human add `%s/` to .gitignore; do not edit .gitignore "
            "yourself." % STATE_DIR)


def rescan(gate, state):
    """Re-baseline chunk hashes after code changed outside the gate (pause, human edits)."""
    notes = []
    for cid, ch in list(state["chunks"].items()):
        if ch["status"] == "removed":
            continue
        v = build_view(gate, ch)
        if not v.found:
            ch["status"] = "removed"
            if cid in state["queue"]:
                state["queue"].remove(cid)
            gate.log("removed", chunk=cid, file=ch["file"], reason=v.error)
            notes.append("%s removed (%s)" % (cid, v.error))
        elif v.code_hash != ch["hash"]:
            gate.log("ungated_change", chunk=cid, file=ch["file"])
            ch["hash"] = v.code_hash
            notes.append("%s changed outside the gate; logged as ungated" % cid)
        if v.found and v.code_range:
            ch["lines"] = [v.comment_line, v.code_range[1]]
    return notes


def start_build(gate, state, args, by):
    gate.ensure_config()
    prev = state["mode"]
    notes = rescan(gate, state)
    if prev == "paused":
        gate.log("resume", mode="build", by=by)
    state["mode"] = "build"
    state["paused_from"] = None
    if args and args.strip():
        state["task"] = args.strip()
    cfg = gate.config()
    text = ["comments-by-humans: build mode is ON (depth=%s, target %d lines per chunk). Next free "
            "chunk id: %s." % (cfg["depth"], cfg["target_chunk_lines"], next_free_id(state))]
    if notes:
        text.append("Rescan: " + "; ".join(notes) + ".")
    cur = current_chunk(state)
    if cur:
        text.append("The gate is LOCKED on %s (%s): finish that chunk's explanation before writing "
                    "anything else." % (cur["id"], where(cur)))
    if prev == "review" and state.get("review") and not state["review"].get("done"):
        text.append("The review in progress is suspended; /comments-by-humans:review resumes it.")
    text.append(gitignore_note(gate, state).strip())
    for w in gate.warnings:
        text.append("Config warning: " + w)
    return " ".join(t for t in text if t)


def pause(gate, state):
    if state["mode"] not in ("build", "review"):
        return "comments-by-humans: the gate is not active (mode=%s); nothing to pause." % state["mode"]
    cur = current_chunk(state)
    state["paused_from"] = state["mode"]
    state["mode"] = "paused"
    state["pauses"] = state.get("pauses", 0) + 1
    gate.log("pause", from_mode=state["paused_from"], chunk=cur["id"] if cur else None)
    return ("comments-by-humans: the human PAUSED the gate (pause #%d, logged). Writes are not gated "
            "and code written now is logged as ungated, until the human runs /comments-by-humans:build "
            "or /comments-by-humans:review again.%s"
            % (state["pauses"], " %s stays pending for later." % cur["id"] if cur else ""))


# ---------------------------------------------------------------------------
# Review mode


def git(gate, *args):
    import subprocess
    out = subprocess.run(["git", "-C", gate.root] + list(args), capture_output=True, text=True,
                         timeout=60)
    if out.returncode != 0:
        raise Refusal("git %s failed: %s" % (" ".join(args), out.stderr.strip()))
    return out.stdout


def list_code_files(gate, path, cfg):
    import comments
    import subprocess
    if os.path.isfile(path):
        files = [path]
    else:
        files = []
        out = subprocess.run(["git", "-C", gate.root, "ls-files", "--cached", "--others",
                              "--exclude-standard", "--", os.path.relpath(path, gate.root)],
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            files = [os.path.join(gate.root, f) for f in out.stdout.splitlines() if f]
        else:
            skip = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "venv"}
            for d, dirs, names in os.walk(path):
                dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in skip)
                files.extend(os.path.join(d, n) for n in sorted(names))
    result = []
    for f in files:
        rel = gate.rel(os.path.realpath(f))
        if rel.startswith(STATE_DIR + "/") or rel.startswith("../") or is_exempt(rel, cfg):
            continue
        if os.path.isfile(f) and comments.language_for(rel, _head(f)):
            result.append(rel)
    return sorted(set(result))


def _head(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(200)
    except OSError:
        return ""


def diff_changes(gate, rng):
    """Changed new-side lines per file for a diff range, plus the new side's source."""
    left, right = rng, None
    for sep in ("...", ".."):
        if sep in rng:
            left, right = rng.split(sep, 1)
            break
    for rev in (left or "HEAD", right):
        if rev:
            git(gate, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    raw = git(gate, "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-renames", rng, "--")
    changes, current = {}, None
    for line in raw.splitlines():
        if line.startswith("+++ "):
            current = None if line[4:] == "/dev/null" else line[6:] if line.startswith("+++ b/") else line[4:]
            if current:
                changes.setdefault(current, set())
        elif line.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start, count = int(m.group(1)), int(m.group(2) or "1")
            changes[current].update(range(start, start + count) if count else [max(start, 1)])
    source = "worktree"
    if right is not None:
        head = git(gate, "rev-parse", "HEAD").strip()
        target = git(gate, "rev-parse", (right or "HEAD") + "^{commit}").strip()
        source = "worktree" if target == head else "rev:" + target
    return changes, source


def collect_review_chunks(gate, targets, cfg):
    import comments
    chunks = []
    target_lines = cfg["target_chunk_lines"]
    for target in targets:
        path = os.path.realpath(os.path.join(gate.root, target))
        if os.path.exists(path):
            files = [(rel, "worktree", None) for rel in list_code_files(gate, path, cfg)]
        else:
            try:
                changes, source = diff_changes(gate, target)
            except Refusal:
                raise Refusal("`%s` is neither a path nor a diff range in %s" % (target, gate.root))
            files = []
            for rel, changed in sorted(changes.items()):
                if is_exempt(rel, cfg) or rel.startswith(STATE_DIR + "/"):
                    continue
                src = source
                if source.startswith("rev:"):
                    try:
                        git(gate, "diff", "--quiet", "HEAD", source[4:], "--", rel)
                        src = "worktree" if not git(gate, "status", "--porcelain", "--", rel).strip() \
                            else source
                    except Refusal:
                        src = source
                files.append((rel, src, changed))
        for rel, src, changed in files:
            ch_stub = {"file": rel, "source": src}
            lines = _source_lines(gate, ch_stub)
            if lines is None:
                continue
            lang = comments.language_for(rel, "\n".join(lines[:1]))
            if not lang:
                continue
            info = comments.lex(lines, lang)
            markers = comments.find_markers(lines, lang)
            for s, e in comments.units(lines, lang, target_lines):
                if changed is not None and not any(s + 1 <= n <= e + 1 for n in changed):
                    continue
                chunks.append({
                    "file": rel, "source": src, "code_lines": [s + 1, e + 1], "lines": [s + 1, e + 1],
                    "anchor": lines[s], "hash": comments.region_hash(lines, s, e, markers),
                    "defs": sorted(comments.defined_names(lines, info, s, e)),
                    "refs": comments.referenced_names(info, s, e),
                    "locals": comments.local_names(info, s, e),
                    "entry": comments.is_entry_point(lines, s, e),
                })
    return chunks


def construction_order(chunks):
    """Dependencies first, entry points last; file order breaks ties."""
    n = len(chunks)
    definers = {}
    for i, ch in enumerate(chunks):
        for name in ch["defs"]:
            definers.setdefault(name, set()).add(i)
    deps = []
    for i, ch in enumerate(chunks):
        d = set()
        for name in ch["refs"] - set(ch["defs"]) - ch["locals"]:
            found = definers.get(name, set())
            same_file = {k for k in found if chunks[k]["file"] == ch["file"]}
            d.update(same_file or found)
        d.discard(i)
        deps.append(d)
    order, done = [], set()
    while len(order) < n:
        ready = [i for i in range(n) if i not in done and deps[i] <= done]
        pool = ready or [i for i in range(n) if i not in done]
        pick = min(pool, key=lambda i: (chunks[i]["entry"], i))
        order.append(pick)
        done.add(pick)
    return [chunks[i] for i in order]


def start_review(gate, state, args, by):
    import shlex
    gate.ensure_config()
    cfg = gate.config()
    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()
    inline = cfg["review"].get("inline", False)
    if "--inline" in tokens:
        inline = True
    if "--report" in tokens:
        inline = False
    targets = [t for t in tokens if t not in ("--inline", "--report")]
    rv = state.get("review")
    if not targets:
        if rv and not rv.get("done"):
            if state["mode"] != "review":
                state["resume_mode"] = _resume_mode(state)
            state["mode"] = "review"
            state["paused_from"] = None
            cur = review_current(state)
            return ("comments-by-humans: review %s resumed (%d of %d chunks done). %s"
                    % (rv["id"], rv["position"] + (0 if cur else 1), len(rv["order"]),
                       present_text(gate, state, cur) if cur else "The current chunk passed; run `%s "
                       "next` for the next one." % GATE)), True
        return ("comments-by-humans: give a path or a diff range to review, for example "
                "/comments-by-humans:review src/ or /comments-by-humans:review main..HEAD"), False
    try:
        found = collect_review_chunks(gate, targets, cfg)
    except Refusal as e:
        return "comments-by-humans: %s" % e, False
    if not found:
        return "comments-by-humans: no code chunks found in %s." % " ".join(targets), False
    ordered = construction_order(found)
    if inline and any(c["source"] != "worktree" for c in ordered):
        inline = False
        inline_note = " Inline mode needs the working tree to match the range, so explanations go in the report."
    else:
        inline_note = ""
    if rv and not rv.get("done"):
        _append_report(gate, rv, "\n---\n\nReview abandoned at %s when a new review started.\n" % now())
        gate.log("review_abandoned", review=rv["id"])
    rid = time.strftime("%Y%m%d-%H%M%S")
    chunks = {}
    order = []
    for k, c in enumerate(ordered, 1):
        cid = "r%02d" % k
        c.update(id=cid, status="pending", attempts=0, attempts_total=0, findings=[],
                 last_graded=None, seen_comment=None, followup_prompt=None, presented=None)
        c["refs"] = []
        c.pop("locals", None)
        chunks[cid] = c
        order.append(cid)
    if state["mode"] != "review":
        state["resume_mode"] = _resume_mode(state)
    state["review"] = {
        "id": rid, "targets": targets, "inline": inline, "order": order, "position": 0,
        "current": order[0], "chunks": chunks, "done": False, "started": now(),
        "report": "%s/review/%s.md" % (STATE_DIR, rid),
    }
    state["mode"] = "review"
    state["paused_from"] = None
    os.makedirs(os.path.join(gate.dir, "review"), exist_ok=True)
    header = ["# Review %s" % rid, "", "Targets: %s" % ", ".join("`%s`" % t for t in targets),
              "Started: %s · Chunks: %d · Explanations: %s · Depth: %s" % (
                  now(), len(order), "inline in the source" if inline else "in this report",
                  cfg["depth"]), ""]
    write_text(gate.abs(state["review"]["report"]), "\n".join(header) + "\n")
    gate.log("review_started", review=rid, targets=targets, chunks=len(order), inline=inline, by=by)
    present(gate, state, chunks[order[0]])
    return ("comments-by-humans: review %s started: %d chunk%s in construction order (dependencies "
            "first, entry points last). The assistant writes no code in review mode.%s %s%s"
            % (rid, len(order), "" if len(order) == 1 else "s", inline_note, present_text(gate, state, chunks[order[0]]),
               gitignore_note(gate, state))), True


def _resume_mode(state):
    """The mode to return to when a review finishes."""
    mode = state["paused_from"] if state["mode"] == "paused" else state["mode"]
    if mode == "build":
        return "build"
    if mode == "review":
        return state.get("resume_mode") or "off"
    return "off"


def _append_report(gate, rv, text):
    path = gate.abs(rv["report"])
    old = read_text(path) if os.path.isfile(path) else ""
    write_text(path, old + text)


def present(gate, state, ch):
    """Put the chunk's explanation slot in the report (or inline) and mark it current."""
    import comments
    rv = state["review"]
    ch["presented"] = now()
    section = []
    if rv["inline"]:
        lines, rng = locate_review_code(gate, ch)
        if rng is None:
            raise Refusal("%s changed since the review started; restart the review" % ch["file"])
        lang = comments.language_for(ch["file"], "\n".join(lines[:1]))
        indent = lines[rng[0]][:len(lines[rng[0]]) - len(lines[rng[0]].lstrip())]
        block = comments.embedded_block(lines, rng[0]) if lang.markup else None
        placeholder = lang.placeholder(ch["id"], indent, block)
        lines[rng[0]:rng[0]] = placeholder
        write_text(gate.abs(ch["file"]), "\n".join(lines))
        added = len(placeholder)
        ch["code_lines"] = [rng[0] + 1 + added, rng[1] + 1 + added]
        ch["lines"] = [rng[0] + 1, rng[1] + 1 + added]
        section.append("Explanation: inline at `%s:%d`." % (ch["file"], rng[0] + 1))
    else:
        section += ["Write your explanation between the two markers below, in your own words.", "",
                    "<!-- EXPLAIN(human) %s -->" % ch["id"], "", "<!-- /EXPLAIN %s -->" % ch["id"]]
    head = ["", "## %s · %s:%d-%d" % (ch["id"], ch["file"], ch["code_lines"][0], ch["code_lines"][1]), ""]
    _append_report(gate, rv, "\n".join(head + section) + "\n")
    rv["current"] = ch["id"]


def present_text(gate, state, ch):
    rv = state["review"]
    if rv["inline"]:
        place = "the EXPLAIN(human) %s placeholder at %s:%d" % (ch["id"], ch["file"], ch["lines"][0])
    else:
        found = report_block(read_text(gate.abs(rv["report"])), ch["id"])
        place = "%s, line %d, between the EXPLAIN markers" % (rv["report"], found[1] if found else 0)
    names = ", ".join(ch.get("defs") or [])
    return ("Present chunk %s (%d of %d): %s%s. Tell the human to read that code and write their "
            "explanation in %s, then save and send any message. Show where the chunk is, not what it "
            "does: do not summarize, explain or critique it before their explanation passes."
            % (ch["id"], rv["order"].index(ch["id"]) + 1, len(rv["order"]),
               "%s:%d-%d" % (ch["file"], ch["code_lines"][0], ch["code_lines"][1]),
               " (defines %s)" % names if names else "", place))


def finish_review(gate, state):
    rv = state["review"]
    rv["done"] = True
    rv["current"] = None
    passed = [rv["chunks"][c] for c in rv["order"] if rv["chunks"][c]["status"] == "passed"]
    findings = sum(len(c["findings"]) for c in passed)
    lines = ["", "---", "", "## Summary", "",
             "Finished: %s · Chunks passed: %d of %d · Findings: %d" % (
                 now(), len(passed), len(rv["order"]), findings), "",
             "| Chunk | Location | Attempts | Findings |", "| --- | --- | --- | --- |"]
    for cid in rv["order"]:
        c = rv["chunks"][cid]
        lines.append("| %s | `%s` | %s | %d |" % (cid, where(c), c["attempts_total"], len(c["findings"])))
    _append_report(gate, rv, "\n".join(lines) + "\n")
    gate.log("review_finished", review=rv["id"], chunks=len(rv["order"]), findings=findings)
    state["mode"] = state.get("resume_mode") or "off"
    state["resume_mode"] = None


# ---------------------------------------------------------------------------
# CLI commands (run by Claude through Bash)


def cli_root(root=None):
    root = root or find_root(os.getcwd()) or find_root(os.environ.get("CLAUDE_PROJECT_DIR"))
    if not root:
        raise Refusal("comments-by-humans is off here: no %s/state.json above %s. The human starts it "
                      "with /comments-by-humans:build or /comments-by-humans:review." % (STATE_DIR, os.getcwd()))
    return root


def cmd_status(gate, state, args):
    cfg = gate.config()
    out = []
    lock = "LOCKED" if is_locked(state) else "unlocked"
    out.append("comments-by-humans · mode: %s · %s · depth: %s · min words: %d"
               % (state["mode"], lock, cfg["depth"], cfg["min_words"]))
    if state["mode"] == "paused":
        out.append("Paused from %s. The human resumes with /comments-by-humans:%s."
                   % (state.get("paused_from"), state.get("paused_from") or "build"))
    if state.get("task"):
        out.append("Task: %s" % state["task"])
    cur = current_chunk(state)
    if cur:
        fu = cur.get("followup_prompt")
        fu_text = ""
        if cfg["depth"] == "strict":
            fu_text = (" · follow-up: not yet asked" if fu is None else
                       " · follow-up: answered" if state["prompt_count"] > fu else
                       " · follow-up: asked, awaiting the human's answer")
        out.append("Current chunk: %s · %s · attempts so far: %d%s"
                   % (cur["id"], where(cur), cur.get("attempts", 0), fu_text))
    if state["queue"]:
        out.append("Pending build chunks: %s" % ", ".join(
            "%s%s" % (c, " (re-explain)" if state["chunks"][c].get("regates") else "")
            for c in state["queue"]))
    passed = [c for c in state["chunks"].values() if c["status"] == "passed"]
    if passed:
        out.append("Passed chunks:")
        for c in sorted(passed, key=lambda c: int(c["id"][1:])):
            out.append("  %-4s %-36s %d attempt%s%s" % (
                c["id"], where(c), c["attempts"], "" if c["attempts"] == 1 else "s",
                " (re-explained %dx)" % c["regates"] if c.get("regates") else ""))
    rv = state.get("review")
    if rv:
        done = sum(1 for c in rv["chunks"].values() if c["status"] == "passed")
        out.append("Review %s · %s · %d of %d chunks passed · report: %s"
                   % (rv["id"], "finished" if rv.get("done") else "in progress", done,
                      len(rv["order"]), rv["report"]))
        for cid in rv["order"]:
            c = rv["chunks"][cid]
            if c["status"] == "passed":
                out.append("  %-4s %-36s %d attempt%s, %d finding%s" % (
                    cid, where(c), c["attempts"], "" if c["attempts"] == 1 else "s",
                    len(c["findings"]), "" if len(c["findings"]) == 1 else "s"))
    out.append("Next free build id: %s · Pauses: %d" % (next_free_id(state), state.get("pauses", 0)))
    for w in gate.warnings:
        out.append("Config warning: " + w)
    print("\n".join(out))


def _require_active(state):
    if state["mode"] not in ("build", "review"):
        raise Refusal("the gate is not active (mode=%s)" % state["mode"])


def _require_current(state, cid):
    _require_active(state)
    ch = get_chunk(state, cid)
    if ch is None:
        raise Refusal("there is no chunk %s" % cid)
    if ch["status"] != "pending":
        raise Refusal("%s is not waiting for an explanation (status: %s)" % (cid, ch["status"]))
    cur = current_chunk(state)
    if not cur or cur["id"] != cid:
        raise Refusal("%s is not the current chunk; the current chunk is %s"
                      % (cid, cur["id"] if cur else "none"))
    return ch


def cmd_followup(gate, state, args):
    if len(args) != 1:
        raise Refusal("usage: followup <chunk-id>")
    ch = _require_current(state, args[0])
    cfg = gate.config()
    if cfg["depth"] != "strict":
        raise Refusal("depth is %s, so no follow-up is needed; approve when the rubric is met"
                      % cfg["depth"])
    import comments
    v = view_of(gate, state, ch)
    if not v.found or not v.body:
        raise Refusal("the comment for %s is empty; the follow-up comes after the comment meets the "
                      "rubric" % ch["id"])
    if ch.get("seen_comment") != comments.text_hash(v.body):
        raise Refusal("the human has not submitted this version of the comment yet")
    ch["followup_prompt"] = state["prompt_count"]
    gate.save(state)
    print("Follow-up recorded for %s. End your turn and wait for the human's answer; then grade it "
          "and run `%s approve %s` if it shows understanding." % (ch["id"], GATE, ch["id"]))


def cmd_approve(gate, state, args):
    if len(args) != 1:
        raise Refusal("usage: approve <chunk-id>")
    import comments
    ch = _require_current(state, args[0])
    cfg = gate.config()
    v = view_of(gate, state, ch)
    if not v.found:
        raise Refusal(v.error)
    if not v.body:
        raise Refusal("the comment for %s is still the empty placeholder" % ch["id"])
    words = comments.word_count(v.body)
    if words < cfg["min_words"]:
        raise Refusal("the comment for %s has %d words; the minimum is %d"
                      % (ch["id"], words, cfg["min_words"]))
    if ch.get("seen_comment") != comments.text_hash(v.body):
        raise Refusal("the human has not submitted this version of the comment; they save the file "
                      "and send a message first")
    if v.code_hash != ch["hash"]:
        raise Refusal("the code under %s changed since the gate locked; the human can run "
                      "/comments-by-humans:%s to accept the new code"
                      % (ch["id"], "review" if ch["id"].startswith("r") else "build"))
    if cfg["depth"] == "strict":
        fu = ch.get("followup_prompt")
        if fu is None:
            raise Refusal("depth is strict: ask one follow-up question about %s, run `%s followup %s`, "
                          "and wait for the human's answer first" % (ch["id"], GATE, ch["id"]))
        if state["prompt_count"] <= fu:
            raise Refusal("the human has not answered the follow-up question yet")
    ch["status"] = "passed"
    ch["passed_at"] = now()
    if ch["id"].startswith("r"):
        approve_review(gate, state, ch, v)
        gate.save(state)
        print("Approved %s after %d attempt%s. Now share your own concerns about this chunk; the human "
              "chooses which to record with `%s finding %s \"...\"`. Then run `%s next`."
              % (ch["id"], ch["attempts"], "" if ch["attempts"] == 1 else "s", GATE, ch["id"], GATE))
        return
    path = gate.abs(ch["file"])
    lines = read_text(path).split("\n")
    lang = comments.language_for(ch["file"], "\n".join(lines[:1]))
    marker = next(m for m in comments.find_markers(lines, lang) if m.cid == ch["id"])
    write_text(path, "\n".join(comments.set_marker_tag(lines, marker, "EXPLAINED")))
    state["queue"].remove(ch["id"])
    ch["lines"] = [v.comment_line, v.code_range[1] if v.code_range else v.comment_line]
    gate.log("passed", chunk=ch["id"], file=ch["file"], lines=ch["lines"], attempts=ch["attempts"],
             regates=ch.get("regates", 0), mode="build", depth=cfg["depth"])
    gate.save(state)
    msg = "Approved %s after %d attempt%s; its tag is now EXPLAINED(human)." % (
        ch["id"], ch["attempts"], "" if ch["attempts"] == 1 else "s")
    nxt = current_chunk(state)
    if nxt:
        msg += (" The gate stays LOCKED on %s (%s): ask the human to explain it before you continue."
                % (nxt["id"], where(nxt)))
    else:
        msg += " The gate is open: continue with the next chunk (next free id: %s)." % next_free_id(state)
    print(msg)


def approve_review(gate, state, ch, v):
    import comments
    rv = state["review"]
    report = gate.abs(rv["report"])
    text = read_text(report)
    if rv["inline"]:
        path = gate.abs(ch["file"])
        lines = read_text(path).split("\n")
        lang = comments.language_for(ch["file"], "\n".join(lines[:1]))
        marker = next(m for m in comments.find_markers(lines, lang) if m.cid == ch["id"])
        write_text(path, "\n".join(comments.set_marker_tag(lines, marker, "EXPLAINED")))
        quoted = "\n".join("> " + l if l else ">" for l in v.body.split("\n"))
        text = text.replace("Explanation: inline at `%s:%d`." % (ch["file"], ch["lines"][0]),
                            "Explanation (inline at `%s:%d`):\n\n%s" % (ch["file"], ch["lines"][0], quoted), 1)
    else:
        text = text.replace("<!-- EXPLAIN(human) %s -->" % ch["id"], "<!-- EXPLAINED(human) %s -->" % ch["id"], 1)
    end = "<!-- /EXPLAIN %s -->" % ch["id"]
    status = "Status: passed · Attempts: %d\n\nFindings:\n<!-- FINDINGS %s -->\n<!-- /FINDINGS %s -->" % (
        ch["attempts"], ch["id"], ch["id"])
    if end in text:
        text = text.replace(end, end + "\n\n" + status, 1)
    else:
        text = text.rstrip("\n") + "\n\n" + status + "\n"
    write_text(report, text)
    gate.log("passed", chunk=ch["id"], file=ch["file"], lines=ch["code_lines"], attempts=ch["attempts"],
             mode="review", review=rv["id"], depth=gate.config()["depth"])


def cmd_finding(gate, state, args):
    if len(args) < 2:
        raise Refusal('usage: finding <chunk-id> "text of the finding"')
    cid, text = args[0], " ".join(args[1:]).strip()
    rv = state.get("review")
    ch = rv["chunks"].get(cid) if rv else None
    if not ch:
        raise Refusal("%s is not a chunk of the current review" % cid)
    if ch["status"] != "passed":
        raise Refusal("findings come after the human's explanation of %s passes" % cid)
    ch["findings"].append(text)
    report = gate.abs(rv["report"])
    body = read_text(report)
    end = "<!-- /FINDINGS %s -->" % cid
    body = body.replace(end, "- %s\n%s" % (text, end), 1)
    write_text(report, body)
    gate.log("finding", review=rv["id"], chunk=cid, text=text)
    gate.save(state)
    print("Recorded finding %d for %s." % (len(ch["findings"]), cid))


def cmd_next(gate, state, args):
    rv = state.get("review")
    if not rv or rv.get("done"):
        raise Refusal("no review is in progress")
    if state["mode"] != "review":
        raise Refusal("review mode is not active (mode=%s); the human resumes it with "
                      "/comments-by-humans:review" % state["mode"])
    cur = review_current(state)
    if cur:
        raise Refusal("%s has not passed yet; `next` opens only after the human's explanation passes"
                      % cur["id"])
    rv["position"] += 1
    if rv["position"] >= len(rv["order"]):
        finish_review(gate, state)
        gate.save(state)
        print("Review %s is complete: every chunk passed. Report: %s. Mode is now %s. Summarize the "
              "findings for the human." % (rv["id"], rv["report"], state["mode"]))
        return
    ch = rv["chunks"][rv["order"][rv["position"]]]
    present(gate, state, ch)
    gate.save(state)
    print(present_text(gate, state, ch))


def cmd_show(gate, state, args):
    cur = current_chunk(state)
    cid = args[0] if args else (cur["id"] if cur else None)
    ch = get_chunk(state, cid) if cid else None
    if not ch:
        raise Refusal("no such chunk%s" % (" " + cid if cid else ""))
    v = view_of(gate, state, ch)
    print("%s · %s · status: %s · attempts: %d" % (ch["id"], where(ch), ch["status"], ch.get("attempts", 0)))
    if not v.found:
        print("Problem: %s" % v.error)
        return
    print("Comment:\n%s" % (v.body or "(empty)"))
    if v.code_range:
        print("Code:")
        for k, line in enumerate(v.code.split("\n"), v.code_range[0]):
            print("%5d  %s" % (k, line))
    else:
        print("Code: (changed since the chunk was registered)")


def cmd_next_id(gate, state, args):
    print(next_free_id(state))


COMMANDS = {
    "status": cmd_status, "approve": cmd_approve, "followup": cmd_followup, "next": cmd_next,
    "finding": cmd_finding, "show": cmd_show, "next-id": cmd_next_id,
}

USAGE = """usage: gate.py <command> [args]

Commands for Claude (the gate decides; Claude only asks):
  status                 show mode, lock, current chunk and attempts
  show [id]              print a chunk's comment and code
  followup <id>          record that the strict follow-up question was asked
  approve <id>           pass the current chunk if the mechanical checks hold
  next                   review mode: present the next chunk
  finding <id> "text"    review mode: record a finding the human chose
  next-id                print the next free build chunk id

Hook entry points (called by Claude Code):
  hook pre-write | pre-bash | pre-skill | post-write | prompt-submit | prompt-expansion
"""


def run_cli(argv, root=None, fallback=True):
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    cmd = COMMANDS.get(argv[0])
    if not cmd:
        print("comments-by-humans: unknown command %r (pause and mode changes are typed by the human)"
              % argv[0])
        return 1
    try:
        if root and not fallback and not Gate(root).exists():
            raise Refusal("comments-by-humans is off in %s. The human starts it with "
                          "/comments-by-humans:build or /comments-by-humans:review." % root)
        root = cli_root(root if root and Gate(root).exists() else None)
        gate = Gate(root)
        with gate:
            state = gate.load()
            cmd(gate, state, argv[1:])
    except Refusal as e:
        if argv[0] == "status":
            print(str(e))
            return 0
        print("comments-by-humans: refused: %s" % e)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Hook dispatch


def run_hook(name):
    """Claude Code entry point: payload on stdin, Claude Code's output format on stdout."""
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    try:
        result = dispatch_hook(name, payload)
    except Denied as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(2)
    if not result:
        return
    if name == "post-write":
        emit({"decision": "block", "reason": result})
    elif name == "prompt-submit":
        emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": result}})
    elif name == "prompt-expansion":
        sys.stdout.write(result + "\n")


def dispatch_hook(name, payload, start=None):
    """Run one hook. Raises Denied to block; returns a message for the assistant, or None."""
    if name == "prompt-expansion":
        return hook_prompt_expansion(payload, start)
    if name == "prompt-submit":
        return hook_prompt_submit(payload, start)
    start = start or os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    root = find_root(start)
    if name == "pre-skill":
        return hook_pre_skill(root, payload)
    if not root:
        return None  # mode is off: exit at once
    gate = Gate(root)
    with gate:
        state = gate.load()
        if state["mode"] == "off":
            return None
        if name == "pre-write":
            return hook_pre_write(gate, state, payload)
        if name == "pre-bash":
            return hook_pre_bash(gate, state, payload)
        if name == "post-write":
            return hook_post_write(gate, state, payload)
    raise ValueError("unknown hook %r" % name)


# ---------------------------------------------------------------------------
# Library API for other front ends, such as the model-agnostic agent in ../agent.
# Same checks as the hooks; messages are translated to that front end's words.

FRONTEND_WORDS = {
    "agent": [(GATE + " ", "gate "), (GATE, "gate"), ("/comments-by-humans:", "/"),
              ("Write or Edit", "write_file or edit_file"), ("Write and Edit", "write_file and edit_file")],
    "claude-code": [],
}


def localize(text, frontend="agent"):
    if not text:
        return text
    for old, new in FRONTEND_WORDS[frontend]:
        text = text.replace(old, new)
    return text


def api_root(project):
    """The directory that holds (or will hold) the project's .comments-by-humans/."""
    return find_root(project, create=True)


def api_state(root):
    gate = Gate(root)
    if not gate.exists():
        return None
    with gate:
        return gate.load()


def api_pre_tool(root, tool, tool_input, frontend="agent"):
    """None when the tool call may run, else the reason it is denied. Fails closed."""
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": root}
    name = "pre-bash" if tool == "Bash" else "pre-skill" if tool == "Skill" else "pre-write"
    try:
        dispatch_hook(name, payload, start=root)
    except Denied as e:
        return localize(str(e), frontend)
    except Exception as e:  # fail closed
        return "comments-by-humans: internal error, failing closed: %s: %s" % (type(e).__name__, e)
    return None


def api_post_tool(root, tool, tool_input, frontend="agent"):
    """After a write: register chunks, lock, re-gate. Returns a message for the assistant, or None."""
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": root}
    try:
        return localize(dispatch_hook("post-write", payload, start=root), frontend)
    except Exception as e:
        return ("comments-by-humans: internal error after the write (%s: %s); the gate may not have "
                "registered it. Stop and tell the human." % (type(e).__name__, e))


def api_prompt(root, text, prompt_id, frontend="agent"):
    """A message the human sent. Returns gate context for the assistant, or None."""
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": text, "prompt_id": prompt_id, "cwd": root}
    return localize(dispatch_hook("prompt-submit", payload, start=root), frontend)


def api_command(root, cmd, args="", frontend="agent"):
    """A command the human typed: build, review or pause. Returns (text, ok)."""
    gate = Gate(find_root(root, create=cmd in ("build", "review")) or root)
    if not gate.exists() and cmd not in ("build", "review"):
        return "comments-by-humans: the gate is off here; start it with %s or %s." % (
            localize("/comments-by-humans:build", frontend), localize("/comments-by-humans:review", frontend)), False
    with gate:
        state = gate.load()
        text, ok = run_command(gate, state, cmd, args, "human")
        gate.save(state)
    return localize(text, frontend), ok


def api_cli(root, argv, frontend="agent"):
    """Run a gate CLI command for the assistant. Returns (exit code, output)."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_cli(argv, root, fallback=False)
    return code, localize(buf.getvalue().rstrip("\n"), frontend)


def main(argv):
    sys.path.insert(0, os.path.dirname(SCRIPT))
    if argv[:1] == ["hook"] and len(argv) == 2:
        try:
            run_hook(argv[1])
        except SystemExit:
            raise
        except BaseException as e:  # fail closed
            sys.stderr.write("comments-by-humans: internal error in hook %s, failing closed: %s: %s\n"
                             "If this persists, the human can inspect %s/state.json.\n"
                             % (argv[1], type(e).__name__, e, STATE_DIR))
            sys.exit(2)
        return 0
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
