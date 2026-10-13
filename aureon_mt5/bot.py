"""Discord slash commands (needs DISCORD_TOKEN).

The command UI uses compact native Discord embeds/cards.  Webhook alerts use the
same visual language through notify.py.  Discord is presentation-only and never
controls guardian execution.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import broker, telemetry
from . import alerts as alerts_mod
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


def status_card(sym: str, ag, cfg, journal=None, monitor=None, claude=None, codex=None) -> dict:
    h = ag.health; S = ag.S; snap = _snap(ag)
    mk = "OPEN" if snap.get("market_open") else ("CLOSED" if snap.get("at") else "—")
    age = snap.get("tick_age"); age_s = f"{age}s" if age is not None else "—"
    today = snap.get("today") or {"signals": 0, "closed": 0, "errors": 0, "secured": 0}
    connected = bool(snap.get("connected"))
    color = GREEN if ag.is_alive() and connected and h.get("status") in ("running", "market closed") else YELLOW if ag.is_alive() else RED

    from .halt import load as _load_halt
    _halt = _load_halt(cfg.log_dir)
    if _halt:
        color = RED

    fields = [
        _field("Mode", S.display_name),
        _field("Market", f"{'🟢' if mk == 'OPEN' else '⏸'} {mk} · tick {age_s}"),
        _field("MT5", {"CONNECTED": "🟢 CONNECTED", "DRY": "🧪 DRY"}.get(snap.get("mt5"), "🔴 DISCONNECTED")),
        _field("Agent", f"{'🟢' if ag.is_alive() else '🔴'} {h['status'].upper()}"),
        _field("Guardian", h.get("guardian") or "—"),
        _field("Last bar", f"{h.get('last_bar') or '—'} IST"),
    ]
    if _halt:
        fields.insert(0, _field("HALT", f"⛔ HALTED since {_halt.get('ist', '?')} IST (by {_halt.get('by', '?')}: {_halt.get('reason', '')})", False))
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
    ]
    if h.get("chop"):
        fields.append(_field("Chop", f"🌀 {h['chop']}"))
    if cfg.execution_enabled:
        demo_s = "demo" if (broker.is_demo() if not cfg.dry else None) else ("live" if cfg.allow_live else "live (blocked)")
        fields.append(_field("Execution", f"on · buttons {'/'.join(f'{v:g}' for v in cfg.lot_buttons)} · max {cfg.max_lots:g} · {demo_s}"))
    else:
        fields.append(_field("Execution", "off"))
    try:
        from .control import mode_label
        fields.append(_field("Control", mode_label(journal, sym) if journal else "—"))
    except Exception:
        pass
    try:
        from .governor import Governor
        _gov = Governor(cfg)
        _gov_ctx = {"trades_today": 0, "realised_money_today": 0, "equity": 0}  # lightweight default for /status
        fields.append(_field("Governor", _gov.status(_gov_ctx)))
    except Exception:
        pass
    fields += [
        _field("Claude", claude_status_word(ag, claude)),                          # v2.0.1
        _field("Codex", claude_status_word(ag, codex)),                            # v2.3.0 (shown only, never applied)
        _field("Bot", _bot_line(monitor), False),
    ]
    return _embed(f"{sym} · MT5 · STATUS", f"AUREON MT5 v{VERSION} · {S.display_name} · {age_text(ag)}", color=color, fields=fields,
                  footer=f"{S.display_name} · {datetime.now(IST):%H:%M} IST")


def watchdog_status(wd) -> str:
    """One-line watchdog status for /status."""
    if wd is None:
        return "off"
    inc = wd.open_incidents()
    nxt = wd.next_heartbeat_ist()
    if inc:
        return f"on · next heartbeat {nxt} · open incidents: {', '.join(i['key'] + ' ' + str(i['duration_min']) + 'm' for i in inc)}"
    return f"on · next heartbeat {nxt} · open incidents: none"


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
        chop_tag = f" · 🌀 CHOP" if h.get("chop") else ""
        try:
            from .control import mode_label as _ml
            mode_tag = f" · {_ml(journal, sym)}" if journal else ""
        except Exception:
            mode_tag = ""
        value = (f"{icon} **{h['status'].upper()}** · {ag.S.display_name} · {age_text(ag)}{chop_tag}{mode_tag}\n"
                 f"Price `{px}` · {ptxt}\n"
                 f"Signal `{h.get('last_signal') or '—'}` · Errors `{h.get('errors', 0)}`")
        fields.append(_field(sym, value, False))
    return _embed("AUREON · MT5 · PARALLEL STATUS", f"v{VERSION} · {len(agents)} symbol(s)", color=GREEN if all_ok else YELLOW, fields=fields)


def command_directory(tree, descriptions):
    """Return grouped, rendered entries for *every* registered root command."""
    actual = {c.name: c for c in tree.get_commands()}
    seen = set()
    result = {}
    for group, entries in descriptions.items():
        lines = []
        for name, explanation in entries.items():
            if name in actual:
                lines.append(f"/{name} — {explanation}")
                seen.add(name)
        if lines: result[group] = lines
    leftovers = sorted(set(actual) - seen)
    if leftovers:
        result["Other installed"] = [
            f"/{name} — {getattr(actual[name], 'description', 'Registered command')}"
            for name in leftovers
        ]
    return result


def agents_card(agents, cfg, started_at, monitor=None, claude=None, journal=None) -> dict:
    """One operational block per running agent, using cached data only."""
    from .common.timeutil import ist_str, server_str
    fields = []
    midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    records = journal.read(int(midnight.timestamp())) if journal is not None else []
    for sym, ag in agents.items():
        snap = _snap(ag)
        h = ag.health
        pos = snap.get("positions") or []
        state = snap.get("state") or {}
        if pos:
            p = pos[0]
            st = state.get(p.get("ticket"), state.get(str(p.get("ticket")), {}))
            phase = f"in trade: {p.get('direction','?')} · {float(p.get('points',0)):+.1f}pts · SL {p.get('sl') or '—'} · secured {st.get('secured',0)}"
        else:
            r = getattr(ag, "last_res", None) or {}
            bars = r.get("bar_states") or []
            confirm = bars[-1].get("confirm") if bars else None
            phase = ("confirmed, waiting pullback" if getattr(ag, "last_confirmed_cross", None) or str(confirm).startswith("confirmed") else
                     f"cross seen, confirming {confirm}" if confirm else "waiting cross")
        t = ag.last_bar
        off = float(getattr(ag, "off", cfg.server_utc_offset))
        bar = f"{server_str(t)} server / {ist_str(t, off)} IST" if t else "—"
        day = snap.get("today") or {}
        bars_today = 0
        crosses_today = 0
        df = getattr(ag, "last_df", None)
        if df is not None and not df.empty:
            start_srv = int(midnight.timestamp() + off * 3600)
            recent = df[df["time"] >= start_srv]
            bars_today = len(recent)
            if bars_today >= 2:
                fast, slow = ag.S.ema_cols
                delta = recent[fast].to_numpy() - recent[slow].to_numpy()
                crosses_today = sum(delta[i-1] * delta[i] < 0 for i in range(1, len(delta)))
        own = [r for r in records if r.get("symbol") == sym]
        trade_ids = {str(r.get("ticket")) for r in own if r.get("ticket") is not None
                     and r.get("event") in ("position_seen", "exit", "closed")}
        pnl_by_ticket = {}
        for rec in own:
            if rec.get("event") == "exit" and rec.get("points") is not None:
                pnl_by_ticket[str(rec.get("ticket"))] = float(rec["points"])
            if rec.get("event") == "closed" and rec.get("final_points") is not None:
                pnl_by_ticket[str(rec.get("ticket"))] = float(rec["final_points"])
        pnl = sum(pnl_by_ticket.values())
        ai = claude.attachment(ag).get("state", "OFF") if claude else "OFF"
        from .crashes import reason as last_crash_reason
        err = last_crash_reason(sym) or str(h.get("status", ""))
        if "error" not in err.lower(): err = "—"
        lines = [
            f"Phase **{phase}**",
            f"Last bar {bar}",
            f"Bars today **{bars_today}** · crosses **{crosses_today}**",
            f"Trades **{len(trade_ids)}** · pnl pts **{pnl:+.1f}**",
            f"Guardian **{'ON' if ag.g else 'OFF'}** · Claude **{ai}**" + (f" · CHOP" if h.get("chop") else ""),
            f"Thread **{'alive' if ag.is_alive() else 'DEAD'}** · uptime **{int((_time.time()-getattr(ag, 'started_wall', started_at))//60)}m**",
            f"Last error {err[:150]}",
        ]
        fields.append(_field(f"{sym} · {ag.S.display_name}", "\n".join(lines)[:1024], False))
    try:
        import subprocess
        git_hash = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=1).stdout.strip() or "—"
    except Exception: git_hash = "—"
    market = "OPEN" if any(_snap(a).get("market_open") for a in agents.values()) else "CLOSED"
    return _embed("AUREON · MT5 · AGENTS", f"{len(agents)} agent(s)", color=BLUE,
                  fields=fields[:25], footer=f"v{VERSION} · {git_hash} · market {market}")


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


def claude_attachment_line(sym: str, ag, claude) -> str:
    """One line per agent: 'XAUUSD · EMA 20/50 · ATTACHED (advisory) · triggers: … · last verdict 11:40 IST TAKE'."""
    if claude is None:
        return f"{sym} · {ag.S.display_name} · DETACHED (mode=off)"
    a = claude.attachment(ag)
    line = f"{sym} · {ag.S.display_name} · {a['label']}"
    if a["state"] in ("ATTACHED", "REVIEW"):
        line += f" · triggers: {a['triggers']}"
    lv = claude.status().get("last_verdict_by_symbol", {}).get(sym)
    if lv and a["state"] == "ATTACHED":
        line += f" · last verdict {lv}"
    return line


def claude_status_word(ag, claude) -> str:
    """/status one-liner: ATTACHED (mode) | REVIEW | OFF | NOT ATTACHED | DETACHED."""
    if claude is None:
        return "OFF"
    st = claude.attachment(ag)["state"]
    return {"ATTACHED": f"ATTACHED ({claude.mode})", "REVIEW": "REVIEW (cards only)", "NOT_ATTACHED": "NOT ATTACHED", "DETACHED": "DETACHED"}.get(st, st)


ADVISOR_INFO = {
    "claude": {"label": "Claude", "env": "AUREON_CLAUDE", "bin_env": "AUREON_CLAUDE_BIN", "cli": "Claude Code",
               "login": "run `claude` in the work folder and log in, then `/claude-test`",
               "desc": "second opinion only — Claude never places a trade · TIGHTEN / CLOSE-in-profit applied only in manage · "
                       "`/claude-attach <symbol>` · `/claude-detach <symbol>`"},
    "codex": {"label": "Codex", "env": "AUREON_CODEX", "bin_env": "AUREON_CODEX_BIN", "cli": "Codex CLI",
              "login": "run `codex login` (CLI sign-in, no API key), then `/codex-test`",
              "desc": "second opinion only — Codex verdicts are journaled and shown, **never applied** (review and advisory) · "
                      "`/codex-attach <symbol>` · `/codex-detach <symbol>`"},
}


def advisor_card(adv, cfg, agents: dict | None = None, source: str = "claude", login_line: str | None = None) -> dict:
    """v2.0.1 /claude, v2.3.0 /codex: header (mode, bin, version, login), models, budget, one ATTACHED/REVIEW/OFF line per agent."""
    agents = agents or {}
    info = ADVISOR_INFO[source]; tag = info["label"].upper()
    if adv is None:
        fields = [_field("Mode", "OFF"), _field("Attachment", "\n".join(claude_attachment_line(s, ag, None) for s, ag in agents.items()) or "no agents", False)]
        return _embed(f"AUREON · MT5 · {tag}", f"{info['label']} add-on is **off** (`{info['env']}=off`) — zero {info['label']} calls.", color=GREY, fields=fields)
    st = adv.status()
    login = st["login"]
    bad_bin = login == "NOT FOUND" or (st.get("version") is None and "not found" in (st.get("version_error") or ""))
    bad_auth = login == "FAILED"
    icon = "🔴 " if (bad_bin or bad_auth) else "🟢 " if login == "OK" else "⚪ "
    ver = st.get("version") or (f"unavailable — {st.get('version_error')}" if st.get("version_error") else f"not checked yet (/{source}-test)")
    header = f"mode **{st['mode'].upper()}** · bin `{st['bin_path']}` · {ver} · login {login}" + (f" · {login_line}" if login_line else "")
    if bad_bin:
        header += f"\n🔴 **{info['cli']} not found** — install it or set `{info['bin_env']}` to the full path, then `/{source}-test`"
    elif bad_auth:
        header += f"\n🔴 **not logged in** — {info['login']}"
    ids = st.get("model_ids", {}); rep_ = st.get("reported_models", {})
    models = " · ".join(f"{k} `{ids.get(k) or 'CLI default'}`" + (f" → reported `{rep_[k]}`" if rep_.get(k) else "") for k in ("entry", "pullback", "review"))
    ce = st.get("calls_by_event", {})
    lat_last = f"{st['last_latency']:.0f} s" if st.get("last_latency") is not None else "—"
    budget = (f"calls **{st['calls']} / {st['limit']}** today · entry {ce.get('entry', 0)} · pullback {ce.get('pullback', 0)} · alert {ce.get('alert', 0)} · "
              f"fast-exit cancellations {st.get('fast_exit_cancels', 0)} · last latency {lat_last} · last error {st['last_error'][:200]}")
    per_agent = "\n".join(claude_attachment_line(s, ag, adv) for s, ag in agents.items()) or "no agents running"
    fields = [_field("Header", (icon + header)[:1024], False), _field("Models", models[:1024], False), _field("Budget", budget[:1024], False),
              _field("Per agent", per_agent[:1024], False), _field("Last verdict", st["last_verdict"], False), _field("Queue", st["queue"])]
    return _embed(f"AUREON · MT5 · {tag}", info["desc"], color=RED if (bad_bin or bad_auth) else BLURPLE, fields=fields)


def claude_card(claude, cfg, agents: dict | None = None) -> dict:
    return advisor_card(claude, cfg, agents, "claude")


def usage_fields(log_dir: str, journal, source: str = "claude") -> list[dict]:
    """/claude-status and (v2.3.0) /codex-status: today / week / month from logs/<source>_usage.jsonl + the journal."""
    from .claude_usage import summarize
    fields = []
    now = datetime.now(IST)
    for span in ("today", "week", "month"):
        x = summarize(log_dir, span, source=source)
        start = {
            "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
            "week": (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0),
            "month": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        }[span]
        records = journal.read(int(start.timestamp()))
        stale = sum(r.get("event") == f"{source}_verdict" and bool(r.get("stale")) for r in records)
        fast_cancel = sum(r.get("event") == f"{source}_fast_exit_cancel" for r in records)
        by_kind = ", ".join(f"{k}:{v}" for k, v in sorted(x["by_kind"].items())) or "none"
        verdicts = ", ".join(f"{k}:{v}" for k, v in sorted(x["verdicts"].items())) or "none"
        val = (f"Calls {x['calls']} · ok {x['ok']} · errors {x['errors']} · timeout {x['timeouts']}\n"
               f"Kinds {by_kind}\nVerdicts {verdicts}\n"
               f"Tokens in/out {x['input_tokens']}/{x['output_tokens']} · "
               f"cache read/write {x['cache_read_input_tokens']}/{x['cache_creation_input_tokens']}\n"
               f"CLI reported cost ${x['cost_usd']:.4f} · "
               f"avg/max {x['avg_latency']:.1f}/{x['max_latency']:.1f}s · "
               f"STALE {stale} · fast-exit cancels {fast_cancel}")
        fields.append(_field(span.title(), val[:1024], False))
    return fields


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


def current_price(sym: str, ag, cfg, lock_timeout: float = 1.0) -> float | None:
    """Live tick when MT5 is free within `lock_timeout` s, else the agent's last closed-bar close (never blocks a command)."""
    if ag is not None and getattr(ag, "dry", False):
        return ag.health.get("close")
    try:
        with broker.try_lock(lock_timeout) as got:
            if got:
                t = broker.tick(sym)
                if t:
                    return (t["bid"] + t["ask"]) / 2
    except Exception:
        pass
    return ag.health.get("close") if ag is not None else None


def _governor_context(cfg, agents, sym: str, ist_now=None) -> dict:
    """Build the context dict the governor needs. Cached per call (not per bar)."""
    from datetime import datetime as _dt
    ist_now = ist_now or _dt.now(IST)
    ag = agents.get(sym)
    trades_today = 0; realised_money = 0.0; equity = 0.0; open_pos = []
    try:
        if not getattr(cfg, "dry", False):
            start = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
            from datetime import timezone as _tz
            deals = broker.closed_deals(None, start.astimezone(_tz.utc), _dt.now(_tz.utc) + timedelta(minutes=1))
            trades_today = len(deals)
            realised_money = sum(d["profit"] for d in deals)
            acc = broker.account()
            equity = acc["balance"] if acc else 0.0
            open_pos = [{"symbol": p["symbol"], "direction": p["direction"]} for p in (broker.positions(sym) or [])]
    except Exception:
        pass
    news_block = False
    if ag is not None:
        try:
            import time as _t
            tu = int(_t.time())
            news_block = any(r - 60 * 60 <= tu < r + 30 * 60 for r in (ag.news or ()))
        except Exception:
            pass
    from .halt import is_halted
    halted = is_halted(cfg.log_dir)
    return {"trades_today": trades_today, "losses_today": sum(1 for d in (deals if 'deals' in dir() else []) if d.get("profit", 0) < 0),
            "realised_money_today": realised_money, "equity": equity, "ist_now": ist_now,
            "news_block": news_block, "open_positions": open_pos, "halted": halted,
            "_skip_session": getattr(cfg, "_test_skip_session", False)}


def execute_entry(cfg, agents, journal, item: dict, side: str, user, notify=None, retry_sleep: float = 2.0, card_key=None, lots=None) -> dict:
    """Core execution: governor check → tick → SL → place_market when allowed.
    Returns {"line", "text", "ok", "ticket", "placed", "lots"}."""
    from .governor import Governor
    m = item.get("meta", {}); sym = m.get("symbol"); aid = m.get("alert_id") or card_key
    side = side.upper()
    ag = agents.get(sym); g = ag.g if ag is not None else None
    tk = None
    if not getattr(cfg, "dry", False):
        try: tk = broker.tick(sym)
        except Exception: tk = None
    px = (tk["ask"] if side == "LONG" else tk["bid"]) if tk else m.get("price")
    sl = ((px - g.pre_stop) if side == "LONG" else (px + g.pre_stop)) if (g is not None and px is not None) else None
    sl_txt = f" · SL {sl:.2f} (−{g.pre_stop:g})" if sl is not None else ""
    demo = broker.is_demo() if not getattr(cfg, "dry", False) else None
    n = notify if notify is not None else (ag.notify if ag is not None else None)
    lots_suggested = float(m["lots"]) if m.get("lots") is not None else None
    lots_source = "button" if lots is not None else "card"
    lots_chosen = min(float(lots), cfg.max_lots) if lots is not None else min(float(lots_suggested or cfg.max_lots), cfg.max_lots)
    sugg_txt = f" (suggested {lots_suggested:g}★)" if lots_suggested is not None else ""
    half_size = bool(m.get("half_size") or m.get("chop"))
    # v2.3.12: governor check BEFORE any order
    gov = Governor(cfg)
    ctx = _governor_context(cfg, agents, sym)
    dec = gov.check(sym, side, lots_chosen, ctx)
    if not dec.allowed:
        journal.decision("governor_block", actor="system", rationale=f"{dec.rule}: {dec.detail}",
                         symbol=sym, side=side, lots=lots_chosen, rule=dec.rule, detail=dec.detail, by=str(user))
        return {"line": f"⛔ BLOCKED · {dec.rule} · {dec.detail}", "text": f"⛔ {dec.detail}",
                "ok": False, "ticket": None, "placed": False, "lots": lots_chosen}
    if cfg.execution_enabled and (demo or cfg.allow_live) and not getattr(cfg, "dry", False):
        r = broker.place_market(sym, side, lots_chosen, sl, comment=f"aureon {aid}", retry_sleep=retry_sleep)
        card_px = float(m.get("price") or px or 0.0)
        fill = r.price if r.price is not None else px
        slip = ((fill - card_px) if side == "LONG" else (card_px - fill)) if (fill is not None and card_px) else None
        spread_at_fill = round(r.ask - r.bid, 5) if (r.ask is not None and r.bid is not None) else None
        _actor = "agent" if str(user) == "agent" else "human"
        journal.decision("order_placed", actor=_actor, rationale="agent entry" if _actor == "agent" else "button press",
                         symbol=sym, mode=m.get("mode"), alert_id=aid, side=side, lots=lots_chosen, sl=sl, ok=bool(r.ok),
                         status=r.status, retcode=r.retcode, ticket=r.ticket, fill=r.price, by=str(user),
                         lots_chosen=lots_chosen, lots_suggested=lots_suggested, lots_source=lots_source, half_size=half_size,
                         slippage_pts=round(slip, 2) if slip is not None else None, spread_pts=spread_at_fill, card_price=card_px)
        if ag is not None: ag.attach_alert(aid, side, card_key=card_key)
        if r.ok:
            spread = spread_at_fill
            sugg_s = f"{lots_suggested:g}" if lots_suggested is not None else "—"
            size_field = f"{lots_chosen:g} lots ({lots_source}) · suggested {sugg_s} · half size: {'yes' if half_size else 'no'}"
            capped = lots is not None and float(lots) > cfg.max_lots
            if capped:
                size_field += f" · capped at max {cfg.max_lots:g}"
            order_fields = [_field("Ticket", f"#{r.ticket}" if r.ticket else "—"), _field("Side", side), _field("Lots", f"{lots_chosen:g}"),
                            _field("Fill", f"{fill:.2f}" if fill is not None else "—"),
                            _field("Slippage vs card", f"{slip:+.2f}" if slip is not None else "—"),
                            _field("SL as set", f"{sl:.2f}" if sl is not None else "—"),
                            _field("Spread at fill", f"{spread:.2f}" if spread is not None else "—"),
                            _field("Size", size_field),
                            _field("Time", f"{datetime.now(IST):%H:%M:%S} IST")]
            if n is not None:
                n.send(f"alert:{sym}:{aid}:ORDER", f"{sym} · MT5 · ORDER PLACED · {side}", [f"alert {aid} · {lots_chosen:g} lots at market — the guardian takes it from here"],
                       fields=order_fields, color=GREEN, footer=f"{ag.S.display_name if ag else cfg.mode} · alert {aid}")
            line = f"TAKEN {side} {lots_chosen:g} lots{sugg_txt} · ticket {r.ticket or '—'} · filled {fill:.2f}" if fill is not None else f"TAKEN {side} {lots_chosen:g} lots{sugg_txt} · ticket {r.ticket or '—'}"
            if capped:
                line += f" (capped at max {cfg.max_lots:g})"
            return {"line": line, "text": f"✅ placed {side} {lots_chosen:g} lots {sym}{sl_txt}", "ok": True, "ticket": r.ticket, "placed": True, "lots": lots_chosen}
        return {"line": f"TAKEN {side} {lots_chosen:g} lots{sugg_txt} · ❌ order rejected ({r.status} {r.retcode or ''} {r.comment}) · place {lots_chosen:g} lots in MT5, I will manage it{sl_txt}".strip(),
                "text": f"❌ order rejected ({r.why()}) — place {lots_chosen:g} lots in MT5, I will manage it{sl_txt}", "ok": False, "ticket": None, "placed": False, "lots": lots_chosen}
    if ag is not None: ag.attach_alert(aid, side, card_key=card_key)
    why = ("execution off (AUREON_EXECUTION=0)" if not cfg.execution_enabled else
           "dry run" if getattr(cfg, "dry", False) else "live account — set AUREON_ALLOW_LIVE=1 to let the button place it")
    return {"line": f"TAKEN {side} · place {lots_chosen:g} lots · waiting for your ticket…{sl_txt} (guardian will set it)",
            "text": f"noted — place {lots_chosen:g} lots in MT5, I will manage it{sl_txt} · {why}", "ok": True, "ticket": None, "placed": False, "lots": lots_chosen}


def alert_decision(cfg, agents, journal, alerts, item: dict, side: str, user, notify=None, retry_sleep: float = 2.0, lots=None) -> dict:
    """LONG / SHORT / SKIP on an ALERT REACHED card. Runs OFF the event loop. Journals alert_decision; delegates to execute_entry.
    Returns {"line", "text", "ok", "ticket", "placed", "lots"}."""
    m = item.get("meta", {}); sym = m.get("symbol"); aid = m.get("alert_id"); sugg = m.get("suggested_side")
    side = side.upper()
    lots_suggested = float(m["lots"]) if m.get("lots") is not None else None
    a = alerts.get(aid) if (alerts is not None and aid) else None
    claude_side = (a or {}).get("claude_side"); claude_verdict = (a or {}).get("claude_verdict")
    agent_verdict = m.get("agent_verdict") or (m.get("suggestion") or "—")
    common = dict(symbol=sym, mode=m.get("mode"), alert_id=aid, suggested_side=sugg, agent_verdict=agent_verdict,
                  claude_verdict=claude_verdict, claude_side=claude_side, by=str(user), price=m.get("price"), bar=m.get("bar"),
                  lots_chosen=lots, lots_suggested=lots_suggested)
    if side == "SKIP":
        journal.decision("alert_decision", actor="human", rationale="SKIP via button",
                         side="skip", agreed=(sugg is None), agreed_agent=(sugg is None),
                         agreed_claude=(None if claude_verdict is None else claude_side is None), **common)
        if alerts is not None: alerts.decide(aid, "skip")
        return {"line": "SKIPPED", "text": "⏭ skipped", "ok": True, "ticket": None, "placed": False, "lots": None}
    agreed = (sugg == side)
    journal.decision("alert_decision", actor="human", rationale=f"{side} via button",
                     side=side, agreed=agreed, agreed_agent=agreed,
                     agreed_claude=(None if claude_verdict is None else (claude_side == side)), **common)
    if alerts is not None: alerts.decide(aid, side)
    return execute_entry(cfg, agents, journal, item, side, user, notify=notify, retry_sleep=retry_sleep, lots=lots)


def ask_decision(cfg, agents, journal, item: dict, decision: str, user, notify=None, retry_sleep: float = 2.0, lots=None) -> dict:
    """TAKE / SKIP on a Confirm card (signal ask). Journals the decision event; TAKE delegates to execute_entry.
    Returns {"line", "text", "ok", "ticket", "placed", "lots"}."""
    m = item.get("meta", {})
    lots_suggested = float(m["lots"]) if m.get("lots") is not None else None
    journal.decision("decision", actor="human", rationale=f"{decision} via button",
                     symbol=m.get("symbol"), mode=m.get("mode"), side=m.get("side"), signal_kind=m.get("kind"),
                     price=m.get("price"), bar=m.get("bar"), decision=decision, by=str(user),
                     lots_chosen=lots, lots_suggested=lots_suggested)
    item["decided"] = decision
    if decision == "SKIP":
        return {"line": "SKIPPED", "text": "⏭ skipped", "ok": True, "ticket": None, "placed": False, "lots": None}
    return execute_entry(cfg, agents, journal, item, m.get("side", "LONG"), user, notify=notify,
                         retry_sleep=retry_sleep, card_key=item.get("key"), lots=lots)


def lot_button_spec(cfg, meta: dict) -> list[dict]:
    """v2.3.7: build the lot button descriptors for ENTER / ALERT cards when execution is on.
    Returns [{"label", "value", "disabled", "star"}] — max 5 buttons. The ★ goes on the button nearest meta["lots"];
    no ★ when meta["lots"] is None (v2.3.11)."""
    if not cfg.execution_enabled:
        return []
    suggested = meta.get("lots")  # None when no risk calc available
    out = []
    best_dist = None; best_idx = None
    for v in cfg.lot_buttons:
        disabled = v > cfg.max_lots
        label = f"{v:g}" if not disabled else f"{v:g} (>max)"
        out.append({"label": label, "value": v, "disabled": disabled, "star": False})
        if suggested is not None:
            d = abs(v - float(suggested))
            if not disabled and (best_dist is None or d < best_dist):
                best_dist = d; best_idx = len(out) - 1
    if best_idx is not None:
        out[best_idx]["star"] = True
        out[best_idx]["label"] += "★"
    return out


# ----------------------------------------------------------------------------- v2.0.2 command sync + /pull-history (pure helpers, testable)
def sync_plan(local: set, remote: set) -> dict:
    return {"add": sorted(local - remote), "keep": sorted(local & remote), "remove": sorted(remote - local)}


async def sync_commands(tree, guild_id: int | None, log=None) -> dict:
    """Sync the command tree (guild-scoped when DISCORD_GUILD is set), dropping stale commands not defined in bot.py.
    Returns {"guild", "final", "removed", "added"} and logs the final list."""
    local = {c.name for c in tree.get_commands()}
    guild = None
    if guild_id:
        import discord  # type: ignore
        guild = discord.Object(id=int(guild_id))
        tree.copy_global_to(guild=guild)
    try:
        before = {c.name for c in await tree.fetch_commands(guild=guild)}
    except Exception:
        before = set()
    plan = sync_plan(local, before)
    synced = await tree.sync(guild=guild)
    final = sorted({c.name for c in synced}) if synced is not None else sorted(local)
    out = {"guild": guild_id, "final": final, "removed": plan["remove"], "added": plan["add"]}
    (log or telemetry.info)(f"commands synced{' (guild ' + str(guild_id) + ')' if guild_id else ' (global)'}: {final} · removed stale: {plan['remove']}")
    return out


def pull_history(cfg, agents, journal, days: int = 7, symbol: str | None = None, lock_timeout: float = 2.0) -> Reply:
    """/pull-history: MT5 closed deals of the window (shared RLock, `lock_timeout` s, else 'MT5 busy'), matched to journal
    position_seen / closed rows by ticket; missing final_points filled (journal `final_points` rows); one `history_pull` row."""
    from .common.timeutil import IST as _IST
    days = max(1, min(int(days), 90))
    end = datetime.now(timezone.utc) + timedelta(minutes=1); start = end - timedelta(days=days)
    with broker.try_lock(lock_timeout) as got:
        if not got:
            return Reply(content="MT5 busy — try again", source="cache")
        deals = broker.closed_deals(symbol or None, start, end)
    recs = journal.read(int(start.timestamp()) - 7 * 86400)
    seen = {r.get("ticket"): r for r in recs if r.get("event") == "position_seen"}
    closed = {r.get("ticket"): r for r in recs if r.get("event") == "closed"}
    filled = {r.get("ticket") for r in recs if r.get("event") == "final_points"}
    matched, unmatched, newly = [], [], []
    per_day: dict = {}
    net_pts = 0.0; graded_pts = 0
    for d in sorted(deals, key=lambda x: x["time"]):
        t = d["ticket"]; c = closed.get(t); s = seen.get(t)
        entry = (c or {}).get("entry") or (s or {}).get("entry")
        direction = (c or {}).get("direction") or (("long" if s.get("side") == "LONG" else "short") if s else d.get("direction"))
        pts = round((d["price"] - float(entry)) if direction == "long" else (float(entry) - d["price"]), 2) if entry else None
        day = datetime.fromtimestamp(d["time"], tz=_IST).strftime("%Y-%m-%d")
        pd_ = per_day.setdefault(day, {"trades": 0, "points": 0.0, "profit": 0.0, "graded": 0})
        pd_["trades"] += 1; pd_["profit"] += float(d.get("profit") or 0.0)
        if pts is not None:
            pd_["points"] += pts; pd_["graded"] += 1; net_pts += pts; graded_pts += 1
        if c is not None or s is not None:
            matched.append(d)
            needs = c is None or c.get("final_points") is None or c.get("final_approx")
            if pts is not None and needs and t not in filled:
                journal.log("final_points", symbol=d["symbol"], ticket=t, final_points=pts, profit=d.get("profit"), price=d["price"],
                            alert_id=(c or {}).get("alert_id") or (s or {}).get("alert_id"), source="history")
                newly.append(t); filled.add(t)
        else:
            unmatched.append(d)
    journal.log("history_pull", days=days, symbol=symbol, rows=len(deals), matched=len(matched), unmatched=len(unmatched), newly_graded=len(newly),
                net_points=round(net_pts, 2))
    try:
        from . import reports
        graded, note = reports.graded_signals(cfg, agents, journal, int(start.timestamp()))
        grade_line = f"graded signals (scorecard): {len(graded)}" + (f" · {note}" if note else "")
    except Exception as e:
        grade_line = f"graded signals: unavailable ({e!r})"[:200]
    lines = [f"{d} · {v['trades']} trades · {v['points']:+.1f} pts" + (f" ({v['graded']}/{v['trades']} graded)" if v["graded"] != v["trades"] else "")
             + f" · {v['profit']:+.0f} $" for d, v in sorted(per_day.items())]
    fields = [_field("Deals", f"found **{len(deals)}** · matched **{len(matched)}** · newly graded **{len(newly)}** · unmatched {len(unmatched)}"),
              _field("Net", f"**{net_pts:+.1f} pts** over {graded_pts} graded · {sum(float(d.get('profit') or 0) for d in deals):+.0f} $ all deals"),
              _field("Per day", "\n".join(lines) or "no closed deals in the window", False)]
    if unmatched:
        fields.append(_field("Unmatched tickets", "manual trades with no alert / signal: " + ", ".join(f"#{d['ticket']}" for d in unmatched[:20])
                             + (" …" if len(unmatched) > 20 else ""), False))
    fields.append(_field("Grading", grade_line, False))
    card = _embed(f"AUREON · MT5 · PULL HISTORY · {days} d" + (f" · {symbol}" if symbol else ""),
                  f"{start:%d %b} → {datetime.now(timezone.utc):%d %b} · MT5 history ↔ journal by ticket", color=BLUE, fields=fields,
                  footer=f"Aureon MT5 v{VERSION}")
    return Reply(card=card, source="live")


def alert_preview(ag, a: dict, current: float | None) -> dict | None:
    """v2.0.2 /alert reply: the PRICE / EMA / TREND / AGENT VERDICT blocks for the current bar, labelled PREVIEW. Zero Claude calls."""
    if ag is None or current is None or getattr(ag, "last_df", None) is None:
        return None
    try:
        df = ag.last_df; res = getattr(ag, "last_res", None)
        if res is None:
            from .common.source import Bars
            res = ag.S.analyse({"M5": Bars(ag.symbol, "M5", df[["time", "open", "high", "low", "close"] + [c for c in ("volume",) if c in df]])},
                               ag.symbol, ag.off, ag.news)["M5"]
            ag.last_res = res
        c = alerts_mod.preview_card(ag, a, df, res, int(df["time"].iloc[-1]), float(current))
        return _embed(c["title"], c["description"], color=BLUE, fields=c["fields"], footer=c["footer"])
    except Exception as e:
        telemetry.info(f"alert preview failed: {e!r}")
        return None


def run_bot(cfg, agents: dict, journal, notify, started_at: float, *, health_notify=None, claude=None, alerts=None, codex=None):
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

    def _card_edit(key, updates: dict) -> bool:
        """v2.0.2 (any thread): upsert named fields on a posted ask card (CLAUDE VERDICT, Decision, …) and re-render it."""
        item = pending.get(key)
        if not item or not item.get("message"):
            return False
        fields = list(item.get("fields") or [])
        for name, value in updates.items():
            for f in fields:
                if f.get("name") == name:
                    f["value"] = str(value); break
            else:
                fields.append({"name": name, "value": str(value), "inline": False})
        item["fields"] = fields
        fut = asyncio.run_coroutine_threadsafe(item["message"].edit(embed=_ask_embed(item)), client.loop)
        fut.result(timeout=10)
        return True

    notify.card_edit_hook = _card_edit

    def _decide(item, decision, user):
        m = item.get("meta", {})
        journal.log("decision", symbol=m.get("symbol"), mode=m.get("mode"), side=m.get("side"), signal_kind=m.get("kind"),
                    price=m.get("price"), bar=m.get("bar"), decision=decision, by=str(user))
        item["decided"] = decision

    class AskView(discord.ui.View):
        def __init__(self, item):
            super().__init__(timeout=cfg.ask_ttl_min * 60); self.item = item
            btns = lot_button_spec(cfg, item.get("meta") or {})
            if btns:
                # execution on: lot-size buttons instead of TAKE
                for b in btns:
                    btn = discord.ui.Button(label=b["label"], custom_id=f"take:{b['value']}", disabled=b["disabled"],
                                            style=discord.ButtonStyle.success)
                    btn.callback = self._make_lot_cb(b["value"])
                    self.add_item(btn)
                skip_btn = discord.ui.Button(label="SKIP", custom_id="skip", style=discord.ButtonStyle.secondary, emoji="⏭")
                skip_btn.callback = self._make_skip_cb()
                self.add_item(skip_btn)
            else:
                take_btn = discord.ui.Button(label="TAKE", custom_id="take", style=discord.ButtonStyle.success, emoji="✅")
                take_btn.callback = self._make_take_cb()
                self.add_item(take_btn)
                skip_btn = discord.ui.Button(label="SKIP", custom_id="skip", style=discord.ButtonStyle.secondary, emoji="⏭")
                skip_btn.callback = self._make_skip_cb()
                self.add_item(skip_btn)

        def _make_lot_cb(self, lot_val):
            async def cb(inter): await self._answer(inter, "TAKE", lots=lot_val)
            return cb

        def _make_take_cb(self):
            async def cb(inter): await self._answer(inter, "TAKE")
            return cb

        def _make_skip_cb(self):
            async def cb(inter): await self._answer(inter, "SKIP")
            return cb

        async def _answer(self, inter, decision, lots=None):
            if self.item.get("decided"):
                await inter.response.send_message(f"already answered: {self.item['decided']}", ephemeral=True); return
            self.item["decided"] = decision
            await inter.response.defer()
            r = await asyncio.to_thread(ask_decision, cfg, agents, journal, self.item, decision, inter.user, notify, lots=lots)
            # v2.3.9: mark ask decided for miss tracking
            m = self.item.get("meta", {})
            ag = agents.get(m.get("symbol"))
            if ag is not None:
                ag.mark_ask_decided(self.item.get("key", ""), decision, placed=r.get("placed", False))
            for c in self.children: c.disabled = True
            sugg = float(m.get("lots") or cfg.max_lots)
            if decision == "TAKE":
                lot_txt = f" {r.get('lots', ''):g} lots (suggested {sugg:g})" if r.get("lots") else ""
                dec_value = f"✅ **{r['line']}** · {inter.user.display_name} · {datetime.now(IST):%H:%M} IST — {r['text']}"
                dec_color = GREEN if r.get("ok") else RED
            else:
                dec_value = f"⏭ skipped · {inter.user.display_name} · {datetime.now(IST):%H:%M} IST"
                dec_color = GREY
            self.item["fields"] = [f for f in (self.item.get("fields") or []) if f.get("name") != "Decision"] + \
                [{"name": "Decision", "value": dec_value[:1024], "inline": False}]
            em = _ask_embed(self.item); em.color = dec_color
            try:
                await self.item["message"].edit(embed=em, view=self)
            except Exception:
                try:
                    await inter.followup.send(content=dec_value[:2000], ephemeral=True)
                except Exception:
                    pass

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
                em = _ask_embed(item); files = []
                view = AlertView(item) if (item.get("meta") or {}).get("kind") == "alert" else AskView(item)
                if item.get("png") and os.path.exists(item["png"]):
                    fname = os.path.basename(item["png"]); em.set_image(url=f"attachment://{fname}"); files = [discord.File(item["png"], filename=fname)]
                msg = await ch.send(embed=em, view=view, files=files)
                item["message"] = msg; pending[item["key"]] = item
                if (item.get("meta") or {}).get("kind") == "alert" and alerts is not None:      # v2.0.2: message id on the alert
                    try:
                        alerts.set_message(item["meta"]["alert_id"], msg.id, getattr(ch, "id", None))
                    except Exception:
                        pass
                while len(pending) > 50: pending.pop(next(iter(pending)))
            except Exception as e:
                print("  (ask post failed:", e, ") — falling back to webhook")
                notify.sent.discard(item["key"]); notify.bot_ready = False
                try:                                     # webhook post is blocking I/O — never on the event loop
                    await asyncio.to_thread(notify.send, item["key"], item["title"], [item["description"]], item.get("png"),
                                            fields=item.get("fields"), color=item.get("color"), footer=item.get("footer"))
                finally:
                    notify.bot_ready = True

    # ------------------------------------------------------------------ v2.0.0 ALERT REACHED: [LONG] [SHORT] [SKIP]
    class AlertView(discord.ui.View):
        def __init__(self, item):
            super().__init__(timeout=cfg.ask_ttl_min * 60); self.item = item
            meta = item.get("meta") or {}
            btns = lot_button_spec(cfg, meta)
            sugg_side = meta.get("suggested_side")
            if btns:
                # execution on: always both rows (LONG + SHORT) + SKIP; ★ only on the suggested_side row
                sides = ["LONG", "SHORT"]
                if sugg_side:
                    sides = [sugg_side] + [s for s in sides if s != sugg_side]  # suggested first
                for row_idx, side in enumerate(sides):
                    style = discord.ButtonStyle.success if side == "LONG" else discord.ButtonStyle.danger
                    show_star = (side == sugg_side)  # ★ only on the suggested side
                    for b in btns:
                        base_label = b["label"].rstrip("★")
                        lbl = f"{side} {base_label}" + ("★" if show_star and b["star"] else "")
                        btn = discord.ui.Button(label=lbl, custom_id=f"{side.lower()}:{b['value']}", disabled=b["disabled"],
                                                style=style, row=row_idx)
                        btn.callback = self._make_lot_cb(side, b["value"])
                        self.add_item(btn)
                skip_btn = discord.ui.Button(label="SKIP", custom_id="skip", style=discord.ButtonStyle.secondary, emoji="⏭",
                                             row=len(sides))
                skip_btn.callback = self._make_skip_cb()
                self.add_item(skip_btn)
            else:
                long_btn = discord.ui.Button(label="LONG", custom_id="long", style=discord.ButtonStyle.success, emoji="🟢")
                long_btn.callback = self._make_side_cb("LONG")
                self.add_item(long_btn)
                short_btn = discord.ui.Button(label="SHORT", custom_id="short", style=discord.ButtonStyle.danger, emoji="🔴")
                short_btn.callback = self._make_side_cb("SHORT")
                self.add_item(short_btn)
                skip_btn = discord.ui.Button(label="SKIP", custom_id="skip", style=discord.ButtonStyle.secondary, emoji="⏭")
                skip_btn.callback = self._make_skip_cb()
                self.add_item(skip_btn)

        def _make_lot_cb(self, side, lot_val):
            async def cb(inter): await self._answer(inter, side, lots=lot_val)
            return cb

        def _make_side_cb(self, side):
            async def cb(inter): await self._answer(inter, side)
            return cb

        def _make_skip_cb(self):
            async def cb(inter): await self._answer(inter, "SKIP")
            return cb

        async def _answer(self, inter, side, lots=None):
            if self.item.get("decided"):
                await inter.response.send_message(f"already answered: {self.item['decided']}", ephemeral=True); return
            self.item["decided"] = side
            await inter.response.defer()
            r = await asyncio.to_thread(alert_decision, cfg, agents, journal, alerts, self.item, side, inter.user, notify, lots=lots)
            for c in self.children: c.disabled = True
            dec_value = f"**{r['line']}** · {inter.user.display_name} · {datetime.now(IST):%H:%M} IST — {r['text']}"[:1024]
            self.item["fields"] = [f for f in (self.item.get("fields") or []) if f.get("name") != "Decision"] + \
                [{"name": "Decision", "value": dec_value, "inline": False}]
            em = _ask_embed(self.item)
            em.color = GREEN if (side != "SKIP" and r.get("ok")) else RED if (side != "SKIP" and not r.get("ok")) else GREY
            try:
                await self.item["message"].edit(embed=em, view=self)
            except Exception:
                try:
                    await inter.followup.send(content=dec_value[:2000], ephemeral=True)
                except Exception:
                    pass

        async def on_timeout(self):
            if not self.item.get("decided"):                      # expired: card edited, nothing journaled
                self.item["decided"] = "EXPIRED"
                for c in self.children: c.disabled = True
                try:
                    em = _ask_embed(self.item); em.color = GREY
                    em.add_field(name="Decision", value=f"⌛ expired after {cfg.ask_ttl_min} min", inline=False)
                    await self.item["message"].edit(embed=em, view=self)
                except Exception:
                    pass

    def _latest_open():
        return next((i for i in reversed(list(pending.values())) if not i.get("decided")), None)

    @tree.command(name="take", description="Accept pending ask; places order when AUREON_EXECUTION=1, else arms guardian")
    @app_commands.describe(lots="Lot size (from AUREON_LOT_BUTTONS); omit to use the card's suggested size")
    async def take_cmd(inter: discord.Interaction, lots: float | None = None):
        user = inter.user
        def work():
            item = _latest_open()
            if not item:
                return Reply(content="no open trade question")
            r = ask_decision(cfg, agents, journal, item, "TAKE", user, notify, lots=lots)
            notify.edit_card(item.get("key"), {"Decision": f"✅ **{r['line']}** · {user.display_name if hasattr(user, 'display_name') else user} · {datetime.now(IST):%H:%M} IST — {r['text']}"[:1024]})
            return Reply(content=f"✅ {r['line']} — {r['text']}")
        await run(inter, "take", work)

    @tree.command(name="skip", description="Skip pending ask; records operator decision")
    async def skip_cmd(inter: discord.Interaction):
        user = inter.user
        def work():
            item = _latest_open()
            if not item:
                return Reply(content="no open trade question")
            _decide(item, "SKIP", user)
            return Reply(content=f"⏭ skipped {item['meta'].get('side')} {item['meta'].get('symbol')}")
        await run(inter, "skip", work)

    @tree.command(name="override", description="Override a missed setup — places the order at the current price")
    @app_commands.describe(symbol="Symbol (default: first)", lots="Lot size")
    async def override_cmd(inter: discord.Interaction, symbol: str = "", lots: float | None = None):
        user = inter.user
        def work():
            sym = (symbol.strip().upper() or cfg.symbols[0])
            ag = agents.get(sym)
            if ag is None:
                return Reply(content=f"no agent for {sym}")
            miss = ag.check_override()
            if miss is None:
                reason = ""
                if ag.open_misses:
                    reason = ag.open_misses[-1].get("expired_reason", "expired")
                return Reply(content=f"no overridable miss for {sym}" + (f" ({reason})" if reason else ""))
            # Build an item for execute_entry
            item = {"meta": {"symbol": sym, "side": miss["side"], "kind": miss["kind"], "price": miss.get("now_price", miss["price"]),
                             "bar": miss["bar"], "mode": ag.S.name, "sl": miss.get("sl"),
                             "lots": miss.get("lots"), "chop": ag._is_chop(), "alert_id": miss.get("card_key")},
                    "key": miss["card_key"]}
            r = execute_entry(cfg, agents, journal, item, miss["side"], user, notify=notify, lots=lots, card_key=miss["card_key"])
            journal.log("override", symbol=sym, card_key=miss["card_key"], side=miss["side"], lots=r.get("lots"),
                        by=str(user), drift_at_press=miss.get("drift_at_press"), placed=r.get("placed"), ticket=r.get("ticket"),
                        lots_source="override")
            ag.consume_miss(miss["card_key"])
            notify.edit_card(miss["card_key"], {"Decision": f"⚡ **OVERRIDE {r['line']}** · {user.display_name if hasattr(user, 'display_name') else user} · {datetime.now(IST):%H:%M} IST"[:1024]})
            return Reply(content=f"⚡ {r['line']} — {r['text']}")
        await run(inter, "override", work)

    @tree.command(name="ignore-miss", description="Ignore a missed setup (no action taken)")
    @app_commands.describe(symbol="Symbol (default: first)")
    async def ignore_miss_cmd(inter: discord.Interaction, symbol: str = ""):
        user = inter.user
        def work():
            sym = (symbol.strip().upper() or cfg.symbols[0])
            ag = agents.get(sym)
            if ag is None or not ag.open_misses:
                return Reply(content=f"no open miss for {sym}")
            miss = ag.open_misses[-1]
            journal.log("override_ignored", symbol=sym, card_key=miss["card_key"], side=miss["side"], by=str(user))
            ag.consume_miss(miss["card_key"])
            return Reply(content=f"⏭ miss ignored · {miss['side']} {sym} · {miss['kind']}")
        await run(inter, "ignore-miss", work)

    @tree.command(name="watchdog", description="Show open watchdog incidents and their durations")
    async def watchdog_cmd(inter: discord.Interaction):
        def work():
            import json as _json
            path = os.path.join(cfg.log_dir, "watchdog.json")
            try:
                with open(path, encoding="utf-8") as f:
                    data = _json.load(f)
                incidents = data.get("incidents", {})
                now = _time.time()
                open_inc = [(k, v) for k, v in incidents.items() if v.get("open")]
                if not open_inc:
                    return Reply(content="watchdog: no open incidents")
                lines = []
                for k, v in open_inc:
                    dur = int((now - (v.get("open_t") or now)) // 60)
                    lines.append(f"**{k}** · open {dur} min")
                return Reply(content="**Open incidents:**\n" + "\n".join(lines))
            except FileNotFoundError:
                return Reply(content="watchdog: no state file yet (first tick hasn't run)")
            except Exception as e:
                return Reply(content=f"watchdog: error reading state — {e}")
        await run(inter, "watchdog", work)

    @tree.command(name="approve-tuning", description="Approve a tuning proposal by ID")
    @app_commands.describe(proposal_id="Proposal ID from the tuning card")
    async def approve_tuning_cmd(inter: discord.Interaction, proposal_id: int):
        def work():
            from . import tuning
            result = tuning.approve(cfg.log_dir, proposal_id, journal)
            if isinstance(result, str):
                return Reply(content=result)
            return Reply(content=f"✅ approved #{proposal_id}: {result['param']} → {result['new']}")
        await run(inter, "approve-tuning", work)

    @tree.command(name="reject-tuning", description="Reject a tuning proposal by ID")
    @app_commands.describe(proposal_id="Proposal ID")
    async def reject_tuning_cmd(inter: discord.Interaction, proposal_id: int):
        def work():
            from . import tuning
            result = tuning.reject(cfg.log_dir, proposal_id, journal)
            if isinstance(result, str):
                return Reply(content=result)
            return Reply(content=f"❌ rejected #{proposal_id}")
        await run(inter, "reject-tuning", work)

    @tree.command(name="revert-tuning", description="Revert an active tuning override")
    @app_commands.describe(param="Parameter name to revert")
    async def revert_tuning_cmd(inter: discord.Interaction, param: str):
        def work():
            from . import tuning
            if tuning.revert(cfg.log_dir, param.strip(), journal):
                return Reply(content=f"↩️ reverted {param}")
            return Reply(content=f"{param} has no active override")
        await run(inter, "revert-tuning", work)

    @tree.command(name="tuning", description="Show active tuning overrides and pending proposals")
    async def tuning_cmd(inter: discord.Interaction):
        def work():
            from . import tuning
            overrides = tuning.load_overrides(cfg.log_dir)
            proposals = [p for p in tuning.load_proposals(cfg.log_dir) if p["status"] == "pending"]
            lines = []
            if overrides:
                lines.append("**Active overrides:**")
                for k, v in overrides.items():
                    lines.append(f"  `{k}` = {v}")
            else:
                lines.append("no active overrides")
            if proposals:
                lines.append("**Pending proposals:**")
                for p in proposals:
                    lines.append(f"  {tuning.format_proposal(p)}")
            return Reply(content="\n".join(lines))
        await run(inter, "tuning", work)

    @tree.command(name="review", description="Show the latest nightly review (pattern flags)")
    async def review_cmd(inter: discord.Interaction):
        def work():
            from . import review as _review
            result = _review.compute(journal, cfg.server_utc_offset)
            c = _review.card(result)
            return Reply(card=c)
        await run(inter, "review", work)

    @tree.command(name="eod", description="Run the end-of-day review now (or for a specific date)")
    @app_commands.describe(date="YYYY-MM-DD (default: today)")
    async def eod_cmd(inter: discord.Interaction, date: str = ""):
        def work():
            from .eod_review import run_review
            result = run_review(cfg, journal, notify, date_str=date or None)
            if result["card"]:
                return Reply(card=result["card"])
            return Reply(content=result.get("error", "EOD review unavailable"))
        await run(inter, "eod", work)

    @tree.command(name="agent-on", description="Switch a symbol to AGENT mode for N minutes")
    @app_commands.describe(symbol="Symbol (default: first)", minutes="Duration in minutes (default: AUREON_AGENT_DEFAULT_MIN)")
    async def agent_on_cmd(inter: discord.Interaction, symbol: str = "", minutes: int | None = None):
        user = inter.user
        def work():
            from .control import current_mode, to_agent
            sym = (symbol.strip().upper() or cfg.symbols[0])
            ctrl = current_mode(journal, sym)
            if ctrl["mode"] == "AGENT":
                return Reply(content=f"{sym} already in AGENT mode")
            mins = minutes if minutes is not None else cfg.agent_default_min
            to_agent(journal, notify, sym, str(user), mins)
            return Reply(content=f"🤖 AGENT ON · {sym} · {mins} min")
        await run(inter, "agent-on", work)

    @tree.command(name="manual", description="Switch a symbol back to MANUAL mode")
    @app_commands.describe(symbol="Symbol (default: first)")
    async def manual_cmd(inter: discord.Interaction, symbol: str = ""):
        user = inter.user
        def work():
            from .control import current_mode, to_manual
            sym = (symbol.strip().upper() or cfg.symbols[0])
            ctrl = current_mode(journal, sym)
            if ctrl["mode"] == "MANUAL":
                return Reply(content=f"{sym} already in MANUAL mode")
            to_manual(journal, notify, sym, str(user), "/manual command")
            return Reply(content=f"🧑 MANUAL · {sym}")
        await run(inter, "manual", work)

    @tree.command(name="agent-schedule", description="Manage scheduled AGENT windows")
    @app_commands.describe(action="add / list / remove", symbol="Symbol", time_range="HH:MM-HH:MM (IST)", days="Mon-Fri or Mon,Wed,Fri", schedule_id="ID to remove")
    async def agent_schedule_cmd(inter: discord.Interaction, action: str = "list", symbol: str = "", time_range: str = "", days: str = "Mon-Fri", schedule_id: int = 0):
        def work():
            from . import schedule as sched
            sym = symbol.strip().upper() or cfg.symbols[0]
            if action == "add" and time_range:
                result = sched.add(cfg.log_dir, sym, time_range, days)
                if isinstance(result, str):
                    return Reply(content=f"error: {result}")
                return Reply(content=f"schedule added: {sched.format_schedule(result)}")
            elif action == "remove" and schedule_id:
                if sched.remove(cfg.log_dir, schedule_id):
                    return Reply(content=f"schedule #{schedule_id} removed")
                return Reply(content=f"schedule #{schedule_id} not found")
            else:
                schedules = sched.list_schedules(cfg.log_dir, sym if symbol else None)
                if not schedules:
                    return Reply(content="no schedules")
                return Reply(content="**Schedules:**\n" + "\n".join(sched.format_schedule(s) for s in schedules))
        await run(inter, "agent-schedule", work)

    @tree.command(name="plan", description="Show today's daily plan (news, sessions, yesterday, chop, governor)")
    async def plan_cmd(inter: discord.Interaction):
        def work():
            from .daily_plan import plan_card
            card = plan_card(cfg, agents, journal)
            return Reply(card=card)
        await run(inter, "plan", work)

    @tree.command(name="halt", description="Kill switch: close all positions and block all entries")
    @app_commands.describe(reason="Reason for the halt")
    async def halt_cmd(inter: discord.Interaction, reason: str = ""):
        user = inter.user
        def work():
            from . import halt as halt_mod
            if halt_mod.is_halted(cfg.log_dir):
                return Reply(content="already halted — /unhalt confirm:UNHALT to resume")
            halt_mod.set_halt(cfg.log_dir, str(user), reason)
            journal.decision("halt", actor="human", rationale=reason or "manual", by=str(user), reason=reason or "manual")
            if cfg.dry or not cfg.execution_enabled:
                # Dry mode: report what would be closed
                pos_lines = []
                for sym, ag in agents.items():
                    snap = getattr(ag, "snapshot", {}) or {}
                    for p in snap.get("positions", []):
                        pos_lines.append(f"would close #{p['ticket']} {p['direction'].upper()} {p['points']:+.1f}")
                summary = "\n".join(pos_lines) if pos_lines else "no open positions"
                return Reply(content=f"⛔ HALTED (dry/exec off) · {reason or 'manual'}\n{summary}")
            results = halt_mod.close_all_positions(cfg.symbols)
            summary = halt_mod.halt_summary(results)
            notify.card("⛔ HALT", summary, footer=f"by {user} · {datetime.now(IST):%H:%M} IST")
            return Reply(content=summary)
        await run(inter, "halt", work)

    @tree.command(name="unhalt", description="Resume after a halt — requires exact confirmation phrase")
    @app_commands.describe(confirm="Type UNHALT to confirm")
    async def unhalt_cmd(inter: discord.Interaction, confirm: str = ""):
        user = inter.user
        def work():
            from . import halt as halt_mod
            if not halt_mod.is_halted(cfg.log_dir):
                return Reply(content="not halted")
            if confirm.strip() != "UNHALT":
                return Reply(content="type `/unhalt confirm:UNHALT` (exact) to resume")
            prev = halt_mod.clear_halt(cfg.log_dir)
            journal.decision("unhalt", actor="human", rationale="manual unhalt", by=str(user))
            notify.card("✅ UNHALT", f"Resumed by {user}", footer=f"{datetime.now(IST):%H:%M} IST")
            return Reply(content="✅ unhalted — entries allowed again")
        await run(inter, "unhalt", work)

    @tree.command(name="governor", description="Show risk governor rules and current values")
    async def governor_cmd(inter: discord.Interaction):
        def work():
            from .governor import Governor
            gov = Governor(cfg)
            ctx = _governor_context(cfg, agents, cfg.symbols[0])
            rules = gov.rules_table(ctx)
            lines = []
            for r in rules:
                icon = "🟢" if r["ok"] else "🔴"
                lines.append(f"{icon} **{r['rule']}** · {r['value']} / {r['limit']}")
            return Reply(content="**Risk Governor:**\n" + "\n".join(lines))
        await run(inter, "governor", work)

    @client.event
    async def on_ready():
        monitor.gateway_up()
        if not getattr(client, "_aureon_started", False):
            client._aureon_started = True
            try:
                await sync_commands(tree, cfg.guild_id)                       # v2.0.2: guild sync, stale commands dropped, list logged
            except Exception as e:
                telemetry.info(f"command sync failed: {e!r}")
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

    @tree.command(name="status", description="Read primary symbol health and trade status")
    async def status(inter: discord.Interaction):
        sym = cfg.symbols[0]
        await run(inter, "status", lambda: Reply(card=status_card(sym, agents[sym], cfg, journal, monitor, claude, codex)))

    @tree.command(name="parallel-status", description="Show all monitored symbols and positions")
    async def pstatus(inter: discord.Interaction):
        await run(inter, "parallel-status", lambda: Reply(card=parallel_card(agents, cfg, journal)))

    @tree.command(name="agents", description="Show each agent phase, health, uptime and guardian state")
    async def agents_cmd(inter: discord.Interaction):
        await run(inter, "agents", lambda: Reply(card=agents_card(agents, cfg, started_at, monitor, claude, journal)))

    @tree.command(name="symbols", description="List configured trading symbols and modes")
    async def symbols(inter: discord.Interaction):
        await run(inter, "symbols", lambda: Reply(card=symbols_card(agents, cfg)))

    @tree.command(name="symbol-present", description="Check whether a symbol is monitored")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def present(inter: discord.Interaction, symbol: str):
        await run(inter, "symbol-present", lambda: Reply(card=present_card(symbol, agents, cfg)))

    @tree.command(name="market", description="Show market open or closed status; read-only")
    async def market(inter: discord.Interaction):
        sym = cfg.symbols[0]
        await run(inter, "market", lambda: Reply(card=market_card(sym, agents[sym], cfg)))

    @tree.command(name="report", description="Weekly trade report; read-only")
    @app_commands.describe(current="true = this week so far")
    async def report(inter: discord.Interaction, current: bool = False):
        def work():
            card, source = report_card(cfg, agents, journal, current)
            return Reply(card=card, source=source)
        await run(inter, "report", work, timeout=20)

    @tree.command(name="chart", description="Show latest cached EMA market context")
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

    # ------------------------------------------------------------------ v2.0.0 price alerts
    @tree.command(name="alert", description="Arm price alert; Aureon watches ticks automatically")
    @app_commands.describe(price="price level, e.g. 4120", symbol="default: primary symbol", note="optional note shown on the card")
    async def alert_cmd(inter: discord.Interaction, price: float, symbol: str = "", note: str = ""):
        user = inter.user
        def work():
            sym = (symbol or cfg.symbols[0]).upper(); ag = agents.get(sym)
            if alerts is None:
                return Reply(content="alerts store unavailable")
            cur = current_price(sym, ag, cfg)
            a = alerts.arm(sym, price, cur, note, getattr(user, "display_name", str(user)), getattr(ag, "last_bar", None))
            journal.log("alert_armed", symbol=sym, mode=cfg.mode, alert_id=a["id"], price=a["price"], side_hint=a["side_hint"], note=a["note"],
                        current=cur, by=str(user))
            card = alert_preview(ag, a, cur)                                   # v2.0.2: PREVIEW of the two blocks, no Claude call
            return Reply(content=alerts_mod.armed_reply(a, cur), card=card, source="live" if cur is not None else "cache")
        await run(inter, "alert", work)

    @tree.command(name="pull-history", description="Compare MT5 closed deals with Aureon journal")
    @app_commands.describe(days="window in days (default 7)", symbol="default: all symbols")
    async def pull_history_cmd(inter: discord.Interaction, days: int = 7, symbol: str = ""):
        await run(inter, "pull-history", lambda: pull_history(cfg, agents, journal, days, symbol.upper() or None), timeout=30)

    @tree.command(name="git-history", description="Show running version and recent Git changes")
    @app_commands.describe(n="merges to show (default 10)", tags="true = the last 10 tags with dates")
    async def git_history_cmd(inter: discord.Interaction, n: int = 10, tags: bool = False):
        def work():
            from .common import gitinfo
            h = gitinfo.history(n)
            c = gitinfo.card(h, VERSION, n, tags)
            if not h["ok"]:
                return Reply(content=c["description"])
            return Reply(card=_embed(c["title"], c["description"], color=c["color"], fields=c["fields"], footer=f"Aureon MT5 v{VERSION} · git"), source="live")
        await run(inter, "git-history", work, timeout=15)

    bot_group = app_commands.Group(name="discord-bot", description="Bot administration")

    @bot_group.command(name="sync", description="Admin: re-sync the slash commands now (drops stale ones)")
    async def discord_bot_sync(inter: discord.Interaction):
        user = inter.user
        def work():
            perms = getattr(user, "guild_permissions", None)
            if perms is not None and not getattr(perms, "administrator", False):
                return Reply(content="admin only")
            fut = asyncio.run_coroutine_threadsafe(sync_commands(tree, cfg.guild_id), client.loop)
            out = fut.result(timeout=25)
            return Reply(content=f"synced {len(out['final'])} command(s)" + (f" for guild {out['guild']}" if out["guild"] else " globally")
                                 + f" · removed stale: {', '.join(out['removed']) or 'none'} · added: {', '.join(out['added']) or 'none'}", source="live")
        await run(inter, "discord-bot sync", work, timeout=30)

    tree.add_command(bot_group)

    @tree.command(name="alerts", description="List armed price watches; read-only")
    @app_commands.describe(symbol="default: primary symbol")
    async def alerts_cmd(inter: discord.Interaction, symbol: str = ""):
        def work():
            sym = (symbol or cfg.symbols[0]).upper()
            if alerts is None:
                return Reply(content="alerts store unavailable")
            return Reply(content=alerts_mod.list_reply(alerts, sym, current_price(sym, agents.get(sym), cfg))[:1900])
        await run(inter, "alerts", work)

    @tree.command(name="alert-cancel", description="Cancel one armed alert by ID; operator action")
    @app_commands.describe(id="alert id, e.g. A3")
    async def alert_cancel_cmd(inter: discord.Interaction, id: str):
        user = inter.user
        def work():
            if alerts is None:
                return Reply(content="alerts store unavailable")
            a = alerts.cancel(id.strip().upper())
            if a is None:
                return Reply(content=f"no armed alert {id}")
            journal.log("alert_cancelled", symbol=a["symbol"], alert_id=a["id"], price=a["price"], by=str(user))
            return Reply(content=f"cancelled {a['id']} · {a['symbol']} {a['price']:.2f}")
        await run(inter, "alert-cancel", work)

    @tree.command(name="alert-clear", description="Cancel all armed alerts for a symbol; operator action")
    @app_commands.describe(symbol="default: primary symbol")
    async def alert_clear_cmd(inter: discord.Interaction, symbol: str = ""):
        user = inter.user
        def work():
            sym = (symbol or cfg.symbols[0]).upper()
            if alerts is None:
                return Reply(content="alerts store unavailable")
            n = alerts.clear(sym)
            journal.log("alert_cleared", symbol=sym, count=n, by=str(user))
            return Reply(content=f"cleared {n} alert(s) for {sym}")
        await run(inter, "alert-clear", work)

    # ------------------------------------------------------------------ v2.3.0 Codex add-on (all off the loop via run()) — shown, never applied
    # Every call goes through the advisor: same budget (AUREON_CODEX_MAX_CALLS), one call at a time, logs/codex_usage.jsonl.
    from .claude_advisor import build_snapshot
    codex_wait = float(getattr(cfg, "codex_timeout", 30)) * 2 + 15
    codex_off = "Codex add-on is off (`AUREON_CODEX=off`) — set review|advisory and restart. Signals and guardian unaffected."

    @tree.command(name="codex", description="Codex CLI health, login and per-agent attachment (shown only, never applied)")
    async def codex_cmd(inter: discord.Interaction):
        def work():
            login = codex.login_status() if codex is not None else None
            return Reply(card=advisor_card(codex, cfg, agents, "codex", login_line=f"`codex login status`: {login}" if login else None))
        await run(inter, "codex", work, timeout=30)

    @tree.command(name="codex-status", description="Codex usage today/week/month: calls, tokens, cost, latency, errors")
    async def codex_status_cmd(inter: discord.Interaction):
        def work():
            head = (f"mode **{codex.mode.upper()}** · {codex.status().get('version') or 'version not checked (/codex-test)'} · "
                    f"`codex login status`: {codex.login_status()}") if codex is not None else codex_off
            return Reply(card=_embed("AUREON · CODEX USAGE", head + "\nCLI reported usage (tokens when `codex exec --json` exposes them; "
                                     "cost is null unless the CLI reports it).", fields=usage_fields(cfg.log_dir, journal, "codex")))
        await run(inter, "codex-status", work, timeout=30)

    @tree.command(name="codex-attach", description="Re-bind the Codex add-on to a symbol's agent at runtime (advisory only)")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def codex_attach_cmd(inter: discord.Interaction, symbol: str):
        user = inter.user
        def work():
            sym = symbol.upper(); ag = agents.get(sym)
            if ag is None:
                return Reply(content=f"{sym}: not running (configured: {', '.join(cfg.symbols)})")
            if codex is None:
                return Reply(content=codex_off)
            ok, why = codex.attach(ag, str(user))
            return Reply(content=(f"🟢 {sym} · {ag.S.display_name} · Codex {why}" if ok else f"⚪ {sym}: cannot attach — {why}"))
        await run(inter, "codex-attach", work)

    @tree.command(name="codex-detach", description="Detach the Codex add-on from a symbol's agent (cancels its queued jobs)")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def codex_detach_cmd(inter: discord.Interaction, symbol: str):
        user = inter.user
        def work():
            sym = symbol.upper(); ag = agents.get(sym)
            if ag is None:
                return Reply(content=f"{sym}: not running (configured: {', '.join(cfg.symbols)})")
            if codex is None or getattr(ag, "codex", None) is None:
                return Reply(content=f"{sym}: Codex already detached ({'mode=off' if codex is None else 'not attached'})")
            n = codex.detach(ag, str(user))
            return Reply(content=f"⚪ {sym} · {ag.S.display_name} · Codex DETACHED · {n} queued job(s) cancelled · guardian and signals unaffected")
        await run(inter, "codex-detach", work)

    @tree.command(name="codex-test", description="Verify the Codex CLI login with one schema-constrained verdict (consumes one call)")
    async def codex_test_cmd(inter: discord.Interaction):
        def work():
            if codex is None:
                return Reply(content=codex_off)
            if codex._call_lock.locked():
                return Reply(content="Codex already busy. No additional job queued.")
            codex.refresh_version()
            prompt = ('Return a JSON verdict for event ALERT. No market data is provided, '
                      'so decision must be SKIP, side null, confidence low, tighten_to null, '
                      'reason "No market data", evidence empty. JSON only.')
            r = codex._call(prompt, codex.entry_model, "ALERT", kind_override="test")
            if r is None:
                return Reply(content="⛔ daily Codex budget used — no call made")
            journal.log("codex_test", ok=r.ok, latency_s=round(r.latency_s, 2), error=(r.error or "")[:120])
            return Reply(content=(f"🟢 Codex test OK · {r.latency_s:.1f}s · {r.verdict['decision']}" if r.ok
                                  else f"🔴 Codex test failed · {r.error}"), source="live")
        await run(inter, "codex-test", work, timeout=codex_wait)

    @tree.command(name="codex-review", description="Request the Codex day review; uses Codex CLI quota")
    @app_commands.describe(date="YYYY-MM-DD (IST), default today")
    async def codex_review_cmd(inter: discord.Interaction, date: str = ""):
        def work():
            if codex is None:
                return Reply(content=codex_off)
            day = date.strip() or None
            if day:
                datetime.strptime(day, "%Y-%m-%d")
            text = codex.daily_review(day)
            return Reply(content=f"CODEX REVIEW {day or 'today'}:\n{text}"[:1900], source="live")
        await run(inter, "codex-review", work, timeout=max(codex_wait, 150))

    @tree.command(name="codex-alert", description="Read-only Codex evaluation of a frozen closed-bar snapshot")
    @app_commands.describe(symbol="XAUUSD or XAGUSD (default: primary)", side="BUY or SELL")
    async def codex_alert_cmd(inter: discord.Interaction, symbol: str = "", side: str = "BUY"):
        def work():
            if codex is None:
                return Reply(content=codex_off)
            sym = (symbol or cfg.symbols[0]).upper()
            direction = side.upper()
            if direction not in ("BUY", "SELL"):
                return Reply(content="Side must be BUY or SELL")
            ag = agents.get(sym)
            if ag is None or getattr(ag, "last_df", None) is None:
                return Reply(content=f"{sym}: no cached closed-bar snapshot available")
            # Snapshot and bar timestamp are frozen BEFORE the blocking subprocess.
            frame = ag.last_df.copy(deep=True)
            if frame.empty:
                return Reply(content=f"{sym}: empty cached frame")
            bar_t = int(frame["time"].iloc[-1])
            # MT5 bar timestamps are server-local shifted by ag.off hours.
            age = _time.time() - (bar_t - float(ag.off) * 3600)
            if age < -60 or age > 600:
                return Reply(content=f"{sym}: cached bar stale or clock offset invalid; no Codex call")
            if codex._call_lock.locked():
                return Reply(content="Codex busy; no new snapshot queued. Aureon alerts unaffected.")
            snap = build_snapshot(ag, "ALERT", direction, bar_t, frame)
            r = codex._call(codex.prompt_for(snap), codex.entry_model, "ALERT", kind_override="alert")
            if r is None:
                return Reply(content="Codex local daily call limit reached.")
            elapsed = _time.time() - (bar_t - float(ag.off) * 3600)
            # Research verdict only. Never attached to a live trade or an alert's action buttons.
            v = r.verdict or {}
            journal.log("codex_alert", symbol=sym, side=direction, bar_t=bar_t,
                        verdict=v if r.ok else None, error=(r.error or "")[:120],
                        latency_s=round(r.latency_s, 2), stale=elapsed > 300)
            if not r.ok:
                return Reply(content=f"Codex unavailable · {r.error} · original Aureon signals unaffected")
            stale = " · STALE (journal only)" if elapsed > 300 else " · SHOWN ONLY"
            from . import advisor_board
            return Reply(content=(f"{advisor_board.verdict_line('codex', v)}{stale}\n{sym} · {direction} · bar {snap.get('bar_time_ist')} IST · "
                                  f"CLI {r.latency_s:.1f}s · no orders or SL changes"))
        await run(inter, "codex-alert", work, timeout=codex_wait)

    @tree.command(name="codex-rules", description="Show current Codex rule proposals")
    async def codex_rules_cmd(inter: discord.Interaction):
        await run(inter, "codex-rules", lambda: _rules_reply("codex"))

    @tree.command(name="codex-rules-approve", description="Approve one Codex rule proposal into advisor_rules.md; operator action")
    @app_commands.describe(n="proposal number from the CODEX RULE PROPOSALS card")
    async def codex_rules_approve_cmd(inter: discord.Interaction, n: int):
        user = inter.user
        await run(inter, "codex-rules-approve", lambda: _approve_reply("codex", n, user))

    def _rules_reply(source: str) -> Reply:
        from . import compare
        d = compare.load_proposals(cfg.log_dir, source); props = d.get("proposals") or []
        if not props:
            return Reply(content=f"no {source} rule proposals yet (made on Saturday with the COMPARE card)")
        lines = [f"{source} proposals · {d.get('week')} · made {d.get('made')} · rules {compare.rules_hash()}"]
        for p in props:
            lines.append(f"`{p['n']}` {p['line']} — {p['support']}" + (f" · ✅ approved {p['approved']}" if p.get("approved") else ""))
        return Reply(content="\n".join(lines)[:1900])

    def _approve_reply(source: str, n: int, user) -> Reply:
        from . import compare
        line = compare.approve_proposal(n, cfg.log_dir, source=source)
        journal.log(f"{source}_rule_approved", n=n, line=line, by=str(user), rules_hash=compare.rules_hash())
        return Reply(content=f"appended to advisor_rules.md (shared by both advisors):\n{line}\nrules hash now {compare.rules_hash()} · "
                             "takes effect on the next advisor call")

    # ------------------------------------------------------------------ v2.2.0 command directory, taken from actual tree registrations
    _help = {
        "Monitoring": {
            "status": "Primary symbol snapshot — automatic, read-only",
            "agents": "Running agent phases and health — automatic, read-only",
            "parallel-status": "All symbols — automatic, read-only",
            "market": "Market state — automatic, read-only",
            "chart": "Current chart — automatic, read-only",
            "symbols": "Configured symbols — automatic, read-only",
            "symbol-present": "Symbol presence — user supplies symbol",
        },
        "Trading": {
            "take": "Accept pending ask — user places order unless AUREON_EXECUTION=1",
            "skip": "Reject pending ask — user action",
            "alert": "Arm price watch — user chooses price; bot monitors",
            "alerts": "Show armed watches — automatic",
            "alert-cancel": "Cancel one alert — user action",
            "alert-clear": "Clear alerts — user action",
        },
        "Claude": {
            "claude": "Health, attachment and login — read-only",
            "claude-status": "Usage and spend — automatic ledger",
            "claude-test": "One CLI test — consumes usage",
            "claude-review": "Review day — consumes usage",
            "claude-rules": "Pending rule proposals — read-only",
            "claude-rules-approve": "Approve rule proposal — manual operator",
            "claude-report": "Alias of /advisor-report — historical, read-only",
        },
        "Codex": {
            "codex": "Health, login and attachment — read-only; verdicts shown, never applied",
            "codex-status": "Usage and spend — automatic ledger",
            "codex-test": "One CLI test — consumes usage",
            "codex-review": "Review day — consumes usage",
            "codex-alert": "Frozen-snapshot verdict on demand — consumes usage",
            "codex-attach": "Re-bind to a symbol — operator",
            "codex-detach": "Detach from a symbol — operator",
            "codex-rules": "Pending Codex rule proposals — read-only",
            "codex-rules-approve": "Approve Codex rule proposal — manual operator",
        },
        "Reports": {
            "report": "Weekly trade summary — automatic",
            "compare": "Detector, Claude, Codex, trader outcomes — automatic",
            "advisor-report": "Claude and Codex performance, head-to-head — historical, read-only",
            "compare-breakdown": "Compare by signal/session — automatic",
            "git-history": "Commit history — automatic",
            "pull-history": "MT5 history — automatic",
        },
        "Control": {
            "restart": "Restart bot — admin only",
            "mode": "Change process mode and restart — admin only",
            "discord-bot": "Bot administration — admin",
        },
    }

    @tree.command(name="commands", description="Grouped Aureon command directory: automatic versus user actions")
    async def commands_cmd(inter: discord.Interaction):
        def work():
            groups = command_directory(tree, _help)
            fields = [_field(group, "\n".join(lines)[:1024], False)
                      for group, lines in groups.items()]
            return Reply(card=_embed("AUREON · COMMANDS", "Only registered commands appear.", fields=fields))
        await run(inter, "commands", work)

    @tree.command(name="claude-status", description="Claude usage today/week/month: calls, tokens, cost, latency, errors")
    async def claude_status_cmd(inter: discord.Interaction):
        def work():
            return Reply(card=_embed("AUREON · CLAUDE USAGE", "CLI reported usage; not independently priced at model list rates.",
                                     fields=usage_fields(cfg.log_dir, journal, "claude")))
        await run(inter, "claude-status", work)

    def _advisor_report_reply(period: str) -> Reply:
        from .claude_report import advisor_report
        if period not in ("week", "month"):
            return Reply(content="Choose period: week or month")
        r = advisor_report(cfg, agents, journal, period)
        return Reply(card=_embed(r["title"], r["description"], fields=r["fields"][:25], footer=f"v{VERSION} · as of now · partial grading possible"))

    @tree.command(name="advisor-report", description="Claude and Codex graded performance, head-to-head and usage (week or month)")
    @app_commands.describe(period="week or month")
    async def advisor_report_cmd(inter: discord.Interaction, period: str = "week"):
        await run(inter, "advisor-report", lambda: _advisor_report_reply(period), timeout=40)

    @tree.command(name="claude-report", description="Alias of /advisor-report (Claude and Codex, week or month)")
    @app_commands.describe(period="week or month")
    async def claude_report_cmd(inter: discord.Interaction, period: str = "week"):
        await run(inter, "claude-report", lambda: _advisor_report_reply(period), timeout=40)

    # ------------------------------------------------------------------ v2.2.0 admin controls
    from . import ops

    def _restart_later(*args):
        # after the followup is sent: restart_process stops agents and re-execs (the process does not return)
        threading.Timer(2.0, ops.restart_process, args=(agents, journal, notify) + args,
                        kwargs={"log_dir": cfg.log_dir}).start()

    @tree.command(name="restart", description="Admin: stop agents cleanly and restart process; requires role")
    @app_commands.describe(reason="Reason shown on the BACK ONLINE card")
    async def restart_cmd(inter: discord.Interaction, reason: str = ""):
        def work():
            if not ops.admin_allowed(inter.user, os.environ.get("AUREON_ADMIN_ROLE_ID", "")):
                return Reply(content="Denied: configured administrator role required")
            # v2.3.6: list what will be resumed
            lines = []
            for sym, ag in agents.items():
                snap = getattr(ag, "snapshot", None) or {}
                pos = snap.get("positions") or []
                for p in pos:
                    st = (snap.get("state") or {}).get(p["ticket"], (snap.get("state") or {}).get(str(p["ticket"]), {}))
                    sec = st.get("secured", 0)
                    lines.append(f"#{p['ticket']} {p['direction'].upper()} +{sec:g} secured · {p['points']:+.1f} pts")
                armed = len(ag.alerts.armed(sym)) if ag.alerts else 0
                if armed:
                    lines.append(f"{sym}: {armed} armed alert(s)")
                if ag.pending_alert:
                    lines.append(f"{sym}: pending TAKE (alert {ag.pending_alert.get('alert_id')})")
            summary = "\n".join(lines) if lines else "no open positions, alerts or pending TAKEs"
            _restart_later(inter.user, reason)
            return Reply(content=f"RESTARTING · {reason or 'manual'}\n**Will resume:**\n{summary}")
        await run(inter, "restart", work)

    @tree.command(name="mode", description="Admin: change EMA process mode and restart; rejects open positions")
    @app_commands.describe(mode="ema2050 or ema5080", symbols="Comma-separated symbols; blank preserves existing", force="Override open-position gate (unsafe)")
    async def mode_cmd(inter: discord.Interaction, mode: str, symbols: str = "", force: bool = False):
        def work():
            if not ops.admin_allowed(inter.user, os.environ.get("AUREON_ADMIN_ROLE_ID", "")):
                return Reply(content="Denied: configured administrator role required")
            if mode not in ("ema2050", "ema5080"):
                return Reply(content="Unsupported mode; use ema2050 or ema5080")
            if not force and ops.positions_block_mode_change(agents):
                return Reply(content="position open or position status unverified — /mode after it closes or pass force:true")
            # .env is the deployment source of truth; never write to a different unknown path.
            env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
            try:
                names = ops.write_mode(env_path, mode, symbols or None)
            except Exception as exc:
                return Reply(content=f"MODE NOT CHANGED: {exc}")
            _restart_later(inter.user, f"mode change to {mode}", mode, names)
            return Reply(content=f"MODE CHANGE → {mode} · restarting")
        await run(inter, "mode", work)

    # ------------------------------------------------------------------ v1.10.0 Claude add-on (all off the loop via run())
    claude_wait = float(getattr(cfg, "claude_timeout", 90)) * 2 + 15      # a call may queue behind one in flight

    @tree.command(name="claude", description="Claude CLI health, authentication and attachment status")
    async def claude_cmd(inter: discord.Interaction):
        await run(inter, "claude", lambda: Reply(card=claude_card(claude, cfg, agents)))

    @tree.command(name="claude-attach", description="Re-bind the Claude add-on to a symbol's agent at runtime (advisory/manage only)")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def claude_attach_cmd(inter: discord.Interaction, symbol: str):
        user = inter.user
        def work():
            sym = symbol.upper(); ag = agents.get(sym)
            if ag is None:
                return Reply(content=f"{sym}: not running (configured: {', '.join(cfg.symbols)})")
            if claude is None:
                return Reply(content="Claude add-on is off (`AUREON_CLAUDE=off`) — set advisory|manage and restart")
            ok, why = claude.attach(ag, str(user))
            return Reply(content=(f"🟢 {sym} · {ag.S.display_name} · {why}" if ok else f"⚪ {sym}: cannot attach — {why}"))
        await run(inter, "claude-attach", work)

    @tree.command(name="claude-detach", description="Detach the Claude add-on from a symbol's agent (cancels its queued jobs)")
    @app_commands.describe(symbol="e.g. XAUUSD")
    async def claude_detach_cmd(inter: discord.Interaction, symbol: str):
        user = inter.user
        def work():
            sym = symbol.upper(); ag = agents.get(sym)
            if ag is None:
                return Reply(content=f"{sym}: not running (configured: {', '.join(cfg.symbols)})")
            if claude is None or ag.claude is None:
                return Reply(content=f"{sym}: already detached ({'mode=off' if claude is None else 'not attached'})")
            n = claude.detach(ag, str(user))
            return Reply(content=f"⚪ {sym} · {ag.S.display_name} · DETACHED · {n} queued job(s) cancelled · guardian and signals unaffected")
        await run(inter, "claude-detach", work)

    @tree.command(name="claude-test", description="Verify Claude login by consuming one CLI test call")
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

    @tree.command(name="claude-review", description="Request Claude day review; uses CLI quota")
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

    @tree.command(name="claude-rules", description="Show current Claude rule proposals")
    async def claude_rules_cmd(inter: discord.Interaction):
        await run(inter, "claude-rules", lambda: _rules_reply("claude"))

    @tree.command(name="claude-rules-approve", description="Approve one Claude rule proposal into advisor_rules.md; operator action")
    @app_commands.describe(n="proposal number from the CLAUDE RULE PROPOSALS card")
    async def claude_rules_approve_cmd(inter: discord.Interaction, n: int):
        user = inter.user
        await run(inter, "claude-rules-approve", lambda: _approve_reply("claude", n, user))

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

    @tree.command(name="compare", description="Compare detector, Claude and trader on graded signals")
    @app_commands.describe(days="window in days (default 7)", private="only you see the answer")
    async def compare_cmd(inter: discord.Interaction, days: int = 7, private: bool = False):
        await run(inter, "compare", lambda: _compare_reply(days, False), ephemeral=private, timeout=30)

    @tree.command(name="compare-breakdown", description="Compare signals by event type and market session")
    @app_commands.describe(days="window in days (default 7)")
    async def compare_breakdown_cmd(inter: discord.Interaction, days: int = 7):
        await run(inter, "compare-breakdown", lambda: _compare_reply(days, True), timeout=30)

    def _runner():
        asyncio.set_event_loop(asyncio.new_event_loop())
        client.run(cfg.bot_token, log_handler=None)

    threading.Thread(target=_runner, daemon=True, name="discord-bot").start()
    return monitor
