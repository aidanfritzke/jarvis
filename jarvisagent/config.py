"""Configuration loading for Jarvis.

Reads YAML config (path from $JARVIS_CONFIG, else config.yaml in the project root),
expands ~ in paths, and allows a couple of env overrides.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

import yaml

CONFIG_ENV = "JARVIS_CONFIG"


@dataclass
class OllamaCfg:
    base_url: str
    api_key: str
    chat_model: str
    embed_model: str


@dataclass
class AgentCfg:
    system_prompt: str


@dataclass
class MemoryCfg:
    db_path: str


@dataclass
class ApiCfg:
    host: str
    port: int


@dataclass
class NotesCfg:
    path: str            # vault root (mounted at /srv/notes)
    inbox_subdir: str    # where quick-capture notes land (e.g. "inbox")
    todo_subdir: str     # where to-do items land (e.g. "to-do")
    db_path: str         # SQLite FTS index location
    top_k: int


@dataclass
class SearxngCfg:
    enabled: bool
    base_url: str        # internal: http://searxng:8080


@dataclass
class AuditCfg:
    log_path: str


@dataclass
class VapidCfg:
    enabled: bool
    public_key: str        # Application Server Key (base64url) sent to the browser
    private_key_path: str  # PEM file (in secrets/) used to sign pushes
    subject: str           # "mailto:you@example.com"


@dataclass
class Config:
    ollama: OllamaCfg
    agent: AgentCfg
    memory: MemoryCfg
    api: ApiCfg
    notes: NotesCfg
    searxng: SearxngCfg
    audit: AuditCfg
    vapid: VapidCfg
    notify_token: str      # shared secret for the /notify webhook (any script that pings your devices)


def _expand(p: str) -> str:
    return str(pathlib.Path(os.path.expanduser(p)))


def _default_config_path() -> str:
    root = pathlib.Path(__file__).resolve().parent.parent
    return str(root / "config.yaml")


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get(CONFIG_ENV) or _default_config_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config not found at {path}. Copy config.example.yaml to config.yaml and edit it, "
            f"or set ${CONFIG_ENV}."
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    o = raw["ollama"]
    ollama = OllamaCfg(
        base_url=os.environ.get("JARVIS_OLLAMA_BASE_URL", o["base_url"]),
        api_key=o.get("api_key", "ollama"),
        chat_model=o["chat_model"],
        embed_model=o.get("embed_model", "nomic-embed-text"),
    )
    agent = AgentCfg(system_prompt=raw["agent"]["system_prompt"])
    memory = MemoryCfg(db_path=_expand(raw["memory"]["db_path"]))
    api = ApiCfg(host=raw["api"]["host"], port=int(raw["api"]["port"]))

    n = raw.get("notes", {})
    notes = NotesCfg(
        path=_expand(n.get("path", "/srv/notes")),
        inbox_subdir=n.get("inbox_subdir", "inbox"),
        todo_subdir=n.get("todo_subdir", "to-do"),
        db_path=_expand(n.get("db_path", "/data/notes_fts.db")),
        top_k=int(n.get("top_k", 5)),
    )

    s = raw.get("searxng", {})
    searxng = SearxngCfg(
        enabled=bool(s.get("enabled", True)),
        base_url=s.get("base_url", "http://searxng:8080"),
    )

    a = raw.get("audit", {})
    audit = AuditCfg(log_path=_expand(a.get("log_path", "/data/audit.log")))

    v = raw.get("vapid", {})
    vapid = VapidCfg(
        enabled=bool(v.get("enabled", False)),
        public_key=v.get("public_key", ""),
        private_key_path=_expand(v.get("private_key_path", "/app/secrets/vapid_private.pem")),
        subject=v.get("subject", "mailto:jarvis@example.com"),
    )

    notify_token = str(raw.get("notify", {}).get("token", ""))

    return Config(
        ollama=ollama, agent=agent, memory=memory, api=api,
        notes=notes, searxng=searxng, audit=audit, vapid=vapid,
        notify_token=notify_token,
    )
