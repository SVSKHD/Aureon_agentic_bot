"""v2.16.1 — manual orders for every mode: /place, /sl, /tp (and the existing /close).

    /place long|short [lots] [sl=<pts|price>] [tp=<pts|price>]
        Defaults from the active mode: ema921 → its Rules (sl 30, tp none) · every other mode → its guardian pre_stop, no tp.
        A bare number ≤ 200 is points from entry (a distance: the stop goes against the trade, the TP with it); larger is a price.
        Policy (same as an automatic entry): governor · max lots cap · drawdown guard · AUREON_EXECUTION=1 · demo or
        AUREON_ALLOW_LIVE=1. AUREON_PAPER=1 → a paper position in the ema921 tracker instead. No cross needed — the human override.
        The position is then adopted by the mode's guardian exactly like an auto entry. Journal: order_placed actor=human
        rationale="/place" (paper: paper_fill actor=human).

    /sl <pts|price> [ticket] [force]      /tp <pts|price|off> [ticket]
        Points are relative to entry, signed from the trade's side: /sl -20, /sl +15, /tp +60. /tp off removes the TP.
        ONE TRADE_ACTION_SLTP carrying BOTH values (MT5 overwrites both). An SL that loosens a locked (in-profit) level is
        refused unless force. Journal: sltp_modified with before / after.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import broker
from .common.timeutil import IST

POINTS_MAX = 200.0


# ----------------------------------------------------------------------------- parsing
def parse_value(raw) -> tuple[str, float | None]:
    """'30' / '+15' / '-20' → ("pts", v) · '2450.5' → ("price", v) · 'off' / 'none' / '0' → ("off", None)."""
    s = str(raw).strip().lower()
    for pre in ("sl=", "tp=", "sl:", "tp:"):
        if s.startswith(pre):
            s = s[len(pre):]
    if s in ("off", "none", "0", "0.0", ""):
        return "off", None
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"not a number: {raw!r} (points ≤ {POINTS_MAX:g}, a price, or off)")
    return ("pts", v) if abs(v) <= POINTS_MAX else ("price", v)


def parse_place(text: str) -> dict:
    """'short 0.24 sl=25 tp=2450' → {side, lots, sl, tp} (sl / tp raw strings or None). Positional order: side lots."""
    toks = text.split()
    if not toks or toks[0].lower() not in ("long", "short", "buy", "sell"):
        raise ValueError("usage: /place long|short [lots] [sl=<pts|price>] [tp=<pts|price>]")
    out = {"side": "LONG" if toks[0].lower() in ("long", "buy") else "SHORT", "lots": None, "sl": None, "tp": None}
    for t in toks[1:]:
        k = t.lower()
        if k.startswith(("sl=", "sl:")):
            out["sl"] = t[3:]
        elif k.startswith(("tp=", "tp:")):
            out["tp"] = t[3:]
        elif out["lots"] is None and re.fullmatch(r"\d+(\.\d+)?", t):
            out["lots"] = float(t)
        else:
            raise ValueError(f"unexpected argument {t!r}")
    return out


def mode_defaults(S, symbol: str) -> tuple[float | None, float | None]:
    """(sl points, tp points | None) of the active mode."""
    if getattr(S, "name", "") == "ema921":
        r = S.rules_for(symbol)
        return float(r.stop), (float(r.tp) if r.tp else None)
    g = S.guardian_for(symbol)
    return (float(g.pre_stop) if g else None), None


def place_levels(side: str, entry: float, sl_raw, tp_raw, defaults: tuple) -> tuple[float | None, float | None]:
    """The /place SL / TP prices. Points are DISTANCES: the SL against the trade, the TP with it."""
    s = 1 if side == "LONG" else -1
    def level(raw, default_pts, sign):
        if raw is None:
            return (entry + sign * s * default_pts) if default_pts else None
        kind, v = parse_value(raw)
        if kind == "off":
            return None
        return (entry + sign * s * abs(v)) if kind == "pts" else v
    sl = level(sl_raw, defaults[0], -1); tp = level(tp_raw, defaults[1], +1)
    if sl is not None and (sl - entry) * s >= 0:
        raise ValueError(f"SL {sl:.2f} is not on the losing side of a {side} at {entry:.2f}")
    if tp is not None and (tp - entry) * s <= 0:
        raise ValueError(f"TP {tp:.2f} is not on the winning side of a {side} at {entry:.2f}")
    return sl, tp


def modify_level(p: dict, raw) -> float | None:
    """/sl /tp value → price. Points are signed from the trade's side relative to entry; 'off' → None."""
    kind, v = parse_value(raw)
    if kind == "off":
        return None
    s = 1 if p["direction"] == "long" else -1
    return p["price_open"] + s * v if kind == "pts" else v


def _pts(p: dict, price: float | None) -> str:
    if not price:
        return "none"
    s = 1 if p["direction"] == "long" else -1
    return f"{price:.2f} ({(price - p['price_open']) * s:+.1f})"


# ----------------------------------------------------------------------------- /place
def place(cfg, agents: dict, journal, agent, side: str, *, lots=None, sl_raw=None, tp_raw=None, user="human",
          governor_ctx=None) -> dict:
    """→ {"ok", "text", "card", "placed", "paper", "ticket"}. Never raises on a refusal (ValueError → refused)."""
    from .governor import Governor
    S = agent.S; sym = agent.symbol; side = side.upper()
    paper = bool(cfg.paper or cfg.dry)
    tk = None if cfg.dry else broker.tick(sym)
    entry = (tk["ask"] if side == "LONG" else tk["bid"]) if tk else float(getattr(agent, "health", {}).get("close") or 0.0)
    if not entry:
        return _refused("no price (MT5 not connected)")
    try:
        sl, tp = place_levels(side, entry, sl_raw, tp_raw, mode_defaults(S, sym))
    except ValueError as e:
        return _refused(str(e))
    stop_dist = abs(entry - sl) if sl else None
    want = float(lots) if lots else float(((agent._suggested(stop_dist) or {}).get("lots") if stop_dist else None) or cfg.max_lots)
    lots_ = min(want, float(cfg.max_lots)); over_cap = want > cfg.max_lots
    from . import position_cap                                           # v2.16.2: max one open position per symbol
    capped = position_cap.block(sym, side, journal=journal, source="/place", by=str(user),
                                paper_positions=(agent._paper_open() if paper and S.name == "ema921" else ()))
    if capped:
        return _refused(capped)
    if governor_ctx is None:
        from .bot import _governor_context
        governor_ctx = _governor_context(cfg, agents, sym)
    dec = Governor(cfg).check(sym, side, lots_, governor_ctx)
    if not dec.allowed:
        journal.decision("governor_block", actor="system", rationale=f"{dec.rule}: {dec.detail}", symbol=sym, side=side, lots=lots_,
                         rule=dec.rule, detail=dec.detail, by=str(user), source="/place")
        return _refused(f"governor · {dec.rule} · {dec.detail}")
    dd = agent._equity_dd_block() if hasattr(agent, "_equity_dd_block") else None
    if dd:
        return _refused(dd)
    if paper:
        if S.name != "ema921":
            return _refused("AUREON_PAPER=1: the paper tracker is ema921's — /place needs AUREON_PAPER=0 in this mode")
        from . import ema921_live as L
        bar = int(getattr(agent, "last_bar", 0) or 0)
        st = agent._state921(); pos = L.new_paper(side, entry, lots_, sl, tp, bar)
        pos["id"] = f"P{bar}m"; st["paper"] = list(st.get("paper") or []) + [pos]; agent._save921()
        journal.decision("paper_fill", actor="human", rationale="/place", symbol=sym, mode=S.name, side=side, lots=lots_, price=entry,
                         sl=sl, tp=tp, bar=bar, by=str(user))
        return _placed(agent, side, lots_, entry, sl, tp, ticket=pos["id"], status="PAPER", paper=True, capped=over_cap, cfg=cfg)
    if not cfg.execution_enabled:
        return _refused("execution off (AUREON_EXECUTION=0)")
    demo = broker.is_demo()
    if not (demo or cfg.allow_live):
        return _refused("live account — AUREON_ALLOW_LIVE=1 needed" if demo is False else "account type unknown — refused")
    r = broker.place_market(sym, side, lots_, sl, comment="aureon /place", tp=tp)
    journal.decision("order_placed", actor="human", rationale="/place", symbol=sym, mode=S.name, side=side, lots=lots_, sl=sl, tp=tp,
                     ok=bool(r.ok), status=r.status, retcode=r.retcode, ticket=r.ticket, fill=r.price, fill_mode=r.fill_mode, by=str(user))
    if not r.ok:
        return _refused(f"order rejected ({r.why()})")
    if hasattr(agent, "closed_by_aureon") and r.ticket:
        agent.closed_by_aureon.discard(r.ticket)
    return _placed(agent, side, lots_, r.price or entry, sl, tp, ticket=r.ticket, status=f"{r.status} · {r.fill_mode}", paper=False,
                   capped=over_cap, cfg=cfg)


def _refused(why: str) -> dict:
    return {"ok": False, "text": f"⛔ not placed — {why}", "card": None, "placed": False, "paper": False, "ticket": None}


def _placed(agent, side, lots, fill, sl, tp, *, ticket, status, paper, capped, cfg) -> dict:
    card = enter_card(agent, side, lots, fill, sl, tp, ticket=ticket, status=status, paper=paper, capped=capped, cfg=cfg)
    tag = "PAPER " if paper else ""
    text = (f"✅ {tag}PLACED {side} {lots:g} lots {agent.symbol} · ticket {ticket} · fill {fill:.2f} · SL {sl:.2f}" if sl else
            f"✅ {tag}PLACED {side} {lots:g} lots {agent.symbol} · ticket {ticket} · fill {fill:.2f} · SL none") + \
           (f" · TP {tp:.2f}" if tp else " · TP none") + (f" (capped at max {cfg.max_lots:g})" if capped else "")
    return {"ok": True, "text": text, "card": card, "placed": not paper, "paper": paper, "ticket": ticket}


def enter_card(agent, side, lots, fill, sl, tp, *, ticket, status, paper, capped, cfg) -> dict:
    """The ENTER card the auto entry posts, titled PLACED <side>, with ticket / fill / SL / TP."""
    S = agent.S; s = 1 if side == "LONG" else -1; f = []
    add = lambda n, v, inline=True: f.append({"name": n, "value": str(v)[:1024], "inline": inline})
    add("Side", f"**{side}**"); add("Entry", f"**{fill:.2f}** (market · /place)")
    add("Lot", f"{lots:g}" + (" · PAPER" if paper else "") + (f" · capped at {cfg.max_lots:g}" if capped else ""))
    df = getattr(agent, "last_df", None)
    if S.name == "ema921" and df is not None and len(df):
        row = df.iloc[-1]; r = S.rules_for(agent.symbol)
        add("EMA 9 / 21", f"{float(row['ema9']):.2f} / {float(row['ema21']):.2f}")
        if row["ema200"] == row["ema200"]:
            add("EMA 200", f"{float(row['ema200']):.2f} ({fill - float(row['ema200']):+.1f})")
        add("Stop / TP", f"{sl:.2f} ({(sl - fill) * s:+.1f}) / " + (f"{tp:.2f} ({(tp - fill) * s:+.1f})" if tp else "none") if sl else
            "none / " + (f"{tp:.2f}" if tp else "none"), inline=False)
        add("Lock", f"+{r.lock_at:g} seen at a bar close → SL {fill + s * r.lock_to:.2f} (+{r.lock_to:g})")
        add("Exit", "first M30 close through EMA 9" + (f" · EMA 200 after +{r.lock_at:g}" if r.exit_mode == "e9>e200" else "")
            + f" · Friday flat {r.friday_flat_hour:g}:00 server")
    else:
        add("SL", f"{sl:.2f} ({(sl - fill) * s:+.1f})" if sl else "none"); add("TP", f"{tp:.2f} ({(tp - fill) * s:+.1f})" if tp else "none")
        g = S.guardian_for(agent.symbol)
        add("Manage", (g.note or S.display_name) if g else "no guardian profile — signal only", inline=False)
    add("Order", f"{status} · ticket {ticket}", inline=False)
    return {"title": f"{agent.symbol} · MT5 · PLACED {side}{' · PAPER' if paper else ''}", "fields": f,
            "color": 0x2ECC71 if side == "LONG" else 0xE74C3C,
            "footer": {"text": f"{S.display_name} · /place · {datetime.now(IST):%H:%M} IST"}}


# ----------------------------------------------------------------------------- /sl /tp
def pick_position(positions: list[dict], ticket) -> tuple[dict | None, str | None]:
    if ticket:
        p = next((x for x in positions if int(x["ticket"]) == int(ticket)), None)
        return (p, None) if p else (None, f"no open position #{ticket}")
    if not positions:
        return None, "no open position"
    if len(positions) > 1:
        return None, "more than one position — name it: " + ", ".join(f"#{x['ticket']}" for x in positions)
    return positions[0], None


def modify(cfg, journal, agent, which: str, raw, *, ticket=None, force: bool = False, user="human") -> dict:
    """/sl or /tp. → {"ok", "text"}. ONE SLTP request with both values; loosening a locked SL needs force."""
    which = which.lower(); sym = agent.symbol
    pos = broker.positions(sym)
    p, why = pick_position(pos, ticket)
    if p is None:
        return {"ok": False, "text": f"⛔ /{which} — {why}"}
    s = 1 if p["direction"] == "long" else -1
    try:
        new = modify_level(p, raw)
    except ValueError as e:
        return {"ok": False, "text": f"⛔ /{which} — {e}"}
    cur_sl, cur_tp = (p.get("sl") or None), (p.get("tp") or None)
    px = p.get("current") or p["price_open"]
    if which == "sl":
        if new is None and not force:
            return {"ok": False, "text": "⛔ /sl off would remove the stop — add force:true"}
        if new is not None and (new - px) * s >= 0:
            return {"ok": False, "text": f"⛔ /sl {new:.2f} is through the market ({px:.2f}) — the broker would reject it"}
        locked = bool(cur_sl) and (cur_sl - p["price_open"]) * s > 0
        if locked and new is not None and (new - cur_sl) * s < 0 and not force:
            return {"ok": False, "text": f"⛔ /sl {_pts(p, new)} loosens the locked level {_pts(p, cur_sl)} — add force:true to do it"}
        sl, tp = new, cur_tp
    else:
        if new is not None and (new - px) * s <= 0:
            return {"ok": False, "text": f"⛔ /tp {new:.2f} is through the market ({px:.2f}) — the broker would reject it"}
        sl, tp = cur_sl, new
    r = broker.modify_sltp(p["ticket"], sym, sl, tp)
    journal.decision("sltp_modified", actor="human", rationale=f"/{which}", symbol=sym, mode=agent.S.name, ticket=p["ticket"],
                     sl_before=cur_sl, sl_after=sl, tp_before=cur_tp, tp_after=tp, ok=bool(r.ok), status=r.status, retcode=r.retcode,
                     forced=bool(force), by=str(user))
    if not r.ok:
        return {"ok": False, "text": f"⛔ /{which} rejected by the broker ({r.why()})"}
    what = (f"SL {_pts(p, cur_sl)} → {_pts(p, sl)}" if which == "sl" else f"TP {_pts(p, cur_tp)} → {_pts(p, tp)}")
    return {"ok": True, "text": f"✅ #{p['ticket']} {p['direction'].upper()} {sym} · {what}" + (" · forced" if force and which == "sl" else "")}
