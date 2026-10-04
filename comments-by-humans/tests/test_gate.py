"""Deterministic tests for the gate: every hook driven with real-shaped payloads.

Run: python3 -m unittest discover -s comments-by-humans/tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
GATE = os.path.join(PLUGIN, "scripts", "gate.py")
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
import comments  # noqa: E402

RETRY = '''import time


def retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("out of attempts")
'''

RETRY_WITH_PLACEHOLDER = RETRY.replace("def retry", "# EXPLAIN(human) c01\n#\ndef retry")
GOOD_COMMENT = ("# Calls fn until it succeeds, sleeping 1s, 2s, 4s between failures so a flaky\n"
                "# service can recover. Takes a zero-argument callable and returns its result;\n"
                "# callers get RuntimeError when every attempt fails. Catch: it swallows every\n"
                "# Exception, bugs included, and still sleeps after the final failure.\n")


class GateCase(unittest.TestCase):
    """A scratch git repository plus helpers that play Claude Code and the human."""

    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="cbh-test-"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"], check=True)
        self.prompt_no = 0

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    # plumbing ---------------------------------------------------------------

    def env(self):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = self.repo
        return env

    def hook(self, name, payload):
        payload.setdefault("cwd", self.repo)
        out = subprocess.run([sys.executable, GATE, "hook", name], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env(), timeout=60)
        return out.returncode, out.stdout, out.stderr

    def cli(self, *args):
        out = subprocess.run([sys.executable, GATE] + list(args), capture_output=True, text=True,
                             cwd=self.repo, env=self.env(), timeout=60)
        return out.returncode, out.stdout + out.stderr

    def path(self, rel):
        return os.path.join(self.repo, rel)

    def read(self, rel):
        with open(self.path(rel)) as f:
            return f.read()

    def put(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w") as f:
            f.write(text)

    def state(self):
        with open(self.path(".comments-by-humans/state.json")) as f:
            return json.load(f)

    def log(self):
        p = self.path(".comments-by-humans/log.jsonl")
        if not os.path.isfile(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f]

    def config(self, **values):
        os.makedirs(self.path(".comments-by-humans"), exist_ok=True)
        with open(self.path(".comments-by-humans/config.json"), "w") as f:
            json.dump(values, f)

    # Claude Code events --------------------------------------------------------

    def expand(self, command, args=""):
        self.prompt_no += 1
        pid = "p%d" % self.prompt_no
        code, out, err = self.hook("prompt-expansion", {
            "hook_event_name": "UserPromptExpansion", "expansion_type": "slash_command",
            "command_name": "comments-by-humans:" + command, "command_args": args,
            "command_source": "plugin", "prompt": "/comments-by-humans:%s %s" % (command, args),
            "prompt_id": pid})
        if code == 0:
            self.hook("prompt-submit", {"hook_event_name": "UserPromptSubmit", "prompt_id": pid,
                                        "prompt": "/comments-by-humans:%s %s" % (command, args)})
        return code, out, err

    def say(self, text="done"):
        self.prompt_no += 1
        code, out, err = self.hook("prompt-submit", {"hook_event_name": "UserPromptSubmit",
                                                     "prompt": text, "prompt_id": "p%d" % self.prompt_no})
        self.assertEqual(code, 0, err)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""

    def pre(self, tool, tool_input, **extra):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                   "tool_use_id": "toolu_test"}
        payload.update(extra)
        return self.hook("pre-bash" if tool == "Bash" else "pre-skill" if tool == "Skill"
                         else "pre-write", payload)

    def write(self, rel, content, expect_ok=True):
        """Claude's Write tool: PreToolUse, the write itself, PostToolUse."""
        ti = {"file_path": self.path(rel), "content": content}
        code, out, err = self.pre("Write", ti)
        if not expect_ok:
            self.assertEqual(code, 2, "write should be denied:\n" + out)
            return err
        self.assertEqual(code, 0, err)
        self.put(rel, content)
        code, out, err = self.hook("post-write", {"hook_event_name": "PostToolUse", "tool_name": "Write",
                                                  "tool_input": ti, "tool_response": {"type": "create"}})
        self.assertEqual(code, 0, err)
        return out

    def edit(self, rel, old, new, expect_ok=True):
        ti = {"file_path": self.path(rel), "old_string": old, "new_string": new, "replace_all": False}
        code, out, err = self.pre("Edit", ti)
        if not expect_ok:
            self.assertEqual(code, 2, "edit should be denied")
            return err
        self.assertEqual(code, 0, err)
        self.put(rel, self.read(rel).replace(old, new, 1))
        code, out, err = self.hook("post-write", {"hook_event_name": "PostToolUse", "tool_name": "Edit",
                                                  "tool_input": ti, "tool_response": {}})
        self.assertEqual(code, 0, err)
        return out

    def human_comment(self, rel, cid, text_lines):
        """The human types into the placeholder in their editor (no hooks fire)."""
        src = self.read(rel)
        marker = "# EXPLAIN(human) %s\n#\n" % cid
        self.assertIn(marker, src)
        self.put(rel, src.replace(marker, "# EXPLAIN(human) %s\n%s" % (cid, text_lines), 1))

    def locked_on_c01(self):
        self.expand("build", "add a retry helper")
        out = self.write("retry.py", RETRY_WITH_PLACEHOLDER)
        self.assertIn('"decision": "block"', out)
        return out


# ---------------------------------------------------------------------------
# Milestone 1: gate skeleton


class TestSkeleton(GateCase):
    def test_off_mode_exits_at_once(self):
        for tool, ti in (("Write", {"file_path": self.path("a.py"), "content": "x = 1\n"}),
                         ("Bash", {"command": "rm -rf build"}),
                         ("Skill", {"skill": "comments-by-humans:pause"})):
            code, out, err = self.pre(tool, ti)
            self.assertEqual((code, out, err), (0, "", ""), tool)
        self.assertEqual(self.hook("prompt-submit", {"prompt": "hi"})[:2], (0, ""))
        self.assertFalse(os.path.exists(self.path(".comments-by-humans")))

    def test_hand_set_lock_blocks_writes_and_commands(self):
        os.makedirs(self.path(".comments-by-humans"))
        state = {"mode": "build", "queue": ["c01"], "chunks": {"c01": {
            "id": "c01", "file": "a.py", "lines": [1, 3], "hash": "x", "status": "pending",
            "attempts": 0}}}
        with open(self.path(".comments-by-humans/state.json"), "w") as f:
            json.dump(state, f)
        cases = [
            ("Write", {"file_path": self.path("b.py"), "content": "y = 2\n"}),
            ("Write", {"file_path": self.path("notes.md"), "content": "exempt but locked\n"}),
            ("Write", {"file_path": "/tmp/outside-the-repo.py", "content": "z = 3\n"}),
            ("Edit", {"file_path": self.path("a.py"), "old_string": "a", "new_string": "b"}),
            ("MultiEdit", {"file_path": self.path("a.py"), "edits": []}),
            ("NotebookEdit", {"notebook_path": self.path("n.ipynb"), "new_source": "x"}),
            ("mcp__filesystem__write_file", {"path": self.path("c.py"), "content": "1"}),
            ("mcp__github__push_files", {"files": []}),
            ("Bash", {"command": "ls"}),
            ("Bash", {"command": "echo 'x = 1' > c.py"}),
            ("Bash", {"command": 'python3 "%s" approve c01; touch x.py' % GATE}),
            ("Bash", {"command": 'python3 "%s" approve c01 && touch x.py' % GATE}),
            ("Bash", {"command": 'python3 "%s" finding r01 "$(touch x.py)"' % GATE}),
            ("Bash", {"command": 'python3 "%s" status | tee x.py' % GATE}),
            ("Bash", {"command": 'python3 /tmp/gate.py status'}),
        ]
        for tool, ti in cases:
            code, _, err = self.pre(tool, ti)
            self.assertEqual(code, 2, "%s %s should be denied" % (tool, ti))
            self.assertIn("comments-by-humans", err)
        for cmd in ('python3 "%s" status' % GATE, "python3 %s show c01" % GATE,
                    'python3 "%s" finding r01 "quoted; text (fine)"' % GATE):
            self.assertEqual(self.pre("Bash", {"command": cmd})[0], 0, cmd)
        self.assertEqual(self.pre("mcp__github__get_file_contents", {"path": "x"})[0], 0)
        # Subagent tool calls carry agent_id and are gated the same way.
        code, _, _ = self.pre("Write", {"file_path": self.path("b.py"), "content": "1"},
                              agent_id="a1", agent_type="general-purpose")
        self.assertEqual(code, 2)

    def test_cli_status_reports_off(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("off", out)


# ---------------------------------------------------------------------------
# Milestone 2: build loop


class TestBuildLoop(GateCase):
    def test_placeholder_rules(self):
        self.expand("build", "retry helper")
        err = self.write("retry.py", RETRY, expect_ok=False)
        self.assertIn("outside any chunk", err)
        self.assertIn("# EXPLAIN(human) c01", err)
        two = RETRY_WITH_PLACEHOLDER + "\n\n# EXPLAIN(human) c02\n#\ndef other():\n    return 1\n"
        self.assertIn("adds 2 placeholders", self.write("retry.py", two, expect_ok=False))
        prefilled = RETRY_WITH_PLACEHOLDER.replace("# EXPLAIN(human) c01\n#", "# EXPLAIN(human) c01\n# it retries")
        self.assertIn("not empty", self.write("retry.py", prefilled, expect_ok=False))
        claimed = RETRY_WITH_PLACEHOLDER.replace("EXPLAIN(human)", "EXPLAINED(human)")
        self.assertIn("marked EXPLAINED", self.write("retry.py", claimed, expect_ok=False))
        empty = "# EXPLAIN(human) c01\n#\n"
        self.assertIn("no code directly under it", self.write("a.py", empty, expect_ok=False))
        self.assertIn("review id", self.write("retry.py", RETRY_WITH_PLACEHOLDER.replace("c01", "r01"),
                                              expect_ok=False))

    def test_exempt_and_unknown_files_need_no_placeholder(self):
        self.expand("build")
        for rel in ("README.md", "package.json", "config/app.yaml", "dist/bundle.js", "poetry.lock"):
            self.assertEqual(self.write(rel, "anything at all\n"), "", rel)
        self.assertEqual(self.write("data.weird", "x = 1\n"), "")
        self.assertEqual(self.log()[-1]["event"], "ungated_write")
        self.assertFalse(self.state()["locked"])

    def test_full_loop_strict(self):
        out = self.locked_on_c01()
        self.assertIn("LOCKED on c01", out)
        st = self.state()
        self.assertTrue(st["locked"])
        self.assertEqual(st["chunks"]["c01"]["lines"], [4, 12])
        ctx = self.say("what next?")
        self.assertIn("still empty", ctx)
        code, out = self.cli("approve", "c01")
        self.assertEqual(code, 1)
        self.assertIn("empty placeholder", out)

        self.human_comment("retry.py", "c01", "# retries\n")
        ctx = self.say()
        self.assertIn("attempt 1", ctx)
        self.assertIn("gate needs at least 5", ctx)
        self.assertIn("open question", ctx)
        self.assertIn("min", self.cli("approve", "c01")[1])

        src = self.read("retry.py").replace("# retries\n", GOOD_COMMENT)
        self.put("retry.py", src)
        code, out = self.cli("approve", "c01")
        self.assertIn("has not submitted this version", out)
        ctx = self.say()
        self.assertIn("attempt 2", ctx)
        self.assertIn("pointer to a specific line", ctx)
        self.assertIn("followup c01", ctx)
        self.assertIn("ask one follow-up", self.cli("approve", "c01")[1])
        self.assertEqual(self.cli("followup", "c01")[0], 0)
        self.assertIn("not answered the follow-up", self.cli("approve", "c01")[1])
        ctx = self.say("With attempts=0 it raises at once without calling fn.")
        self.assertIn("unchanged since attempt 2", ctx)
        self.assertIn("follow-up", ctx)
        code, out = self.cli("approve", "c01")
        self.assertEqual(code, 0, out)
        self.assertIn("Approved c01 after 2 attempts", out)
        self.assertIn("# EXPLAINED(human) c01", self.read("retry.py"))
        self.assertFalse(self.state()["locked"])
        passed = [e for e in self.log() if e["event"] == "passed"]
        self.assertEqual(len(passed), 1)
        self.assertEqual((passed[0]["chunk"], passed[0]["attempts"], passed[0]["file"]),
                         ("c01", 2, "retry.py"))
        code, out = self.cli("status")
        self.assertIn("c01", out)
        self.assertIn("2 attempts", out)

    def test_normal_depth_needs_no_followup(self):
        self.config(depth="normal")
        self.locked_on_c01()
        self.human_comment("retry.py", "c01", GOOD_COMMENT)
        ctx = self.say()
        self.assertNotIn("followup", ctx)
        code, out = self.cli("approve", "c01")
        self.assertEqual(code, 0, out)

    def test_approve_refuses_changed_code(self):
        self.config(depth="light")
        self.locked_on_c01()
        self.human_comment("retry.py", "c01", GOOD_COMMENT)
        self.say()
        self.put("retry.py", self.read("retry.py").replace("attempts=3", "attempts=4"))
        code, out = self.cli("approve", "c01")
        self.assertEqual(code, 1)
        self.assertIn("changed since the gate locked", out)
        self.assertIn("changed since the gate locked", self.say())
        self.expand("build")  # the human accepts their own code change
        self.say()
        self.assertEqual(self.cli("approve", "c01")[0], 0)

    def test_second_chunk_and_existing_code(self):
        self.config(depth="light")
        self.locked_on_c01()
        self.human_comment("retry.py", "c01", GOOD_COMMENT)
        self.say()
        self.cli("approve", "c01")
        # Appending a function needs its own placeholder.
        add = "\n\ndef double(x):\n    return 2 * x\n"
        self.assertIn("outside any chunk", self.edit("retry.py", 'raise RuntimeError("out of attempts")\n',
                                                     'raise RuntimeError("out of attempts")\n' + add,
                                                     expect_ok=False))
        out = self.edit("retry.py", 'raise RuntimeError("out of attempts")\n',
                        'raise RuntimeError("out of attempts")\n\n\n# EXPLAIN(human) c02\n#\n'
                        'def double(x):\n    return 2 * x\n')
        self.assertIn("New chunk c02", out)
        self.assertEqual(self.state()["current"], "c02")

    def test_preexisting_code_needs_placeholder_above_function(self):
        self.put("legacy.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
        self.expand("build")
        self.assertIn("outside any chunk", self.edit("legacy.py", "return 2", "return 3", expect_ok=False))
        out = self.edit("legacy.py", "def b():\n    return 2", "# EXPLAIN(human) c01\n#\ndef b():\n    return 3")
        self.assertIn("New chunk c01", out)
        self.assertEqual(self.state()["chunks"]["c01"]["lines"], [5, 8])

    def test_claude_cannot_touch_the_humans_comment(self):
        self.config(depth="light")
        self.locked_on_c01()
        self.human_comment("retry.py", "c01", GOOD_COMMENT)
        self.say()
        self.cli("approve", "c01")
        self.assertIn("only the human edits it",
                      self.edit("retry.py", "Calls fn until", "Invokes fn until", expect_ok=False))
        self.assertIn("only the human edits it",
                      self.edit("retry.py", "EXPLAINED(human) c01", "EXPLAIN(human) c01", expect_ok=False))
        src = self.read("retry.py")
        comment_block = src[src.index("# EXPLAINED"):src.index("def retry")]
        self.assertIn("keeps its code", self.edit("retry.py", comment_block, "", expect_ok=False))
        # Deleting the whole chunk is allowed and logged.
        whole = src[src.index("# EXPLAINED"):]
        self.edit("retry.py", whole, "")
        self.assertEqual(self.state()["chunks"]["c01"]["status"], "removed")
        self.assertEqual(self.log()[-1]["event"], "removed")

    def test_typescript_block_comment(self):
        self.config(depth="light")
        self.expand("build")
        ts = ("/* EXPLAIN(human) c01\n *\n */\nexport function retryWithBackoff(fn: () => number, attempts = 3) {\n"
              "  for (let i = 0; i < attempts; i++) {\n    try { return fn(); } catch { }\n  }\n"
              "  throw new Error(\"out of attempts\");\n}\n")
        self.write("src/retry.ts", ts)
        self.assertEqual(self.state()["chunks"]["c01"]["lines"], [1, 9])
        src = self.read("src/retry.ts").replace(" *\n */", " * Calls fn until it works, at most attempts times, then\n"
                                                  " * throws. Used by the API client for flaky calls.\n */", 1)
        self.put("src/retry.ts", src)
        self.say()
        code, out = self.cli("approve", "c01")
        self.assertEqual(code, 0, out)
        self.assertIn("/* EXPLAINED(human) c01", self.read("src/retry.ts"))


# ---------------------------------------------------------------------------
# Milestone 3: hardening


class TestHardening(GateCase):
    def approved_c01(self):
        self.config(depth="light")
        self.locked_on_c01()
        self.human_comment("retry.py", "c01", GOOD_COMMENT)
        self.say()
        self.assertEqual(self.cli("approve", "c01")[0], 0)

    def test_regate_on_any_change(self):
        self.approved_c01()
        out = self.edit("retry.py", "attempts=3", "attempts=5")
        self.assertIn("gate reopened", out)
        self.assertIn("# EXPLAIN(human) c01", self.read("retry.py"))
        self.assertIn("Calls fn until", self.read("retry.py"))
        st = self.state()
        self.assertTrue(st["locked"])
        self.assertEqual(st["chunks"]["c01"]["regates"], 1)
        self.assertEqual(self.log()[-1]["event"], "regated")
        self.write("other.py", "# EXPLAIN(human) c02\n#\nX = 1\n", expect_ok=False)
        self.say()
        self.assertEqual(self.cli("approve", "c01")[0], 0)

    def test_duplicate_markers_are_reported_not_guessed(self):
        self.config(depth="light")
        self.locked_on_c01()
        src = self.read("retry.py").replace(
            "# EXPLAIN(human) c01\n#\n",
            "# EXPLAIN(human) c01\n# first try at it\n# EXPLAINED(human) c01\n# Calls fn until it works.\n", 1)
        self.put("retry.py", src)
        ctx = self.say()
        self.assertIn("2 EXPLAIN markers for c01", ctx)
        self.assertIn("keep exactly one", ctx)
        code, out = self.cli("approve", "c01")
        self.assertEqual(code, 1)
        self.assertIn("markers for c01", out)

    def test_nested_chunk_approval_does_not_regate_outer(self):
        self.config(depth="light")
        self.expand("build")
        src = "# EXPLAIN(human) c01\n#\nclass A:\n    x = 1\n"
        self.write("a.py", src)
        self.put("a.py", self.read("a.py").replace("#\nclass", "# A holds x for the demo, used by tests later on.\nclass"))
        self.say()
        self.assertEqual(self.cli("approve", "c01")[0], 0)
        out = self.edit("a.py", "    x = 1\n", "    x = 1\n\n    # EXPLAIN(human) c02\n    #\n    def f(self):\n        return self.x\n")
        self.assertIn("New chunk c02", out)
        self.assertIn("c01", out)  # adding a method changes the class: both gates open
        st = self.state()
        self.assertEqual(st["queue"], ["c01", "c02"])

    def test_pause_is_human_only_and_logged(self):
        self.locked_on_c01()
        code, _, err = self.pre("Skill", {"skill": "comments-by-humans:pause", "args": ""})
        self.assertEqual(code, 2)
        self.assertIn("only the human", err)
        self.assertEqual(self.cli("pause")[0], 1)
        code, out, _ = self.expand("pause")
        self.assertEqual(code, 0)
        self.assertIn("PAUSED", out)
        st = self.state()
        self.assertEqual((st["mode"], st["locked"], st["pauses"]), ("paused", False, 1))
        self.assertEqual(self.log()[-1]["event"], "pause")
        self.assertEqual(len([e for e in self.log() if e["event"] == "pause"]), 1)
        self.write("free.py", "print('ungated')\n")
        self.assertEqual(self.log()[-1]["event"], "ungated_write")
        self.assertEqual(self.pre("Bash", {"command": "ls"})[0], 0)
        self.put("retry.py", self.read("retry.py").replace("attempts=3", "attempts=9"))
        code, out, _ = self.expand("build")
        self.assertIn("LOCKED on c01", out)
        self.assertIn("ungated", out)
        events = [e["event"] for e in self.log()]
        self.assertIn("resume", events)
        self.assertIn("ungated_change", events)
        self.assertTrue(self.state()["locked"])

    def test_typed_command_fallback_without_expansion_hook(self):
        self.locked_on_c01()
        self.prompt_no += 1
        self.hook("prompt-submit", {"prompt": "/comments-by-humans:pause", "prompt_id": "solo"})
        self.assertEqual(self.state()["mode"], "paused")
        self.assertEqual(len([e for e in self.log() if e["event"] == "pause"]), 1)

    def test_protected_paths(self):
        self.locked_on_c01()
        self.expand("pause")
        for path in (self.path(".comments-by-humans/state.json"), self.path(".comments-by-humans/config.json"),
                     self.path(".claude/settings.json"), self.path(".claude/settings.local.json"),
                     os.path.join(PLUGIN, "scripts", "gate.py"), os.path.join(PLUGIN, "hooks", "hooks.json")):
            code, _, err = self.pre("Write", {"file_path": path, "content": "{}"})
            self.assertEqual(code, 2, path)
            self.assertIn("protected", err)
        for cmd in ("rm -rf .comments-by-humans", "cat .comments-by-humans/state.json",
                    "claude plugin disable comments-by-humans",
                    "jq '.hooks={}' .claude/settings.json > /tmp/s && mv /tmp/s .claude/settings.json"):
            self.assertEqual(self.pre("Bash", {"command": cmd})[0], 2, cmd)

    def test_shell_writes_into_code_are_blocked_when_unlocked(self):
        self.expand("build")
        for cmd in ("cat > app.py <<'EOF'\nprint(1)\nEOF", "echo 'x=1' >> app.py", "sed -i 's/a/b/' app.py",
                    "cp template.py app.py", "printf 'x' | tee src/app.ts", "git apply fix.patch"):
            self.assertEqual(self.pre("Bash", {"command": cmd})[0], 2, cmd)
        for cmd in ("pytest -q", "npm install lodash", "echo hi > notes.md", "ls > /dev/null 2>&1",
                    "git status", "python3 -c 'print(1)'"):
            self.assertEqual(self.pre("Bash", {"command": cmd})[0], 0, cmd)

    def test_mcp_file_writes_must_go_through_write_and_edit(self):
        self.expand("build")
        self.assertEqual(self.pre("mcp__filesystem__write_file", {"path": "a.py"})[0], 2)
        self.assertEqual(self.pre("mcp__filesystem__edit_file", {"path": "a.py"})[0], 2)
        self.assertEqual(self.pre("mcp__filesystem__read_file", {"path": "a.py"})[0], 0)
        self.assertEqual(self.pre("mcp__slack__send_message", {"text": "hi"})[0], 0)

    def test_fail_closed(self):
        self.locked_on_c01()
        with open(self.path(".comments-by-humans/state.json"), "w") as f:
            f.write("{not json")
        code, _, err = self.pre("Write", {"file_path": self.path("b.py"), "content": "1"})
        self.assertEqual(code, 2)
        self.assertIn("failing closed", err)
        self.assertEqual(self.pre("Bash", {"command": "ls"})[0], 2)
        out = subprocess.run([sys.executable, GATE, "hook", "pre-write"], input="not json",
                             capture_output=True, text=True, env=self.env())
        self.assertEqual(out.returncode, 2)

    def test_claude_starting_build_is_allowed(self):
        code, _, _ = self.pre("Skill", {"skill": "comments-by-humans:build", "args": "x"})
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["mode"], "build")


# ---------------------------------------------------------------------------
# Milestone 4: review mode

APP = '''"""Tiny app."""
from store import Store
from util import slugify


def handle(store, title):
    slug = slugify(title)
    store.put(slug, {"title": title})
    return slug


if __name__ == "__main__":
    print(handle(Store(), "Hello World"))
'''
UTIL = '''import re

MAX = 40


def slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text.strip("-")[:MAX]
'''
STORE = '''class Store:
    def __init__(self):
        self.data = {}

    def put(self, key, value):
        if key in self.data:
            raise KeyError(key)
        self.data[key] = value
'''


class TestReview(GateCase):
    def setUp(self):
        super().setUp()
        self.put("src/app.py", APP)
        self.put("src/util.py", UTIL)
        self.put("src/store.py", STORE)
        self.put("src/README.md", "docs\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "init"], check=True)
        self.config(depth="light")

    def explain(self, cid, text="This chunk does one specific job for a reason, taking input and returning output."):
        st = self.state()
        rv = st["review"]
        if rv["inline"]:
            ch = rv["chunks"][cid]
            src = self.read(ch["file"])
            self.put(ch["file"], src.replace("# EXPLAIN(human) %s\n#\n" % cid,
                                             "# EXPLAIN(human) %s\n# %s\n" % (cid, text), 1))
        else:
            report = self.read(rv["report"])
            self.put(rv["report"], report.replace("<!-- EXPLAIN(human) %s -->\n\n" % cid,
                                                  "<!-- EXPLAIN(human) %s -->\n%s\n" % (cid, text), 1))
        self.say()

    def test_review_directory_in_construction_order(self):
        code, out, err = self.expand("review", "src/")
        self.assertEqual(code, 0, err)
        self.assertIn("review", out)
        rv = self.state()["review"]
        files = [rv["chunks"][c]["file"] for c in rv["order"]]
        defs = [rv["chunks"][c]["defs"] for c in rv["order"]]
        self.assertEqual(files[0], "src/store.py")
        self.assertIn("slugify", sum(defs[:3], []))
        self.assertTrue(rv["chunks"][rv["order"][-1]]["entry"])
        self.assertNotIn("src/README.md", files)
        idx = {name: i for i, d in enumerate(defs) for name in d}
        self.assertLess(idx["slugify"], idx["handle"])
        self.assertLess(idx["Store"], idx["handle"])
        self.assertEqual(self.state()["mode"], "review")
        self.assertTrue(self.state()["locked"])

        self.assertEqual(self.write("src/new.py", "x = 1\n", expect_ok=False).count("review mode"), 1)
        self.assertEqual(self.pre("Bash", {"command": "pytest"})[0], 2)
        self.assertIn("has not passed", self.cli("next")[1])
        first = rv["order"][0]
        self.assertIn("after the human's explanation", self.cli("finding", first, "x")[1])

        for k, cid in enumerate(rv["order"]):
            self.explain(cid)
            code, out = self.cli("approve", cid)
            self.assertEqual(code, 0, out)
            if k == 0:
                self.assertEqual(self.cli("finding", cid, "put() raises KeyError on duplicates")[0], 0)
                self.assertEqual(self.pre("Bash", {"command": "pytest"})[0], 0)
            code, out = self.cli("next")
            self.assertEqual(code, 0, out)
        st = self.state()
        self.assertEqual(st["mode"], "off")
        report = self.read(st["review"]["report"])
        self.assertIn("## Summary", report)
        self.assertIn("- put() raises KeyError on duplicates", report)
        self.assertEqual(report.count("EXPLAINED(human)"), len(rv["order"]))
        self.assertIn("review_finished", [e["event"] for e in self.log()])

    def test_review_diff_range_inline(self):
        self.put("src/util.py", UTIL + "\n\ndef title_case(text):\n    return text.title()\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "title"], check=True)
        code, out, err = self.expand("review", "HEAD~1..HEAD --inline")
        self.assertEqual(code, 0, err)
        rv = self.state()["review"]
        self.assertTrue(rv["inline"])
        self.assertEqual(len(rv["order"]), 1)
        self.assertIn("# EXPLAIN(human) r01", self.read("src/util.py"))
        self.explain("r01")
        self.assertEqual(self.cli("approve", "r01")[0], 0)
        self.assertIn("# EXPLAINED(human) r01", self.read("src/util.py"))
        self.assertEqual(self.cli("next")[0], 0)
        self.assertIn("This chunk does one specific job", self.read(rv["report"]))

    def test_review_returns_to_build_and_keeps_build_lock(self):
        self.expand("build")
        self.write("job.py", "# EXPLAIN(human) c01\n#\ndef job():\n    return 1\n")
        self.expand("review", "src/store.py")
        st = self.state()
        self.assertEqual(st["current"], "r01")
        self.explain("r01")
        self.cli("approve", "r01")
        self.cli("next")
        st = self.state()
        self.assertEqual((st["mode"], st["current"], st["locked"]), ("build", "c01", True))

    def test_pausing_a_review_keeps_the_return_to_build(self):
        self.expand("build")
        self.expand("review", "src/store.py")
        self.expand("pause")
        self.assertEqual(self.state()["mode"], "paused")
        code, out, _ = self.expand("review")
        self.assertIn("resumed", out)
        self.explain("r01")
        self.cli("approve", "r01")
        self.cli("next")
        self.assertEqual(self.state()["mode"], "build")

    def test_bad_target_blocks_the_command(self):
        code, _, err = self.expand("review", "no/such/thing")
        self.assertEqual(code, 2)
        self.assertIn("neither a path nor a diff range", err)
        code, _, err = self.expand("review", "")
        self.assertEqual(code, 2)


# ---------------------------------------------------------------------------
# comments.py


class TestComments(unittest.TestCase):
    def extent(self, src, path, start):
        lines = src.split("\n")
        lang = comments.language_for(path)
        return comments.extent(lines, comments.lex(lines, lang), start, lang) + 1

    def test_python_extent_with_blank_lines_and_decorator(self):
        src = "@cache\ndef f(x):\n    y = x\n\n    return y\n\n\ndef g():\n    pass\n"
        self.assertEqual(self.extent(src, "a.py", 0), 5)

    def test_python_docstring_at_column_zero(self):
        src = 'def f():\n    """Doc\nstill doc\n    """\n    return 1\n\nX = 2\n'
        self.assertEqual(self.extent(src, "a.py", 0), 5)

    def test_brace_languages(self):
        js = "export function f(a,\n  b) {\n  if (a) {\n    return '}';\n  } else {\n    return b;\n  }\n}\n\nconst x = 1;\n"
        self.assertEqual(self.extent(js, "a.ts", 0), 8)
        cs = "public void Foo()\n{\n    Bar();\n}\n\npublic void Baz() {}\n"
        self.assertEqual(self.extent(cs, "a.cs", 0), 4)
        rust = "fn longest<'a>(x: &'a str, y: &'a str) -> &'a str {\n    if x.len() > y.len() { x } else { y }\n}\n\nfn main() {}\n"
        self.assertEqual(self.extent(rust, "a.rs", 0), 3)
        go = "func (s *S) Run() error {\n\treturn nil\n}\n\nfunc other() {}\n"
        self.assertEqual(self.extent(go, "a.go", 0), 3)

    def test_ruby_and_shell(self):
        rb = "def foo\n  bar\nend\n\ndef baz\nend\n"
        self.assertEqual(self.extent(rb, "a.rb", 0), 3)
        sh = "deploy() {\n  echo \"#notacomment\"\n}\n\nmain\n"
        self.assertEqual(self.extent(sh, "a.sh", 0), 3)

    def test_enclosing_scope_ends_chunk(self):
        java = "class A {\n    void f() {\n        g();\n    }\n}\n"
        self.assertEqual(self.extent(java, "A.java", 1), 4)

    def test_markers_and_bodies(self):
        lines = ["/* EXPLAIN(human) c07", " * Retries fn.", " * Twice.", " */", "function f() {}"]
        m = comments.find_markers(lines, comments.language_for("a.js"))[0]
        self.assertEqual((m.cid, m.tag, m.start, m.end, m.body), ("c07", "EXPLAIN", 0, 3, "Retries fn.\nTwice."))
        lines = ["  <!-- EXPLAINED(human) c02", "  The nav bar.", "  -->", "  <nav></nav>"]
        m = comments.find_markers(lines, comments.language_for("a.html"))[0]
        self.assertEqual((m.cid, m.explained, m.body), ("c02", True, "The nav bar."))
        lines = ["# EXPLAIN(human) c01", "#", "x = 1"]
        m = comments.find_markers(lines, comments.language_for("a.py"))[0]
        self.assertEqual((m.end, m.body), (1, ""))
        self.assertEqual(comments.find_markers(['s = "EXPLAIN(human) c01"'], comments.language_for("a.py")), [])

    def test_placeholders_round_trip(self):
        for path in ("a.ts", "a.py", "a.html", "a.lua", "a.hs", "a.sql", "a.clj", "Makefile", "a.css"):
            lang = comments.language_for(path)
            lines = lang.placeholder("c03", "  ") + ["  x = 1"]
            m = comments.find_markers(lines, lang)
            self.assertEqual(len(m), 1, path)
            self.assertEqual((m[0].cid, m[0].body), ("c03", ""), path)

    def test_units_split_large_class(self):
        methods = "".join("    def m%d(self):\n%s\n" % (i, "        x = 1\n" * 30) for i in range(4))
        src = "class Big:\n" + methods
        lines = src.split("\n")
        units = comments.units(lines, comments.language_for("a.py"), target=40)
        self.assertEqual(len(units), 4)
        self.assertEqual(units[0][0], 0)

    def test_hash_ignores_nested_marker_comments(self):
        lang = comments.language_for("a.py")
        a = "class A:\n    # EXPLAIN(human) c02\n    #\n    def f(self):\n        pass\n".split("\n")
        b = "class A:\n    # EXPLAINED(human) c02\n    # f does nothing on purpose.\n    def f(self):\n        pass\n".split("\n")
        ha = comments.region_hash(a, 0, 4, comments.find_markers(a, lang))
        hb = comments.region_hash(b, 0, 4, comments.find_markers(b, lang))
        self.assertEqual(ha, hb)


if __name__ == "__main__":
    unittest.main()
