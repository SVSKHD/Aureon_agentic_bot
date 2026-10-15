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

v2.11.0 — notifications that arrive when the trade does
    Every card has an EVENT (explicit `event=`, else the key's event segment, else an ops title) and a CLASS
    (EVENT_CLASS by prefix, default "info"; `klass=` overrides). Trade-class events listed in AUREON_DISCORD_MENTION_EVENTS
    carry `<@AUREON_DISCORD_USER_ID>` in `content` with allowed_mentions limited to that user; trade-class cards are also
    posted to AUREON_DISCORD_ASKS_WEBHOOK and edit_card() updates that copy too. The push layer (push.py) gets one push per
    dedupe key. With none of the v2.11 variables set the webhook payloads are byte-identical to v2.10.1.
"""
from __future__ import annotations

import json
import os
import threading
import time as _time
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

# v2.11.0 event class by event PREFIX (CLOSED_BY_YOU → trade via CLOSED); anything else is "info"
EVENT_CLASS = {
    "ASK": "trade", "ALERT": "trade", "FILLED": "trade", "CLOSED": "trade", "STOP": "trade", "FLIP_HOLD": "trade",
    "POSITION_ASK": "trade",
    "MAINTENANCE": "ops", "COLD_START": "ops", "MT5_NOT_UP": "ops", "MT5_RESTARTED": "ops", "REPAIR_AUTO": "ops",
    "CLOCK_DRIFT": "ops", "NETWORK_BACK": "ops",
}
EVENT_ALIASES = {"ORDER": "FILLED"}
TITLE_EVENTS = (("COLD START", "COLD_START"), ("MT5 NOT UP", "MT5_NOT_UP"), ("MT5 RESTARTED", "MT5_RESTARTED"),
                ("REPAIR AUTO-APPLY", "REPAIR_AUTO"), ("MAINTENANCE", "MAINTENANCE"), ("NETWORK BACK", "NETWORK_BACK"),
                ("CLOCK DRIFT", "CLOCK_DRIFT"))


def event_class(event: str | None) -> str:
    ev = str(event or "").upper()
    for prefix in sorted(EVENT_CLASS, key=len, reverse=True):
        if ev.startswith(prefix):
            return EVENT_CLASS[prefix]
    return "info"


def parse_key(key: str | None) -> tuple[str, str]:
    """dedupe key → (symbol, EVENT). 'mode:SYM:EVENT:ident' and 'alert:SYM:aid:ORDER'. ('', '') when it has no event."""
    parts = str(key or "").split(":")
    if parts[0] == "alert" and len(parts) >= 4:
        sym, ev = parts[1], parts[3]
    elif len(parts) >= 3:
        sym, ev = parts[1], parts[2]
    else:
        return "", ""
    ev = ev.upper()
    return sym, EVENT_ALIASES.get(ev, ev)


def title_event(title: str | None) -> str:
    t = str(title or "").upper()
    return next((ev for frag, ev in TITLE_EVENTS if frag in t), "")


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
        import queue as _q
        self.ask_queue: "_q.Queue[dict]" = _q.Queue()   # consumed by the bot (TAKE / SKIP buttons)
        self.bot_ready = False                          # set by bot.py when it can post interactive asks
        self.last_ok = True                             # v2.10.1: last webhook POST succeeded (network watch)
        self.backlog: list[str] = []                    # v2.10.1: titles that failed to post; one summary on reconnect
        self.claude_edit_hook = None                    # v1.10.0: set by bot.py — adds the Claude field to a posted ask card
        self.card_edit_hook = None                      # v2.0.2: set by bot.py — upserts named fields on a posted ask card (alerts)
        self.cfg = None                                 # v2.11.0: configure() — mentions / asks channel read it live (hot)
        self.push = None                                # v2.11.0: push.Push — one push per dedupe key
        self.open_asks: dict[str, dict] = {}                 # v2.11.0: open trade questions by key (bot, webhook, console share them)
        self.copies: dict[str, list] = {}               # v2.11.0: key → [[webhook, message_id, embed]] webhook copies to edit
        self._tl = threading.local()
        self.queue = None                               # v2.11.2: netguard.CardQueue once configure() ran

    def configure(self, cfg, push=None) -> "Notifier":
        """v2.11.0: give the notifier the config (mention user, mention events, asks webhook) and the push layer."""
        self.cfg = cfg
        self.push = push
        from .netguard import CardQueue                 # v2.11.2: trade cards survive a network loss (logs/card_queue.jsonl)
        self.queue = CardQueue(os.path.join(getattr(cfg, "log_dir", "logs") or "logs", "card_queue.jsonl"))
        return self

    # ------------------------------------------------------------------ v2.11.0 event context
    def classify(self, key: str | None, title: str, event: str | None = None, klass: str | None = None) -> tuple[str, str, str]:
        sym, kev = parse_key(key)
        ev = str(event or "").upper() or (kev if event_class(kev) != "info" else "") or title_event(title) or kev
        return sym, ev, (klass or event_class(ev))

    def mention_for(self, event: str, klass: str) -> str | None:
        cfg = self.cfg
        uid = str(getattr(cfg, "discord_user_id", "") or "")
        if klass != "trade" or not uid.isdigit():
            return None
        return uid if event in tuple(getattr(cfg, "discord_mention_events", ()) or ()) else None

    def asks_webhook(self) -> str:
        return str(getattr(self.cfg, "discord_asks_webhook", "") or "")

    def _set_ctx(self, ctx):
        tl = self.__dict__.get("_tl")
        if tl is None:
            tl = self.__dict__["_tl"] = threading.local()
        tl.ctx = ctx

    def _take_ctx(self):
        tl = self.__dict__.get("_tl")
        ctx = getattr(tl, "ctx", None) if tl is not None else None
        if tl is not None:
            tl.ctx = None
        return ctx

    @staticmethod
    def _with_mention(payload: dict, mention: str | None) -> dict:
        if mention:
            payload = dict(payload)
            payload["content"] = f"<@{mention}>" + (" " + payload["content"] if payload.get("content") else "")
            payload["allowed_mentions"] = {"users": [mention]}
        return payload

    def _remember_copy(self, key, url, r, embed):
        try:
            mid = (r.json() or {}).get("id")
        except Exception:
            mid = None
        if key and mid and embed:
            self.copies.setdefault(key, []).append([url, str(mid), json.loads(json.dumps(embed))])
            while len(self.copies) > 200:
                self.copies.pop(next(iter(self.copies)))

    def _post_asks_copy(self, key, body, png, embed, mention):
        """Trade-class copy to the asks channel (?wait=true so edit_card can PATCH it). Never raises."""
        url = self.asks_webhook()
        if not url:
            return False
        try:
            import requests  # type: ignore
            payload = self._with_mention({"embeds": [embed]} if embed else {"content": body[:1900]}, mention)
            if png and os.path.exists(png):
                with open(png, "rb") as f:
                    r = requests.post(url + "?wait=true", data={"payload_json": json.dumps(payload)},
                                      files={"file": (os.path.basename(png), f, "image/png")}, timeout=15)
            else:
                r = requests.post(url + "?wait=true", json=payload, timeout=15)
            ok = 200 <= r.status_code < 300
            if ok:
                self._remember_copy(key, url, r, embed)
            else:
                print(f"  (asks channel post failed: HTTP {r.status_code})")
            return ok
        except Exception as e:
            print("  (asks channel post failed:", e, ")")
            return False

    def mirror_edit(self, key: str, updates: dict) -> bool:
        """v2.11.0: upsert named fields on every tracked webhook copy of `key` (asks channel, and the main webhook card in
        webhook mode). True when at least one copy was patched. Never raises."""
        copies = self.copies.get(key) or []
        done = False
        for c in copies:
            url, mid, embed = c
            fields = list(embed.get("fields") or [])
            for name, value in updates.items():
                for f in fields:
                    if f.get("name") == name:
                        f["value"] = str(value)[:1024]; break
                else:
                    fields.append({"name": str(name)[:256], "value": str(value)[:1024], "inline": False})
            embed["fields"] = fields[:25]
            try:
                import requests  # type: ignore
                r = requests.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}, timeout=10)
                done = done or (200 <= r.status_code < 300)
            except Exception as e:
                print("  (copy edit failed:", e, ")")
        return done

    def _push_after(self, key, title, lines, fields, png, sym, ev, klass, points=None, meta=None):
        """One push per delivered dedupe key (push.Push dedupes too). Never raises."""
        p = self.push
        if p is None:
            return
        try:
            p.notify(key=key, title=title, lines=list(lines or []), fields=fields, png=png, symbol=sym, event=ev, klass=klass,
                     points=points, meta=meta or {})
        except Exception as e:
            print("  (push failed:", e, ")")

    def edit_card(self, key: str, updates: dict) -> bool:
        """Replace / add fields by name on the card posted under `key` (bot only). False when there is no such card
        (webhook mode, expired) — the caller then posts a follow-up card instead. Never raises."""
        hook = self.card_edit_hook
        mirrored = self.mirror_edit(key, dict(updates)) if self.copies.get(key) else False    # v2.11.0 asks-channel copy
        if hook is None:
            return bool(mirrored) and any(c[0] == self.webhook for c in self.copies.get(key) or [])
        try:
            return bool(hook(key, dict(updates)))
        except Exception as e:
            print("  (card edit failed:", e, ")")
            return False

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
             fields: list[dict] | None = None, color: int | None = None, footer: str | None = None,
             event: str | None = None, klass: str | None = None, points: float | None = None, meta: dict | None = None) -> bool:
        """One deduped card. Returns True when delivered (or printed in console mode). Never raises.
        v2.11.0: event / klass (see EVENT_CLASS) drive the mention, the asks channel and the push; points → STOP loss alert."""
        try:
            if key in self.sent:
                return False
            sym, ev, k = self.classify(key, title, event, klass)
            mention = self.mention_for(ev, k)
            fan = k == "trade" and bool(self.asks_webhook())
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
            from . import netguard
            skip = bool(self.webhook) and netguard.webhook_down()     # v2.11.2: no blocking POST while the network is down
            ok = False
            if not skip:
                self._set_ctx({"mention": mention, "track": fan, "key": key} if (mention or fan) else None)
                try:
                    ok = self.raw(body, png, embed=embed)
                except TypeError:                       # subclasses with the old raw(body, png) signature
                    ok = self.raw(body, png)
                finally:
                    self._set_ctx(None)
            if ok:
                self.sent.add(key); self.attempts.pop(key, None)
                if len(self.sent) > 20000:
                    self.sent = set(list(self.sent)[-5000:])
                if fan:
                    self._post_asks_copy(key, body, png, embed, mention)
                self._push_after(key, title, lines, fields, png, sym, ev, k, points=points, meta=meta)
                return True
            if self.attempts.get(key, 0) == 0:          # Discord down: the phone still hears it (push dedupes per key)
                self._push_after(key, title, lines, fields, png, sym, ev, k, points=points, meta=meta)
            if k == "trade" and self.queue is not None and self.webhook:    # v2.11.2: queued, flushed in order on reconnect
                self.queue.append({"t": _time.time(), "key": key, "title": title, "body": body, "embed": embed, "png": png})
                self.sent.add(key); self.attempts.pop(key, None)
                return False
            if skip:
                self._note_missed(embed, body)
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if self.attempts[key] >= MAX_ATTEMPTS:
                print(f"  (giving up on Discord delivery of {key} after {MAX_ATTEMPTS} attempts)")
                self.sent.add(key); self.attempts.pop(key, None)
            return False
        except Exception as e:
            print("  (notify failed:", e, ")")
            return False

    # ------------------------------------------------------------------ ask to trade (buttons when the bot is up)
    def ask(self, key: str, title: str, lines: list[str], *, fields=None, color=None, footer=None, png=None, meta: dict | None = None,
            event: str | None = None, klass: str | None = None, points: float | None = None) -> bool:
        """A trade question. With the bot running: card + [TAKE] [SKIP] buttons in the channel. Otherwise: the same card
        via webhook, with 'reply /take or /skip' instructions. Deduped like send(). Never places orders.
        v2.11.0: event defaults to ASK; the open question is kept in self.open_asks for /take, /skip and the console."""
        if key in self.sent:
            return False
        ev = str(event or "ASK").upper()
        if ev == "ASK":
            self.open_asks[key] = item = {"key": key, "title": title, "description": "\n".join(lines), "fields": fields or [],
                                     "color": color, "footer": footer, "png": png, "meta": meta or {}, "t": _time.time()}
            while len(self.open_asks) > 50:
                self.open_asks.pop(next(iter(self.open_asks)))
        else:
            item = {"key": key, "title": title, "description": "\n".join(lines), "fields": fields or [], "color": color,
                    "footer": footer, "png": png, "meta": meta or {}, "t": _time.time()}
        if self.bot_ready:
            sym, ev, k = self.classify(key, title, ev, klass)
            mention = self.mention_for(ev, k)
            if mention:
                item["mention"] = mention
            self.sent.add(key)
            self.ask_queue.put(item)
            print(f"\n[ask] {title}", flush=True)
            if k == "trade" and self.asks_webhook():
                self._post_asks_copy(key, f"**{title}**\n" + "\n".join(lines), png,
                                     self.make_embed(title, "\n".join(lines), fields=fields, color=color, footer=footer, png=png), mention)
            self._push_after(key, title, lines, fields, png, sym, ev, k, points=points, meta=meta)
            return True
        return self.send(key, title, lines + ["answer with `/take` or `/skip` (buttons need the bot token + DISCORD_CHANNEL)"],
                         png, fields=fields, color=color, footer=footer, event=ev, klass=klass, points=points, meta=meta)

    # ------------------------------------------------------------------ non-deduped card
    def card(self, title: str, description: str = "", *, fields: list[dict] | None = None, color: int | None = None,
             footer: str | None = None, png: str | None = None, event: str | None = None, klass: str | None = None) -> bool:
        embed = self.make_embed(title, description, fields=fields, color=color, footer=footer, png=png)
        console = self._console(title, [description] if description else [], fields)
        from . import netguard
        if self.webhook and netguard.webhook_down():          # v2.11.2: no blocking POST while the network is down
            print("\n" + console, flush=True)
            self._note_missed(embed, console)
            return False
        try:
            ok = self.raw(console, png, embed=embed)
        except TypeError:
            ok = self.raw(console, png)
        except Exception as e:
            print("  (notify card failed:", e, ")")
            return False
        if ok and self.push is not None:                                      # v2.11.0: ops cards (COLD START, MT5 NOT UP…)
            sym, ev, k = self.classify(None, title, event, klass)
            if ev:
                self._push_after(f"card:{ev}:{title}:{int(_time.time() // 60)}", title, [description] if description else [],
                                 fields, png, sym, ev, k)
        return ok

    # ------------------------------------------------------------------ transport
    def raw(self, body: str, png: str | None = None, *, embed: dict | None = None) -> bool:
        """Post an embed (or plain content) to the webhook. True on success. No webhook = console = delivered."""
        ctx = self._take_ctx() or {}                     # v2.11.0: mention / copy tracking for this one post
        if not self.webhook:
            return True
        try:
            import requests  # type: ignore
            payload: dict = {"embeds": [embed]} if embed else {"content": body[:1900]}
            payload = self._with_mention(payload, ctx.get("mention"))
            url = self.webhook + ("?wait=true" if ctx.get("track") else "")
            if png and os.path.exists(png):
                with open(png, "rb") as f:
                    r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                      files={"file": (os.path.basename(png), f, "image/png")}, timeout=15)
            else:
                r = requests.post(url, json=payload, timeout=15)
            ok = 200 <= r.status_code < 300
            if ok and ctx.get("track"):
                self._remember_copy(ctx.get("key"), self.webhook, r, embed)
            self.posts += int(ok); self.failures += int(not ok)
            if not ok:
                _rlog("discord-post", f"discord post failed: HTTP {r.status_code} {r.text[:120]}")
            self._track(ok, embed, body)
            return ok
        except Exception as e:                       # DNS, timeout, proxy, TLS — never escapes (v2.11.2)
            self.failures += 1
            _rlog("discord-post", f"discord post failed: {type(e).__name__}: {str(e)[:200]} — cards queue until it is back")
            self._track(False, embed, body)
            return False

    def _note_missed(self, embed, body):
        lines = (body or "").splitlines()
        t = (embed or {}).get("title") or (lines[0] if lines else "?")
        self.backlog = (self.backlog + [str(t)[:120]])[-200:]

    def _track(self, ok: bool, embed, body):
        """v2.10.1: remember what failed. v2.11.2: the webhook streak feeds netguard; the first success after failures
        flushes (summary card first, then the queued trade cards in order)."""
        from . import netguard
        self.last_ok = ok
        netguard.webhook_result(ok)
        if not ok:
            self._note_missed(embed, body)
            return
        if not getattr(self, "_flushing", False) and (self.backlog or (self.queue is not None and len(self.queue))):
            self.flush()

    def flush(self) -> dict:
        """v2.11.2 (main loop, every 30 s, independent of the gateway; and on the first success): while the webhook is down
        or anything is pending, post ONE summary card `BACK ONLINE · network · down N min · K cards queued`, then the
        queued trade cards in order. Stops at the first failure and keeps the rest. Never raises."""
        from . import netguard
        if getattr(self, "_flushing", False) or not self.webhook:
            return {}
        try:
            q = self.queue.items() if self.queue is not None else []
            if not (q or self.backlog or netguard.webhook_down()):
                return {}
            since = netguard.STATE.get("webhook_down_since") or (q[0].get("t") if q else None)
            mins = max(0, round((_time.time() - since) / 60)) if since else 0
            missed = [t for t in self.backlog if t]
            desc = (f"{len(missed)} other card(s) could not be posted:\n" + "\n".join(f"• {t}" for t in missed[-25:])) if missed else \
                "nothing else was missed"
            title = f"AUREON · MT5 · BACK ONLINE · network · down {mins} min · {len(q)} cards queued"
            self._flushing = True
            try:
                if not self.raw("", embed=self.make_embed(title, desc, color=GREY)):
                    return {"ok": False, "queued": len(q)}
                self.backlog = []
                for i, it in enumerate(q):
                    try:
                        ok = self.raw(it.get("body", ""), it.get("png"), embed=it.get("embed"))
                    except TypeError:
                        ok = self.raw(it.get("body", ""), it.get("png"))
                    if not ok:
                        self.queue.replace(q[i:])
                        return {"ok": False, "sent": i, "queued": len(q) - i}
                if self.queue is not None:
                    self.queue.replace([])
                return {"ok": True, "sent": len(q), "title": title}
            finally:
                self._flushing = False
        except Exception as e:
            _rlog("flush", f"card queue flush failed: {e!r}")
            return {"ok": False}


def _rlog(key, msg):
    try:
        from .netguard import rlog
        rlog(key, msg)
    except Exception:
        print(f"  ({msg})")


# --------------------------------------------------------------------------- v1.11.0 COMPARE card
def post_compare(notify: "Notifier", key: str, c: dict) -> bool:
    """Post a card built by compare.card() / compare.breakdown_card() through the normal deduped path."""
    return notify.send(key, c["title"], [c["description"]], c.get("png"), fields=c.get("fields"), color=BLURPLE, footer=c.get("footer"))
