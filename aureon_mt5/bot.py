"""Discord slash commands (needs DISCORD_TOKEN).

The command UI uses compact native Discord embeds/cards.  Webhook alerts use the
same visual language through notify.py.  Discord is presentation-only and never
controls guardian execution.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import broker, telemetry
from .config import VERSION

IST = timezone(timedelta(hours=5, minutes=30))
BLURPLE = 0x5865F2
GREEN = 0x57F287
YELLOW = 0xFEE75C
RED = 0xED4245
BLUE = 0x3498DB
GREY = 0x95A5A6


def _field(name, value, inline=True):
    return {"name": str(name), "value": str(value) or "—", "inline": inline}


def _embed(title, description="", *, color=BLURPLE, fields=None, footer=None):
    d = {"title": title, "color": color, "timestamp": datetime.now(timezone.utc).isoformat()}
    if description:
        d["description"] = description
    if fields:
        d["fields"] = fields[:25]
    d["footer"] = {"text": footer or f"Aureon MT5 · {datetime.now(IST):%H:%M} IST"}   # computed per card (was frozen at import)
    return d


# ----------------------------------------------------------------------------- v1.9.8 cached snapshot helpers
# Slash commands never call MT5 for status. Each SymbolAgent refreshes `ag.snapshot` every poll (agent thread);
# the cards below read only that and the in-memory `ag.health`.
def _snap(ag) -> dict:
    return getattr(ag, "snapshot", None) or {"at": None}


def snapshot_age(ag, now: float | None = None) -> float | None:
    at = _snap(ag).get("at")
    return None if at is None else max(0.0, (now or _time.time()) - at)


def age_text(ag) -> str:
    a = snapshot_age(ag)
    return "no snapshot yet — agent starting" if a is None else f"as of {a:.0f} s ago"


def _bot_line(monitor) -> str:
    return monitor.summary() if monitor is not None else "—"


def status_card(sym: str, ag, cfg, journal=None, monitor=None) -> dict:
    h = ag.health; S = ag.S; snap = _snap(ag)
    mk = "OPEN" if snap.get("market_open") else ("CLOSED" if snap.get("at") else "—")
    age = snap.get("tick_age"); age_s = f"{age}s" if age is not None else "—"
    today = snap.get("today") or {"signals": 0, "closed": 0, "errors": 0, "secured": 0}
    connected = bool(snap.get("connected"))
    color = GREEN if ag.is_alive() and connected and h.get("status") in ("running", "market closed") else YELLOW if ag.is_alive() else RED

    fields = [
        _field("Mode", S.display_name),
        _field("Market", f"{'🟢' if mk == 'OPEN' else '⏸'} {mk} · tick {age_s}"),
        _field("MT5", {"CONNECTED": "🟢 CONNECTED", "DRY": "🧪 DRY"}.get(snap.get("mt5"), "🔴 DISCONNECTED")),
        _field("Agent", f"{'🟢' if ag.is_alive() else '🔴'} {h['status'].upper()}"),
        _field("Guardian", h.get("guardian") or "—"),
        _field("Last bar", f"{h.get('last_bar') or '—'} IST"),
    ]
    if h.get("close") is not None:
        order = "↑ fast > slow" if h.get("ema_fast", 0) > h.get("ema_slow", 0) else "↓ fast < slow"
        fields += [
            _field("Price", f"{h['close']:.2f}"),
            _field(f"EMA {S.fast}", f"{h['ema_fast']:.2f}"),
            _field(f"EMA {S.slow}", f"{h['ema_slow']:.2f} · {order}"),
        ]

    pos = snap.get("positions") or []
    states = snap.get("state") or {}
    if pos:
        for p in pos[:3]:
            st = states.get(p["ticket"], {})
            phase = "PRE CROSS" if st.get("pre") else "POST CROSS"
            fields.append(_field(
                f"Position #{p['ticket']}",
                f"**{p['direction'].upper()}** · entry `{p['price_open']:.2f}` · SL `{p['sl'] or '—'}`\n"
                f"{phase} · secured **+{st.get('secured', 0):g}** · peak **+{st.get('peak', 0):.1f}** · now **{p['points']:+.1f}**",
                False,
            ))
    else:
        fields.append(_field("Position", "No active position", False))

    fields += [
        _field("Today", f"Signals **{today['signals']}** · Closed **{today['closed']}** · Errors **{today['errors']}**", False),
        _field("Secured today", f"+{today['secured']:g}"),
        _field("Last signal", h.get("last_signal") or "—"),
        _field("Last broker action", h.get("last_broker_action") or "—"),
        _field("Bot", _bot_line(monitor), False),
    ]
    return _embed(f"{sym} · MT5 · STATUS", f"AUREON MT5 v{VERSION} · {S.display_name} · {age_text(ag)}", color=color, fields=fields,
                  footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")


def parallel_card(agents, cfg, journal=None) -> dict:
    fields = []
    all_ok = True
    for sym, ag in agents.items():
        h = ag.health; snap = _snap(ag)
        pos = snap.get("positions") or []
        if pos:
            p = pos[0]; st = (snap.get("state") or {}).get(p["ticket"], {})
            ptxt = f"{p['direction'].upper()} #{p['ticket']} · **+{st.get('secured', 0):g} secured** · now {p['points']:+.1f}"
        else:
            ptxt = "No active position"
        healthy = ag.is_alive() and h.get("errors", 0) == 0
        all_ok &= healthy
        icon = "🟢" if healthy else ("🟡" if ag.is_alive() else "🔴")
        px = f"{h['close']:.2f}" if h.get("close") is not None else "—"
        value = (f"{icon} **{h['status'].upper()}** · {ag.S.display_name} · {age_text(ag)}\n"
                 f"Price `{px}` · {ptxt}\n"
                 f"Signal `{h.get('last_signal') or '—'}` · Errors `{h.get('errors', 0)}`")
        fields.append(_field(sym, value, False))
    return _embed("AUREON · MT5 · PARALLEL STATUS", f"v{VERSION} · {len(agents)} symbol(s)", color=GREEN if all_ok else YELLOW, fields=fields)


def agents_card(agents, cfg, started_at, monitor=None) -> dict:
    up = _time.time() - started_at
    fields = []
    for sym, ag in agents.items():
        h = ag.health; snap = _snap(ag)
        checks = [
            ("Detector", ag.is_alive() and h["status"] in ("running", "market closed")),
            ("Guardian", ag.g is not None),
            ("MT5", bool(snap.get("connected"))),
            ("Journal", True), ("Telemetry", True),
            ("Discord", bool(cfg.webhook)),
            ("News", len(ag.news) > 0), ("Market guard", True),
        ]
        lines = [f"{'✅' if ok else '❌'} {name}" for name, ok in checks]
        lines.append(f"Errors `{h['errors']}` · thread `{'alive' if ag.is_alive() else 'DEAD'}` · {age_text(ag)}")
        fields.append(_field(f"{sym} · {ag.S.display_name}", "\n".join(lines), True))
    fields.append(_field("Bot", _bot_line(monitor), False))
    desc = f"Uptime **{int(up // 3600)}h {int(up % 3600 // 60)}m**"
    return _embed("AUREON · MT5 · AGENTS", desc, color=BLUE, fields=fields)


def symbols_card(agents, cfg) -> dict:
    fields = []
    for sym, ag in agents.items():
        g = ag.S.guardian_for(sym)
        prof = (g.note if g else "No guardian profile")
        guardian = "🟢 ENABLED" if ag.g else ("🟡 DISABLED" if g else "⚪ SIGNALS ONLY")
        fields.append(_field(sym, f"**{ag.S.display_name}**\n{guardian}\n{prof}\nStatus: `{ag.health['status'].upper()}`", True))
    return _embed("AUREON · MT5 · SYMBOLS", f"Configured: **{', '.join(cfg.symbols)}**", color=BLURPLE, fields=fields)


def market_card(sym: str, ag, cfg) -> dict:
    snap = _snap(ag)
    is_open = bool(snap.get("market_open"))
    srv = datetime.now(timezone.utc) + timedelta(hours=cfg.server_utc_offset)
    age = snap.get("tick_age")
    return _embed(f"{'🟢' if is_open else '⏸'} MARKET {'OPEN' if is_open else 'CLOSED'}", f"{sym} · {age_text(ag)}",
                  color=GREEN if is_open else YELLOW,
                  fields=[_field("Server", f"{srv:%a %d %b %H:%M}"), _field("IST", f"{datetime.now(IST):%H:%M}"),
                          _field("Tick age", f"{age if age is not None else '—'}s")])


def present_card(symbol: str, agents, cfg) -> dict:
    s = symbol.upper()
    if s in agents:
        ag = agents[s]
        mk = "OPEN" if _snap(ag).get("market_open") else "CLOSED"
        return _embed(f"📍 {s}", f"{ag.S.display_name} · {age_text(ag)}", color=GREEN if ag.is_alive() else RED,
                      fields=[_field("Agent", "🟢 RUNNING" if ag.is_alive() else "🔴 DOWN"), _field("Market", mk),
                              _field("Guardian", ag.health.get("guardian") or "—")])
    return _embed(f"📍 {s}", "Not running on this Aureon instance.", color=YELLOW, fields=[_field("Configured", ", ".join(cfg.symbols), False)])


def report_card(cfg, agents, journal, current: bool, lock_timeout: float = 2.0) -> tuple[dict, str]:
    """Runs OFF the event loop. Waits at most `lock_timeout` s for MT5; if busy, journal data only."""
    from .reports import weekly_report
    with broker.try_lock(lock_timeout) as got:
        text = weekly_report(cfg, agents, journal, previous=not current, mt5=got)
    card = _embed("AUREON · MT5 · WEEKLY REPORT", ("This week so far" if current else "Previous week") + ("" if got else " · MT5 busy — cached data"),
                  color=BLUE, fields=[_field("Report", text[:1000] or "No report data", False)], footer=f"Aureon MT5 v{VERSION}")
    return card, ("live" if got else "cache")


def chart_card(sym: str, ag) -> tuple[dict | None, str | None]:
    """Runs OFF the event loop (matplotlib). Uses the agent's last closed-bar frame — no MT5 call."""
    path = ag.chart_now()
    if not path:
        return None, None
    h = ag.health; S = ag.S
    fields = []
    if h.get("close") is not None:
        fields += [_field("Price", f"**{h['close']:.2f}**"), _field(f"EMA {S.fast}", f"{h['ema_fast']:.2f}"), _field(f"EMA {S.slow}", f"{h['ema_slow']:.2f}"),
                   _field("Lines", "▲ fast > slow" if h["ema_fast"] > h["ema_slow"] else "▼ fast < slow")]
    fields.append(_field("Last signal", h.get("last_signal") or "—", False))
    card = _embed(f"{sym} · MT5 · MARKET CONTEXT", f"{S.display_name} · last bar {h.get('last_bar') or '—'} IST", color=BLUE, fields=fields,
                  footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")
    card["image"] = {"url": f"attachment://{os.path.basename(path)}"}
    return card, path


# Backwards-compatible text helpers for scripts/tests that may import them.
def status_text(sym: str, ag, cfg, journal=None) -> str:
    c = status_card(sym, ag, cfg, journal)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def parallel_text(agents, cfg, journal=None) -> str:
    c = parallel_card(agents, cfg, journal)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def agents_text(agents, cfg, started_at) -> str:
    c = agents_card(agents, cfg, started_at)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def symbols_text(agents, cfg) -> str:
    c = symbols_card(agents, cfg)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def claude_card(claude, cfg) -> dict:
    if claude is None:
        return _embed("AUREON · MT5 · CLAUDE", "Claude add-on is **off** (`AUREON_CLAUDE=off`) — zero Claude calls.", color=GREY)
    st = claude.status()
    lat = f"{st['avg_latency']:.0f} s" if st["avg_latency"] is not None else "—"
    login = st["login"]
    fields = [_field("Mode", st["mode"].upper()), _field("Login", ("🟢 " if login == "OK" else "🔴 " if login == "FAILED" else "⚪ ") + login),
              _field("Calls today", f"{st['calls']} / {st['limit']}"), _field("Avg latency", lat), _field("Queue", st["queue"]),
              _field("Last verdict", st["last_verdict"], False), _field("Last error", st["last_error"][:300], False),
              _field("Models", st["models"], False)]
    return _embed("AUREON · MT5 · CLAUDE", "second opinion only — Claude never places a trade", color=BLURPLE, fields=fields)


# ----------------------------------------------------------------------------- v1.9.8 command wrapper
CMD_TIMEOUT = 15.0          # seconds a command's off-loop work may take before we answer "timed out"


@dataclass
class Reply:
    """What a command returns. Built off the event loop; sent as one followup."""
    card: dict | None = None
    content: str | None = None
    file: str | None = None
    source: str = "cache"           # cache | live — logged per command


async def off_loop(fn, *args, timeout: float = CMD_TIMEOUT):
    """Run blocking work (MT5, matplotlib, files, subprocess) in a worker thread with a hard timeout."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)


def discord_kwargs(reply: Reply) -> dict:
    import discord  # type: ignore
    kw: dict = {}
    if reply.content:
        kw["content"] = reply.content[:1900]
    if reply.card:
        kw["embed"] = discord.Embed.from_dict(reply.card)
    if reply.file and os.path.exists(reply.file):
        kw["file"] = discord.File(reply.file, filename=os.path.basename(reply.file))
    return kw


async def respond(inter, name: str, work, *, notify=None, journal=None, ephemeral: bool = False,
                  timeout: float = CMD_TIMEOUT, to_kwargs=discord_kwargs) -> Reply | None:
    """THE way every slash command answers:
    1) defer immediately (Discord's 3 s limit), 2) run `work` (a sync callable returning Reply) off the loop with a timeout,
    3) one followup. Any exception → '⚠️ command failed: …' followup + traceback in logs/aureon.log + telemetry (5-min backoff).
    Logs name, time to ack, total time and cache/live."""
    t0 = _time.monotonic()
    try:
        await inter.response.defer(thinking=True, ephemeral=ephemeral)
    except Exception as e:                                   # interaction already expired / acknowledged
        telemetry.info(f"cmd /{name} defer failed after {(_time.monotonic() - t0) * 1000:.0f} ms: {e!r}")
        return None
    ack_ms = (_time.monotonic() - t0) * 1000
    reply: Reply | None = None
    try:
        reply = await off_loop(work, timeout=timeout)
        if not isinstance(reply, Reply):
            reply = Reply(content=str(reply))
        await inter.followup.send(**to_kwargs(reply))
    except Exception as e:
        short = "timed out" if isinstance(e, (asyncio.TimeoutError, TimeoutError)) else (str(e).strip().splitlines() or [type(e).__name__])[-1]
        try:
            await inter.followup.send(content=f"⚠️ command failed: /{name} — {short[:180]}")
        except Exception:
            pass
        if notify is not None:
            try:
                telemetry.failure(notify, journal, title="AUREON COMMAND FAILED", key=f"cmd:{name}:{type(e).__name__}",
                                  action=f"/{name}", exc=e, retry="run the command again")
            except Exception:
                pass
        else:
            telemetry.info(f"cmd /{name} failed: {e!r}")
    total_ms = (_time.monotonic() - t0) * 1000
    telemetry.info(f"cmd /{name} ack={ack_ms:.0f}ms total={total_ms:.0f}ms source={reply.source if reply else 'error'}")
    return reply


def run_bot(cfg, agents: dict, journal, notify, started_at: float, *, health_notify=None, claude=None):
    try:
        import discord  # type: ignore
        from discord import app_commands  # type: ignore
    except Exception:
        print("discord.py not installed — commands unavailable (alerts still work via webhook)"); return None
    from .common.loop_health import LoopMonitor

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    monitor = LoopMonitor(health_notify or notify)

    def run(inter, name, work, **kw):
        """Every command goes through respond(): defer first, work off the loop, one followup, errors always reported."""
        return respond(inter, name, work, notify=health_notify or notify, journal=journal, **kw)

    pending: dict[str, dict] = {}          # last asks by key, for /take /skip without buttons

    def _ask_embed(item):
        em = discord.Embed(title=item["title"][:256], description=item["description"][:4096], color=item.get("color") or BLUE,
                           timestamp=datetime.now(timezone.utc))
        for f in item.get("fields") or []:
            em.add_field(name=str(f["name"])[:256], value=str(f["value"])[:1024], inline=bool(f.get("inline", True)))
        em.set_footer(text=(item.get("footer") or "Aureon MT5")[:2048])
        return em

    def _claude_edit(key, fld) -> bool:
        """v1.10.0 (called from the Claude worker thread): add the Claude field to the posted ask card if it is still open."""
        item = pending.get(key)
        if not item or item.get("decided") or not item.get("message"):
            return False
        item["fields"] = list(item.get("fields") or []) + [fld]
        fut = asyncio.run_coroutine_threadsafe(item["message"].edit(embed=_ask_embed(item)), client.loop)
        fut.result(timeout=10)
        return True

    notify.claude_edit_hook = _claude_edit

    def _decide(item, decision, user):
        m = item.get("meta", {})
        journal.log("decision", symbol=m.get("symbol"), mode=m.get("mode"), side=m.get("side"), signal_kind=m.get("kind"),
                    price=m.get("price"), bar=m.get("bar"), decision=decision, by=str(user))
        item["decided"] = decision

    class AskView(discord.ui.View):
        def __init__(self, item):
            super().__init__(timeout=cfg.ask_ttl_min * 60); self.item = item

        async def _answer(self, inter, decision):
            if self.item.get("decided"):
                await inter.response.send_message(f"already answered: {self.item['decided']}", ephemeral=True); return
            _decide(self.item, decision, inter.user)
            for c in self.children: c.disabled = True
            em = _ask_embed(self.item)
            m = self.item.get("meta", {})
            if decision == "TAKE":
                em.add_field(name="Decision", value=f"✅ **TAKING {m.get('side')}** · {inter.user.display_name} · {datetime.now(IST):%H:%M} IST — "
                                                    f"place it in MT5; I'll start managing the moment the ticket appears", inline=False)
                em.color = GREEN
            else:
                em.add_field(name="Decision", value=f"⏭ skipped · {inter.user.display_name} · {datetime.now(IST):%H:%M} IST", inline=False)
                em.color = GREY
            await inter.response.edit_message(embed=em, view=self)

        @discord.ui.button(label="TAKE", style=discord.ButtonStyle.success, emoji="✅")
        async def take(self, inter, button): await self._answer(inter, "TAKE")

        @discord.ui.button(label="SKIP", style=discord.ButtonStyle.secondary, emoji="⏭")
        async def skip(self, inter, button): await self._answer(inter, "SKIP")

        async def on_timeout(self):
            if not self.item.get("decided"):
                _decide(self.item, "EXPIRED", "timeout")
                for c in self.children: c.disabled = True
                try:
                    em = _ask_embed(self.item); em.color = GREY
                    em.add_field(name="Decision", value=f"⌛ expired after {cfg.ask_ttl_min} min", inline=False)
                    await self.item["message"].edit(embed=em, view=self)
                except Exception:
                    pass

    async def _ask_pump():
        await client.wait_until_ready()
        ch = client.get_channel(cfg.channel_id) if cfg.channel_id else None
        if ch is None:
            print("DISCORD_CHANNEL not set or not visible — asks fall back to webhook cards"); notify.bot_ready = False; return
        notify.bot_ready = True
        while not client.is_closed():
            try:
                item = await asyncio.to_thread(notify.ask_queue.get, True, 2.0)
            except Exception:
                continue
            try:
                em = _ask_embed(item); view = AskView(item); files = []
                if item.get("png") and os.path.exists(item["png"]):
                    fname = os.path.basename(item["png"]); em.set_image(url=f"attachment://{fname}"); files = [discord.File(item["png"], filename=fname)]
                msg = await ch.send(embed=em, view=view, files=files)
                item["message"] = msg; pending[item["key"]] = item
                while len(pending) > 50: pending.pop(next(iter(pending)))
            except Exception as e:
                print("  (ask post failed:", e, ") — falling back to webhook")
                notify.sent.discard(item["key"]); notify.bot_ready = False
                try:                                     # webhook post is blocking I/O — never on the event loop
                    await asyncio.to_thread(notify.send, item["key"], item["title"], [item["description"]], item.get("png"),
                                            fields=item.get("fields"), color=item.get("color"), footer=item.get("footer"))
                finally:
                    notify.bot_ready = True

    def _latest_open():
        return next((i for i in reversed(list(pending.values())) if not i.get("decided")), None)

    @tree.command(name="take", description="Take the latest open trade question")
    async def take_cmd(inter: discord.Interaction):
        user = inter.user
        def work():
            item = _latest_open()
            if not item:
                return Reply(content="no open trade question")
            _decide(item, "TAKE", user)
            return Reply(content=f"✅ taking **{item['meta'].get('side')}** {item['meta'].get('symbol')} — place it in MT5, I manage it from there")
        await run(inter, "take", work)

    @tree.command(name="skip", description="Skip the latest open trade question")
    async def skip_cmd(inter: discord.Interaction):
        user = inter.user
        def work():
            item = _latest_open()
            if not item:
                return Reply(content="no open trade question")
            _decide(item, "SKIP", user)
            return Reply(content=f"⏭ skipped {item['meta'].get('side')} {item['meta'].get('symbol')}")
        await run(inter, "skip", work)

    @client.event
    async def on_ready():
        monitor.gateway_up()
        if not getattr(client, "_aureon_started", False):
            client._aureon_started = True
            await tree.sync()
            client.loop.create_task(_ask_pump())
            client.loop.create_task(monitor.ticker(client))
            monitor.start_watchdog()
        print(f"discord bot ready as {client.user}")

    @client.event
    async def on_disconnect():
        monitor.gateway_down()

    @client.event
    async def on_resumed():
        monitor.gateway_up()

    @tree.command(name="status", description="Aureon status for the primary symbol")
    async def status(inter: discord.Interaction):
        sym = cfg.symbols[0]
        await run(inter, "status", lambda: Reply(card=status_card(sym, agents[sym], cfg, journal, monitor)))

    @tree.command(name="parallel-status", description="All symbols at a glance")
    async def pstatus(inter: discord.Interaction):
        await run(inter, "parallel-status", lambda: Reply(card=parallel_card(agents, cfg, journal)))

    @tree.command(name="agents", description="Components and health")
    async def agents_cmd(inter: discord.Interaction):
        await run(inter, "agents", lambda: Reply(card=agents_card(agents, cfg, started_at, monitor)))

    @tree.command(name="symbols", description="Configured symbols and mode")
    async def symbols(inter: discord.Interaction):
        await run(inter, "symbols", lambda: Reply(card=symbols_card(agents, cfg)))

    @tree.command(name="symbol-present", description="Is a symbol running here?")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def present(inter: discord.Interaction, symbol: str):
        await run(inter, "symbol-present", lambda: Reply(card=present_card(symbol, agents, cfg)))

    @tree.command(name="market", description="Is the market open?")
    async def market(inter: discord.Interaction):
        sym = cfg.symbols[0]
        await run(inter, "market", lambda: Reply(card=market_card(sym, agents[sym], cfg)))

    @tree.command(name="report", description="Weekly report (previous week by default)")
    @app_commands.describe(current="true = this week so far")
    async def report(inter: discord.Interaction, current: bool = False):
        def work():
            card, source = report_card(cfg, agents, journal, current)
            return Reply(card=card, source=source)
        await run(inter, "report", work, timeout=20)

    @tree.command(name="chart", description="Market context chart with EMA lines")
    @app_commands.describe(symbol="e.g. XAUUSD (default: primary symbol)")
    async def chart(inter: discord.Interaction, symbol: str = ""):
        sym = (symbol or cfg.symbols[0]).upper()
        def work():
            if sym not in agents:
                return Reply(content=f"{sym}: not running (configured: {', '.join(cfg.symbols)})")
            card, path = chart_card(sym, agents[sym])
            if not card:
                return Reply(content=f"{sym}: no bars yet — try again after the first closed M5 bar")
            return Reply(card=card, file=path, source="live")
        await run(inter, "chart", work, timeout=20)

    # ------------------------------------------------------------------ v1.10.0 Claude add-on (all off the loop via run())
    claude_wait = float(getattr(cfg, "claude_timeout", 90)) * 2 + 15      # a call may queue behind one in flight

    @tree.command(name="claude", description="Claude add-on: mode, login, calls today, last verdict")
    async def claude_cmd(inter: discord.Interaction):
        await run(inter, "claude", lambda: Reply(card=claude_card(claude, cfg)))

    @tree.command(name="claude-test", description="One tiny Claude call to check the login")
    async def claude_test(inter: discord.Interaction):
        def work():
            if claude is None:
                return Reply(content="Claude add-on is off (`AUREON_CLAUDE=off`)")
            r = claude.test_call()
            if r is None:
                return Reply(content="⛔ daily Claude budget used — no call made")
            ok = r.ok and "OK" in (r.text or "").upper()
            return Reply(content=(f"🟢 Claude login OK · {r.latency_s:.0f} s" if ok else f"🔴 Claude test failed: {r.error or r.text[:200]}"), source="live")
        await run(inter, "claude-test", work, timeout=claude_wait)

    @tree.command(name="claude-review", description="Claude review of a day's journal (default today, IST)")
    @app_commands.describe(date="YYYY-MM-DD (IST), default today")
    async def claude_review(inter: discord.Interaction, date: str = ""):
        def work():
            if claude is None:
                return Reply(content="Claude add-on is off (`AUREON_CLAUDE=off`)")
            day = date.strip() or None
            if day:
                datetime.strptime(day, "%Y-%m-%d")       # ValueError → '⚠️ command failed'
            text = claude.daily_review(day)
            return Reply(content=f"CLAUDE REVIEW {day or 'today'}:\n{text}"[:1900], source="live")
        await run(inter, "claude-review", work, timeout=claude_wait)

    # ------------------------------------------------------------------ v1.11.0 weekly compare (measurement only)
    def _compare_reply(days: int, breakdown: bool) -> Reply:
        from . import compare
        days = max(1, min(int(days), 60))
        since, until = compare.window_days(days)
        res = compare.gather(cfg, agents, journal, since, until)
        c = compare.breakdown_card(res) if breakdown else compare.card(res, title=f"AUREON · MT5 · COMPARE · last {days} days")
        em = {"title": c["title"], "description": c["description"], "fields": c["fields"][:25], "color": BLURPLE,
              "footer": {"text": c["footer"]}, "timestamp": datetime.now(timezone.utc).isoformat()}
        if c.get("png"):
            em["image"] = {"url": f"attachment://{os.path.basename(c['png'])}"}
        return Reply(card=em, file=c.get("png"), source="cache" if (res.grading_note or "").startswith("MT5 busy") else "live")

    @tree.command(name="compare", description="Detector vs Claude vs Me on the same signals (measurement only)")
    @app_commands.describe(days="window in days (default 7)", private="only you see the answer")
    async def compare_cmd(inter: discord.Interaction, days: int = 7, private: bool = False):
        await run(inter, "compare", lambda: _compare_reply(days, False), ephemeral=private, timeout=30)

    @tree.command(name="compare-breakdown", description="Compare by event type (P/CROSS/RE) and session")
    @app_commands.describe(days="window in days (default 7)")
    async def compare_breakdown_cmd(inter: discord.Interaction, days: int = 7):
        await run(inter, "compare-breakdown", lambda: _compare_reply(days, True), timeout=30)

    def _runner():
        asyncio.set_event_loop(asyncio.new_event_loop())
        client.run(cfg.bot_token, log_handler=None)

    threading.Thread(target=_runner, daemon=True, name="discord-bot").start()
    return monitor
