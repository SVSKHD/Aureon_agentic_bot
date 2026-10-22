"""v2.17.0 — /auto-trade: the runtime switch for automatic placement (ema921 — market now, pending orders too).

    state       cfg.auto_trade. Boot default = AUREON_AUTO (cfg.auto_mode); `/auto-trade on|off` persists it in
                logs/features.json as `auto_trade` (source discord > env), so it survives restarts.
    off         every automatic placement stops at once. Crosses are still detected, journaled
                cross_skipped why="auto-trade off", and the SIGNAL card (no order line) is posted so /place can be used.
                Never closes or cancels anything.
    on          applies from the next bar close: a cross on a bar that closed before the switch is never taken.
                Refused (with the reason) when execution is off, on a live account without AUREON_ALLOW_LIVE, without an
                explicit AUREON_MAX_LOTS <= 1.0 (force overrides the lots check only), or while the drawdown guard is tripped.
                Never touches open positions.
    journal     auto_trade_changed {by, from, to, reason}.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import features

IST = timezone(timedelta(hours=5, minutes=30))
NAME = "auto_trade"
AUTO_MODES = ("ema921",)


def is_on(cfg) -> bool:
    return bool(getattr(cfg, "auto_trade", getattr(cfg, "auto_mode", False)))


def since(cfg) -> float | None:
    """When the switch was last set from Discord (epoch), None when it is the boot default."""
    meta = features._load_meta(cfg.log_dir).get(NAME) or {}
    return float(meta["t"]) if meta.get("t") else None


def source(cfg, environ=None) -> str:
    return features.source(NAME, features.load_overrides(cfg.log_dir), environ)


def on_for_bar(cfg, bar_t: int, bar_s: int, off_h: float) -> bool:
    """On, and already on when this bar CLOSED (bar times are server-clock epochs: close = bar_t + bar_s − offset)."""
    if not is_on(cfg):
        return False
    t = since(cfg)
    return t is None or t <= int(bar_t) + int(bar_s) - float(off_h) * 3600 + 1


def paper(cfg) -> bool:
    return bool(getattr(cfg, "paper", False) or getattr(cfg, "dry", False))


def status_line(cfg, environ=None) -> str:
    t = since(cfg)
    when = f" · since {datetime.fromtimestamp(t, IST):%a %d %b %H:%M} IST" if t else ""
    if not is_on(cfg):
        return f"⬜ off · signals only ({source(cfg, environ)}){when}"
    return f"✅ on · {cfg.mode} · {cfg.max_lots:g} lots · {'paper' if paper(cfg) else 'live'} ({source(cfg, environ)}){when}"


# ----------------------------------------------------------------------------- refusals
def dd_tripped(agents) -> str | None:
    for sym, ag in (agents or {}).items():
        fn = getattr(ag, "_equity_dd_block", None)
        if fn is None:
            continue
        try:
            why = fn()
        except Exception:
            why = None
        if why:
            return f"{sym}: {why}"
    return None


def refusal(cfg, agents, *, force: bool = False, is_demo=None, environ=None) -> str | None:
    """Why `on` must be refused, or None. Paper never sends an order, so the execution / live checks do not apply to it."""
    env = os.environ if environ is None else environ
    if not paper(cfg):
        if not cfg.execution_enabled:
            return "AUREON_EXECUTION=0 — orders are off; set AUREON_EXECUTION=1 (or AUREON_PAPER=1) and restart"
        if is_demo is None:
            from . import broker
            is_demo = broker.is_demo
        demo = is_demo()
        if demo is not True and not cfg.allow_live:
            return ("live account and AUREON_ALLOW_LIVE=0" if demo is False
                    else "account type unknown (MT5 not answering) and AUREON_ALLOW_LIVE=0")
    raw = str(env.get("AUREON_MAX_LOTS", "") or "").strip()
    if not force:
        if not raw:
            return "AUREON_MAX_LOTS is not set — set it explicitly (≤ 1.0) or pass force:true"
        if float(cfg.max_lots) > 1.0:
            return f"AUREON_MAX_LOTS={cfg.max_lots:g} > 1.0 — pass force:true to allow it"
    dd = dd_tripped(agents)
    if dd:
        return f"drawdown guard tripped — {dd}"
    return None


# ----------------------------------------------------------------------------- the switch
def set_state(cfg, agents, journal, notify, user, want: bool, *, force: bool = False, reason: str = "",
              is_demo=None, environ=None) -> str:
    """Admin checks are the caller's. Returns the reply text."""
    old = is_on(cfg)
    if want:
        why = refusal(cfg, agents, force=force, is_demo=is_demo, environ=environ)
        if why:
            journal.log("auto_trade_refused", by=str(user), why=why)
            return f"⛔ auto-trade stays {'on' if old else 'off'} — {why}"
    features.set_override(cfg.log_dir, cfg, NAME, want, str(user))
    cancelled = []
    if not want:                                     # an automatic pending order must not fill after off (positions untouched)
        for sym, ag in (agents or {}).items():
            fn = getattr(ag, "_cancel_pending921", None)
            if fn is not None:
                try:
                    if fn("auto-trade off", only_by="auto"):
                        cancelled.append(sym)
                except Exception:
                    pass
    journal.log("auto_trade_changed", by=str(user), **{"from": "on" if old else "off"}, to="on" if want else "off",
                reason=reason or "", force=bool(force))
    title, body = card(cfg, want, user)
    if cancelled:
        body += f"\nautomatic pending order cancelled: {', '.join(cancelled)}"
    try:
        notify.card(title, body, color=0x2ECC71 if want else 0x95A5A6, footer="Aureon MT5 · auto-trade")
    except Exception:
        pass
    return f"**{title}**\n{body}"


def card(cfg, on: bool, user="") -> tuple[str, str]:
    if on:
        title = f"AUTO-TRADE ON · {cfg.mode} · {cfg.max_lots:g} · {'paper' if paper(cfg) else 'live'}"
        body = (f"by {user} · applies from the next bar close — a cross that fired while off is never taken · open positions untouched"
                + ("" if cfg.mode in AUTO_MODES else f"\n{cfg.mode} has no automatic placement — the switch only matters for ema921"))
    else:
        title = "AUTO-TRADE OFF · signals only"
        body = f"by {user} · crosses still post a SIGNAL card (no order) — /place to take one by hand · no position was closed"
    return title, body


def status_text(cfg, environ=None) -> str:
    return f"auto-trade {status_line(cfg, environ)}"


# ----------------------------------------------------------------------------- weekly card line
def week_line(rows: list[dict], week_start: float, now: float, current: bool) -> str:
    """`auto-trade: on 5/5 days`, or the off periods (IST). rows = journal rows since the week start (any events)."""
    ch = sorted((r for r in rows if r.get("event") == "auto_trade_changed" and float(r.get("t", 0)) >= week_start),
                key=lambda r: float(r["t"]))
    state = (ch[0].get("from") == "on") if ch else bool(current)
    spans, t0 = [], week_start
    for r in ch:
        new = r.get("to") == "on"
        if new != state:
            spans.append((t0, float(r["t"]), state)); t0 = float(r["t"]); state = new
    spans.append((t0, now, state))
    days = sum(1 for i in range(7) if (week_start + i * 86400) < now and datetime.fromtimestamp(week_start + i * 86400, IST).weekday() < 5)
    days = max(1, min(days, 5))
    offs = [(a, b) for a, b, s in spans if not s and b > a]
    if not offs:
        return f"auto-trade: on {days}/{days} days"
    if len(offs) == 1 and offs[0][0] <= week_start and offs[0][1] >= now:
        return "auto-trade: off all week (signals only)"
    fmt = lambda t: datetime.fromtimestamp(t, IST).strftime("%a %H:%M")
    return "auto-trade: off " + ", ".join(f"{fmt(a)} → {'now' if b >= now else fmt(b)}" for a, b in offs) + " IST"

