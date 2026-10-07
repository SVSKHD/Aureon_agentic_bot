"""Discord webhook delivery as native embed cards, with symbol-aware de-duplication.

Discord is presentation only: delivery failures never propagate into the trading runtime.
Dedupe keys are committed only after a successful delivery. Console-only mode counts as delivered.

Card model
    send(key, title, lines, fields=None, color=None, footer=None, png=None)
      - title  : card title (emoji first)
      - lines  : free text -> description (kept for backwards compatibility)
      - fields : [{"name", "value", "inline"}] -> the structured grid (preferred)
      - color  : explicit accent; otherwise inferred from the title
      - png    : attached and shown as the embed image
    card(...) : same, non-deduped (startup notes, info)
    Helpers build consistent cards for the guardian: signal_fields(), position_fields().
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MAX_ATTEMPTS = 20

# accent colours (decimal RGB)
BLURPLE = 0x5865F2
GREEN = 0x57F287
YELLOW = 0xFEE75C
RED = 0xED4245
BLUE = 0x3498DB
ORANGE = 0xE67E22
GREY = 0x95A5A6

EVENT_COLOURS = {
    "signal_long": GREEN, "signal_short": RED, "protected": BLUE, "cross": BLUE, "secured": GREEN,
    "closed_win": GREEN, "closed_loss": ORANGE, "flip": YELLOW, "news": YELLOW, "error": RED,
    "market": GREY, "info": BLURPLE,
}


def _colour_for(title: str) -> int:
    t = title.upper()
    if any(x in t for x in ("❌", "FAILED", "FAILURE", "ERROR")):
        return RED
    if any(x in t for x in ("⚠", "📰", "🔁", "WARNING")):
        return YELLOW
    if any(x in t for x in ("🔒", "🛡", "✅", "ONLINE", "RESUMED", "▶")):
        return GREEN
    if "🔫" in title:
        return BLUE
    if "⏸" in title or "CLOSED" in t and "MARKET" in t:
        return GREY
    return BLURPLE


def field(name: str, value, inline: bool = True) -> dict:
    return {"name": str(name), "value": str(value), "inline": inline}


# --------------------------------------------------------------------------- house style (matches the Aureon V4 cards)
#   title : SYMBOL · MT5 · EVENT [· SIDE]        e.g.  XAUUSD · MT5 · P PRE-CROSS · SHORT
#   desc  : one short line                        e.g.  place it — I manage it
#   fields: bold names, values below (inline grid)
#   footer: mode · HH:MM IST [· extra]
def title(symbol: str, event: str, side: str | None = None) -> str:
    parts = [symbol.upper(), "MT5", event.upper()] + ([side.upper()] if side else [])
    return " · ".join(parts)


def footer(mode_display: str, *extra: str) -> str:
    return " · ".join([mode_display, f"{datetime.now(IST):%H:%M} IST"] + [e for e in extra if e])


# --------------------------------------------------------------------------- card helpers used by the guardian
def signal_fields(*, side: str, kind: str, price: float, ema_fast: float, ema_slow: float, fast: int, slow: int,
                  stop: float | None, secure_at: float | None, bar_ist: str, session: str = "") -> list[dict]:
    f = [field("Side", f"**{side}**"), field("Setup", kind), field("Bar", f"{bar_ist} IST"),
         field("Price", f"**{price:.2f}**"), field(f"EMA {fast}", f"{ema_fast:.2f}"), field(f"EMA {slow}", f"{ema_slow:.2f}")]
    if stop is not None:
        f.append(field("Stop", f"{stop:.2f} until the lines cross, then EMA {slow}", inline=False))
    if secure_at is not None:
        f.append(field("Secure", f"at {secure_at:.2f} → SL to {secure_at:.2f} · then ride", inline=False))
    if session:
        f.append(field("Session", session))
    return f


def position_fields(*, ticket, direction: str, entry: float, now: float | None = None, points: float | None = None,
                    sl: float | None = None, secured: float | None = None, peak: float | None = None, extra: dict | None = None) -> list[dict]:
    f = [field("Ticket", f"#{ticket}"), field("Side", direction.upper()), field("Entry", f"{entry:.2f}")]
    if now is not None: f.append(field("Now", f"{now:.2f}"))
    if points is not None: f.append(field("P&L", f"**{points:+.2f}**"))
    if sl is not None: f.append(field("SL", f"{sl:.2f}"))
    if secured is not None: f.append(field("Secured", f"+{secured:g}"))
    if peak is not None: f.append(field("Peak", f"+{peak:.2f}"))
    for k, v in (extra or {}).items():
        f.append(field(k, v, inline=False))
    return f


class Notifier:
    def __init__(self, webhook: str | None, app_name: str = "Aureon MT5"):
        self.webhook = webhook
        self.app_name = app_name
        self.sent: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.recent: list[str] = []
        self.posts = 0
        self.failures = 0

    # ------------------------------------------------------------------ embed
    def make_embed(self, title: str, description: str = "", *, fields: list[dict] | None = None, color: int | None = None,
                   footer: str | None = None, timestamp: bool = True, png: str | None = None) -> dict:
        embed: dict = {"title": title[:256], "color": color if color is not None else _colour_for(title)}
        if description:
            embed["description"] = description[:4096]
        clean = []
        for f in fields or []:
            name = str(f.get("name", ""))[:256]; value = str(f.get("value", "—"))[:1024]
            if name and value:
                clean.append({"name": name, "value": value, "inline": bool(f.get("inline", True))})
        if clean:
            embed["fields"] = clean[:25]
        embed["footer"] = {"text": (footer or self.app_name)[:2048]}
        if timestamp:
            embed["timestamp"] = datetime.now(timezone.utc).isoformat()
        if png and os.path.exists(png):
            embed["image"] = {"url": f"attachment://{os.path.basename(png)}"}
        return embed

    @staticmethod
    def _console(title: str, lines: list[str], fields: list[dict] | None) -> str:
        parts = list(lines or [])
        if fields:
            parts.append(" · ".join(f"{f.get('name')}: {f.get('value')}" for f in fields))
        return title + ("\n  " + "\n  ".join(p for p in parts if p) if parts else "")

    # ------------------------------------------------------------------ deduped card
    def send(self, key: str, title: str, lines: list[str] | None = None, png: str | None = None, *,
             fields: list[dict] | None = None, color: int | None = None, footer: str | None = None) -> bool:
        """One deduped card. Returns True when delivered (or printed in console mode). Never raises."""
        try:
            if key in self.sent:
                return False
            lines = [str(x) for x in (lines or []) if str(x).strip()]
            description = "\n".join(lines)
            body = f"**{title}**" + (f"\n{description}" if description else "")
            if fields:
                body += "\n" + " · ".join(f"{f.get('name')}: {f.get('value')}" for f in fields)
            embed = self.make_embed(title, description, fields=fields, color=color, footer=footer, png=png)
            stamp = datetime.now(IST).strftime("%H:%M:%S")
            if self.attempts.get(key, 0) == 0:
                print(f"\n[{stamp}] " + self._console(title, lines, fields), flush=True)
                self.recent = (self.recent + [f"{stamp} {title}"])[-50:]
            try:
                ok = self.raw(body, png, embed=embed)
            except TypeError:                       # subclasses with the old raw(body, png) signature
                ok = self.raw(body, png)
            if ok:
                self.sent.add(key); self.attempts.pop(key, None)
                if len(self.sent) > 20000:
                    self.sent = set(list(self.sent)[-5000:])
                return True
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if self.attempts[key] >= MAX_ATTEMPTS:
                print(f"  (giving up on Discord delivery of {key} after {MAX_ATTEMPTS} attempts)")
                self.sent.add(key); self.attempts.pop(key, None)
            return False
        except Exception as e:
            print("  (notify failed:", e, ")")
            return False

    # ------------------------------------------------------------------ non-deduped card
    def card(self, title: str, description: str = "", *, fields: list[dict] | None = None, color: int | None = None,
             footer: str | None = None, png: str | None = None) -> bool:
        embed = self.make_embed(title, description, fields=fields, color=color, footer=footer, png=png)
        console = self._console(title, [description] if description else [], fields)
        try:
            return self.raw(console, png, embed=embed)
        except TypeError:
            return self.raw(console, png)
        except Exception as e:
            print("  (notify card failed:", e, ")")
            return False

    # ------------------------------------------------------------------ transport
    def raw(self, body: str, png: str | None = None, *, embed: dict | None = None) -> bool:
        """Post an embed (or plain content) to the webhook. True on success. No webhook = console = delivered."""
        if not self.webhook:
            return True
        try:
            import requests  # type: ignore
            payload: dict = {"embeds": [embed]} if embed else {"content": body[:1900]}
            if png and os.path.exists(png):
                with open(png, "rb") as f:
                    r = requests.post(self.webhook, data={"payload_json": json.dumps(payload)},
                                      files={"file": (os.path.basename(png), f, "image/png")}, timeout=15)
            else:
                r = requests.post(self.webhook, json=payload, timeout=15)
            ok = 200 <= r.status_code < 300
            self.posts += int(ok); self.failures += int(not ok)
            if not ok:
                print(f"  (discord post failed: HTTP {r.status_code} {r.text[:120]})")
            return ok
        except Exception as e:
            self.failures += 1
            print("  (discord post failed:", e, ")")
            return False
