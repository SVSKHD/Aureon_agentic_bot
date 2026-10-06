"""Aureon MT5 v1.8.3 — start here.

  python main.py --mode ema5080 --symbols XAUUSD
  python main.py --dry --mode ema5080 --symbols XAUUSD      # synthetic bars, no MT5 modifications
Mode resolution: --mode > AUREON_MODE > ema5080. The mode is fixed for the life of the process.
"""
from __future__ import annotations

import argparse
import os
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
from aureon_mt5 import broker, telemetry
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.bot import run_bot
from aureon_mt5.config import Config, VERSION
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.reports import weekly_report
from aureon_mt5.strategies import UnknownMode, get_strategy, mode_help, resolve_mode
from aureon_mt5.common.news import load_news

IST = timezone(timedelta(hours=5, minutes=30))
load_dotenv()

def git_version() -> str:
    try:
        import subprocess
        return subprocess.check_output(["git", "describe", "--tags", "--always", "--dirty"], stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except Exception:
        return ""


def banner() -> str:
    try:
        from pyfiglet import figlet_format  # type: ignore
        return figlet_format("AUREON MT5", font="big")
    except Exception:
        return "AUREON MT5\n"


def news_status(path: str | None) -> tuple[str, bool]:
    if not path or not os.path.exists(path):
        return "NFP auto only (no news file)", False
    txt = open(path, encoding="utf-8").read().lower()
    unverified = "example" in txt or "verify" in txt
    return ("UNVERIFIED — file contains example events" if unverified else "file + NFP auto"), unverified


def greet(notify, cfg, S, why, news_line):
    art = banner()
    gv = git_version()
    print(art); print(f"v{VERSION}{' · ' + gv if gv else ''} · {S.display_name} · {why} · {datetime.now(IST):%a %d %b %H:%M} IST · {', '.join(cfg.symbols)}")
    srv = datetime.now(timezone.utc) + timedelta(hours=cfg.server_utc_offset)
    notify.raw(f"```\n{art}```\n**⚡ AUREON MT5 v{VERSION}{' (' + gv + ')' if gv else ''} {why.upper()}**\n"
               f"Mode: {S.display_name}\nSymbols: {', '.join(cfg.symbols)}\nGuardian: {'ON' if any(S.guardian_for(s) for s in cfg.symbols) else 'OFF'}\n"
               f"MT5: {'CONNECTED' if broker.connected() else ('DRY' if cfg.dry else 'DISCONNECTED')} · server offset {cfg.server_utc_offset:+g}h\n"
               f"Server: {srv:%a %d %b %H:%M} · IST: {datetime.now(IST):%H:%M}\nNews: {news_line}\n{S.describe()}")


def supervise(agents: dict, factory, notify, mode: str) -> list[str]:
    """Restart ONLY dead agents; the others are never touched. Returns the symbols restarted."""
    restarted = []
    for sym, ag in list(agents.items()):
        if not ag.is_alive():
            agents[sym] = factory(sym); agents[sym].start(); restarted.append(sym)
            notify.send(f"{mode}:{sym}:RESTART:{int(_time.time()) // 60}", f"⚠️ agent {sym} restarted", ["other symbols unaffected"])
    return restarted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode"); ap.add_argument("--symbols"); ap.add_argument("--dry", action="store_true")
    ap.add_argument("--webhook"); ap.add_argument("--token"); ap.add_argument("--enable-silver", action="store_true")
    a = ap.parse_args()
    cfg = Config()
    try:
        cfg.mode = resolve_mode(a.mode); S = get_strategy(cfg.mode)
    except UnknownMode as e:
        print(mode_help(str(e))); sys.exit(2)
    if a.symbols: cfg.symbols = [s.strip().upper() for s in a.symbols.split(",")]
    cfg.dry = a.dry; cfg.enable_silver = a.enable_silver
    if a.dry: cfg.source = "synthetic"
    if a.webhook: cfg.webhook = a.webhook
    if a.token: cfg.bot_token = a.token

    notify = Notifier(cfg.webhook); journal = Journal(cfg.log_dir); telemetry.setup(cfg.log_dir)
    if not cfg.dry:
        if not broker.connect():
            print("MT5 not available — is the terminal open and logged in? (use --dry for a pipe test)"); sys.exit(1)
        cfg.server_utc_offset = broker.server_offset_hours(cfg.symbols[0], cfg.server_utc_offset)
    news = load_news(cfg.news_file if cfg.news_file and os.path.exists(cfg.news_file) else None, date.today().year)
    news_line, unverified = news_status(cfg.news_file)

    agents: dict[str, SymbolAgent] = {}; started = _time.time()

    def start_agents():
        for s in cfg.symbols:
            if s not in agents or not agents[s].is_alive():
                agents[s] = SymbolAgent(s, cfg, S, notify, journal, news); agents[s].start()

    greet(notify, cfg, S, "online", news_line)
    if unverified:
        notify.raw("⚠️ **NEWS CALENDAR CONTAINS UNVERIFIED EVENTS** — news_blackout.txt has example entries; verify against the economic calendar. NFP first-Friday is automatic.")
    for s in cfg.symbols:
        g = S.guardian_for(s)
        if g is None:
            notify.raw(f"ℹ️ {s}: {S.display_name} has no live guardian profile — signals only, positions not managed.")
        elif not g.enabled and not (cfg.enable_silver and s.startswith("XAG")):
            notify.raw(f"ℹ️ {s}: guardian profile is EXPERIMENTAL and disabled — signals only. Start with --enable-silver to manage.")
    start_agents()
    if cfg.bot_token: run_bot(cfg, agents, journal, notify, started)
    else: print("no DISCORD_TOKEN — slash commands off, alerts via webhook only")

    was_open = broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry); report_day = None
    while True:
        try:
            is_open = broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry)
            if was_open and not is_open:
                notify.send(f"{cfg.mode}:ALL:MARKET:closed-{date.today()}", "⏸ Market closed", [f"{datetime.now(IST):%a %d %b %H:%M} IST · agents sleeping"])
                if report_day != date.today():
                    notify.raw(weekly_report(cfg, agents, journal, previous=False)); report_day = date.today()
            if not was_open and is_open:
                greet(notify, cfg, S, "market open — Aureon resumed", news_line); start_agents()
            was_open = is_open
            supervise(agents, lambda sym: SymbolAgent(sym, cfg, S, notify, journal, news), notify, cfg.mode)
        except KeyboardInterrupt:
            broker.close_connection(); print("bye"); return
        except Exception as e:
            telemetry.failure(notify, journal, title="AUREON SUPERVISOR ERROR", key="supervisor", mode=cfg.mode, action="supervisor", exc=e)
        _time.sleep(30)


if __name__ == "__main__":
    main()
