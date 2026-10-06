"""Discord slash commands (needs DISCORD_TOKEN). Without it: webhook alerts only.
/status /parallel-status /agents /symbols /symbol-present /market /report"""
from __future__ import annotations

import asyncio
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from . import broker
from .config import VERSION

IST = timezone(timedelta(hours=5, minutes=30))


def status_text(sym: str, ag, cfg, journal) -> str:
    h = ag.health; S = ag.S; off = cfg.server_utc_offset
    mk = "OPEN" if broker.market_open(sym, off, cfg.dry) else "CLOSED"
    age = broker.tick_age(sym, off); age_s = f"{age}s" if age is not None else "—"
    today = journal.today(sym)
    L = [f"**AUREON MT5 v{VERSION}**", f"Mode       {S.display_name}", f"Symbol     {sym}", f"Market     {mk} · tick age {age_s}",
         f"MT5        {'CONNECTED' if broker.connected() else ('DRY' if cfg.dry else 'DISCONNECTED')} · server offset {h.get('offset_h')}h",
         f"Agent      {h['status'].upper()} · last bar {h.get('last_bar') or '—'} IST · poll {h.get('last_poll') or '—'}",
         f"Guardian   {h['guardian']}"]
    if h.get("close"):
        L.append(f"Close {h['close']:.2f} · EMA{S.fast} {h['ema_fast']:.2f} · EMA{S.slow} {h['ema_slow']:.2f} · {'fast>slow ↑' if h['ema_fast'] > h['ema_slow'] else 'fast<slow ↓'}")
    pos = broker.positions(sym)
    if pos:
        for p in pos:
            st = ag.state.get(p["ticket"], {})
            L.append(f"Position   {p['direction'].upper()} #{p['ticket']} · entry {p['price_open']:.2f} · SL {p['sl'] or '—'} · "
                     f"phase {'PRE CROSS' if st.get('pre') else 'POST CROSS'} · secured +{st.get('secured', 0):g} · peak +{st.get('peak', 0):.1f} · now {p['points']:+.1f}")
    else:
        L.append("Position   none")
    L += [f"Signals today {today['signals']} · Secured today +{today['secured']:g} · Closed today {today['closed']} · Errors today {today['errors']}",
          f"Last signal {h.get('last_signal') or '—'} · Last broker action {h.get('last_broker_action') or '—'}"]
    return "\n".join(L)


def parallel_text(agents, cfg, journal) -> str:
    """One glanceable line per symbol. One symbol's error never colours another's line."""
    rows = [f"**AUREON v{VERSION} · parallel**"]
    for sym, ag in agents.items():
        h = ag.health
        pos = broker.positions(sym) if (broker.connected() or cfg.dry) else []
        if pos:
            p = pos[0]; st = ag.state.get(p["ticket"], {})
            ptxt = f"{p['direction'].upper()} #{p['ticket']} · +{st.get('secured', 0):g} secured · now {p['points']:+.1f}"
        else:
            ptxt = "no position"
        ok = "✅" if (ag.is_alive() and h["errors"] == 0) else ("⚠️" if ag.is_alive() else "❌")
        px = f"{h['close']:.2f}" if h.get("close") else "—"
        rows.append(f"{sym} | {ag.S.display_name} | {h['status'].upper()} | px {px} | {ptxt} | sig {h.get('last_signal') or '—'} | "
                    f"broker {h.get('last_broker_action') or '—'} | err {h['errors']} | {ok}")
    return "\n".join(rows)


def agents_text(agents, cfg, started_at) -> str:
    up = _time.time() - started_at
    L = [f"**AUREON AGENTS** · v{VERSION} · up {int(up // 3600)}h {int(up % 3600 // 60)}m"]
    for sym, ag in agents.items():
        ok = lambda b: "✅" if b else "❌"
        L += [f"\n**{sym} · {ag.S.display_name}**",
              f"Detector     {ok(ag.is_alive() and ag.health['status'] in ('running', 'market closed'))}",
              f"Guardian     {ok(ag.g is not None)} {'' if ag.g else '(no profile)'}",
              f"MT5 Broker   {ok(broker.connected() or cfg.dry)}",
              f"Journal      ✅", f"Telemetry    ✅", f"Discord      {ok(bool(cfg.webhook))}",
              f"News Guard   {ok(len(ag.news) > 0)} ({len(ag.news)} events)", f"Market Guard ✅",
              f"errors {ag.health['errors']} · thread {'alive' if ag.is_alive() else 'DEAD'}"]
    return "\n".join(L)


def symbols_text(agents, cfg) -> str:
    L = []
    for sym, ag in agents.items():
        g = ag.S.guardian_for(sym)
        prof = (g.note if g else "none") + ("" if (g and ag.g) else (" · DISABLED" if g else ""))
        L.append(f"**{sym}**\nMode: {ag.S.display_name}\nGuardian: {prof}\nStatus: {ag.health['status'].upper()}")
    return "\n\n".join(L)


def run_bot(cfg, agents: dict, journal, notify, started_at: float):
    try:
        import discord  # type: ignore
        from discord import app_commands  # type: ignore
    except Exception:
        print("discord.py not installed — commands unavailable (alerts still work via webhook)"); return
    from .reports import weekly_report
    client = discord.Client(intents=discord.Intents.default()); tree = app_commands.CommandTree(client)

    @client.event
    async def on_ready():
        await tree.sync(); print(f"discord bot ready as {client.user}")

    @tree.command(name="status", description="Aureon status for the primary symbol")
    async def status(inter: discord.Interaction):
        sym = cfg.symbols[0]; await inter.response.send_message(status_text(sym, agents[sym], cfg, journal)[:1990])

    @tree.command(name="parallel-status", description="All symbols at a glance")
    async def pstatus(inter: discord.Interaction):
        await inter.response.send_message(parallel_text(agents, cfg, journal)[:1990])

    @tree.command(name="agents", description="Components and health")
    async def agents_cmd(inter: discord.Interaction):
        await inter.response.send_message(agents_text(agents, cfg, started_at)[:1990])

    @tree.command(name="symbols", description="Configured symbols and mode")
    async def symbols(inter: discord.Interaction):
        await inter.response.send_message(symbols_text(agents, cfg)[:1990])

    @tree.command(name="symbol-present", description="Is a symbol running here?")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def present(inter: discord.Interaction, symbol: str):
        s = symbol.upper()
        if s in agents:
            await inter.response.send_message(f"{s}: running · {agents[s].S.display_name} · market {'open' if broker.market_open(s, cfg.server_utc_offset, cfg.dry) else 'closed'}")
        else:
            await inter.response.send_message(f"{s}: not running (configured: {', '.join(cfg.symbols)})")

    @tree.command(name="market", description="Is the market open?")
    async def market(inter: discord.Interaction):
        srv = datetime.now(timezone.utc) + timedelta(hours=cfg.server_utc_offset)
        age = broker.tick_age(cfg.symbols[0], cfg.server_utc_offset)
        await inter.response.send_message(f"market **{'OPEN' if broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry) else 'CLOSED'}** · "
                                          f"server {srv:%a %d %b %H:%M} · IST {datetime.now(IST):%H:%M} · tick age {age if age is not None else '—'}s")

    @tree.command(name="report", description="Weekly report (previous week by default)")
    @app_commands.describe(current="true = this week so far")
    async def report(inter: discord.Interaction, current: bool = False):
        await inter.response.defer()
        await inter.followup.send(weekly_report(cfg, agents, journal, previous=not current)[:1990])

    def _runner():
        asyncio.set_event_loop(asyncio.new_event_loop()); client.run(cfg.bot_token, log_handler=None)
    threading.Thread(target=_runner, daemon=True, name="discord-bot").start()
