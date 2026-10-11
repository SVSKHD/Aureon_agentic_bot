"""Aureon MT5 — start here (version: aureon_mt5/config.py VERSION).

  python main.py --mode ema5080 --symbols XAUUSD
  python main.py --dry --mode ema5080 --symbols XAUUSD      # synthetic bars, no MT5 modifications
Mode resolution: --mode > AUREON_MODE > ema5080. The mode is fixed for the life of the process.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone

from aureon_mt5 import broker, telemetry
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.alerts import AlertStore
from aureon_mt5.bot import run_bot
from aureon_mt5.claude_advisor import ClaudeAdvisor
from aureon_mt5 import compare
from aureon_mt5.notify import post_compare
from aureon_mt5.config import Config, VERSION
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.reports import weekly_report
from aureon_mt5.strategies import UnknownMode, get_strategy, mode_help, resolve_mode
from aureon_mt5.common.news import load_news

IST = timezone(timedelta(hours=5, minutes=30))

AUTO_UPDATE_INTERVAL = 300  # check remote every 5 minutes


def check_for_updates(notify) -> bool:
    """Fetch remote master; if ahead of local, pull and restart the process."""
    try:
        subprocess.run(["git", "fetch", "origin", "master"], capture_output=True, timeout=30)
        local = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", "origin/master"], capture_output=True, text=True, timeout=10).stdout.strip()
        if not local or not remote or local == remote:
            return False
        result = subprocess.run(["git", "pull", "origin", "master"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            notify.card("AUREON · MT5 · UPDATE FAILED", f"git pull failed: {result.stderr[:200]}",
                        fields=[{"name": "Action", "value": "Manual pull required", "inline": False}],
                        footer="Aureon MT5 · Auto-Update")
            return False
        notify.card("AUREON · MT5 · RESTARTING", f"New commits pulled from master.\n`{local[:8]}` → `{remote[:8]}`",
                    fields=[{"name": "Pull", "value": "Success", "inline": True},
                            {"name": "Action", "value": "Restarting now...", "inline": True}],
                    footer=f"Aureon MT5 · Auto-Update · {datetime.now(IST):%H:%M} IST")
        _time.sleep(2)
        os.environ["AUREON_AUTO_RESTARTED"] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print(f"auto-update check failed: {e}")
    return False


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
    gv = git_version() if "git_version" in globals() else ""
    try:
        from aureon_mt5.common import gitinfo
        gv = (gv or subprocess.run(["git", "describe", "--tags", "--always"], capture_output=True, text=True, timeout=10, cwd=gitinfo.ROOT).stdout.strip())
        md = gitinfo.head_merge_date()
        if md:
            gv = f"{gv} · merged {md}" if gv else f"merged {md}"      # v2.0.2: the merge date of HEAD on the banner
    except Exception:
        pass
    print(art); print(f"v{VERSION}{' · ' + gv if gv else ''} · {S.display_name} · {why} · {datetime.now(IST):%a %d %b %H:%M} IST · {', '.join(cfg.symbols)}")
    srv = datetime.now(timezone.utc) + timedelta(hours=cfg.server_utc_offset)
    guardian_on = any(S.guardian_for(s) for s in cfg.symbols)
    mt5_state = "CONNECTED" if broker.connected() else ("DRY" if cfg.dry else "DISCONNECTED")
    notify.card(
        f"AUREON · MT5 · {why.upper()}",
        f"```\n{art}```\n**v{VERSION}**{' · ' + gv if gv else ''} · {S.display_name} · you place, I manage",
        fields=[
            {"name": "Mode", "value": S.display_name, "inline": True},
            {"name": "Symbols", "value": ", ".join(cfg.symbols), "inline": True},
            {"name": "Guardian", "value": "🟢 ON" if guardian_on else "⚪ OFF", "inline": True},
            {"name": "MT5", "value": ("🟢 " if mt5_state in ("CONNECTED", "DRY") else "🔴 ") + mt5_state, "inline": True},
            {"name": "Server", "value": f"{srv:%a %d %b %H:%M} · UTC{cfg.server_utc_offset:+g}", "inline": True},
            {"name": "IST", "value": f"{datetime.now(IST):%H:%M}", "inline": True},
            {"name": "News", "value": news_line, "inline": False},
            {"name": "Rules", "value": S.describe(), "inline": False},
        ],
        footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST",
    )


def supervise(agents: dict, factory, notify, mode: str) -> list[str]:
    """Restart ONLY dead agents; the others are never touched. Returns the symbols restarted."""
    restarted = []
    for sym, ag in list(agents.items()):
        if not ag.is_alive():
            agents[sym] = factory(sym); agents[sym].start(); restarted.append(sym)
            notify.send(f"{mode}:{sym}:RESTART:{int(_time.time()) // 60}", f"{sym} · MT5 · AGENT RESTARTED", ["other symbols unaffected"])
    return restarted


def claude_review_tick(claude, cfg, review_day, now: datetime | None = None):
    """23:00 IST (cfg.daily_report_ist): queue the day's CLAUDE REVIEW once per IST day. Runs on the Claude worker, never here."""
    if claude is None:
        return review_day
    now = now or datetime.now(IST)
    hh, mm = (int(x) for x in cfg.daily_report_ist.split(":"))
    today = now.strftime("%Y-%m-%d")
    if review_day != today and (now.hour, now.minute) >= (hh, mm):
        claude.request_review(today)
        return today
    return review_day


_COMPARE_RETRY_AT: dict[str, float] = {}


def compare_saturday_tick(cfg, agents, journal, notify, claude=None, now: datetime | None = None, gather=None) -> str | None:
    """Saturday ≥ cfg.weekly_report_ist (10:00 IST), after the WEEKLY report: post the COMPARE card for Mon–Fri once.
    Restart-safe: a week already in logs/compare_weekly.jsonl is skipped. Both modes (v2.0.0). Measurement only."""
    if cfg.mode not in compare.KINDS_BY_MODE:
        return None
    now = now or datetime.now(IST)
    hh, mm = (int(x) for x in cfg.weekly_report_ist.split(":"))
    if now.weekday() != 5 or (now.hour, now.minute) < (hh, mm):
        return None
    week, since, until = compare.week_window(now)
    if _time.time() < _COMPARE_RETRY_AT.get(week, 0) or any(h.get("week") == week for h in compare.read_history(cfg.log_dir)):
        return None
    try:
        res = (gather or compare.gather)(cfg, agents, journal, since, until)
        c = compare.card(res, title=f"AUREON · MT5 · COMPARE · {week}")
        post_compare(notify, f"{cfg.mode}:ALL:COMPARE:{week}", c)
        compare.append_history(cfg.log_dir, week, res)
        if claude is not None:                                   # v2.0.0: CLAUDE RULE PROPOSALS (no call, no edit; /claude-rules-approve <n>)
            try:
                compare.rule_proposals_tick(cfg, journal, notify, res, week, next(iter(agents.values())).S.display_name if agents else cfg.mode)
            except Exception as e:
                telemetry.info(f"rule proposals failed: {e!r}")
    except Exception as e:                                   # back off 30 min instead of re-grading every 30 s
        _COMPARE_RETRY_AT[week] = _time.time() + 1800
        telemetry.failure(notify, journal, title="AUREON COMPARE FAILED", key=f"{cfg.mode}:ALL:COMPARE_ERROR:{week}",
                          mode=cfg.mode, action="weekly compare", exc=e, retry="retry in 30 min")
        return None
    if cfg.compare_claude_review and claude is not None:
        import threading
        threading.Thread(target=compare.self_review, args=(claude, res, notify, week), daemon=True, name="compare-self-review").start()
    return week


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
    health_notify = Notifier(cfg.health_webhook) if cfg.health_webhook else notify
    if not cfg.dry:
        if not broker.connect():
            print("MT5 not available — is the terminal open and logged in? (use --dry for a pipe test)"); sys.exit(1)
        cfg.server_utc_offset = broker.server_offset_hours(cfg.symbols[0], cfg.server_utc_offset)
    news = load_news(cfg.news_file if cfg.news_file and os.path.exists(cfg.news_file) else None, date.today().year)
    news_line, unverified = news_status(cfg.news_file)

    agents: dict[str, SymbolAgent] = {}; started = _time.time()
    alerts = AlertStore(cfg.log_dir)                                                   # v2.0.0 /alert store, shared by agents + bot
    claude = ClaudeAdvisor.create(cfg, notify, journal, health_notify=health_notify)     # None when AUREON_CLAUDE=off
    if claude is not None:
        print(f"Claude add-on: {claude.mode} · bin {cfg.claude_bin} · workdir {cfg.claude_workdir} · {cfg.claude_max_calls} calls/day")
        if os.environ.get("ANTHROPIC_API_KEY"):
            health_notify.card("AUREON · MT5 · CLAUDE · API KEY SET",
                               "`ANTHROPIC_API_KEY` is set in this environment. Aureon removes it from the `claude -p` call so your "
                               "subscription login is used — remove it from the environment to be sure nothing bills the API.",
                               footer="Aureon MT5 · Claude")

    def start_agents():
        for s in cfg.symbols:
            if s not in agents or not agents[s].is_alive():
                agents[s] = SymbolAgent(s, cfg, S, notify, journal, news, claude=claude, alerts=alerts); agents[s].start()

    why = "online"
    try:
        last_msg = subprocess.run(["git", "log", "-1", "--pretty=%s"], capture_output=True, text=True, timeout=10).stdout.strip()
        last_hash = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        if os.environ.get("AUREON_AUTO_RESTARTED"):
            why = f"restarted · auto-update · {last_hash}"
            notify.card("AUREON · MT5 · BACK ONLINE", f"Auto-update restart complete.\nRunning `{last_hash}` · {last_msg}",
                        fields=[{"name": "Version", "value": f"v{VERSION}", "inline": True},
                                {"name": "Commit", "value": f"`{last_hash}`", "inline": True},
                                {"name": "Message", "value": last_msg[:200], "inline": False}],
                        footer=f"Aureon MT5 · Auto-Update · {datetime.now(IST):%H:%M} IST")
    except Exception:
        pass
    greet(notify, cfg, S, why, news_line)
    if unverified:
        notify.card("AUREON · MT5 · NEWS CALENDAR UNVERIFIED", "Unverified events are present in `news_blackout.txt`.",
                    fields=[{"name": "Action", "value": "Verify the listed releases against the economic calendar.", "inline": False},
                            {"name": "NFP", "value": "First-Friday protection remains automatic.", "inline": False}],
                    footer="Aureon MT5 · News Guard")
    for s in cfg.symbols:
        g = S.guardian_for(s)
        if g is None:
            notify.card(f"{s} · MT5 · SIGNALS ONLY", f"{S.display_name} has no live guardian profile.",
                        fields=[{"name": "Position management", "value": "OFF", "inline": True}],
                        footer="Aureon MT5")
        elif not g.enabled and not (cfg.enable_silver and s.startswith("XAG")):
            notify.card(f"{s} · MT5 · GUARDIAN DISABLED", "Experimental guardian profile — signals only.",
                        fields=[{"name": "Enable explicitly", "value": "`--enable-silver`", "inline": True}],
                        footer="Aureon MT5")
    start_agents()
    if cfg.bot_token: run_bot(cfg, agents, journal, notify, started, health_notify=health_notify, claude=claude, alerts=alerts)
    else: print("no DISCORD_TOKEN — slash commands off, alerts via webhook only")

    was_open = broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry); report_day = None; last_update_check = 0
    review_day = None
    while True:
        try:
            now = _time.time()
            if now - last_update_check >= AUTO_UPDATE_INTERVAL:
                last_update_check = now
                check_for_updates(notify)
            is_open = broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry)
            if was_open and not is_open:
                notify.send(f"{cfg.mode}:ALL:MARKET:closed-{date.today()}", "AUREON · MT5 · MARKET CLOSED", ["agents sleeping"], footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")
                if report_day != date.today():
                    notify.raw(weekly_report(cfg, agents, journal, previous=False)); report_day = date.today()
            if not was_open and is_open:
                greet(notify, cfg, S, "market open — Aureon resumed", news_line); start_agents()
            was_open = is_open
            review_day = claude_review_tick(claude, cfg, review_day)
            compare_saturday_tick(cfg, agents, journal, notify, claude)
            supervise(agents, lambda sym: SymbolAgent(sym, cfg, S, notify, journal, news, claude=claude, alerts=alerts), notify, cfg.mode)
        except KeyboardInterrupt:
            broker.close_connection(); print("bye"); return
        except Exception as e:
            telemetry.failure(notify, journal, title="AUREON SUPERVISOR ERROR", key="supervisor", mode=cfg.mode, action="supervisor", exc=e)
        _time.sleep(30)


if __name__ == "__main__":
    main()
