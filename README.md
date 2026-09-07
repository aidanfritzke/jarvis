# Jarvis — Self-Hosted Agent on the Home Server

A private ai agent that lives on a home server. Runs in Docker, and is reachable over Tailscale only. It can search the web
(via self-hosted SearXNG), send reminders as push notifications to iPhone, read and write notes or todos.
The brain is a local model running on CPU. 

## Architecture (Docker compose on the server)
```
 Phone ┐                          ┌─ jarvis-agent ─ egress-proxy ─▶ googleapis.com ONLY
 Desk  ┘─Tailscale─▶ caddy ─▶ jarvis-agent ─┼─ ollama        (local brain, internal-only)
 (ingress: tailscale IP only)               ├─ searxng       (web egress; no secrets/notes)
                                            └─ /srv/notes     (vault; writes to inbox/ or to todo/)
``'
