"""v2.0.0 — /alert price alerts (both modes). Reads the active mode's rules; never adds one.

  /alert <price> [symbol] [note]  → armed, stored in logs/alerts.json (restart-safe), one list per symbol
  /alerts · /alert-cancel <id> · /alert-clear [symbol]
Firing: every poll on the live tick (ask for an approach from below, bid from above — the guardian's tick). Fires once when the
tick crosses the level in the direction of side_hint; never twice; not on a stale tick (> heartbeat_stale_min) or a closed market.
ALERT REACHED card (dedupe key alert:<symbol>:<id>): price block · EMA behaviour · trend behaviour · guardian context ·
SUGGEST line = the mode's own entry verdict for the last closed bar (common/verdict.py — the same function detect.py prints) ·
[LONG] [SHORT] [SKIP] buttons (bot.py). Decisions are journaled as alert_decision and feed /report and the Saturday card."""
from __future__ import annotations

import json
import os
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np

from .common.news import session_of
from .common.timeutil import ist_str, server_str
from .common.verdict import entry_verdict
from .notify import EVENT_COLOURS, field, footer as cfooter, title as ctitle
from .strategies.ema5080.trend import state_of

IST = timezone(timedelta(hours=5, minutes=30))
BAR_S = 300
STATUSES = ("armed", "fired", "cancelled")
SESSION_NAMES = {"asia": "Asia", "london": "London", "ny": "New York", "off": "off-hours"}


def key_for(symbol: str, alert_id: str) -> str:
    return f"alert:{symbol.upper()}:{alert_id}"


# ============================================================================= store
class AlertStore:
    """logs/alerts.json: {"next_id": n, "alerts": {SYMBOL: [alert, ...]}}. Every write is atomic; one lock for the process."""

    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "alerts.json")
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("alerts"), dict):
                d.setdefault("next_id", 1); return d
        except Exception:
            pass
        return {"next_id": 1, "alerts": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1)
        os.replace(tmp, self.path)

    # ---- queries
    def all(self, symbol: str | None = None) -> list[dict]:
        with self._lock:
            if symbol:
                return [dict(a) for a in self._data["alerts"].get(symbol.upper(), [])]
            return [dict(a) for xs in self._data["alerts"].values() for a in xs]

    def armed(self, symbol: str | None = None) -> list[dict]:
        return [a for a in self.all(symbol) if a["status"] == "armed"]

    def get(self, alert_id: str) -> dict | None:
        with self._lock:
            for xs in self._data["alerts"].values():
                for a in xs:
                    if a["id"] == alert_id:
                        return dict(a)
        return None

    def symbols(self) -> list[str]:
        with self._lock:
            return [s for s, xs in self._data["alerts"].items() if any(a["status"] == "armed" for a in xs)]

    # ---- changes
    def arm(self, symbol: str, price: float, current: float | None, note: str = "", created_by: str = "", bar_time: int | None = None) -> dict:
        with self._lock:
            symbol = symbol.upper()
            if current is None:
                hint = "unknown"
            else:
                hint = "from below" if current < price else "from above"
            a = {"id": f"A{self._data['next_id']}", "symbol": symbol, "price": round(float(price), 2), "side_hint": hint, "note": (note or "")[:120],
                 "created_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"), "created_t": int(_time.time()), "created_by": str(created_by)[:64],
                 "created_bar": bar_time, "current_at_arm": current, "status": "armed", "hit_price": None, "fired_t": None, "fired_bar": None,
                 "decision": None}
            self._data["next_id"] += 1
            self._data["alerts"].setdefault(symbol, []).append(a)
            self._save()
            return dict(a)

    def _set(self, alert_id: str, **changes) -> dict | None:
        with self._lock:
            for xs in self._data["alerts"].values():
                for a in xs:
                    if a["id"] == alert_id:
                        a.update(changes); self._save(); return dict(a)
        return None

    def cancel(self, alert_id: str) -> dict | None:
        a = self.get(alert_id)
        if a is None or a["status"] != "armed":
            return None
        return self._set(alert_id, status="cancelled", cancelled_t=int(_time.time()))

    def clear(self, symbol: str) -> int:
        n = 0
        for a in self.armed(symbol):
            if self.cancel(a["id"]):
                n += 1
        return n

    def decide(self, alert_id: str, decision: str) -> dict | None:
        return self._set(alert_id, decision=decision)

    def set_message(self, alert_id: str, message_id, channel_id=None) -> dict | None:
        """v2.0.2: the Discord message of the ALERT REACHED card (edited later with the Claude verdict / the decision)."""
        return self._set(alert_id, message_id=message_id, channel_id=channel_id)

    def set_claude(self, alert_id: str, verdict: dict | None, side: str | None, agreed: bool | None) -> dict | None:
        return self._set(alert_id, claude_verdict=(verdict or {}).get("decision"), claude_side=side, claude_agreed=agreed,
                         claude_reason=(verdict or {}).get("reason"), claude_p_win=(verdict or {}).get("p_win"))

    # ---- firing
    @staticmethod
    def crossed(a: dict, bid: float, ask: float) -> bool:
        """An approach from below fires on the ask (what a long pays); from above on the bid (what a short gets)."""
        if a["side_hint"] == "from below":
            return ask >= a["price"]
        if a["side_hint"] == "from above":
            return bid <= a["price"]
        return False

    def check(self, symbol: str, bid: float, ask: float, bar_time: int | None = None) -> list[dict]:
        """Mark and return the armed alerts the tick just crossed. Each alert fires once, ever."""
        fired = []
        with self._lock:
            for a in self._data["alerts"].get(symbol.upper(), []):
                if a["status"] != "armed" or not self.crossed(a, bid, ask):
                    continue
                a.update(status="fired", hit_price=ask if a["side_hint"] == "from below" else bid, fired_t=int(_time.time()), fired_bar=bar_time)
                fired.append(dict(a))
            if fired:
                self._save()
        return fired


# ============================================================================= replies for the commands
def armed_reply(a: dict, current: float | None) -> str:
    dist = f"{abs(a['price'] - current):.1f} pts away" if current is not None else "current price unknown"
    cur = f"{current:.2f}" if current is not None else "—"
    return f"ALERT armed · {a['symbol']} {a['price']:.2f} · current {cur} · {dist} · {a['id']} ({a['side_hint']})" + (f" · {a['note']}" if a["note"] else "")


def list_reply(store: AlertStore, symbol: str, current: float | None) -> str:
    xs = store.armed(symbol)
    if not xs:
        return f"no armed alerts for {symbol.upper()}"
    lines = [f"**{symbol.upper()}** armed alerts" + (f" · current {current:.2f}" if current is not None else "")]
    for a in xs:
        dist = f"{a['price'] - current:+.1f} pts" if current is not None else "—"
        lines.append(f"`{a['id']}` {a['price']:.2f} · {dist} · {a['side_hint']} · set {a['created_ist']} IST" + (f" · {a['note']}" if a["note"] else ""))
    return "\n".join(lines)


# ============================================================================= the card
def _shoot(df, i: int, direction: str, bars: int = 40) -> float:
    lo = max(0, i - bars)
    if i <= lo:
        return 0.0
    c = float(df["close"].iloc[i])
    return round(float(c - df["low"].iloc[lo:i].min()) if direction == "bull" else float(df["high"].iloc[lo:i].max() - c), 2)


def last_cross_info(S, res: dict, df, off: float) -> dict:
    """Side, time, confirmed yes/no, bars ago, multi flag of the last cross — from the mode's own analysis."""
    cf, cs = S.ema_cols
    f = df[cf].to_numpy(); s_ = df[cs].to_numpy(); n = len(df)
    sign = np.sign(f - s_)
    i = next((k for k in range(n - 1, 0, -1) if sign[k] != 0 and sign[k - 1] != 0 and sign[k] != sign[k - 1]), None)
    if i is None:
        return {"side": None, "text": "no cross in the loaded bars"}
    d = "bull" if sign[i] > 0 else "bear"; side = "LONG" if d == "bull" else "SHORT"
    out = {"side": side, "index": i, "time": int(df["time"].iloc[i]), "bars_ago": n - 1 - i, "confirmed": False, "multi": False,
           "shoot": _shoot(df, i, d), "confirm_text": "no"}
    if getattr(S, "name", "") == "ema2050" and res.get("cross_infos"):
        x = next((x for x in res["cross_infos"] if x.index == i), None)
        if x is not None:
            out["confirmed"] = x.confirm_index is not None; out["multi"] = bool(x.multi); out["shoot"] = x.shoot
            out["confirm_text"] = (f"yes ({ist_str(int(df['time'].iloc[x.confirm_index]), off, '%H:%M')} IST)" if x.confirm_index is not None
                                   else ("no — multi cross" if x.multi else "not yet"))
    else:
        evs = [e for e in res.get("events", []) if e.index >= i]
        out["confirmed"] = any(e.label.startswith(("EB", "ES")) for e in evs)
        out["multi"] = any(e.label == "WP" for e in evs)
        out["confirm_text"] = "yes (entry signal)" if out["confirmed"] else ("no — whipsaw" if out["multi"] else "no")
    out["text"] = (f"{side} at {ist_str(out['time'], off, '%d %b %H:%M')} IST · {out['bars_ago']} bars ago · confirmed {out['confirm_text']}"
                   + (" · MULTI CROSS" if out["multi"] else ""))
    return out


def ema_behaviour(S, df, res: dict, off: float) -> tuple[list[dict], dict]:
    cf, cs = S.ema_cols
    f = df[cf].to_numpy(); s_ = df[cs].to_numpy(); c = df["close"].to_numpy()
    ef, es, close = float(f[-1]), float(s_[-1]), float(c[-1])
    gap = ef - es
    gaps = np.abs(f - s_)
    if len(df) > 6:
        dg = float(gaps[-1] - gaps[-7]); gap_trend = "widening" if dg > 0.1 else ("narrowing" if dg < -0.1 else "flat")
    else:
        gap_trend = "—"
    slope = float(f[-1] - f[-5]) if len(df) > 4 else 0.0
    where = "above both" if close > max(ef, es) else ("below both" if close < min(ef, es) else "between")
    lc = last_cross_info(S, res, df, off)
    fields = [field(f"EMA {S.fast} / {S.slow}", f"{ef:.2f} / {es:.2f}"), field("Gap", f"{gap:+.2f} pts · {gap_trend} (6 bars)"),
              field(f"EMA {S.fast} slope", f"{slope:+.2f} pts (4 bars)"), field("Price sits", where),
              field("Last cross", lc["text"], inline=False)]
    return fields, {"ema_fast": ef, "ema_slow": es, "gap": gap, "gap_trend": gap_trend, "slope": slope, "where": where, "last_cross": lc}


def trend_behaviour(S, agent, df, res: dict, off: float, bar_t: int, lc: dict) -> tuple[list[dict], dict]:
    cf, cs = S.ema_cols
    close = float(df["close"].iloc[-1]); ef = float(df[cf].iloc[-1]); es = float(df[cs].iloc[-1])
    state = state_of(close, ef, es)                                   # ema5080/trend.py state machine, parametrised by the mode's columns
    if state.endswith("PULLBACK"):
        try:
            why = agent._turning(df, bar_t, "SHORT" if state.startswith("BEARISH") else "LONG")
        except Exception:
            why = None
        if why:
            state = state.replace("PULLBACK", "WEAKENING")
    slow_slope = float(df[cs].iloc[-1] - df[cs].iloc[-11]) if len(df) > 10 else 0.0
    rng = (df["high"] - df["low"]).to_numpy()
    atr20 = float(rng[-20:].mean()) if len(rng) else 0.0
    day_start = int(datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + off * 3600)
    t = df["time"].to_numpy(); f = df[cf].to_numpy(); s_ = df[cs].to_numpy(); sign = np.sign(f - s_)
    crosses_today = sum(1 for k in range(1, len(df)) if t[k] >= day_start and sign[k] != 0 and sign[k - 1] != 0 and sign[k] != sign[k - 1])
    sess = SESSION_NAMES.get(session_of(bar_t, off), "—")
    fields = [field("Trend", f"**{state}**", inline=False), field(f"EMA {S.slow} slope", f"{slow_slope:+.2f} pts (10 bars)"),
              field("Pre-cross shoot", f"{lc.get('shoot', 0.0):+.1f} pts" if lc.get("side") else "—"), field("Session", sess),
              field("ATR 20", f"{atr20:.2f}"), field("Crosses today", str(crosses_today))]
    return fields, {"trend": state, "slow_slope": slow_slope, "atr20": atr20, "crosses_today": crosses_today, "session": sess}


def guardian_context(agent, bar_t: int) -> tuple[list[dict], dict]:
    S = agent.S; g = agent.g; off = agent.off; cfg = agent.cfg
    snap = getattr(agent, "snapshot", None) or {}
    pos = snap.get("positions") or []; states = snap.get("state") or {}
    if pos:
        p = pos[0]; st = states.get(p["ticket"], {}) or states.get(str(p["ticket"]), {})
        phase = ("P phase" if st.get("pre") else "post-cross") if (g and g.p_phase) else "managed"
        if st.get("secured", 0) >= (g.secure_level if g else 1e9):
            phase += f" · secured +{st.get('secured', 0):g}"
        pos_text = f"{p['direction'].upper()} #{p['ticket']} · {p['points']:+.1f} pts · {phase} · SL {p['sl'] or '—'}"
    else:
        pos_text = "flat"
    # entry window: the mode's own hour rule
    if S.name == "ema2050":
        from .strategies.ema2050.journeys import entry_hour_block, news_block
        rules = S.rules_for(agent.symbol)
        why = entry_hour_block(bar_t, rules, off)
        window = "open" if why is None else f"closed ({why})"
        news = news_block(bar_t, type("R", (), {"news_times": agent.news, "news_entry_block_min": 60, "news_rearm_min": 30})(), off)
    else:
        rules = S.rules_for(agent.symbol)
        ist_h = (((bar_t - off * 3600 + 5.5 * 3600) % 86400) / 3600); lo, hi = rules.entry_hours_ist
        window = "open" if lo <= ist_h < hi else f"closed (outside {lo:g}-{hi:g} IST)"
        tu = bar_t - off * 3600
        news = any(r - rules.news_entry_block_min * 60 <= tu < r + rules.news_rearm_min * 60 for r in agent.news)
    today = snap.get("today") or {}
    losses = _losses_today(agent)
    fields = [field("Position", pos_text, inline=False), field("Entry window", window), field("News block", "ACTIVE" if news else "clear"),
              field("Today", f"signals {today.get('signals', 0)} · trades {today.get('closed', 0)} · losses {losses}/{cfg.daily_max_losses}")]
    return fields, {"position": pos[0] if pos else None, "window_open": window == "open", "news_block": bool(news), "losses": losses}


def _losses_today(agent) -> int:
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        recs = [r for r in agent.journal.read(int(start.timestamp())) if r.get("symbol") == agent.symbol]
        tickets = set()
        for r in recs:
            if r.get("event") == "exit" and (r.get("points") or 0) < 0:
                tickets.add(r.get("ticket"))
            if r.get("event") == "closed" and r.get("final_points") is not None and float(r["final_points"]) < 0:
                tickets.add(r.get("ticket"))
        return len(tickets)
    except Exception:
        return 0


def _news_minutes(agent, bar_t: int) -> int | None:
    """Minutes to the next release inside the entry block window (None when clear)."""
    off = agent.off; tu = bar_t - off * 3600
    for r in sorted(agent.news or ()):
        if r - 60 * 60 <= tu < r + 30 * 60:
            return int(max(0, (r - tu) // 60))
    return None


def suggestion(agent, df, res: dict, bar_t: int, gctx: dict, current: float) -> dict:
    """The AGENT verdict: deterministic, from the mode's own rules on the last closed bar (common/verdict.py) — never a new rule.
    Returns decision LONG|SHORT|WAIT|NO TRADE, side, reason, sl, line ('AGENT: SHORT — …'), sl_line, text (the block)."""
    S = agent.S; g = agent.g; off = agent.off
    v = entry_verdict(S, res, len(df) - 1, off)
    stop_pts = g.pre_stop if g else None
    cf = S.ema_cols[0]
    dist = abs(float(df["close"].iloc[-1]) - float(df[cf].iloc[-1])) if len(df) else 0.0      # the rule's own distance (bar close)
    if gctx.get("position") is not None:
        p = gctx["position"]
        dec, side, reason = "WAIT", None, f"in trade {p['direction'].upper()} {p['points']:+.1f} — the guardian manages it"
    else:
        dec, reason = v["decision"], v["reason"]
        if dec in ("LONG", "SHORT"):
            if gctx.get("news_block"):
                mins = _news_minutes(agent, bar_t)
                dec, reason = "NO TRADE", f"news in {mins} min" if mins is not None else "news block"
            elif not gctx.get("window_open", True):
                dec, reason = "NO TRADE", "no-entry hour"
            else:
                lc = v.get("confirm", "")
                if S.name == "ema2050":
                    touched = bool(v.get("touch")); ctime = ""
                    try:
                        x = next((x for x in (res.get("cross_infos") or []) if x.confirm_index is not None and x.entry_index == len(df) - 1), None)
                        if x is not None:
                            ctime = ist_str(int(df["time"].iloc[x.confirm_index]), off, "%H:%M")
                    except Exception:
                        ctime = ""
                    reason = (f"confirmed cross{(' ' + ctime) if ctime else ''}, {'pullback touch now' if touched else 'no-pullback fallback'}, "
                              f"{dist:.1f} pts from EMA{S.fast}, window open")
                else:
                    reason = f"{reason}, {dist:.1f} pts from EMA{S.fast}, window open"
        elif dec == "NO TRADE" and reason.startswith("too far from EMA"):
            reason = f"{dist:.1f} pts from EMA{S.fast} (chase)"
        elif dec == "NO TRADE" and "multi" in reason:
            reason = "multi cross"
        dec = dec if dec in ("LONG", "SHORT", "WAIT", "NO TRADE") else "WAIT"
        side = dec if dec in ("LONG", "SHORT") else None
    sl = None
    if side and stop_pts is not None:
        sl = current - stop_pts if side == "LONG" else current + stop_pts
    line = f"AGENT: {dec} — {reason}"
    if g is not None:
        if g.early_at is not None:
            sl_line = f"SL would be {sl:.1f} (−{stop_pts:g}) · early lock +{g.early_at:g} → +{g.early_level:g} · secure +{g.secure_at:g}" if sl is not None else \
                      f"stop −{stop_pts:g} · early lock +{g.early_at:g} → +{g.early_level:g} · secure +{g.secure_at:g}"
        else:
            sl_line = f"SL would be {sl:.1f} (−{stop_pts:g}) · secure +{g.secure_at:g} · ride +{g.ride_step:g}" if sl is not None else \
                      f"stop −{stop_pts:g} · secure +{g.secure_at:g} · ride +{g.ride_step:g}"
    else:
        sl_line = "no guardian profile for this symbol"
    pos = gctx.get("position")
    ctx = (f"position: {pos['direction'].upper()} {pos['points']:+.1f}" if pos else "position: flat") + \
          f" · news: {'BLOCK' if gctx.get('news_block') else 'clear'} · losses today {gctx.get('losses', 0)}/{agent.cfg.daily_max_losses}"
    text = f"**{line}**\n{sl_line}\n{ctx}"
    return {"decision": dec, "side": side, "reason": reason, "line": line, "sl": sl, "sl_line": sl_line, "text": text, "verdict": v}


def claude_placeholder(agent) -> tuple[str, str]:
    """(state, text) for the CLAUDE VERDICT block before any call: asking | off | budget | unavailable."""
    cl = getattr(agent, "claude_any", None)
    if cl is None or getattr(cl, "mode", "off") == "off":
        return "off", "— not attached (mode=off)"
    st = cl.stats if hasattr(cl, "stats") else {}
    if st.get("login") in ("NOT FOUND", "FAILED"):
        return "unavailable", f"— unavailable ({'Claude Code not found' if st.get('login') == 'NOT FOUND' else 'not logged in'})"
    try:
        if cl.budget.used() >= cl.cfg.claude_max_calls:
            return "budget", "— budget used"
    except Exception:
        pass
    return "asking", f"⏳ asking Claude ({cl.cfg.claude_entry_model}) …"


def claude_side(v: dict | None) -> str | None:
    if not v or v.get("decision") != "TAKE":
        return None
    return {"BUY": "LONG", "SELL": "SHORT"}.get(str(v.get("side") or "").upper())


def claude_verdict_block(v: dict | None, agent_side: str | None, error: str = "") -> dict:
    """The CLAUDE VERDICT block after the call: text, side, agreed (None when no verdict)."""
    if v is None:
        return {"text": f"— {error or 'unavailable'}", "side": None, "agreed": None, "decision": None}
    cs = claude_side(v)
    head = f"CLAUDE: {cs or '—'} · {v['decision']}" + (f" · p_win {v['p_win']:.2f}" if v.get("p_win") is not None else "") + f" · confidence {v.get('confidence', '—')}"
    lines = [f"**{head}**", f"\"{v.get('reason', '')}\""]
    if v.get("evidence"):
        lines.append("evidence: " + ", ".join(v["evidence"]))
    agreed = (cs == agent_side)
    lines.append("AGREES with agent ✅" if agreed else f"DISAGREES with agent ⚠️ (agent {agent_side or 'no trade'}, Claude {cs or v['decision']})")
    return {"text": "\n".join(lines), "side": cs, "agreed": agreed, "decision": v["decision"]}


def blocks(agent, df, res: dict, bar_t: int, price: float) -> dict:
    """PRICE / EMA / TREND / AGENT VERDICT text blocks for the last closed bar (shared by the hit card and the /alert PREVIEW)."""
    S = agent.S; off = agent.off; cf, cs = S.ema_cols
    ema_f, ema = ema_behaviour(S, df, res, off)
    tr_f, tr = trend_behaviour(S, agent, df, res, off, bar_t, ema["last_cross"])
    g_f, gctx = guardian_context(agent, bar_t)
    sug = suggestion(agent, df, res, bar_t, gctx, price)
    lc = ema["last_cross"]
    ema_text = (f"EMA {S.fast} / {S.slow}: {ema['ema_fast']:.2f} / {ema['ema_slow']:.2f} · gap {ema['gap']:+.2f} ({ema['gap_trend']}, 6 bars) · "
                f"EMA {S.fast} slope {ema['slope']:+.2f} (4 bars) · price {ema['where']}\n"
                f"last cross: {lc['text']}\ncrosses today {tr['crosses_today']} · session {tr['session']} · ATR20 {tr['atr20']:.2f}")
    trend_text = f"**{tr['trend']}** · EMA {S.slow} slope {tr['slow_slope']:+.2f} (10 bars) · pre-cross shoot {lc.get('shoot', 0.0):+.1f} pts"
    d_fast = price - float(df[cf].iloc[-1]); d_slow = price - float(df[cs].iloc[-1])
    return {"ema_text": ema_text, "trend_text": trend_text, "agent": sug, "gctx": gctx, "ema": ema, "trend": tr,
            "dist": f"{d_fast:+.1f} pts from EMA{S.fast} · {d_slow:+.1f} pts from EMA{S.slow}"}


def reached_card(agent, a: dict, hit_price: float, tick_time: int, bar_t: int, df, res: dict) -> dict:
    """{key, title, lines, fields, meta, color, footer, …} for notify.ask(); the bot adds [LONG] [SHORT] [SKIP].
    Blocks: PRICE · EMA · TREND · AGENT VERDICT · CLAUDE VERDICT (placeholder until the add-on answers)."""
    S = agent.S; off = agent.off
    b = blocks(agent, df, res, bar_t, hit_price); sug = b["agent"]
    created_bar = a.get("created_bar"); bars_since = int((bar_t - created_bar) // BAR_S) if created_bar else None
    title = f"🔔 ALERT REACHED · {a['symbol']} {a['price']:.2f}" + (f" · {a['note']}" if a.get("note") else "")
    price_text = (f"hit **{hit_price:.2f}** · {server_str(tick_time, '%H:%M:%S')} server · {ist_str(tick_time, off, '%H:%M:%S')} IST · approach {a['side_hint']}"
                  + (f" · set {bars_since} bars ago" if bars_since is not None else "") + f"\n{b['dist']}")
    state, ph = claude_placeholder(agent)
    fields = [field("PRICE", price_text, inline=False), field("EMA", b["ema_text"], inline=False), field("TREND", b["trend_text"], inline=False),
              field("AGENT VERDICT", sug["text"], inline=False), field("CLAUDE VERDICT", ph, inline=False)]
    meta = {"kind": "alert", "alert_id": a["id"], "symbol": a["symbol"], "price": hit_price, "bar": bar_t, "mode": S.name,
            "suggested_side": sug["side"], "suggestion": sug["decision"], "reason": sug["reason"], "sl": sug["sl"],
            "stop_pts": agent.g.pre_stop if agent.g else None, "note": a.get("note", ""), "agent_verdict": sug["line"],
            "agent_block": sug["text"], "claude_state": state}
    colour = EVENT_COLOURS["signal_long" if sug["side"] == "LONG" else "signal_short" if sug["side"] == "SHORT" else "news"]
    return {"key": key_for(a["symbol"], a["id"]), "title": title, "lines": [f"**{sug['line']}**"], "fields": fields, "meta": meta, "color": colour,
            "footer": cfooter(S.display_name, f"alert {a['id']}"), "ema": b["ema"], "trend": b["trend"], "guardian": b["gctx"], "suggestion": sug,
            "claude_state": state}


def preview_card(agent, a: dict, df, res: dict, bar_t: int, current: float) -> dict:
    """/alert at creation: the same blocks for the current bar ('if it hit right now'), labelled PREVIEW. No Claude call."""
    S = agent.S
    b = blocks(agent, df, res, bar_t, current); sug = b["agent"]
    fields = [field("PRICE · PREVIEW", f"current **{current:.2f}** · alert {a['price']:.2f} ({a['side_hint']}, {abs(a['price'] - current):.1f} pts away)\n{b['dist']}", inline=False),
              field("EMA", b["ema_text"], inline=False), field("TREND", b["trend_text"], inline=False),
              field("AGENT VERDICT · PREVIEW (this bar, not the hit)", sug["text"], inline=False),
              field("CLAUDE VERDICT", "— asked only when the alert is reached", inline=False)]
    return {"title": f"🔔 ALERT ARMED · {a['symbol']} {a['price']:.2f} · PREVIEW" + (f" · {a['note']}" if a.get("note") else ""),
            "description": armed_reply(a, current), "fields": fields, "footer": cfooter(S.display_name, f"alert {a['id']} · preview"),
            "agent": sug}


# ============================================================================= stats for /report and the Saturday card
def _bool(x):
    return None if x is None else bool(x)


def stats(records: list[dict]) -> dict:
    """fired · taken · agreed with the agent · agreed with Claude · points of the taken ones (closed / final_points rows by alert_id)."""
    fired = [r for r in records if r.get("event") == "alert_fired"]
    dec = [r for r in records if r.get("event") == "alert_decision"]
    taken = [r for r in dec if str(r.get("side", "")).upper() in ("LONG", "SHORT")]
    skipped = [r for r in dec if str(r.get("side", "")).lower() == "skip"]
    agreed_agent = sum(1 for r in taken if _bool(r.get("agreed_agent", r.get("agreed"))))
    with_claude = [r for r in taken if r.get("agreed_claude") is not None]
    agreed_claude = sum(1 for r in with_claude if r.get("agreed_claude"))
    closes = {r.get("alert_id"): r for r in records if r.get("event") == "closed" and r.get("alert_id")}
    exits = {r.get("alert_id"): r for r in records if r.get("event") == "exit" and r.get("alert_id")}
    fills = {r.get("alert_id"): r for r in records if r.get("event") == "final_points" and r.get("alert_id")}
    wins = losses = 0; pts = 0.0; graded = 0
    for r in taken:
        aid = r.get("alert_id")
        final = (fills.get(aid) or {}).get("final_points")
        if final is None:
            final = (closes.get(aid) or {}).get("final_points")
        if final is None and aid in exits:
            final = exits[aid].get("points")
        if final is None:
            continue
        graded += 1; pts += float(final)
        if float(final) > 0: wins += 1
        else: losses += 1
    return {"fired": len(fired), "taken": len(taken), "skipped": len(skipped), "agreed": agreed_agent, "agreed_agent": agreed_agent,
            "agreed_claude": agreed_claude, "with_claude": len(with_claude), "graded": graded, "wins": wins, "losses": losses, "points": round(pts, 2)}


def stats_table(st: dict) -> str:
    cl = f"{st['agreed_claude']}/{st['with_claude']}" if st.get("with_claude") else "—"
    return (f"{'fired':<7}{'taken':<7}{'agent✓':<9}{'claude✓':<9}{'pts taken':>10}\n"
            f"{st['fired']:<7}{st['taken']:<7}{str(st['agreed_agent']) + '/' + str(st['taken']) if st['taken'] else '—':<9}{cl:<9}{st['points']:>+10.1f}")


def stats_lines(st: dict) -> list[str]:
    if not (st["fired"] or st["taken"]):
        return []
    return ["\n**ALERTS**", "```\n" + stats_table(st) + "\n```",
            f"skipped {st['skipped']} · taken & closed {st['graded']}: win {st['wins']} / loss {st['losses']}"]
