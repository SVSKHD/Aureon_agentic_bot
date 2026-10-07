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
from datetime import datetime, timedelta, timezone

from . import broker
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


def _embed(title, description="", *, color=BLURPLE, fields=None, footer=f"Aureon MT5 · {datetime.now(IST):%H:%M} IST"):
    d = {"title": title, "color": color, "timestamp": datetime.now(timezone.utc).isoformat()}
    if description:
        d["description"] = description
    if fields:
        d["fields"] = fields[:25]
    if footer:
        d["footer"] = {"text": footer}
    return d


def status_card(sym: str, ag, cfg, journal) -> dict:
    h = ag.health; S = ag.S; off = cfg.server_utc_offset
    mk = "OPEN" if broker.market_open(sym, off, cfg.dry) else "CLOSED"
    age = broker.tick_age(sym, off); age_s = f"{age}s" if age is not None else "—"
    today = journal.today(sym)
    connected = broker.connected() or cfg.dry
    color = GREEN if ag.is_alive() and connected and h.get("status") in ("running", "market closed") else YELLOW if ag.is_alive() else RED

    fields = [
        _field("Mode", S.display_name),
        _field("Market", f"{'🟢' if mk == 'OPEN' else '⏸'} {mk} · tick {age_s}"),
        _field("MT5", "🟢 CONNECTED" if broker.connected() else ("🧪 DRY" if cfg.dry else "🔴 DISCONNECTED")),
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

    pos = broker.positions(sym)
    if pos:
        for p in pos[:3]:
            st = ag.state.get(p["ticket"], {})
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
    ]
    return _embed(f"{sym} · MT5 · STATUS", f"AUREON MT5 v{VERSION} · {S.display_name}", color=color, fields=fields, footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")


def parallel_card(agents, cfg, journal) -> dict:
    fields = []
    all_ok = True
    for sym, ag in agents.items():
        h = ag.health
        pos = broker.positions(sym) if (broker.connected() or cfg.dry) else []
        if pos:
            p = pos[0]; st = ag.state.get(p["ticket"], {})
            ptxt = f"{p['direction'].upper()} #{p['ticket']} · **+{st.get('secured', 0):g} secured** · now {p['points']:+.1f}"
        else:
            ptxt = "No active position"
        healthy = ag.is_alive() and h.get("errors", 0) == 0
        all_ok &= healthy
        icon = "🟢" if healthy else ("🟡" if ag.is_alive() else "🔴")
        px = f"{h['close']:.2f}" if h.get("close") is not None else "—"
        value = (f"{icon} **{h['status'].upper()}** · {ag.S.display_name}\n"
                 f"Price `{px}` · {ptxt}\n"
                 f"Signal `{h.get('last_signal') or '—'}` · Errors `{h.get('errors', 0)}`")
        fields.append(_field(sym, value, False))
    return _embed("AUREON · MT5 · PARALLEL STATUS", f"v{VERSION} · {len(agents)} symbol(s)", color=GREEN if all_ok else YELLOW, fields=fields)


def agents_card(agents, cfg, started_at) -> dict:
    up = _time.time() - started_at
    fields = []
    for sym, ag in agents.items():
        h = ag.health
        checks = [
            ("Detector", ag.is_alive() and h["status"] in ("running", "market closed")),
            ("Guardian", ag.g is not None),
            ("MT5", broker.connected() or cfg.dry),
            ("Journal", True), ("Telemetry", True),
            ("Discord", bool(cfg.webhook)),
            ("News", len(ag.news) > 0), ("Market guard", True),
        ]
        lines = [f"{'✅' if ok else '❌'} {name}" for name, ok in checks]
        lines.append(f"Errors `{h['errors']}` · thread `{'alive' if ag.is_alive() else 'DEAD'}`")
        fields.append(_field(f"{sym} · {ag.S.display_name}", "\n".join(lines), True))
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


# Backwards-compatible text helpers for scripts/tests that may import them.
def status_text(sym: str, ag, cfg, journal) -> str:
    c = status_card(sym, ag, cfg, journal)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def parallel_text(agents, cfg, journal) -> str:
    c = parallel_card(agents, cfg, journal)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def agents_text(agents, cfg, started_at) -> str:
    c = agents_card(agents, cfg, started_at)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def symbols_text(agents, cfg) -> str:
    c = symbols_card(agents, cfg)
    return c["title"] + "\n" + "\n".join(f"{f['name']}: {f['value']}" for f in c.get("fields", []))


def run_bot(cfg, agents: dict, journal, notify, started_at: float):
    try:
        import discord  # type: ignore
        from discord import app_commands  # type: ignore
    except Exception:
        print("discord.py not installed — commands unavailable (alerts still work via webhook)"); return
    from .reports import weekly_report

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)

    def as_embed(card: dict):
        return discord.Embed.from_dict(card)

    pending: dict[str, dict] = {}          # last asks by key, for /take /skip without buttons

    def _ask_embed(item):
        em = discord.Embed(title=item["title"][:256], description=item["description"][:4096], color=item.get("color") or BLUE,
                           timestamp=datetime.now(timezone.utc))
        for f in item.get("fields") or []:
            em.add_field(name=str(f["name"])[:256], value=str(f["value"])[:1024], inline=bool(f.get("inline", True)))
        em.set_footer(text=(item.get("footer") or "Aureon MT5")[:2048])
        return em

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
                notify.send(item["key"], item["title"], [item["description"]], item.get("png"), fields=item.get("fields"), color=item.get("color"), footer=item.get("footer"))
                notify.bot_ready = True

    @tree.command(name="take", description="Take the latest open trade question")
    async def take_cmd(inter: discord.Interaction):
        item = next((i for i in reversed(list(pending.values())) if not i.get("decided")), None)
        if not item:
            await inter.response.send_message("no open trade question"); return
        _decide(item, "TAKE", inter.user)
        await inter.response.send_message(f"✅ taking **{item['meta'].get('side')}** {item['meta'].get('symbol')} — place it in MT5, I manage it from there")

    @tree.command(name="skip", description="Skip the latest open trade question")
    async def skip_cmd(inter: discord.Interaction):
        item = next((i for i in reversed(list(pending.values())) if not i.get("decided")), None)
        if not item:
            await inter.response.send_message("no open trade question"); return
        _decide(item, "SKIP", inter.user)
        await inter.response.send_message(f"⏭ skipped {item['meta'].get('side')} {item['meta'].get('symbol')}")

    @client.event
    async def on_ready():
        await tree.sync()
        client.loop.create_task(_ask_pump()); print(f"discord bot ready as {client.user}")

    @tree.command(name="status", description="Aureon status for the primary symbol")
    async def status(inter: discord.Interaction):
        sym = cfg.symbols[0]
        await inter.response.send_message(embed=as_embed(status_card(sym, agents[sym], cfg, journal)))

    @tree.command(name="parallel-status", description="All symbols at a glance")
    async def pstatus(inter: discord.Interaction):
        await inter.response.send_message(embed=as_embed(parallel_card(agents, cfg, journal)))

    @tree.command(name="agents", description="Components and health")
    async def agents_cmd(inter: discord.Interaction):
        await inter.response.send_message(embed=as_embed(agents_card(agents, cfg, started_at)))

    @tree.command(name="symbols", description="Configured symbols and mode")
    async def symbols(inter: discord.Interaction):
        await inter.response.send_message(embed=as_embed(symbols_card(agents, cfg)))

    @tree.command(name="symbol-present", description="Is a symbol running here?")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def present(inter: discord.Interaction, symbol: str):
        s = symbol.upper()
        if s in agents:
            ag = agents[s]
            mk = "OPEN" if broker.market_open(s, cfg.server_utc_offset, cfg.dry) else "CLOSED"
            card = _embed(f"📍 {s}", f"{ag.S.display_name}", color=GREEN if ag.is_alive() else RED,
                          fields=[_field("Agent", "🟢 RUNNING" if ag.is_alive() else "🔴 DOWN"), _field("Market", mk), _field("Guardian", ag.health.get("guardian") or "—")])
        else:
            card = _embed(f"📍 {s}", "Not running on this Aureon instance.", color=YELLOW,
                          fields=[_field("Configured", ", ".join(cfg.symbols), False)])
        await inter.response.send_message(embed=as_embed(card))

    @tree.command(name="market", description="Is the market open?")
    async def market(inter: discord.Interaction):
        sym = cfg.symbols[0]
        is_open = broker.market_open(sym, cfg.server_utc_offset, cfg.dry)
        srv = datetime.now(timezone.utc) + timedelta(hours=cfg.server_utc_offset)
        age = broker.tick_age(sym, cfg.server_utc_offset)
        card = _embed(f"{'🟢' if is_open else '⏸'} MARKET {'OPEN' if is_open else 'CLOSED'}", sym,
                      color=GREEN if is_open else YELLOW,
                      fields=[_field("Server", f"{srv:%a %d %b %H:%M}"), _field("IST", f"{datetime.now(IST):%H:%M}"), _field("Tick age", f"{age if age is not None else '—'}s")])
        await inter.response.send_message(embed=as_embed(card))

    @tree.command(name="report", description="Weekly report (previous week by default)")
    @app_commands.describe(current="true = this week so far")
    async def report(inter: discord.Interaction, current: bool = False):
        await inter.response.defer()
        text = weekly_report(cfg, agents, journal, previous=not current)
        card = _embed("AUREON · MT5 · WEEKLY REPORT", "This week so far" if current else "Previous week", color=BLUE,
                      fields=[_field("Report", text[:1000] or "No report data", False)], footer=f"Aureon MT5 v{VERSION}")
        await inter.followup.send(embed=as_embed(card))

    @tree.command(name="chart", description="Market context chart with EMA lines")
    @app_commands.describe(symbol="e.g. XAUUSD (default: primary symbol)")
    async def chart(inter: discord.Interaction, symbol: str = ""):
        sym = (symbol or cfg.symbols[0]).upper()
        if sym not in agents:
            await inter.response.send_message(f"{sym}: not running (configured: {', '.join(cfg.symbols)})"); return
        await inter.response.defer()
        ag = agents[sym]
        path = await asyncio.to_thread(ag.chart_now)
        if not path:
            await inter.followup.send(f"{sym}: no bars yet — try again after the first closed M5 bar"); return
        h = ag.health; S = ag.S
        em = discord.Embed(title=f"{sym} · MT5 · MARKET CONTEXT", description=f"{S.display_name} · last bar {h.get('last_bar') or '—'} IST",
                           color=0x3498DB, timestamp=datetime.now(timezone.utc))
        if h.get("close") is not None:
            em.add_field(name="Price", value=f"**{h['close']:.2f}**"); em.add_field(name=f"EMA {S.fast}", value=f"{h['ema_fast']:.2f}")
            em.add_field(name=f"EMA {S.slow}", value=f"{h['ema_slow']:.2f}")
            em.add_field(name="Lines", value="▲ fast > slow" if h["ema_fast"] > h["ema_slow"] else "▼ fast < slow")
        em.add_field(name="Last signal", value=h.get("last_signal") or "—", inline=False)
        fname = os.path.basename(path); em.set_image(url=f"attachment://{fname}")
        em.set_footer(text=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")
        await inter.followup.send(embed=em, file=discord.File(path, filename=fname))

    def _runner():
        asyncio.set_event_loop(asyncio.new_event_loop())
        client.run(cfg.bot_token, log_handler=None)

    threading.Thread(target=_runner, daemon=True, name="discord-bot").start()
