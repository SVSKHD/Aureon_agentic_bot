"""v2.4.1 — control-mode state machine.

MANUAL → AGENT via /agent-on (or scheduled windows, v2.4.3).
AGENT → MANUAL via /manual, timeout, HALT, governor daily-loss-cap hit, MT5 disconnect > 5 min, manual order on that symbol.

Every transition: guardian_state saved, journal "control_mode", one card.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
GREEN = 0x57F287
YELLOW = 0xFEE75C


def current_mode(journal, symbol: str) -> dict:
    """Returns {"mode", "since", "by", "until"}. Handles expired AGENT → returns MANUAL."""
    ctrl = journal.load_control(symbol)
    if ctrl["mode"] == "AGENT" and ctrl.get("until") is not None:
        if _time.time() > ctrl["until"]:
            return {"mode": "MANUAL", "since": None, "by": None, "until": None, "expired": True}
    return ctrl


def to_agent(journal, notify, symbol: str, by: str, minutes: int = 60) -> dict:
    """MANUAL → AGENT. Returns the new control dict."""
    until = _time.time() + minutes * 60
    journal.save_control(symbol, "AGENT", by=by, until=until)
    journal.decision("control_mode", actor="human", rationale=f"/agent-on {minutes}m",
                     symbol=symbol, **{"from": "MANUAL", "to": "AGENT", "by": by, "until": until})
    until_ist = datetime.fromtimestamp(until, tz=IST).strftime("%H:%M")
    notify.card(f"🤖 AGENT ON · {symbol}", f"until {until_ist} IST · by {by}",
                footer=f"Aureon MT5 · control · {datetime.now(IST):%H:%M} IST")
    return journal.load_control(symbol)


def to_manual(journal, notify, symbol: str, by: str, reason: str) -> dict:
    """AGENT → MANUAL. Returns the new control dict."""
    journal.save_control(symbol, "MANUAL", by=by)
    journal.decision("control_mode", actor="system" if by == "system" else "human",
                     rationale=reason, symbol=symbol,
                     **{"from": "AGENT", "to": "MANUAL", "by": by, "reason": reason})
    notify.card(f"🧑 MANUAL · {symbol}", f"{reason} · by {by}",
                footer=f"Aureon MT5 · control · {datetime.now(IST):%H:%M} IST")
    return journal.load_control(symbol)


def check_auto_transitions(journal, notify, symbol: str, agents: dict, cfg) -> str | None:
    """Called every poll. Returns the reason if AGENT → MANUAL was forced, else None."""
    # Check raw persisted state first for timeout detection
    raw = journal.load_control(symbol)
    if raw["mode"] != "AGENT":
        return None

    # Timeout: until has passed
    if raw.get("until") is not None and _time.time() > raw["until"]:
        to_manual(journal, notify, symbol, "system", "timeout")
        return "timeout"

    ctrl = raw

    # HALT
    from .halt import is_halted
    if is_halted(cfg.log_dir):
        to_manual(journal, notify, symbol, "system", "HALT")
        return "HALT"

    # Governor daily-loss-cap
    from .governor import Governor
    gov = Governor(cfg)
    try:
        from .bot import _governor_context
        ctx = _governor_context(cfg, agents, symbol)
        dec = gov.check(symbol, "LONG", 0.01, ctx)
        if not dec.allowed and dec.rule == "daily_loss_cap":
            to_manual(journal, notify, symbol, "system", f"governor: {dec.rule}")
            return f"governor: {dec.rule}"
    except Exception:
        pass

    # MT5 disconnect > 5 min — check watchdog incidents
    try:
        import json, os
        wd_path = os.path.join(cfg.log_dir, "watchdog.json")
        with open(wd_path, encoding="utf-8") as f:
            wd_data = json.load(f)
        mt5_inc = wd_data.get("incidents", {}).get("mt5", {})
        if mt5_inc.get("open") and mt5_inc.get("open_t") and _time.time() - mt5_inc["open_t"] > 300:
            to_manual(journal, notify, symbol, "system", "MT5 disconnect > 5 min")
            return "MT5 disconnect > 5 min"
    except Exception:
        pass

    return None


def mode_label(journal, symbol: str) -> str:
    """One-line label for /status and /agents."""
    ctrl = current_mode(journal, symbol)
    if ctrl["mode"] == "AGENT":
        until = ctrl.get("until")
        if until:
            remaining = max(0, int((until - _time.time()) / 60))
            return f"🤖 AGENT ({remaining}m left)"
        return "🤖 AGENT"
    return "🧑 MANUAL"
