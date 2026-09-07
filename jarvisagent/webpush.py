"""Send Web Push notifications to all subscribed devices (via pywebpush).

The payload is encrypted with keys only the browser holds, so the push relay (Apple/Google)
delivers but can't read it. The outbound POST goes to the subscription's endpoint (for iOS,
`web.push.apple.com`) through the egress proxy — so that host must be on the proxy allowlist.
"""
from __future__ import annotations

import json

from .config import VapidCfg
from .subscriptions import SubscriptionStore


class WebPush:
    def __init__(self, cfg: VapidCfg, subs: SubscriptionStore):
        self._private_key = cfg.private_key_path   # PEM file path
        self._subject = cfg.subject                # e.g. "mailto:you@example.com"
        self._subs = subs

    def send_to_all(self, title: str, body: str) -> int:
        """Push {title, body} to every subscription. Returns how many were delivered."""
        from pywebpush import webpush, WebPushException

        payload = json.dumps({"title": title, "body": body})
        sent = 0
        for sub in self._subs.all():
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=self._private_key,
                    vapid_claims={"sub": self._subject},
                )
                sent += 1
            except WebPushException as e:  # prune dead subscriptions (browser unsubscribed / expired)
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (404, 410):
                    self._subs.remove(sub.get("endpoint", ""))
        return sent
