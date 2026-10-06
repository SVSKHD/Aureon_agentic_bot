"""Discord posting (webhook) with symbol-aware de-duplication. The bot (bot.py) adds slash commands on top.

Delivery semantics: the dedupe key is committed ONLY after a successful delivery, so a failed webhook post is
retried on the next poll instead of being suppressed forever. With no webhook configured, console output IS the
delivery (key committed). A key is also committed after MAX_ATTEMPTS failures so a dead webhook can't loop forever."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MAX_ATTEMPTS = 20          # ~3 minutes of 10-second polls


class Notifier:
    def __init__(self, webhook: str | None):
        self.webhook = webhook
        self.sent: set[str] = set()          # delivered keys
        self.attempts: dict[str, int] = {}   # failed attempts per key
        self.recent: list[str] = []
        self.posts = 0; self.failures = 0

    def send(self, key: str, title: str, lines: list[str], png: str | None = None) -> bool:
        """Returns True when delivered (or printed, in console mode). Never raises."""
        try:
            if key in self.sent:
                return False
            body = f"**{title}**\n" + "\n".join(lines)
            stamp = datetime.now(IST).strftime("%H:%M:%S")
            if self.attempts.get(key, 0) == 0:                 # print once, even if Discord needs retries
                print(f"\n[{stamp}] {title}\n  " + "\n  ".join(lines), flush=True)
                self.recent = (self.recent + [f"{stamp} {title}"])[-50:]
            ok = self.raw(body, png)
            if ok:
                self.sent.add(key); self.attempts.pop(key, None)
                if len(self.sent) > 20000:
                    self.sent = set(list(self.sent)[-5000:])
                return True
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if self.attempts[key] >= MAX_ATTEMPTS:             # give up, but say so in the log
                print(f"  (giving up on Discord delivery of {key} after {MAX_ATTEMPTS} attempts)")
                self.sent.add(key); self.attempts.pop(key, None)
            return False
        except Exception as e:                                   # Discord must never break the runtime
            print("  (notify failed:", e, ")")
            return False

    def raw(self, body: str, png: str | None = None) -> bool:
        """Post to the webhook. True on success. No webhook = console mode = delivered."""
        if not self.webhook:
            return True
        try:
            import requests  # type: ignore
            if png and os.path.exists(png):
                with open(png, "rb") as f:
                    r = requests.post(self.webhook, data={"content": body[:1900]}, files={"file": (os.path.basename(png), f, "image/png")}, timeout=15)
            else:
                r = requests.post(self.webhook, json={"content": body[:1900]}, timeout=15)
            ok = 200 <= r.status_code < 300
            self.posts += ok; self.failures += (not ok)
            if not ok:
                print(f"  (discord post failed: HTTP {r.status_code} {r.text[:120]})")
            return ok
        except Exception as e:
            self.failures += 1
            print("  (discord post failed:", e, ")")
            return False
