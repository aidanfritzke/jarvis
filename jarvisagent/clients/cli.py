"""CLI client — talks to the Jarvis API over HTTP.

One-shot:     python -m jarvisagent.clients.cli "What can you do?"
Interactive:  python -m jarvisagent.clients.cli           (Ctrl-C / 'exit' to quit)

If the agent queues a consequential action (send email / delete event), type 'confirm'
to approve it (this calls POST /confirm — the human-in-the-loop gate).
"""
from __future__ import annotations

import argparse
import sys

import httpx

from ..config import load_config


def _chat(base_url: str, message: str, session_id: str) -> dict:
    resp = httpx.post(
        f"{base_url}/chat",
        json={"message": message, "session_id": session_id},
        timeout=120.0,  # small local models can be slow to first token
    )
    resp.raise_for_status()
    return resp.json()


def _confirm(base_url: str, session_id: str) -> dict:
    resp = httpx.post(f"{base_url}/confirm", json={"session_id": session_id}, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def _render(data: dict) -> None:
    print(f"jarvis> {data['reply']}")
    if data.get("pending"):
        p = data["pending"]
        print(f"   ⚠ pending [{p['kind']}]: {p['summary']}")
        print("   → type 'confirm' to approve, or anything else to ignore it.")


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Chat with Jarvis.")
    parser.add_argument("message", nargs="*", help="message to send (omit for interactive mode)")
    parser.add_argument("--session", default="cli", help="conversation session id")
    parser.add_argument("--url", default=f"http://127.0.0.1:{cfg.api.port}", help="Jarvis API base URL")
    args = parser.parse_args()

    if args.message:
        _render(_chat(args.url, " ".join(args.message), args.session))
        return

    print("Jarvis CLI — type 'exit' to quit.")
    try:
        while True:
            msg = input("you> ").strip()
            if msg.lower() in {"exit", "quit"}:
                break
            if not msg:
                continue
            if msg.lower() == "confirm":
                print(f"jarvis> {_confirm(args.url, args.session)}")
                continue
            _render(_chat(args.url, msg, args.session))
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
