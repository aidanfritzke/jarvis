#!/usr/bin/env python3
"""Put an eval workspace into a real gate state by driving the hooks, never by writing state.

usage: scaffold.py --fixture DIR [--depth strict|normal|light] [--no-start]

Every file in DIR except comment.txt is "written by Claude": copied into the
workspace and passed through the PostToolUse hook, which registers its
EXPLAIN(human) placeholder and locks the gate. If DIR/comment.txt exists, the
"human" then types it into the c01 placeholder.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from human import PLUGIN, fill_comment  # noqa: E402

GATE = os.path.join(PLUGIN, "scripts", "gate.py")


def hook(name, payload, repo):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=repo)
    payload.setdefault("cwd", repo)
    out = subprocess.run([sys.executable, GATE, "hook", name], input=json.dumps(payload),
                         capture_output=True, text=True, env=env)
    if out.returncode not in (0,):
        sys.exit("scaffold: hook %s failed: %s" % (name, out.stderr))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--depth", default="strict")
    ap.add_argument("--no-start", action="store_true", help="leave the gate off")
    args = ap.parse_args()
    repo = os.getcwd()
    subprocess.run(["git", "init", "-q", repo], check=False)
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write(".comments-by-humans/\n")
    files = []
    for d, _, names in os.walk(args.fixture):
        for n in sorted(names):
            src = os.path.join(d, n)
            rel = os.path.relpath(src, args.fixture)
            if rel != "comment.txt":
                files.append(rel)
    if args.no_start:
        for rel in files:
            os.makedirs(os.path.dirname(os.path.join(repo, rel)) or repo, exist_ok=True)
            shutil.copy(os.path.join(args.fixture, rel), os.path.join(repo, rel))
        return
    os.makedirs(os.path.join(repo, ".comments-by-humans"), exist_ok=True)
    with open(os.path.join(repo, ".comments-by-humans", "config.json"), "w") as f:
        json.dump({"depth": args.depth}, f)
    hook("prompt-expansion", {"command_name": "comments-by-humans:build", "command_args": "",
                              "prompt_id": "scaffold"}, repo)
    for rel in files:
        dest = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(dest) or repo, exist_ok=True)
        shutil.copy(os.path.join(args.fixture, rel), dest)
        hook("post-write", {"tool_name": "Write", "tool_input": {"file_path": dest, "content": ""}}, repo)
    comment = os.path.join(args.fixture, "comment.txt")
    if os.path.isfile(comment):
        with open(comment) as f:
            text = f.read()
        state = json.load(open(os.path.join(repo, ".comments-by-humans", "state.json")))
        ch = state["chunks"]["c01"]
        fill_comment(repo, ch["file"], "c01", text)


if __name__ == "__main__":
    main()
