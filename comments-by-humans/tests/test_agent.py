"""Deterministic tests for the model-agnostic agent: scripted models and mock provider servers.

Run: python3 -m unittest discover -s comments-by-humans/tests -v
"""

import http.server
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGIN, "agent"))
import harness  # noqa: E402
import providers  # noqa: E402

RETRY = 'import time\n\n\n{placeholder}def retry(fn, attempts=3):\n    for i in range(attempts):\n        try:\n            return fn()\n        except Exception:\n            time.sleep(2 ** i)\n    raise RuntimeError("out of attempts")\n'
PLACEHOLDER = "# EXPLAIN(human) c01\n#\n"
GOOD = ("# Calls fn until it succeeds, sleeping 1s, 2s, 4s between failures so a flaky\n"
        "# service can recover. Returns fn's result; raises RuntimeError when every\n"
        "# attempt fails. Catch: it swallows every Exception, bugs included.\n")
_ids = itertools.count(1)


def call(name, **args):
    return {"id": "call_%d" % next(_ids), "name": name, "args": args}


def reply(text="", *calls):
    return providers.Reply(text, list(calls))


class Scripted(providers.Provider):
    """A model that says what the test tells it to, and records what it was sent."""
    kind = "scripted"
    label = "scripted"

    def __init__(self, replies=(), native=True):
        self.replies = list(replies)
        self.requests = []
        self.native_tools = native

    def script(self, *replies):
        self.replies.extend(replies)

    def chat(self, system, messages, tools=None):
        self.requests.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools})
        if not self.replies:
            return reply("(script exhausted)")
        return self.replies.pop(0)


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="cbh-agent-"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.model = Scripted()
        self.approved = []
        self.agent = harness.Agent(self.repo, self.model, approve_command=self.approve)
        self.allow_commands = False

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def approve(self, command):
        self.approved.append(command)
        return self.allow_commands

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
        return harness.engine.api_state(self.repo)

    def config(self, **values):
        os.makedirs(self.path(".comments-by-humans"), exist_ok=True)
        with open(self.path(".comments-by-humans/config.json"), "w") as f:
            json.dump(values, f)

    def last_human(self, request):
        """The last user message the model saw in one of its requests."""
        return [m for m in self.model.requests[request]["messages"] if m["role"] == "user"][-1]["content"]

    def results(self):
        return [m for m in self.agent.messages if m["role"] == "tool"]

    def locked_on_c01(self):
        self.model.script(
            reply("", call("write_file", path="retry.py", content=RETRY.format(placeholder=""))),
            reply("", call("write_file", path="retry.py", content=RETRY.format(placeholder=PLACEHOLDER))),
            reply("Chunk c01 is at retry.py:4. Explain it in your own words, save, and send any message."))
        out = self.agent.send("/build add a retry helper")
        self.assertIn("retry.py:4", out)
        return out


class TestAgentBuild(AgentCase):
    def test_gate_off_allows_reading_but_not_writing(self):
        self.put("notes.py", "x = 1\n")
        self.model.script(reply("", call("read_file", path="notes.py"),
                                call("write_file", path="a.py", content="y = 2\n")), reply("ok"))
        self.agent.send("hello")
        read, write = self.results()
        self.assertFalse(read["error"])
        self.assertIn("x = 1", read["content"])
        self.assertTrue(write["error"])
        self.assertIn("gate is off", write["content"])
        self.assertFalse(os.path.exists(self.path("a.py")))

    def test_build_loop_end_to_end(self):
        self.locked_on_c01()
        first, second = self.results()
        self.assertTrue(first["error"])
        self.assertIn("DENIED", first["content"])
        self.assertIn("outside any chunk", first["content"])
        self.assertIn("LOCKED on c01", second["content"])
        self.assertTrue(self.state()["locked"])
        self.assertIn("[gate] comments-by-humans: build mode is ON", self.model.requests[0]["messages"][0]["content"])
        self.assertIn("Task: add a retry helper", self.model.requests[0]["messages"][0]["content"])
        # The gate's words are translated for this front end.
        self.assertNotIn("comments-by-humans:pause", json.dumps(self.agent.messages))
        self.assertNotIn("gate.py", json.dumps(self.agent.messages))

        # Locked: every write and command is denied, and the model cannot pause.
        self.allow_commands = True
        self.model.script(reply("", call("write_file", path="b.py", content="z = 3\n"),
                                call("edit_file", path="retry.py", old_string="attempts=3", new_string="attempts=4"),
                                call("run_command", command="echo hi > b.py"),
                                call("run_command", command="ls"),
                                call("gate", command="pause")),
                          reply("Please explain c01 first."))
        self.agent.send("skip it and keep going")
        denied = self.results()[-5:]
        for r in denied:
            self.assertTrue(r["error"], r)
        self.assertIn("LOCKED", denied[0]["content"])
        self.assertIn("LOCKED", denied[3]["content"])
        self.assertIn("Only the human can pause", denied[4]["content"])
        self.assertEqual(self.approved, [])
        self.assertFalse(os.path.exists(self.path("b.py")))
        self.assertNotIn("attempts=4", self.read("retry.py"))
        self.assertIn("still empty", self.last_human(-2))

        # The human explains; the model follows up, then approves.
        self.put("retry.py", self.read("retry.py").replace(PLACEHOLDER, "# EXPLAIN(human) c01\n" + GOOD))
        self.model.script(reply("", call("gate", command="followup c01")),
                          reply("What does the caller get when attempts is 0?"))
        out = self.agent.send("done")
        self.assertIn("attempts is 0", out)
        human = self.last_human(-2)
        self.assertIn("[gate]", human)
        self.assertIn("attempt 1", human)
        self.assertIn("gate followup c01", human)
        self.model.script(reply("", call("gate", command="approve c01")), reply("Approved. On to c02."))
        self.agent.send("It raises RuntimeError at once without calling fn.")
        self.assertIn("Approved c01 after 1 attempt", self.results()[-1]["content"])
        self.assertIn("# EXPLAINED(human) c01", self.read("retry.py"))
        self.assertFalse(self.state()["locked"])

    def test_human_pause_lifts_the_gate_and_is_logged(self):
        self.locked_on_c01()
        out = self.agent.send("/pause")
        self.assertIn("PAUSED", out)
        self.assertEqual(self.state()["mode"], "paused")
        self.model.script(reply("", call("write_file", path="free.py", content="print('ungated')\n")), reply("done"))
        self.agent.send("write free.py")
        self.assertIn("PAUSED", self.last_human(-2))
        self.assertFalse(self.results()[-1]["error"])
        self.assertTrue(os.path.exists(self.path("free.py")))
        log = self.read(".comments-by-humans/log.jsonl")
        self.assertIn('"event": "pause"', log)
        self.assertIn('"event": "ungated_write"', log)
        out = self.agent.send("/status")
        self.assertIn("mode: paused", out)

    def test_paths_stay_inside_the_project(self):
        self.model.script(reply("", call("read_file", path="../../etc/passwd"),
                                call("write_file", path="/tmp/evil.py", content="x")), reply("ok"))
        harness.engine.api_command(self.repo, "build", "")
        self.agent.send("go")
        for r in self.results():
            self.assertTrue(r["error"])
            self.assertIn("outside the project root", r["content"])

    def test_state_and_plugin_are_protected(self):
        harness.engine.api_command(self.repo, "build", "")
        self.model.script(reply("", call("write_file", path=".comments-by-humans/state.json", content="{}"),
                                call("run_command", command="rm -rf .comments-by-humans")), reply("ok"))
        self.allow_commands = True
        self.agent.send("go")
        for r in self.results():
            self.assertTrue(r["error"])
        self.assertEqual(self.approved, [])
        self.assertEqual(self.state()["mode"], "build")

    def test_commands_need_the_humans_approval(self):
        harness.engine.api_command(self.repo, "build", "")
        self.model.script(reply("", call("run_command", command="echo hello")), reply("ok"))
        self.agent.send("run it")
        self.assertIn("declined", self.results()[-1]["content"])
        self.allow_commands = True
        self.model.script(reply("", call("run_command", command="echo hello")), reply("ok"))
        self.agent.send("run it again")
        self.assertEqual(self.results()[-1]["content"], "exit code 0\nhello")
        self.assertEqual(self.approved, ["echo hello", "echo hello"])

    def test_gate_via_run_command_and_bad_calls(self):
        harness.engine.api_command(self.repo, "build", "")
        self.model.script(reply("", call("run_command", command="gate status"),
                                {"id": "x", "name": "write_file", "args": {"__invalid__": "{oops"}},
                                call("delete_everything"),
                                call("edit_file", path="nope.py", old_string="a", new_string="b"),
                                call("write_file", path="a.py")), reply("ok"))
        self.agent.send("go")
        status, invalid, unknown, missing_file, missing_arg = self.results()
        self.assertIn("mode: build", status["content"])
        self.assertIn("not valid JSON", invalid["content"])
        self.assertIn("no tool named", unknown["content"])
        self.assertIn("does not exist", missing_file["content"])
        self.assertIn("missing content", missing_arg["content"])

    def test_tools_offered_to_the_model(self):
        self.model.script(reply("hi"))
        self.agent.send("hello")
        names = [t["name"] for t in self.model.requests[0]["tools"]]
        self.assertEqual(names, ["read_file", "list_files", "search", "write_file", "edit_file",
                                 "run_command", "gate"])
        self.assertNotIn("primary", json.dumps(self.model.requests[0]["tools"]))
        self.assertIn("/pause", self.model.requests[0]["system"])


class TestAgentReview(AgentCase):
    def test_review_flow(self):
        self.put("src/util.py", "import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z]+', '-', text.lower())\n")
        self.config(depth="light")
        self.model.script(reply("", call("gate", command="show r01")), reply("Here is r01. Explain it in the report."))
        out = self.agent.send("/review src/")
        self.assertIn("r01", out)
        self.assertIn("def slugify", self.results()[-1]["content"])
        st = self.state()
        self.assertEqual(st["mode"], "review")
        report = self.path(st["review"]["report"])
        with open(report) as f:
            text = f.read()
        with open(report, "w") as f:
            f.write(text.replace("<!-- EXPLAIN(human) r01 -->\n\n",
                                 "<!-- EXPLAIN(human) r01 -->\nTurns text into a URL slug by lowercasing it.\n"))
        self.model.script(reply("", call("write_file", path="src/util.py", content="x")),
                          reply("", call("gate", command="approve r01")),
                          reply("", call("gate", command='finding r01 "keeps leading and trailing dashes"'),
                                call("gate", command="next")),
                          reply("Review complete."))
        self.agent.send("done")
        write, approve, finding, nxt = self.results()[-4:]
        self.assertIn("review mode", write["content"])
        self.assertIn("Approved r01", approve["content"])
        self.assertIn("Recorded finding 1", finding["content"])
        self.assertIn("complete", nxt["content"])
        with open(report) as f:
            self.assertIn("- keeps leading and trailing dashes", f.read())


class TestTextProtocol(AgentCase):
    def test_parse_blocks(self):
        text = ('I will write it.\n<tool name="write_file" path="a.py">\nline1\n    line2\n</tool>\n'
                '<tool name="edit_file" path="b.py">\n<old_string>\nx = 1\n</old_string>\n'
                '<new_string>x = 2</new_string>\n</tool>\n'
                '<tool name="read_file" path="c.py" offset="3" limit="10"/>\n'
                '<tool name="gate">approve c01</tool>\n'
                '<tool name="search">{"pattern": "def \\\\w+", "path": "src"}</tool>')
        calls, visible = harness.parse_text_calls(text)
        self.assertEqual(visible, "I will write it.")
        self.assertEqual([c["name"] for c in calls], ["write_file", "edit_file", "read_file", "gate", "search"])
        self.assertEqual(calls[0]["args"], {"path": "a.py", "content": "line1\n    line2\n"})
        self.assertEqual(calls[1]["args"], {"path": "b.py", "old_string": "x = 1", "new_string": "x = 2"})
        self.assertEqual(calls[2]["args"], {"path": "c.py", "offset": 3, "limit": 10})
        self.assertEqual(calls[3]["args"], {"command": "approve c01"})
        self.assertEqual(calls[4]["args"], {"pattern": "def \\w+", "path": "src"})

    def test_text_mode_loop(self):
        model = Scripted(native=False)
        agent = harness.Agent(self.repo, model)
        self.assertEqual(agent.tool_mode, "text")
        model.script(reply('Writing the chunk.\n<tool name="write_file" path="retry.py">\n%s</tool>'
                           % RETRY.format(placeholder=PLACEHOLDER)),
                     reply("Explain c01 at retry.py:4."))
        out = agent.send("/build retry helper")
        self.assertEqual(out, "Writing the chunk.\n\nExplain c01 at retry.py:4.")
        self.assertEqual(self.read("retry.py"), RETRY.format(placeholder=PLACEHOLDER))
        self.assertTrue(self.state()["locked"])
        second = model.requests[1]
        self.assertIsNone(second["tools"])
        self.assertIn("How to call tools", second["system"])
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant", "user"])
        self.assertTrue(second["messages"][2]["content"].startswith("[result of write_file]"))
        self.assertIn("LOCKED on c01", second["messages"][2]["content"])

    def test_command_provider_runs_any_cli_model(self):
        script = os.path.join(self.repo, "fake_model.py")
        with open(script, "w") as f:
            f.write(
                "import sys\n"
                "t = sys.stdin.read()\n"
                "if '[result of write_file]' in t:\n"
                "    print('Explain c01 at retry.py:4.')\n"
                "else:\n"
                "    print('<tool name=\"write_file\" path=\"retry.py\">')\n"
                "    print(%r, end='')\n"
                "    print('</tool>')\n" % RETRY.format(placeholder=PLACEHOLDER))
        provider = providers.make_provider("command", command="%s %s" % (sys.executable, script))
        agent = harness.Agent(self.repo, provider)
        out = agent.send("/build retry helper")
        self.assertEqual(out, "Explain c01 at retry.py:4.")
        self.assertTrue(self.state()["locked"])


class TestEvalRunner(unittest.TestCase):
    def test_every_case_loads(self):
        import run_evals
        cases = [run_evals.load_case(os.path.join(run_evals.EVALS, d)) for d in sorted(os.listdir(run_evals.EVALS))
                 if os.path.isfile(os.path.join(run_evals.EVALS, d, "prompt.md"))]
        self.assertEqual(len(cases), 9)
        for case in cases:
            self.assertTrue(case["prompt"], case["name"])
            self.assertEqual(case["scaffold"], "scaffold.sh")
            self.assertTrue(case["graders"])
            for g in case["graders"]:
                self.assertIn(g["type"], ("regex", "file_exists", "tool_used", "llm"))
        wrong = next(c for c in cases if c["name"] == "grade-wrong-claim")
        no_approve = next(g for g in wrong["graders"] if g["name"] == "no-approve")
        self.assertEqual((no_approve["tool"], no_approve["min"], no_approve["max"]), ("Bash", 0, 0))
        self.assertEqual(no_approve["input_match"], r"gate\.py\S*\s+approve")
        locked = next(g for g in wrong["graders"] if g["name"] == "still-locked")
        self.assertEqual(locked["target"], {"source": "file", "path": ".comments-by-humans/state.json"})

    def test_agent_calls_map_to_claude_code_calls(self):
        import re
        import run_evals
        name, args = run_evals.claude_code_view({"name": "gate", "args": {"command": "approve c01"}}, "/r")
        self.assertEqual(name, "Bash")
        self.assertTrue(re.search(r"gate\.py\S*\s+approve", json.dumps(args)))
        self.assertTrue(run_evals.claude_code_view(
            {"name": "run_command", "args": {"command": "gate followup c01"}}, "/r")[1]["command"].endswith(
            'gate.py" followup c01'))
        self.assertEqual(run_evals.claude_code_view(
            {"name": "write_file", "args": {"path": "a.py", "content": "x"}}, "/r"),
            ("Write", {"file_path": "/r/a.py", "content": "x"}))

    def test_scripted_human_types_plain_prose(self):
        sys.path.insert(0, os.path.join(PLUGIN, "evals", "_lib"))
        import human
        self.assertEqual(human.plain("# # Counts words.\n# EXPLAINED(human) c02\n// it splits\n * on spaces"),
                         "Counts words.\nit splits\non spaces")

    def test_yaml_subset(self):
        import run_evals
        self.assertEqual(run_evals.parse_yaml("a: 1\nb:\n  c: 'x''y'\n  d: [p, \"q\"]\ne: { f: g, h: true }\n"),
                         {"a": 1, "b": {"c": "x'y", "d": ["p", "q"]}, "e": {"f": "g", "h": True}})


# ---------------------------------------------------------------------------
# Wire formats, against mock provider servers


class MockServer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": body})
                data = json.dumps(outer.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestWireFormats(AgentCase):
    def test_anthropic_messages_api(self):
        content = RETRY.format(placeholder=PLACEHOLDER)
        server = MockServer([
            {"model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {"type": "text", "text": "Writing c01."},
                {"type": "tool_use", "id": "toolu_1", "name": "write_file",
                 "input": {"path": "retry.py", "content": content}}]},
            {"model": "claude-opus-5-5", "stop_reason": "end_turn",
             "content": [{"type": "text", "text": "Explain c01."}]}])
        self.addCleanup(server.close)
        provider = providers.AnthropicProvider("claude-opus-5-5", api_key="test-key", base_url=server.url)
        agent = harness.Agent(self.repo, provider)
        out = agent.send("/build retry helper")
        self.assertEqual(out, "Writing c01.\n\nExplain c01.")
        first, second = server.requests
        self.assertEqual(first["path"], "/v1/messages")
        self.assertEqual(first["headers"]["x-api-key"], "test-key")
        self.assertEqual(first["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("anthropic-beta", first["headers"])  # fallbacks only on the Claude API itself
        body = first["body"]
        self.assertEqual((body["model"], body["max_tokens"]), ("claude-opus-5-5", 16000))
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(body["tools"][3]["name"], "write_file")
        self.assertIn("input_schema", body["tools"][3])
        msgs = second["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"][0], {"type": "thinking", "thinking": "", "signature": "sig-1"})
        result = msgs[2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "toolu_1"))
        self.assertIn("LOCKED on c01", result["content"])

    def test_anthropic_fallbacks_and_auth_token(self):
        server = MockServer([{"stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]}])
        self.addCleanup(server.close)
        provider = providers.AnthropicProvider("claude-opus-5-5", auth_token="tok", base_url=server.url,
                                               fallbacks=True, effort=None)
        provider.chat("sys", [{"role": "user", "content": "hello"}], None)
        req = server.requests[0]
        self.assertEqual(req["headers"]["authorization"], "Bearer tok")
        self.assertIn("server-side-fallback-2026-07-01", req["headers"]["anthropic-beta"])
        self.assertEqual(req["body"]["fallbacks"], "default")
        self.assertNotIn("output_config", req["body"])
        on_api = providers.AnthropicProvider("claude-opus-5-5", api_key="k")
        self.assertTrue(on_api.fallbacks)
        self.assertFalse(providers.AnthropicProvider("claude-haiku-4-5", api_key="k").fallbacks)
        self.assertIsNone(providers.AnthropicProvider("claude-haiku-4-5", api_key="k").effort)

    def test_openai_compatible_chat_completions(self):
        content = RETRY.format(placeholder=PLACEHOLDER)
        server = MockServer([
            {"model": "m", "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [
                    {"type": "function", "function": {"name": "write_file",
                                                      "arguments": json.dumps({"path": "retry.py", "content": content})}}]}}]},
            {"model": "m", "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [
                    {"id": "abc", "type": "function", "function": {"name": "gate", "arguments": {"command": "status"}}}]}}]},
            {"model": "m", "choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                                                                            "content": "Explain c01."}}]}])
        self.addCleanup(server.close)
        os.environ["CBH_TEST_KEY"] = "sk-test"
        self.addCleanup(os.environ.pop, "CBH_TEST_KEY", None)
        provider = providers.make_provider("openai-compatible", model="m", base_url=server.url + "/v1",
                                           api_key_env="CBH_TEST_KEY")
        agent = harness.Agent(self.repo, provider)
        out = agent.send("/build retry helper")
        self.assertEqual(out, "Explain c01.")
        first, second, third = server.requests
        self.assertEqual(first["path"], "/v1/chat/completions")
        self.assertEqual(first["headers"]["authorization"], "Bearer sk-test")
        self.assertEqual(first["body"]["messages"][0]["role"], "system")
        self.assertEqual(first["body"]["tools"][0]["type"], "function")
        self.assertIn("parameters", first["body"]["tools"][0]["function"])
        msgs = third["body"]["messages"]
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant", "tool"])
        generated = msgs[2]["tool_calls"][0]["id"]
        self.assertEqual(msgs[3]["tool_call_id"], generated)
        self.assertIn("LOCKED on c01", msgs[3]["content"])
        self.assertEqual(msgs[5]["tool_call_id"], "abc")
        self.assertEqual(json.loads(msgs[4]["tool_calls"][0]["function"]["arguments"]), {"command": "status"})
        self.assertIn("mode: build", msgs[5]["content"])

    def test_presets(self):
        with self.assertRaises(providers.ProviderError):
            providers.make_provider("nope")
        with self.assertRaises(providers.ProviderError):
            providers.make_provider("command")
        with self.assertRaises(providers.ProviderError):
            providers.make_provider("openai-compatible", model="m")
        os.environ.pop("CBH_NO_SUCH_KEY", None)
        with self.assertRaises(providers.ProviderError):
            providers.make_provider("openai", model="m", api_key_env="CBH_NO_SUCH_KEY")
        local = providers.make_provider("ollama", model="qwen")
        self.assertEqual((local.base_url, local.api_key), ("http://localhost:11434/v1", None))
        gemini = providers.PRESETS["gemini"]
        self.assertEqual(gemini["kind"], "openai")


if __name__ == "__main__":
    unittest.main()
