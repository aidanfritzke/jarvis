#!/usr/bin/env python3
"""comments-by-humans for any model: you explain the code the model writes.

  python3 cbh.py --provider anthropic build "add a retry helper"
  python3 cbh.py --provider openai --model <model> review src/
  python3 cbh.py --provider ollama --model <model>
  python3 cbh.py --provider command --command "llm -m <model>"

With no action, starts an interactive session; type /help there. Settings come from flags,
then CBH_* environment variables, then ~/.config/comments-by-humans/agent.json.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402
import providers  # noqa: E402

SETTINGS = ("provider", "model", "base_url", "api_key_env", "command", "tool_mode", "effort",
            "fallbacks", "max_tokens", "max_steps")
CONFIG = os.path.join(os.path.expanduser("~"), ".config", "comments-by-humans", "agent.json")


def settings(args):
    merged = {}
    if os.path.isfile(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            merged.update({k: v for k, v in json.load(f).items() if k in SETTINGS})
    for key in SETTINGS:
        env = os.environ.get("CBH_" + key.upper())
        if env is not None:
            merged[key] = env
        value = getattr(args, key, None)
        if value is not None:
            merged[key] = value
    if "provider" not in merged:
        if merged.get("command"):
            merged["provider"] = "command"
        elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            merged["provider"] = "anthropic"
        else:
            sys.exit("Choose a model provider with --provider (one of: %s), or set CBH_PROVIDER."
                     % ", ".join(sorted(providers.PRESETS)))
    if isinstance(merged.get("fallbacks"), str):
        merged["fallbacks"] = merged["fallbacks"].lower() in ("1", "true", "yes", "on")
    for key in ("max_tokens", "max_steps"):
        if isinstance(merged.get(key), str):
            merged[key] = int(merged[key])
    return merged


def provider_from(cfg):
    return providers.make_provider(
        cfg["provider"], model=cfg.get("model"), base_url=cfg.get("base_url"),
        api_key_env=cfg.get("api_key_env"), command=cfg.get("command"),
        effort=cfg.get("effort", "default"), fallbacks=cfg.get("fallbacks"),
        max_tokens=cfg.get("max_tokens", 16000))


def add_provider_args(ap):
    ap.add_argument("--provider", help="one of: %s" % ", ".join(sorted(providers.PRESETS)))
    ap.add_argument("--model")
    ap.add_argument("--base-url", dest="base_url")
    ap.add_argument("--api-key-env", dest="api_key_env", help="environment variable that holds the API key")
    ap.add_argument("--command", help="for --provider command: a command that reads a transcript on "
                                      "stdin and prints the model's reply")
    ap.add_argument("--tool-mode", dest="tool_mode", choices=("native", "text"),
                    help="native tool calling, or the text protocol for models without it")
    ap.add_argument("--effort", help="Anthropic effort level, or none")
    ap.add_argument("--no-fallbacks", dest="fallbacks", action="store_const", const=False,
                    help="do not request Anthropic server-side refusal fallback")
    ap.add_argument("--max-tokens", dest="max_tokens", type=int)
    ap.add_argument("--max-steps", dest="max_steps", type=int, help="model calls allowed per turn")


def build_agent(args, cfg, on_event=None, approve=None):
    provider = provider_from(cfg)
    return harness.Agent(args.project, provider, tool_mode=cfg.get("tool_mode"),
                         approve_command=approve, max_steps=cfg.get("max_steps", 40), on_event=on_event)


def show_event(kind, data):
    if kind != "tool":
        return
    args = data["args"]
    target = args.get("path") or args.get("command") or args.get("pattern") or ""
    first = (data["result"].splitlines() or [""])[0][:110]
    sys.stderr.write("  · %s %s → %s\n" % (data["name"], str(target)[:60], first))


def ask_to_run(command):
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("  run `%s`? [y/N] " % command)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def open_editor(agent):
    state = harness.engine.api_state(agent.root) or {}
    cid = state.get("current")
    ch = harness.engine.get_chunk(state, cid) if cid else None
    if not ch:
        return "No chunk is waiting for an explanation."
    rv = state.get("review") or {}
    if cid.startswith("r") and not rv.get("inline"):
        path = os.path.join(agent.root, rv["report"])
        found = harness.engine.report_block(open(path, encoding="utf-8").read(), cid)
        line = found[1] if found else 1
    else:
        path, line = os.path.join(agent.root, ch["file"]), ch["lines"][0]
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    argv = shlex.split(editor)
    if os.path.basename(argv[0]) in ("code", "cursor", "subl", "zed"):
        argv += ["-g" if os.path.basename(argv[0]) != "zed" else "", "%s:%d" % (path, line)]
        argv = [a for a in argv if a]
    else:
        argv += ["+%d" % line, path]
    subprocess.call(argv)
    return "Saved? Send any message and the gate will check %s." % cid


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=("build", "review", "status"))
    ap.add_argument("rest", nargs=argparse.REMAINDER, help="the task, or the review target")
    add_provider_args(ap)
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--yes", action="store_true", help="run shell commands without asking")
    ap.add_argument("--transcript", help="append every event to this JSONL file")
    args = ap.parse_args()
    cfg = settings(args)
    approve = (lambda command: True) if args.yes else ask_to_run
    try:
        agent = build_agent(args, cfg, on_event=show_event, approve=approve)
    except (providers.ProviderError, ValueError) as e:
        sys.exit("cbh: %s" % e)
    print("comments-by-humans agent · %s · %s tools · project %s"
          % (agent.provider.label, agent.tool_mode, agent.root))

    def turn(text):
        try:
            out = agent.send(text)
        except providers.ProviderError as e:
            out = "[model error] %s" % e
        if out:
            print("\n" + out + "\n")
        if args.transcript:
            with open(args.transcript, "a", encoding="utf-8") as f:
                for event in agent.events:
                    f.write(json.dumps(event) + "\n")
            agent.events.clear()

    if args.action == "status":
        print(agent.send("/status"))
        return
    if args.action:
        turn("/%s %s" % (args.action, " ".join(args.rest)))
    else:
        print(harness.HELP)
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line.strip():
            continue
        if line.strip() in ("/quit", "/exit"):
            break
        if line.strip() == "/edit":
            print(open_editor(agent))
            continue
        turn(line)


if __name__ == "__main__":
    main()
