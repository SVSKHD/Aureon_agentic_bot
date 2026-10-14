"""v2.7.2 — outside-the-process fallback alarm (Task Scheduler, every 5 min).

    python scripts/deadman_fallback.py            # from the repo root (scripts/aureon_service.ps1 -Action install-fallback)

If logs/heartbeat.txt (written every 30 s by Aureon's health thread) is older than 5 minutes or missing, posts
"🔴 Aureon down" to Telegram once per outage, and "🟢 Aureon back" when it is fresh again.
Needs AUREON_DEADMAN_TELEGRAM (bot token) and AUREON_DEADMAN_TELEGRAM_CHAT (chat id) in the environment or .env.
Standalone on purpose: imports nothing from aureon_mt5, so a broken build cannot silence it."""
from __future__ import annotations

import json
import os
import sys
import time

STALE_S = 300
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str) -> None:
    """Minimal .env reader (KEY=VALUE); existing environment wins."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def telegram_send(token: str, chat: str, text: str, timeout: float = 15.0) -> bool:
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"telegram send failed: {e!r}")
        return False


def check(log_dir: str, send, now: float | None = None, stale_s: float = STALE_S) -> str:
    """One evaluation. Returns 'ok' | 'down' (alert sent) | 'still-down' | 'back' (recovery sent)."""
    now = time.time() if now is None else now
    hb = os.path.join(log_dir, "heartbeat.txt")
    state_path = os.path.join(log_dir, "deadman_fallback.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    try:
        age = now - os.path.getmtime(hb)
        with open(hb, encoding="utf-8") as f:
            last = f.read().strip()
    except OSError:
        age, last = None, "logs/heartbeat.txt missing"
    stale = age is None or age > stale_s
    result = "ok"
    if stale and not state.get("down"):
        mins = "?" if age is None else f"{age / 60:.0f}"
        if send(f"🔴 Aureon down — no heartbeat for {mins} min.\nLast: {last[:300]}"):
            state = {"down": True, "since": now}
            result = "down"
        else:
            result = "still-down"
    elif stale:
        result = "still-down"
    elif state.get("down"):
        send(f"🟢 Aureon back — heartbeat fresh.\n{last[:300]}")
        state = {"down": False}
        result = "back"
    os.makedirs(log_dir, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return result


def main() -> int:
    load_env(os.path.join(ROOT, ".env"))
    token = os.environ.get("AUREON_DEADMAN_TELEGRAM", "")
    chat = os.environ.get("AUREON_DEADMAN_TELEGRAM_CHAT", "")
    if not token or not chat:
        print("AUREON_DEADMAN_TELEGRAM / AUREON_DEADMAN_TELEGRAM_CHAT not set — nothing to do")
        return 0
    res = check(os.path.join(ROOT, "logs"), lambda text: telegram_send(token, chat, text))
    print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
