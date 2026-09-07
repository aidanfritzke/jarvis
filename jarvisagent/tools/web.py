"""Web search via the internal SearXNG instance.

The agent never reaches the open web directly — it queries SearXNG on the internal network,
and SearXNG (which holds no secrets and never sees notes/credentials) does the fetching. This
keeps the privileged agent's egress limited to nothing atm only.
"""
from __future__ import annotations

import httpx


class WebSearch:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, k: int = 5) -> list[dict]:
        # NO_PROXY includes `searxng`, so this internal call bypasses the egress proxy.
        resp = httpx.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json"},
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        hits = []
        for r in data.get("results", [])[:k]:
            hits.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": (r.get("content") or "")[:500],
                }
            )
        return hits
