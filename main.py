"""Aureon MT5 — start here (version: aureon_mt5/config.py VERSION).

  python main.py --mode ema5080 --symbols XAUUSD
  python main.py --dry --mode ema5080 --symbols XAUUSD      # synthetic bars, no MT5 modifications
Mode resolution: --mode > AUREON_MODE > ema5080. The mode is fixed for the life of the process.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()

from aureon_mt5 import broker, telemetry
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.alerts import AlertStore
from aureon_mt5.bot import run_bot
from aureon_mt5.claude_advisor import ClaudeAdvisor
from aureon_mt5.codex_advisor import CodexAdvisor
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


_update_failed_at = 0.0  # last UPDATE FAILED card (posted at most once per hour)


def discarded_card(notify, res) -> None:
    from aureon_mt5.common.updater import discarded_title
    notify.card(discarded_title(res),
                f"Server is authoritative: checkout reset to origin/master `{res['new'][:8]}`.\npatch saved `{res['patch']}`",
                fields=[{"name": "Restore", "value": f"git apply {res['patch']}", "inline": False}],
                footer="Aureon MT5 · Auto-Update")


def check_for_updates(notify, run=subprocess.run, exit_hook=None, log_dir: str = "logs", cwd: str | None = None) -> bool:
    """Server-authoritative: if origin/master moved, bring the checkout exactly to it (local edits → patch, then reset)
    and restart. Restarts only after HEAD == origin/master; any failure keeps the current build running.
    v2.7.2: restart = exit(75) via `exit_hook`; the supervisor starts the new build (no os.execv)."""
    global _update_failed_at
    try:
        from aureon_mt5.common.updater import update_to_origin
        res = update_to_origin(log_dir, run=run, **({"cwd": cwd} if cwd else {}))
        if res["status"] == "failed":
            if _time.time() - _update_failed_at >= 3600:
                _update_failed_at = _time.time()
                notify.card("AUREON · MT5 · UPDATE FAILED", res["error"][:300],
                            fields=[{"name": "Action", "value": "Running on the current build; retried every 5 min", "inline": False}],
                            footer="Aureon MT5 · Auto-Update · shown once per hour")
            return False
        if res["status"] != "updated":
            return False
        if res["discarded"]:
            discarded_card(notify, res)
        notify.card("AUREON · MT5 · RESTARTING", f"Updated to origin/master.\n`{res['old'][:8]}` → `{res['new'][:8]}`",
                    fields=[{"name": "Update", "value": "Success", "inline": True},
                            {"name": "Action", "value": "Restarting now...", "inline": True}],
                    footer=f"Aureon MT5 · Auto-Update · {datetime.now(IST):%H:%M} IST")
        _time.sleep(2)
        from aureon_mt5 import ops
        ops.write_restart_marker(log_dir, "auto-update", f"{res['old'][:8]} -> {res['new'][:8]}")
        (exit_hook or ops.hard_exit)(ops.EXIT_RESTART)
    except Exception as e:
        print(f"auto-update check failed: {e}")
    return False


def seed_news_file(path: str | None) -> bool:
    """v2.2.2: news_blackout.txt is server-local (ignored). If it is missing (first run, or the update that stopped
    tracking it removed it), copy news_blackout.example.txt next to it. Never overwrites an existing file."""
    if not path or os.path.exists(path):
        return False
    example = os.path.join(os.path.dirname(path), "news_blackout.example.txt")
    if not os.path.exists(example):
        return False
    try:
        shutil.copyfile(example, path)
        return True
    except OSError:
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


def timeframe_line(cfg) -> str:
    """v2.14.0: exec TF · the two confirm TFs · the gate."""
    from aureon_mt5.common import tf_synth
    ex = str(getattr(cfg, "exec_tf", "M5")).upper()
    try:
        conf = list(getattr(cfg, "htf_confirm_tfs", ()) or ()) or list(tf_synth.confirm_tfs(ex))
    except ValueError:
        conf = []
    return f"exec {ex} · confirm {', '.join(conf) or '—'} · HTF gate {getattr(cfg, 'htf_align', 'off')}"


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
            {"name": "Timeframe", "value": timeframe_line(cfg), "inline": True},
            {"name": "Filling", "value": filling_line(cfg), "inline": False},
            {"name": "Self-restart", "value": f"self_restart_mode={_selfheal_mode()}", "inline": True},
            {"name": "Guardian", "value": "🟢 ON" if guardian_on else "⚪ OFF", "inline": True},
            {"name": "MT5", "value": ("🟢 " if mt5_state in ("CONNECTED", "DRY") else "🔴 ") + mt5_state, "inline": True},
            {"name": "Server", "value": f"{srv:%a %d %b %H:%M} · UTC{cfg.server_utc_offset:+g}", "inline": True},
            {"name": "IST", "value": f"{datetime.now(IST):%H:%M}", "inline": True},
            {"name": "News", "value": news_line, "inline": False},
            {"name": "Rules", "value": S.describe(), "inline": False},
            {"name": "Features", "value": features_field(cfg), "inline": False},
        ],
        footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST",
    )


def _selfheal_mode() -> str:
    """v2.16.2: exit75 under the launcher / logon task (AUREON_SUPERVISED=1), else in-process gateway rebuilds."""
    from aureon_mt5.selfheal import self_restart_mode
    return self_restart_mode()


def filling_line(cfg, supported_fn=None) -> str:
    """v2.16.1 boot card: AUREON_FILLING and whether each symbol accepts it (symbol_info.filling_mode)."""
    mode = broker.FILLING["mode"]
    if cfg.dry:
        return f"{mode} (dry)"
    out = []
    for s in cfg.symbols:
        sup = (supported_fn or broker.filling_supported)(s)
        if sup is None:
            out.append(f"{s} ?")
        elif not sup.get(mode):
            ok = ", ".join(k for k in broker.FILL_ORDER if sup.get(k)) or "none"
            out.append(f"⚠️ {s} does not accept {mode} (accepts {ok}) — orders fall back on 10030")
    return mode + ("" if not out else " · " + " · ".join(out))


def features_field(cfg) -> str:
    """v2.10.0: one line per feature group; `*` = set from Discord (differs from .env)."""
    try:
        from aureon_mt5 import features
        return "\n".join(features.summary_compact(cfg, features.load_overrides(cfg.log_dir)))[:1024] + "\n(* = set from Discord)"
    except Exception as e:
        return f"unavailable: {e!r}"[:200]


def supervise(agents: dict, factory, notify, mode: str) -> list[str]:
    """Restart ONLY dead agents; the others are never touched. Returns the symbols restarted."""
    restarted = []
    from aureon_mt5.crashes import should_restart, reason
    for sym, ag in list(agents.items()):
        if not ag.is_alive() and should_restart(sym):
            why = reason(sym)
            agents[sym] = factory(sym); agents[sym].start(); restarted.append(sym)
            notify.send(f"{mode}:{sym}:RESTART:{int(_time.time()) // 60}", f"{sym} · MT5 · AGENT RESTARTED", [f"reason: {why or 'thread ended'}", "other symbols unaffected"])
    return restarted


def claude_review_tick(claude, cfg, review_day, now: datetime | None = None):
    """23:00 IST (cfg.daily_report_ist): queue the day's CLAUDE REVIEW (or, v2.3.0, CODEX REVIEW) once per IST day.
    Runs on the advisor's worker, never here."""
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


def compare_saturday_tick(cfg, agents, journal, notify, claude=None, now: datetime | None = None, gather=None, codex=None) -> str | None:
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
        display = next(iter(agents.values())).S.display_name if agents else cfg.mode
        for adv in (claude, codex):                              # v2.0.0 CLAUDE / v2.3.0 CODEX RULE PROPOSALS (no call, no edit)
            if adv is None:
                continue
            src = getattr(adv, "SOURCE", "claude")
            try:
                compare.rule_proposals_tick(cfg, journal, notify, res, week, display, source=src)
            except Exception as e:
                telemetry.info(f"{src} rule proposals failed: {e!r}")
    except Exception as e:                                   # back off 30 min instead of re-grading every 30 s
        _COMPARE_RETRY_AT[week] = _time.time() + 1800
        telemetry.failure(notify, journal, title="AUREON COMPARE FAILED", key=f"{cfg.mode}:ALL:COMPARE_ERROR:{week}",
                          mode=cfg.mode, action="weekly compare", exc=e, retry="retry in 30 min")
        return None
    import threading
    if cfg.compare_claude_review and claude is not None:
        threading.Thread(target=compare.self_review, args=(claude, res, notify, week), daemon=True, name="compare-self-review").start()
    if codex is not None:                                        # v2.3.0: Saturday CODEX REVIEW (its wrong calls, 5 lines)
        threading.Thread(target=compare.self_review, args=(codex, res, notify, week), daemon=True, name="compare-codex-review").start()
    return week


_CLAUDE_REPORT_RUNNING: set[str] = set()


def claude_report_tick(cfg, agents, journal, notify, now=None):
    """Non-blocking, restart-safe weekly/monthly report scheduling."""
    import threading
    from aureon_mt5 import claude_report
    now = now or datetime.now(IST)
    if (now.hour, now.minute) < (10, 0):
        return
    periods = []
    # Weekly report follows COMPARE on Saturday after 10:00 IST.
    if now.weekday() == 5:
        from aureon_mt5.compare import read_history, week_window
        week_key, _, _ = week_window(now)
        if any(row.get("week") == week_key for row in read_history(cfg.log_dir)):
            periods.append(("week", week_key))
    if now.day == 1:
        previous = now.replace(day=1) - timedelta(days=1)
        month_key = previous.strftime("%Y-%m")
        records = journal.read(int((previous - timedelta(days=32)).timestamp()))
        earliest = min((r["t"] for r in records), default=int(now.timestamp()))
        if int(now.timestamp()) - earliest >= 21 * 86400:
            periods.append(("month", month_key))
    posted = {(r.get("period"), r.get("key")) for r in claude_report.read_history(cfg.log_dir)}
    for period, key in periods:
        token = f"{period}:{key}"
        if (period, key) in posted or token in _CLAUDE_REPORT_RUNNING:
            continue
        _CLAUDE_REPORT_RUNNING.add(token)
        def post(period=period, key=key, token=token):
            try:
                r = claude_report.advisor_report(cfg, agents, journal, period, now=now)      # v2.3.0: Claude + Codex + head-to-head
                notify.card(r["title"], r["description"], fields=r["fields"][:25], footer=f"v{VERSION} · {key}")
                claude_report.append_history(cfg.log_dir, period, key)
            except Exception as e:
                telemetry.failure(notify, journal, title="AUREON ADVISOR REPORT FAILED",
                                  key=f"claude-report:{token}", action="scheduled report", exc=e)
            finally:
                _CLAUDE_REPORT_RUNNING.discard(token)
        threading.Thread(target=post, daemon=True, name=f"claude-report-{token}").start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode"); ap.add_argument("--symbols"); ap.add_argument("--dry", action="store_true")
    ap.add_argument("--webhook"); ap.add_argument("--token"); ap.add_argument("--enable-silver", action="store_true")
    a = ap.parse_args()
    cfg = Config()
    from aureon_mt5 import features as _features                     # v2.10.0: Discord overrides (logs/features.json) win over .env
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
    from aureon_mt5 import netguard                                    # v2.11.2: network state + exit_reason go here
    netguard.configure(journal=journal, log_dir=cfg.log_dir)
    feature_overrides = _features.load_overrides(cfg.log_dir)
    if feature_overrides:
        _features.apply(cfg, feature_overrides)
        print("feature overrides (Discord): " + ", ".join(f"{k}={v}" for k, v in feature_overrides.items()))
    from aureon_mt5.common import tf_synth                             # v2.14.0: the execution timeframe, process-wide
    tf_synth.set_exec_tf(cfg.exec_tf, cfg)                             # after the overrides: /decide-timeframe writes exec_tf there
    broker.configure_filling(cfg.filling)                             # v2.16.1: AUREON_FILLING
    broker.SINK["log"] = journal.log                                  # v2.16.2: sl_clamped / guardian_failure rows
    broker.SINK["alert"] = lambda title, body: health_notify.card(title, body, color=0xE74C3C, footer="Aureon MT5 · broker")
    journal.log("process_start", version=VERSION, mode=cfg.mode, exec_tf=tf_synth.exec_tf(), htf_align=cfg.htf_align,
                symbols=list(cfg.symbols), self_restart_mode=_selfheal_mode())
    health_notify = Notifier(cfg.health_webhook) if cfg.health_webhook else notify
    # v2.10.1 BOOT CHECK: unparsable / out-of-range / contradictory settings fall back to the safe default, loudly
    from aureon_mt5.config import apply_fallbacks, validate as validate_cfg
    boot_problems = validate_cfg(cfg)
    if boot_problems:
        fixed = apply_fallbacks(cfg, [p for p in boot_problems if p.get("group") in ("guardian", "regime", "claude", "server", "repair",
                                                                                  "context", "plan", "notify")])
        journal.log("boot_check", problems=boot_problems, fallbacks=fixed)
        health_notify.card("AUREON · MT5 · BOOT CHECK", "\n".join(f"⚠️ `{p['env']}`={p['value'] or '∅'} — {p['issue']} → {p['fallback']}"
                                                                for p in boot_problems)[:3900],
                           footer="Aureon MT5 · /env-check shows this any time")
    # v2.11.0: mentions / asks channel / push from the first card on (after the BOOT CHECK fallbacks)
    from aureon_mt5.push import Push
    push = Push(cfg, journal)
    push.on_pause = lambda b, err: health_notify.card(f"AUREON · MT5 · PUSH PAUSED · {b}",
                                                      f"{b} failed 5× in a row ({err or 'HTTP error'}) — paused 10 min; Discord unaffected",
                                                      footer="Aureon MT5 · netguard")
    notify.configure(cfg, push=push)
    if health_notify is not notify:
        health_notify.configure(cfg, push=push)
    def process_excepthook(typ, value, tb):
        import traceback
        trace = "\n".join("".join(traceback.format_exception(typ, value, tb)).splitlines()[-15:])
        try:
            journal.log("process_exit", reason=f"{typ.__name__}: {value}", trace=trace)
            health_notify.card("AUREON · MT5 · PROCESS EXITING",
                               f"{typ.__name__}: {str(value)[:180]}\n```\n{trace[-1500:]}\n```")
        except Exception:
            pass
        sys.__excepthook__(typ, value, tb)
    sys.excepthook = process_excepthook
    previous_thread_hook = threading.excepthook
    def agent_thread_hook(args):
        if isinstance(args.thread, SymbolAgent):
            try:
                from aureon_mt5.crashes import report
                report(args.thread, args.exc_value)
            except Exception:
                pass
        else:
            previous_thread_hook(args)
    threading.excepthook = agent_thread_hook

    preflight_res = None; prev_heartbeat = None
    if not cfg.dry:
        from aureon_mt5 import preflight, server as _server           # v2.15.0: pre-flight (terminal, MT5 + login, heartbeat)
        prev_heartbeat = _server.last_heartbeat(cfg.log_dir)           # before the pre-flight touches heartbeat.txt
        preflight_res = preflight.run(cfg, broker, health_notify)
        if not preflight_res["ok"]:
            print("MT5 not available — is the terminal open and logged in? (use --dry for a pipe test)")
            from aureon_mt5 import ops as _o
            sys.exit(_o.EXIT_RESTART)                                 # the launcher restarts us (and MT5 first)
        try:
            _server.remember_mt5_path(cfg.log_dir, broker.mt5().terminal_info())
        except Exception:
            pass
        cfg.server_utc_offset = broker.server_offset_hours(cfg.symbols[0], cfg.server_utc_offset)
    seed_news_file(cfg.news_file)
    news = load_news(cfg.news_file if cfg.news_file and os.path.exists(cfg.news_file) else None, date.today().year)
    news_line, unverified = news_status(cfg.news_file)

    agents: dict[str, SymbolAgent] = {}; started = _time.time()
    netguard.configure(positions=lambda: sum(len((getattr(ag, "snapshot", None) or {}).get("positions") or [])
                                             for ag in list(agents.values())))
    alerts = AlertStore(cfg.log_dir)                                                   # v2.0.0 /alert store, shared by agents + bot
    claude = ClaudeAdvisor.create(cfg, notify, journal, health_notify=health_notify)     # None when AUREON_CLAUDE=off
    if claude is not None:
        print(f"Claude add-on: {claude.mode} · bin {cfg.claude_bin} · workdir {cfg.claude_workdir} · {cfg.claude_max_calls} calls/day")
        if os.environ.get("ANTHROPIC_API_KEY"):
            health_notify.card("AUREON · MT5 · CLAUDE · API KEY SET",
                               "`ANTHROPIC_API_KEY` is set in this environment. Aureon removes it from the `claude -p` call so your "
                               "subscription login is used — remove it from the environment to be sure nothing bills the API.",
                               footer="Aureon MT5 · Claude")
    codex = CodexAdvisor.create(cfg, notify, journal, health_notify=health_notify)       # None when AUREON_CODEX=off
    if codex is not None:
        print(f"Codex add-on: {codex.mode} (shown only, never applied) · bin {cfg.codex_bin} · workdir {cfg.codex_workdir} · "
              f"{cfg.codex_max_calls} calls/day")
        if any(os.environ.get(k) for k in ("OPENAI_API_KEY", "CODEX_API_KEY")):
            health_notify.card("AUREON · MT5 · CODEX · API KEY SET",
                               "`OPENAI_API_KEY` / `CODEX_API_KEY` is set in this environment. Aureon removes it from the `codex exec` call so "
                               "your CLI login is used — remove it from the environment to be sure nothing bills the API.",
                               footer="Aureon MT5 · Codex")

    def make_agent(sym):
        return SymbolAgent(sym, cfg, S, notify, journal, news, claude=claude, alerts=alerts, codex=codex)

    def start_agents():
        from aureon_mt5.crashes import should_restart
        for s in cfg.symbols:
            if not should_restart(s):
                continue
            if s not in agents or not agents[s].is_alive():
                agents[s] = make_agent(s); agents[s].start()

    why = "online"
    try:
        last_msg = subprocess.run(["git", "log", "-1", "--pretty=%s"], capture_output=True, text=True, timeout=10).stdout.strip()
        last_hash = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        from aureon_mt5 import ops as _ops
        marker = _ops.pop_restart_marker(cfg.log_dir) or {}          # v2.7.2: written before exit(75)
        from aureon_mt5 import server as _srv
        down = _srv.down_text(marker)                                    # v2.10.1: every BACK ONLINE says how long we were away
        last_exit = netguard.pop_exit_marker(cfg.log_dir)                 # v2.11.2: why the previous process ended
        if not marker and not os.environ.get("AUREON_RESTART_REASON") and not os.environ.get("AUREON_AUTO_RESTARTED"):
            why = "cold start"
            _srv.cold_start(cfg, journal, health_notify,
                            {s_: (broker.positions(s_) if not cfg.dry else []) for s_ in cfg.symbols}, last_exit=last_exit,
                            heartbeat=prev_heartbeat)
        if marker.get("kind") in ("maintenance", "repair-auto", "repair-rollback"):
            why = f"restarted · {marker['kind']}"
            text = {"maintenance": f"maintenance reboot · {marker.get('reason', '')}",
                    "repair-auto": f"auto-applied repair · {marker.get('reason', '')} — verifying",
                    "repair-rollback": f"repair rolled back · {marker.get('reason', '')}"}[marker["kind"]]
            health_notify.card("AUREON · MT5 · BACK ONLINE", f"{text}\n{down} · running `{last_hash}`", footer="Aureon MT5 · unattended")
        manual_reason = os.environ.pop("AUREON_RESTART_REASON", "") or (
            f"manual:{marker.get('reason', '')}" if marker.get("kind") == "manual" else "")
        if marker.get("kind") == "self-heal":
            why = f"restarted · self-heal · {marker.get('reason', '')}"
            health_notify.card("AUREON · MT5 · BACK ONLINE", f"Recovered from a hang by self-restart.\n{marker.get('reason', '')}\n"
                               f"{down} · running `{last_hash}` · {S.display_name}", footer="Aureon MT5 · self-heal")
        if manual_reason:
            why = f"restarted · {manual_reason}"
            notify.card("AUREON · MT5 · BACK ONLINE", f"{manual_reason}\n{down} · running `{last_hash}` · {S.display_name}")
        if os.environ.get("AUREON_AUTO_RESTARTED") or marker.get("kind") == "auto-update":
            why = f"restarted · auto-update · {last_hash}"
            notify.card("AUREON · MT5 · BACK ONLINE", f"Auto-update restart complete · {down}.\nRunning `{last_hash}` · {last_msg}",
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
    if preflight_res is not None:                                  # v2.15.0 pre-flight steps 3–4
        from aureon_mt5 import preflight as _pf
        closed = not broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry)
        if closed:      # never exit on a closed market: the agents sleep and keep the position snapshot fresh (the
            _pf.log("market closed — agents sleeping until the next open")     # /mode and restart gates read it)
        _pf.back_online(health_notify, preflight_res, {s_: broker.positions(s_) for s_ in cfg.symbols}, closed=closed)
    monitor = None
    bot_repair: dict | None = {}                                   # filled below; the bot reads it lazily
    # v2.11.0 notifications: mentions + asks channel (notify), the push layer, the action console (one shared actions path)
    from aureon_mt5.actions import Actions
    actions = Actions(cfg, agents, journal, notify, claude=claude, alerts=alerts)
    push.handler = actions.run
    push.start()
    if push.backends():
        print("push: " + ", ".join(push.backends()))
    console_srv = None
    if cfg.console_token:
        from aureon_mt5 import console as _console
        console_srv = _console.ConsoleServer(cfg, _console.create_app(
            cfg, actions, journal, tokens=push.tokens, chart_dir=os.path.join(cfg.log_dir, "charts"),
            state_fn=lambda: _console.console_state(cfg, agents, actions, notify, started=started)))
        why = console_srv.start()
        print(f"console: {why or 'listening on ' + cfg.console_bind}")
        journal.log("console_start", bind=cfg.console_bind, ok=not why, why=why or None)
    import atexit
    atexit.register(lambda: (console_srv.stop() if console_srv else None, push.stop()))
    if cfg.bot_token: monitor = run_bot(cfg, agents, journal, notify, started, health_notify=health_notify, claude=claude, alerts=alerts, codex=codex,
                                        repair=bot_repair, actions=actions)
    else: print("no DISCORD_TOKEN — slash commands off, alerts via webhook only")

    # v2.7.2: self-heal + dead-man's switch + logs/heartbeat.txt, on its own thread (never the Discord loop)
    from aureon_mt5 import ops
    from aureon_mt5.selfheal import HealthGuard
    restart_exit = lambda code: ops.hard_exit(code, agents)
    guard = HealthGuard(monitor=monitor, agents=agents, log_dir=cfg.log_dir, journal=journal, notify=health_notify,
                        deadman_url=cfg.deadman_url, stale_bar_s=cfg.heartbeat_stale_min * 60, exit_hook=restart_exit,
                        bot_expected=monitor is not None)
    guard.start()
    if cfg.deadman_url:
        print("dead-man's switch: pinging AUREON_DEADMAN_URL every 60 s while healthy")

    # v2.10.0 repair agent: detect on the health tick; propose on the Claude worker; a human applies (/repair-apply)
    from aureon_mt5 import repair as _repair
    repairer = _repair.Repairer(cfg, journal, health_notify)
    def _auto_propose(inc):
        if claude is not None:
            claude._enqueue({"type": "repair", "event": "repair", "symbol": "ALL", "n": inc["id"], "repairer": repairer,
                             "after": lambda res, n=inc["id"]: _repair.maybe_auto_apply(repairer, n, agents,
                                                                                       restart=lambda *a, **k: ops.restart_process(*a, **k, exit_hook=restart_exit))})
    guard.detector = _repair.Detector(cfg, journal, health_notify, agents, on_new=_auto_propose)
    guard.detector.verifier = _repair.Verifier(cfg, journal, health_notify, health_ok=lambda: not guard.last_failures, agents=agents,
                                               restart=lambda *a, **k: ops.restart_process(*a, **k, exit_hook=restart_exit))
    if bot_repair is not None:
        bot_repair["repairer"] = repairer

    from aureon_mt5.watchdog import Watchdog
    wd = Watchdog(health_notify or notify, journal, cfg, agents)
    wd.monitor = monitor

    # v2.10.1 the box belongs to Aureon: maintenance window (reboots, code updates, housekeeping), MT5 manager, clock, network,
    # evidence-gated tuning. All OS calls go through server.run_ps; every action is journaled and carded.
    from aureon_mt5 import server as srv
    from aureon_mt5.tuning_auto import TuningAuto
    maint = srv.Maintenance(cfg, journal, health_notify, agents, exit_hook=restart_exit)
    mt5_mgr = srv.Mt5Manager(cfg, journal, health_notify, broker)
    netguard.MONITOR = monitor

    def on_net_flip(degraded_now):
        def work():
            res = netguard.dns_check(netguard.dns_hosts(cfg))
            journal.log("dns_check", result={k: v["ok"] for k, v in res.items()})
            if degraded_now:
                health_notify.card("AUREON · MT5 · NETWORK DEGRADED", "; ".join(netguard.STATE["reasons"]) +
                                   f"\n{netguard.dns_line(res)}\npolling and the guardian continue on the existing stops · no new "
                                   "entries · trade cards queue (logs/card_queue.jsonl)", footer="Aureon MT5 · netguard")
            else:
                health_notify.card("AUREON · MT5 · NETWORK RESTORED", netguard.dns_line(res), footer="Aureon MT5 · netguard")
        threading.Thread(target=work, daemon=True, name="aureon-netflip").start()
    tuner = TuningAuto(cfg, journal, health_notify, advisors=[claude] if claude is not None else [])
    srv.check_clock(journal, health_notify); last_clock = _time.time()
    was_in_session = srv.in_session(cfg, _time.time())

    was_open = broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry); report_day = None; last_update_check = 0
    review_day = None; codex_review_day = None
    while True:
        try:
            now = _time.time()
            # v2.10.1: code updates moved into the maintenance window (flat, outside the session, suite green)
            maint.tick(now)
            mt5_mgr.tick(now)
            if now - last_clock >= 6 * 3600:
                last_clock = now; srv.check_clock(journal, health_notify)
            # v2.11.2: network state (gateway > 60 s / 2 webhook failures / DNS) and the card queue, every 30 s, independent
            # of the gateway. Degraded = no new entries; the guardian and the poll loop never notice.
            netguard.update(now, journal=journal, on_flip=on_net_flip)
            if netguard.degraded() and now - (netguard.STATE.get("dns_t") or 0) >= 300:
                threading.Thread(target=netguard.dns_check, args=(netguard.dns_hosts(cfg),), daemon=True, name="aureon-dns").start()
            notify.flush()
            if health_notify is not notify:
                health_notify.flush()
            try:
                import shutil as _sh
                srv.STATE["disk_free_gb"] = _sh.disk_usage(cfg.log_dir).free / 1e9
                ages = [ag.snapshot.get("tick_age") for ag in agents.values() if getattr(ag, "snapshot", None)]
                srv.STATE["mt5_age"] = max([a for a in ages if a is not None], default=None)
            except Exception:
                pass
            in_sess = srv.in_session(cfg, now)
            tuner.tick(session_ended=was_in_session and not in_sess)
            was_in_session = in_sess
            is_open = broker.market_open(cfg.symbols[0], cfg.server_utc_offset, cfg.dry)
            if was_open and not is_open:
                notify.send(f"{cfg.mode}:ALL:MARKET:closed-{date.today()}", "AUREON · MT5 · MARKET CLOSED", ["agents sleeping"], footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")
                if report_day != date.today():
                    notify.raw(weekly_report(cfg, agents, journal, previous=False)); report_day = date.today()
            if not was_open and is_open:
                greet(notify, cfg, S, "market open — Aureon resumed", news_line); start_agents()
            was_open = is_open
            review_day = claude_review_tick(claude, cfg, review_day)
            codex_review_day = claude_review_tick(codex, cfg, codex_review_day)        # v2.3.0: CODEX REVIEW, same 23:00 IST
            compare_saturday_tick(cfg, agents, journal, notify, claude, codex=codex)
            claude_report_tick(cfg, agents, journal, notify)
            supervise(agents, make_agent, notify, cfg.mode)
            wd.tick()
            from aureon_mt5.daily_plan import tick as plan_tick
            plan_tick(cfg, agents, journal, notify)
            from aureon_mt5.eod_review import tick as eod_tick
            eod_tick(cfg, journal, notify)
            # v2.6.0 nightly review (deterministic, after EOD)
            from aureon_mt5 import review as _review
            _review_key = f"review:{datetime.now(IST).strftime('%Y-%m-%d')}"
            if _review_key not in notify.sent and datetime.now(IST).hour >= 23 and datetime.now(IST).minute >= 20:
                try:
                    res = _review.compute(journal, cfg.server_utc_offset)
                    c = _review.card(res)
                    notify.send(_review_key, c["title"], [c["description"]], fields=c["fields"], color=0x95A5A6, footer=c["footer"])
                    _review.save(cfg.log_dir, res, datetime.now(IST).strftime("%Y-%m-%d"))
                except Exception:
                    pass
            from aureon_mt5.control import check_auto_transitions
            from aureon_mt5.schedule import tick as schedule_tick
            for _sym in cfg.symbols:
                try:
                    check_auto_transitions(journal, notify, _sym, agents, cfg)
                    schedule_tick(cfg.log_dir, journal, notify, _sym, agents, cfg)
                except Exception:
                    pass
        except KeyboardInterrupt:
            from aureon_mt5 import ops as _o
            _o.STOP.set()
            try:
                notify.card("AUREON · MT5 · PROCESS EXITING", "KeyboardInterrupt")
                broker.close_connection()
            except Exception:
                pass
            print("bye"); raise
        except Exception as e:
            telemetry.failure(notify, journal, title="AUREON SUPERVISOR ERROR", key="supervisor", mode=cfg.mode, action="supervisor", exc=e)
        _time.sleep(30)


class _Signal(BaseException):
    def __init__(self, signum):
        super().__init__(signum); self.signum = signum


def run() -> int:
    """v2.11.2: every way out of the process records process_exit {reason, exc_type, where, uptime_s, positions_open}
    (journal + restart_marker.json or exit_marker.json). Planned exits → 0 (or the restart code 75); anything else → 1, so
    the service restarts us. (os._exit paths — /restart, self-heal — record in ops.hard_exit.)"""
    import signal
    from aureon_mt5 import netguard, ops

    def _sig(signum, frame):
        raise _Signal(signum)
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _sig)
            except (ValueError, OSError):
                pass
    try:
        main()
        netguard.record_exit("normal stop", planned=True, code=0, where="main returned")
        return 0
    except KeyboardInterrupt as e:
        ops.STOP.set()
        netguard.record_exit("SIGINT (Ctrl-C)", exc=e, planned=True, code=0)
        return 0
    except _Signal as e:
        ops.STOP.set()
        name = {getattr(signal, n, None): n for n in ("SIGTERM", "SIGBREAK")}.get(e.signum, str(e.signum))
        netguard.record_exit(f"{name} (service stop)", exc=e, planned=True, code=0)
        return 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        netguard.record_exit(f"SystemExit({code})", exc=e, planned=code in (0, ops.EXIT_RESTART), code=code)
        return code
    except BaseException as e:                                # unplanned: non-zero so the service restarts us
        import traceback
        traceback.print_exc()
        netguard.record_exit(f"unhandled {type(e).__name__}: {str(e)[:200]}", exc=e, planned=False, code=1)
        return 1


if __name__ == "__main__":
    sys.exit(run())
