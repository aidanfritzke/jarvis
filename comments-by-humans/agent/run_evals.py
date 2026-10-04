#!/usr/bin/env python3
"""Run the plugin's eval cases (../evals) against the model-agnostic agent, with any provider.

The cases are the same ones `claude plugin eval` runs for the Claude Code plugin: a scaffold
script puts a scratch repository into a real gate state, the prompt is sent as the human's
message, and graders check the result. Supported graders: regex, file_exists, tool_used,
tool_order and llm. Agent tool calls are mapped to their Claude Code equivalents (write_file to
Write, gate to a Bash call of gate.py, and so on), so the graders work unchanged.

  python3 run_evals.py --provider openai --model <model>
  python3 run_evals.py --provider command --command "llm -m <model>" --runs 3
  python3 run_evals.py --case "grade-*" --judge-provider anthropic --out results.json
"""

import argparse
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
EVALS = os.path.join(PLUGIN, "evals")
sys.path.insert(0, HERE)
import cbh  # noqa: E402
import harness  # noqa: E402
import providers  # noqa: E402

# ---------------------------------------------------------------------------
# The small YAML subset the case files use


def _split_top(text):
    parts, depth, quote, cur = [], 0, None, ""
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def scalar(value):
    v = value.strip()
    if not v:
        return ""
    if v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if v[0] == '"' and v[-1] == '"':
        return json.loads(v)
    if v[0] == "[" and v[-1] == "]":
        return [scalar(x) for x in _split_top(v[1:-1])]
    if v[0] == "{" and v[-1] == "}":
        out = {}
        for item in _split_top(v[1:-1]):
            k, _, val = item.partition(":")
            out[k.strip()] = scalar(val)
        return out
    if v in ("true", "false"):
        return v == "true"
    if v in ("null", "~"):
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def parse_yaml(text):
    root = {}
    stack = [(-1, root)]
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.strip().partition(":")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            parent[key.strip()] = {}
            stack.append((indent, parent[key.strip()]))
        else:
            parent[key.strip()] = scalar(value)
    return root


def frontmatter(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    return (parse_yaml(m.group(1)), m.group(2).strip()) if m else ({}, text.strip())


def load_case(case_dir):
    meta, prompt = frontmatter(os.path.join(case_dir, "prompt.md"))
    case = {}
    if os.path.isfile(os.path.join(case_dir, "case.yaml")):
        with open(os.path.join(case_dir, "case.yaml"), encoding="utf-8") as f:
            case = parse_yaml(f.read())
    graders = []
    for path in sorted(glob.glob(os.path.join(case_dir, "graders", "*.md"))):
        g, body = frontmatter(path)
        g["name"] = os.path.splitext(os.path.basename(path))[0]
        if g.get("type") == "llm" and not g.get("criteria"):
            g["criteria"] = body
        graders.append(g)
    return {"name": case.get("name") or os.path.basename(case_dir), "dir": case_dir, "prompt": prompt,
            "max_turns": meta.get("max_turns", 15), "scaffold": (case.get("context") or {}).get("scaffold_script"),
            "graders": graders}


# ---------------------------------------------------------------------------
# Running a case


def claude_code_view(event, root):
    """An agent tool call as the equivalent Claude Code tool call, for tool_used graders."""
    name, a = event["name"], event["args"]
    full = lambda p: os.path.join(root, p or ".")  # noqa: E731
    if name == "write_file":
        return "Write", {"file_path": full(a.get("path")), "content": a.get("content")}
    if name == "edit_file":
        return "Edit", {"file_path": full(a.get("path")), "old_string": a.get("old_string"),
                        "new_string": a.get("new_string")}
    if name == "gate" or (name == "run_command" and re.match(r"^\s*gate(\s|$)", a.get("command", ""))):
        rest = a.get("command", "")
        rest = rest.strip()[4:].strip() if name == "run_command" else rest
        return "Bash", {"command": 'python3 "%s" %s' % (harness.engine.SCRIPT, rest)}
    if name == "run_command":
        return "Bash", {"command": a.get("command")}
    if name == "read_file":
        return "Read", {"file_path": full(a.get("path"))}
    if name == "search":
        return "Grep", {"pattern": a.get("pattern"), "path": a.get("path")}
    if name == "list_files":
        return "Glob", {"pattern": a.get("pattern"), "path": a.get("path")}
    return name, a


def files_in(root):
    out = set()
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        out.update(os.path.relpath(os.path.join(d, n), root) for n in names)
    return out


def run_once(case, cfg, allow_command, judge_cfg, votes):
    work = os.path.realpath(tempfile.mkdtemp(prefix="cbh-eval-"))
    try:
        if case["scaffold"]:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
            out = subprocess.run(["bash", os.path.join(case["dir"], case["scaffold"])], cwd=work, env=env,
                                 capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                return {"error": "scaffold failed: " + out.stderr[-500:], "score": 0.0, "graders": []}
        before = files_in(work)
        agent = harness.Agent(work, cbh.provider_from(cfg), tool_mode=cfg.get("tool_mode"),
                              approve_command=lambda c: bool(re.match(allow_command, c)),
                              max_steps=int(case["max_turns"]))
        prompt = case["prompt"].replace("/comments-by-humans:", "/")
        started = time.time()
        try:
            final = agent.send(prompt)
            error = None
        except providers.ProviderError as e:
            final, error = "", str(e)
        run = {"final": final, "events": agent.events, "root": work,
               "created": sorted(files_in(work) - before),
               "calls": [claude_code_view(e["data"], work) for e in agent.events if e["kind"] == "tool"]}
        graded = [grade(g, run, judge_cfg, votes) for g in case["graders"]]
        scored = [g for g in graded if g["scored"]]
        total = sum(g["weight"] for g in scored) or 1
        score = sum(g["weight"] for g in scored if g["passed"]) / total
        return {"score": score, "error": error, "graders": graded, "final": final,
                "seconds": round(time.time() - started)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Graders


def subject_of(target, run):
    target = target or "last_message"
    if target == "last_message":
        return run["final"]
    if target == "trace":
        return "\n".join(json.dumps(e) for e in run["events"])
    if target == "files":
        return "\n".join(run["created"])
    if isinstance(target, dict) and target.get("source") == "file":
        path = os.path.join(run["root"], target["path"])
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    raise ValueError("unsupported grader target %r" % (target,))


def judge(criteria, subject, judge_cfg, votes):
    prompt = ("You are grading one reply from an AI coding assistant against a rubric.\n\nRubric:\n%s\n\n"
              "The assistant's reply:\n<<<\n%s\n>>>\n\nDoes the reply PASS or FAIL the rubric? Answer with "
              "one word on the first line, PASS or FAIL, then one sentence of reason." % (criteria, subject))
    provider = cbh.provider_from(judge_cfg)
    verdicts = []
    for _ in range(votes):
        text = provider.chat("You are a strict, fair grader.", [{"role": "user", "content": prompt}], None).text
        word = re.search(r"\b(PASS|FAIL)\b", text.upper())
        verdicts.append(word.group(1) if word else "FAIL")
    return verdicts.count("PASS") * 2 > len(verdicts), verdicts


def grade(g, run, judge_cfg, votes):
    kind = g.get("type")
    result = {"name": g["name"], "type": kind, "weight": float(g.get("weight", 1)), "scored": True}
    try:
        if kind == "regex":
            subject = subject_of(g.get("target"), run)
            flags = re.I if "i" in str(g.get("flags", "")) else 0
            count = len(re.findall(g["pattern"], subject, flags)) if subject is not None else 0
            match = str(g.get("match", "contains"))
            if match == "not_contains":
                passed = count == 0
            elif match.startswith("count:"):
                passed = count == int(match.split(":", 1)[1])
            else:
                passed = count > 0
            result.update(passed=passed, explanation="%d match(es)" % count)
        elif kind == "file_exists":
            hits = [p for p in run["created"] if fnmatch.fnmatch(p, g["path"])]
            want = g.get("exists", True)
            result.update(passed=bool(hits) == bool(want), explanation="created: %s" % (hits or "none"))
        elif kind == "tool_used":
            rx = re.compile(g["input_match"]) if g.get("input_match") else None
            n = sum(1 for name, a in run["calls"] if name == g["tool"] and (not rx or rx.search(json.dumps(a))))
            lo, hi = int(g.get("min", 1)), g.get("max")
            result.update(passed=n >= lo and (hi is None or n <= int(hi)),
                          explanation="%s called %dx" % (g["tool"], n))
        elif kind == "tool_order":
            def first(spec):
                spec = spec if isinstance(spec, dict) else {"tool": spec}
                rx = re.compile(spec["input_match"]) if spec.get("input_match") else None
                for k, (name, a) in enumerate(run["calls"]):
                    if name == spec["tool"] and (not rx or rx.search(json.dumps(a))):
                        return k
                return None
            b, a = first(g["before"]), first(g["after"])
            result.update(passed=b is not None and a is not None and b < a, explanation="%s before %s" % (b, a))
        elif kind == "llm":
            subject = subject_of(g.get("focus"), run)
            passed, verdicts = judge(g["criteria"], subject or "", judge_cfg, votes)
            result.update(passed=passed, explanation="judge votes: " + " ".join(verdicts))
        else:
            result.update(passed=False, scored=False, explanation="grader type %r is not supported" % kind)
    except Exception as e:
        result.update(passed=False, explanation="grader error: %s: %s" % (type(e).__name__, e))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cbh.add_provider_args(ap)
    ap.add_argument("--case", default="*", help="glob over case names")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("-j", "--concurrency", type=int, default=1)
    ap.add_argument("--allow-command", default=r"^python3?\s", help="regex of shell commands the agent may run")
    ap.add_argument("--judge-provider")
    ap.add_argument("--judge-model")
    ap.add_argument("--judge-command")
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--out", help="write full results as JSON")
    args = ap.parse_args()
    cfg = cbh.settings(args)
    judge_cfg = dict(cfg)
    if args.judge_provider:
        judge_cfg = {"provider": args.judge_provider}
    if args.judge_model:
        judge_cfg["model"] = args.judge_model
    if args.judge_command:
        judge_cfg.update(provider="command", command=args.judge_command)
    cases = [load_case(d) for d in sorted(glob.glob(os.path.join(EVALS, "*")))
             if os.path.isfile(os.path.join(d, "prompt.md"))]
    cases = [c for c in cases if fnmatch.fnmatch(c["name"], args.case)]
    if not cases:
        sys.exit("no eval cases match %r" % args.case)
    jobs = [(c, k) for c in cases for k in range(args.runs)]
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        outcomes = list(pool.map(lambda job: run_once(job[0], cfg, args.allow_command, judge_cfg, args.votes),
                                 jobs))
    report = []
    for case in cases:
        runs = [o for (c, _), o in zip(jobs, outcomes) if c is case]
        score = sum(r["score"] for r in runs) / len(runs)
        report.append({"case": case["name"], "score": score, "runs": runs})
        print("%-28s %.2f" % (case["name"], score))
        for r in runs:
            if r.get("error"):
                print("    error: %s" % r["error"][:200])
            for g in r["graders"]:
                if not g["passed"] and g["scored"]:
                    print("    FAIL %s: %s" % (g["name"], g["explanation"]))
    overall = sum(r["score"] for r in report) / len(report)
    print("overall %.2f · %d of %d cases fully passing · provider %s"
          % (overall, sum(1 for r in report if r["score"] == 1.0), len(report), cfg.get("provider")))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"provider": {k: v for k, v in cfg.items() if k != "api_key"}, "overall": overall,
                       "cases": report}, f, indent=2)
    sys.exit(0 if overall == 1.0 else 1)


if __name__ == "__main__":
    main()
