#!/usr/bin/env python3
"""Drive comments-by-humans headless with a scripted learner profile.

The driver plays the human's editor: it starts a build (or review) session in a
repository, and whenever the gate is locked it asks a learner model (playing a
profile from profiles.json) to write or revise the explanation, writes that
text into the placeholder (or the review report), and sends the learner's chat
reply. It never edits the gate's state. At the end it checks the run against
the spec:

* every chunk passed, tagged EXPLAINED(human) (in the source, or in the report)
* each explanation is exactly the learner's text (Claude wrote none)
* profiles that start weak or wrong needed more than one attempt per chunk
* the log has one passed line per chunk with its attempt count
* review mode: the review finished and the report has its summary

Usage:
  python3 drive.py --profile weak-first --task "..." --expect-chunks 5 [--out result.json]
  python3 drive.py --profile diligent --review src/ --workdir path/to/repo [--out result.json]

Needs the `claude` CLI, logged in. Each run costs real model calls.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
import comments  # noqa: E402
import gate  # noqa: E402
import human  # noqa: E402

# Variables that bind a child `claude` to the session that launched it (when the
# driver itself runs inside Claude Code). The child must get its own session.
SCRUB = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_SYNC_SESSION_REFS",
         "CLAUDE_CODE_TEE_SDK_STDOUT", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
         "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2", "CLAUDECODE",
         "CLAUDE_PROJECT_DIR")


def child_env():
    env = dict(os.environ)
    for key in SCRUB:
        env.pop(key, None)
    return env


def claude(args, prompt, cwd, timeout=900):
    """Run one headless turn. Returns the result event plus every assistant text block."""
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"] + args
    out = subprocess.run(cmd, cwd=cwd, env=child_env(), capture_output=True, text=True,
                         timeout=timeout, stdin=subprocess.DEVNULL)
    result, texts, tools = None, [], []
    for line in out.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    tools.append({"name": block.get("name"), "input": block.get("input")})
        elif event.get("type") == "result":
            result = event
    if result is None:
        raise RuntimeError("claude failed (exit %d): %s %s" % (out.returncode, out.stdout[-2000:],
                                                             out.stderr[-2000:]))
    result["all_text"] = "\n\n".join(texts) or (result.get("result") or "")
    result["tool_calls"] = tools
    return result


class AgentSession:
    """The model-agnostic agent (../../agent) in-process, with any provider."""

    def __init__(self, repo, args):
        sys.path.insert(0, os.path.join(PLUGIN, "agent"))
        import cbh
        self.id = "agent"
        self.cost = 0.0
        self.tool_calls = []
        cfg = cbh.settings(args)
        self.agent = cbh.build_agent(argparse.Namespace(project=repo), cfg,
                                     approve=lambda command: bool(re.match(r"^python3?\s", command)))

    def send(self, message):
        message = message.replace("/comments-by-humans:", "/")
        before = len(self.agent.events)
        out = self.agent.send(message)
        self.tool_calls.append([e["data"] for e in self.agent.events[before:] if e["kind"] == "tool"])
        return out


def new_session(repo, args):
    return AgentSession(repo, args) if args.frontend == "agent" else Session(repo, args.model)


class Session:
    def __init__(self, repo, model=None):
        self.repo = repo
        self.id = str(uuid.uuid4())
        self.started = False
        self.model = model
        self.cost = 0.0
        self.tool_calls = []

    def send(self, message):
        args = ["--plugin-dir", PLUGIN, "--dangerously-skip-permissions"]
        if self.model:
            args += ["--model", self.model]
        args += ["--resume", self.id] if self.started else ["--session-id", self.id]
        data = claude(args, message, self.repo)
        self.started = True
        self.cost += data.get("total_cost_usd") or 0.0
        self.tool_calls.append(data["tool_calls"])
        return data["all_text"]


def gate_state(repo):
    path = os.path.join(repo, ".comments-by-humans", "state.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def current(state):
    if not state or state["mode"] not in ("build", "review"):
        return None
    cid = state.get("current")
    if not cid:
        return None
    return state["chunks"].get(cid) or (state.get("review") or {}).get("chunks", {}).get(cid)


def chunk_view(repo, ch):
    """The chunk's marker, plus the whole file with the chunk's lines flagged (a human can scroll)."""
    path = os.path.join(repo, ch["file"])
    lines = open(path).read().split("\n")
    lang = comments.language_for(ch["file"])
    regions = comments.chunk_regions(lines, lang)
    marker, s, e = regions[ch["id"]]
    inside = set(range(marker.start, e + 1)) if s is not None else set()
    code = "\n".join("%s%4d  %s" % (">" if k in inside else " ", k + 1, line)
                     for k, line in enumerate(lines))
    return marker, code


def fill_comment(repo, ch, text):
    human.fill_comment(repo, ch["file"], ch["id"], text)


def review_view(repo, state, ch):
    """(current explanation, whole file with the chunk's lines flagged) for a review chunk."""
    rv = state["review"]
    lines = open(os.path.join(repo, ch["file"])).read().split("\n")
    first, last = ch["code_lines"]
    code = "\n".join("%s%4d  %s" % (">" if first <= k + 1 <= last else " ", k + 1, line)
                     for k, line in enumerate(lines))
    if rv["inline"]:
        lang = comments.language_for(ch["file"])
        marker = next(m for m in comments.find_markers(lines, lang) if m.cid == ch["id"])
        return marker.body, code
    found = gate.report_block(open(os.path.join(repo, rv["report"])).read(), ch["id"])
    return (found[0] if found else ""), code


def fill_review(repo, state, ch, text):
    rv = state["review"]
    if rv["inline"]:
        return human.fill_comment(repo, ch["file"], ch["id"], text)
    path = os.path.join(repo, rv["report"])
    report = open(path).read()
    pattern = re.compile(r"(<!-- EXPLAIN(?:ED)?\(human\) %s -->\n).*?(\n?<!-- /EXPLAIN %s -->)"
                         % (ch["id"], ch["id"]), re.S)
    report = pattern.sub(lambda m: m.group(1) + text.strip() + "\n" + m.group(2).lstrip("\n"), report, 1)
    with open(path, "w") as f:
        f.write(report)


BUILD_SETUP = """You work with a coding assistant that writes code one chunk at a time and will not \
continue until you explain each chunk in your own words in a comment above it."""
REVIEW_SETUP = """You are reviewing code you did not write. A review assistant shows you one chunk at a \
time and will not move on until you explain the chunk in your own words in the review report."""

LEARNER_PROMPT = """You are role-playing a human programmer who is learning. {setup} A reviewer \
grades your comment on four criteria: What (what \
it does, not line by line), Why (why it exists or why this approach), Connections (inputs, outputs or \
side effects, and what depends on it) and Catch (one non-obvious edge case, failure mode or tradeoff).

Your profile: {profile}

The file, with chunk {cid}'s lines marked ">" (attempt {attempt} coming up; you have already \
submitted {submitted} version(s)). You explain only the marked chunk, but you may read the rest:
```
{code}
```

Your current comment text: {comment}

The assistant's latest message to you:
<<<
{message}
>>>

Decide what you do next, following your profile. Write in plain first-person programmer English, \
without the words What/Why/Connections/Catch as labels. Respond with JSON only, no code fence:
{{"comment": <the full new comment text, or null to leave the comment unchanged>, \
"reply": <the chat message you send to the assistant>}}

If the assistant asked you a follow-up question about the code in chat, answer it in "reply" and \
usually leave the comment unchanged. If it asked a question about your comment, revise the comment. \
You cannot run commands or open other files; if asked to check something, reason it out from the code \
above and say what you concluded."""


def learner(profile, ch, code, comment, message, submitted, model, setup=BUILD_SETUP, command=None):
    prompt = LEARNER_PROMPT.format(setup=setup, profile=profile, cid=ch["id"], attempt=submitted + 1,
                                   submitted=submitted, code=code,
                                   comment=json.dumps(comment or ""), message=message.strip())
    last_error = None
    for _ in range(3):
        if command:  # any command-line model: prompt on stdin, reply on stdout
            out = subprocess.run(command, shell=True, input=prompt, capture_output=True, text=True, timeout=300)
            text = out.stdout.strip()
        else:
            with tempfile.TemporaryDirectory() as scratch:
                data = claude(["--tools", "", "--model", model], prompt, scratch, timeout=300)
            text = (data.get("result") or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        m = re.search(r"\{.*\}", text, re.S)
        try:
            result = json.loads(m.group(0) if m else text)
            return result.get("comment"), result.get("reply") or "Done."
        except ValueError as e:
            last_error = e
            prompt += "\n\nYour previous answer was not valid JSON. Answer with the JSON object only."
    raise RuntimeError("the learner model did not return JSON three times: %s" % last_error)


def prepare(args):
    repo = args.workdir or tempfile.mkdtemp(prefix="cbh-learner-")
    os.makedirs(repo, exist_ok=True)
    if not os.path.isdir(os.path.join(repo, ".git")):
        subprocess.run(["git", "init", "-q", repo], check=True)
    ignore = os.path.join(repo, ".gitignore")
    existing = open(ignore).read() if os.path.isfile(ignore) else ""
    if ".comments-by-humans" not in existing:
        with open(ignore, "a") as f:
            f.write("\n.comments-by-humans/\n")
    return repo


def run_review(args, profile):
    repo = prepare(args)
    if args.depth:
        os.makedirs(os.path.join(repo, ".comments-by-humans"), exist_ok=True)
        with open(os.path.join(repo, ".comments-by-humans", "config.json"), "w") as f:
            json.dump({"depth": args.depth}, f)
    session = new_session(repo, args)
    transcript, written, submissions = [], {}, {}
    first = "/comments-by-humans:review " + args.review
    message = session.send(first)
    transcript.append({"human": first, "claude": message})
    for turn in range(args.max_turns):
        state = gate_state(repo) or {}
        rv = state.get("review")
        if not rv or rv.get("done"):
            break
        cid = state.get("current")
        ch = rv["chunks"].get(cid) if cid else None
        if ch is None or ch["status"] != "pending":
            reply = ("Record the single most important concern you raised as a finding (skip it if you "
                     "raised none), then move on to the next chunk.")
            message = session.send(reply)
            transcript.append({"human": reply, "claude": message})
            print("[turn %d] findings/next: %s" % (turn + 1, message.replace("\n", " ")[:160]), flush=True)
            continue
        body, code = review_view(repo, state, ch)
        comment, reply = learner(profile["instructions"], ch, code, body, message,
                                 submissions.get(cid, 0), args.learner_model, REVIEW_SETUP, args.learner_command)
        if comment and comment.strip() != (body or "").strip():
            fill_review(repo, state, ch, comment)
            written[cid] = comment.strip()
            submissions[cid] = submissions.get(cid, 0) + 1
        message = session.send(reply)
        transcript.append({"chunk": cid, "comment": comment, "human": reply, "claude": message})
        print("[turn %d] %s: %s" % (turn + 1, cid, message.replace("\n", " ")[:160]), flush=True)
    return check_review(repo, profile, args, written, transcript, session)


def check_review(repo, profile, args, written, transcript, session):
    state = gate_state(repo) or {}
    rv = state.get("review") or {"chunks": {}, "order": []}
    failures = []
    if not rv.get("done"):
        failures.append("the review did not finish")
    chunks = [rv["chunks"][c] for c in rv["order"]]
    passed = [c for c in chunks if c["status"] == "passed"]
    if len(passed) != len(chunks):
        failures.append("%d of %d review chunks passed" % (len(passed), len(chunks)))
    report = open(os.path.join(repo, rv["report"])).read() if rv.get("report") else ""
    if "## Summary" not in report:
        failures.append("the report has no summary")
    log_path = os.path.join(repo, ".comments-by-humans", "log.jsonl")
    log = [json.loads(l) for l in open(log_path)] if os.path.isfile(log_path) else []
    logged = {e["chunk"] for e in log if e["event"] == "passed" and e.get("review") == rv.get("id")}
    for c in passed:
        found = gate.report_block(report, c["id"])
        body = found[0] if found else ""
        if not rv.get("inline") and " ".join(body.split()) != " ".join(written.get(c["id"], "").split()):
            failures.append("%s's explanation in the report is not the learner's text" % c["id"])
        if c["id"] not in logged:
            failures.append("%s has no passed line in log.jsonl" % c["id"])
        if profile.get("min_attempts", 1) > c["attempts"]:
            failures.append("%s passed after %d attempt(s); profile %s expects at least %d"
                            % (c["id"], c["attempts"], args.profile, profile["min_attempts"]))
    return {
        "profile": args.profile, "review": args.review, "repo": repo, "session": session.id,
        "cost_usd": round(session.cost, 4), "chunks": len(chunks), "passed": len(passed),
        "order": ["%s %s:%d-%d" % (c["id"], c["file"], c["code_lines"][0], c["code_lines"][1]) for c in chunks],
        "attempts": {c["id"]: c["attempts"] for c in passed},
        "findings": {c["id"]: c["findings"] for c in passed if c["findings"]},
        "report": rv.get("report"), "failures": failures, "transcript": transcript,
    }


def run(args):
    profiles = json.load(open(os.path.join(HERE, "profiles.json")))
    profile = profiles[args.profile]
    if args.review:
        return run_review(args, profile)
    repo = prepare(args)
    if args.depth:
        os.makedirs(os.path.join(repo, ".comments-by-humans"), exist_ok=True)
        with open(os.path.join(repo, ".comments-by-humans", "config.json"), "w") as f:
            json.dump({"depth": args.depth}, f)
    session = new_session(repo, args)
    transcript = []
    written = {}      # chunk id -> the learner's latest comment text
    submissions = {}  # chunk id -> number of comment versions the learner wrote
    idle = 0
    message = session.send("/comments-by-humans:build " + args.task)
    transcript.append({"human": "/comments-by-humans:build " + args.task, "claude": message})
    for turn in range(args.max_turns):
        state = gate_state(repo)
        ch = current(state)
        if ch is None:
            passed = [c for c in (state or {}).get("chunks", {}).values() if c["status"] == "passed"]
            if len(passed) >= args.expect_chunks or idle >= 2:
                break
            idle += 1
            human = "Please continue with the next chunk. If the whole task is finished, say DONE."
            message = session.send(human)
            transcript.append({"human": human, "claude": message})
            if "DONE" in message and current(gate_state(repo)) is None:
                break
            continue
        idle = 0
        marker, code = chunk_view(repo, ch)
        comment, reply = learner(profile["instructions"], ch, code, marker.body, message,
                                 submissions.get(ch["id"], 0), args.learner_model,
                                 command=args.learner_command)
        if comment and comment.strip() != (marker.body or "").strip():
            fill_comment(repo, ch, comment)
            written[ch["id"]] = comment.strip()
            submissions[ch["id"]] = submissions.get(ch["id"], 0) + 1
        message = session.send(reply)
        transcript.append({"chunk": ch["id"], "comment": comment, "human": reply, "claude": message})
        print("[turn %d] %s: %s" % (turn + 1, ch["id"], message.replace("\n", " ")[:160]), flush=True)
    return check(repo, profile, args, written, transcript, session)


def check(repo, profile, args, written, transcript, session):
    state = gate_state(repo) or {"chunks": {}}
    failures = []
    chunks = state["chunks"]
    passed = [c for c in chunks.values() if c["status"] == "passed"]
    if len(passed) < args.expect_chunks:
        failures.append("only %d of %d expected chunks passed" % (len(passed), args.expect_chunks))
    pending = [c["id"] for c in chunks.values() if c["status"] == "pending"]
    if pending:
        failures.append("chunks still pending: %s" % ", ".join(pending))
    log_path = os.path.join(repo, ".comments-by-humans", "log.jsonl")
    log = [json.loads(l) for l in open(log_path)] if os.path.isfile(log_path) else []
    logged = {e["chunk"]: e for e in log if e["event"] == "passed"}
    for c in passed:
        lines = open(os.path.join(repo, c["file"])).read().split("\n")
        lang = comments.language_for(c["file"])
        marker = next((m for m in comments.find_markers(lines, lang) if m.cid == c["id"]), None)
        if not marker or not marker.explained:
            failures.append("%s is not tagged EXPLAINED(human) in %s" % (c["id"], c["file"]))
        elif c["id"] in written and " ".join(marker.body.split()) != " ".join(written[c["id"]].split()):
            failures.append("%s's comment is not the learner's text (Claude edited it?)" % c["id"])
        if c["id"] not in written:
            failures.append("%s passed but the learner never wrote its comment" % c["id"])
        if c["id"] not in logged:
            failures.append("%s has no passed line in log.jsonl" % c["id"])
        if profile.get("min_attempts", 1) > c["attempts"]:
            failures.append("%s passed after %d attempt(s); profile %s expects at least %d"
                            % (c["id"], c["attempts"], args.profile, profile["min_attempts"]))
    result = {
        "profile": args.profile, "task": args.task, "repo": repo, "session": session.id,
        "cost_usd": round(session.cost, 4), "passed": len(passed),
        "attempts": {c["id"]: c["attempts"] for c in passed}, "failures": failures,
        "transcript": transcript,
    }
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--task", help="build mode: what to build")
    mode.add_argument("--review", help="review mode: a path or diff range in --workdir")
    ap.add_argument("--expect-chunks", type=int, default=1)
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--depth", choices=("light", "normal", "strict"))
    ap.add_argument("--learner-model", default="haiku")
    ap.add_argument("--learner-command", help="any command-line model for the learner, instead of claude")
    ap.add_argument("--workdir")
    ap.add_argument("--out")
    ap.add_argument("--frontend", choices=("claude-code", "agent"), default="claude-code",
                    help="drive the Claude Code plugin, or the model-agnostic agent")
    sys.path.insert(0, os.path.join(PLUGIN, "agent"))
    import cbh
    agent_args = ap.add_argument_group("agent frontend (same flags as agent/cbh.py)")
    cbh.add_provider_args(agent_args)
    args = ap.parse_args()
    needs_claude = args.frontend == "claude-code" or not args.learner_command
    if needs_claude and not shutil.which("claude"):
        sys.exit("the claude CLI is not on PATH")
    started = time.time()
    result = run(args)
    result["seconds"] = round(time.time() - started)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "transcript"}, indent=2))
    sys.exit(1 if result["failures"] else 0)


if __name__ == "__main__":
    main()
