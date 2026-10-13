"""v2.5.0 — one context builder for every advisor call.

pack(agent, df, signal, journal) -> dict   (fixed key order, rounded numbers)
to_prompt(dict) -> str                     (deterministic text, budget-constrained)

Replaces the ad-hoc build_snapshot in claude_advisor.py. Both live and replay
use this so prompts are byte-for-byte identical for the same bars.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np

from .common.news import session_of
from .common.chop import server_day_start, count_morning_crosses
from .common.timeutil import ist_str, server_str

IST = timezone(timedelta(hours=5, minutes=30))
BAR_S = 300

# Truncation priority: lower = kept first, higher = dropped first
_TRUNC_ORDER = [
    "symbol", "mode", "side", "event", "bar_time_ist", "close", "ema_fast", "ema_slow", "gap",
    "crosses_today", "morning_crosses", "chop", "session", "trend_state",
    "dist_to_fast_ema_pts", "swing_against_last6_pts", "atr20", "confirm_state",
    "minutes_to_news", "minutes_since_news", "day_pnl_pts", "trades_today", "governor",
    "control_mode", "position", "last_trades", "bars_ohlc",
    "whipsaw", "htf_context", "spread_warning", "streak", "track_record",
]


def pack(agent, df, signal: dict, journal=None, *, cfg=None, position: dict | None = None,
         st: dict | None = None, kind: str | None = None) -> dict:
    """Build the context dict. Pure function of agent state + df (no side effects).
    signal: {"event", "side", "bar_t"} — what triggered this call."""
    cfg = cfg or agent.cfg
    off = agent.off
    cf, cs = agent.S.ema_cols
    n_bars = min(cfg.ctx_bars, len(df))
    last = df.iloc[-n_bars:]

    close = float(df["close"].iloc[-1])
    ef = float(df[cf].iloc[-1]); es = float(df[cs].iloc[-1])
    bar_t = signal.get("bar_t", int(df["time"].iloc[-1]))

    # Bars OHLC
    bars = [[ist_str(int(r.time), off, "%H:%M"), round(float(r.close), 2),
             round(float(getattr(r, cf)), 2), round(float(getattr(r, cs)), 2)]
            for r in last.itertuples()]

    # Cross counting
    f_arr = df[cf].to_numpy(); s_arr = df[cs].to_numpy(); t_arr = df["time"].to_numpy()
    sgn = np.sign(f_arr - s_arr)
    cross_bars = [k for k in range(1, len(df)) if sgn[k] != 0 and sgn[k - 1] != 0 and sgn[k] != sgn[k - 1]]
    day_start = server_day_start(bar_t)
    crosses_today = sum(1 for k in cross_bars if t_arr[k] >= day_start)
    morning_crosses = count_morning_crosses(df, cf, cs, off)
    bars_since_cross = (len(df) - 1 - cross_bars[-1]) if cross_bars else None

    # Chop
    chop = agent._is_chop() if hasattr(agent, "_is_chop") else False

    # Session
    session = session_of(bar_t, off)

    # News
    now = int(_time.time())
    upcoming = [r for r in (agent.news or ()) if r >= now]
    past = [r for r in (agent.news or ()) if r < now]
    minutes_to_news = round((min(upcoming) - now) / 60) if upcoming else None
    minutes_since_news = round((now - max(past)) / 60) if past else None

    # HTF
    htf = None
    try:
        htf = (getattr(agent, "_htf_cache", None) or (None, None))[1]
    except Exception:
        pass

    # Spread
    spread_warning = None
    try:
        spread_warning = " · ".join(agent._warnings(df)) or None
    except Exception:
        pass

    # Swing against
    s_dir = 1 if str(signal.get("side", "")).upper() in ("LONG", "BUY") else -1
    h6 = float(df["high"].iloc[-6:].max()); l6 = float(df["low"].iloc[-6:].min())
    swing_against = round((h6 - close) if s_dir > 0 else (close - l6), 2)

    # Day stats
    day_pnl = 0.0; trades_today_n = 0
    try:
        from .claude_advisor import _day_stats
        day_pnl, trades_today_n = _day_stats(agent)
    except Exception:
        pass

    # Last closed trades
    last_trades = []
    try:
        if journal:
            recs = journal.read(now - 14 * 86400)
            closed = [r for r in recs if r.get("event") == "closed" and r.get("mode") == agent.S.name][-10:]
            for r in closed:
                last_trades.append({"side": r.get("direction", "?"), "pts": r.get("final_points"),
                                    "session": r.get("session", "?")})
    except Exception:
        pass

    # Governor
    governor_state = None
    try:
        from .governor import Governor
        gov = Governor(cfg)
        governor_state = gov.status({"trades_today": trades_today_n, "realised_money_today": 0, "equity": 0})
    except Exception:
        pass

    # Control mode
    control_mode = "MANUAL"
    try:
        from .control import current_mode
        if journal:
            control_mode = current_mode(journal, agent.symbol)["mode"]
    except Exception:
        pass

    # Confirm state (ema2050)
    confirm_state = None
    res = getattr(agent, "last_res", None)
    if agent.S.name == "ema2050" and res is not None and res.get("bar_states") and len(res["bar_states"]) == len(df):
        raw = str(res["bar_states"][-1]["confirm"])
        confirm_state = raw if raw.startswith("confirmed") else ("no cross" if raw == "no cross" else "not confirmed")

    # ATR
    rng = (df["high"] - df["low"]).to_numpy()
    atr20 = round(float(rng[-20:].mean()), 2) if len(rng) >= 20 else round(float(rng.mean()), 2)

    ctx = {
        "symbol": agent.symbol, "mode": agent.S.name, "side": str(signal.get("side", "")).upper(),
        "event": signal.get("event", ""), "bar_time_ist": ist_str(bar_t, off, "%Y-%m-%d %H:%M"),
        "close": round(close, 2), "ema_fast": round(ef, 2), "ema_slow": round(es, 2), "gap": round(ef - es, 2),
        "crosses_today": crosses_today, "morning_crosses": morning_crosses, "bars_since_cross": bars_since_cross,
        "chop": chop, "session": session, "confirm_state": confirm_state,
        "dist_to_fast_ema_pts": round(close - ef, 2), "swing_against_last6_pts": swing_against,
        "atr20": atr20, "day_pnl_pts": day_pnl, "trades_today": trades_today_n,
        "minutes_to_news": minutes_to_news, "minutes_since_news": minutes_since_news,
        "governor": governor_state, "control_mode": control_mode,
        "trend_state": getattr(agent, "trend_state", None),
        "htf_context": htf, "spread_warning": spread_warning,
        "streak": (getattr(agent, "_streak_cache", None) or (None, None))[1],
        "whipsaw": {"flips": sum(1 for a, b in zip(sgn[-13:-1], sgn[-12:]) if a != b), "zone": False},
        "bars_ohlc": bars, "last_trades": last_trades, "track_record": None,
    }

    # Position data
    if position is not None:
        st = st or {}
        peak = float(st.get("peak", 0.0)); pts = float(position["points"])
        sl = float(position["sl"]) if position.get("sl") else None
        ctx["position"] = {
            "entry": round(float(position["price_open"]), 2), "sl": sl,
            "secured": float(st.get("secured", 0.0)), "soft_lock": st.get("soft_lock"),
            "peak": round(peak, 2), "open_profit": round(pts, 2), "bars_held": int(st.get("bars", 0)),
            "mae": round(float(st.get("mae", min(0.0, pts))), 2),
            "retrace_from_peak": round(max(0.0, peak - pts), 2),
        }

    return ctx


def to_prompt(ctx: dict, max_chars: int = 6000) -> str:
    """Render context dict as deterministic text for the advisor prompt, respecting size budget."""
    lines = [f"CONTEXT · {ctx.get('symbol')} · {ctx.get('mode')} · {ctx.get('event')} · {ctx.get('side')}"]
    lines.append(f"bar {ctx.get('bar_time_ist')} · close {ctx.get('close')} · EMA fast {ctx.get('ema_fast')} · slow {ctx.get('ema_slow')} · gap {ctx.get('gap')}")
    lines.append(f"crosses today {ctx.get('crosses_today')} · morning {ctx.get('morning_crosses')} · bars since cross {ctx.get('bars_since_cross')}")
    lines.append(f"chop {ctx.get('chop')} · session {ctx.get('session')} · confirm {ctx.get('confirm_state')}")
    lines.append(f"dist to fast EMA {ctx.get('dist_to_fast_ema_pts')} · swing against 6 bars {ctx.get('swing_against_last6_pts')} · ATR20 {ctx.get('atr20')}")

    if ctx.get("minutes_to_news") is not None:
        lines.append(f"news in {ctx['minutes_to_news']} min")
    if ctx.get("minutes_since_news") is not None:
        lines.append(f"news {ctx['minutes_since_news']} min ago")

    lines.append(f"day PnL {ctx.get('day_pnl_pts')} pts · trades today {ctx.get('trades_today')} · governor {ctx.get('governor')}")
    lines.append(f"control {ctx.get('control_mode')} · trend {ctx.get('trend_state')}")

    if ctx.get("position"):
        p = ctx["position"]
        lines.append(f"POSITION entry {p['entry']} SL {p['sl']} secured {p['secured']} peak {p['peak']} profit {p['open_profit']} bars {p['bars_held']}")

    if ctx.get("last_trades"):
        trades_str = " | ".join(f"{t['side']} {t['pts']}" for t in ctx["last_trades"][:5])
        lines.append(f"last trades: {trades_str}")

    # Bars (truncated if over budget)
    bars = ctx.get("bars_ohlc", [])
    text = "\n".join(lines)
    remaining = max_chars - len(text) - 50
    if bars and remaining > 100:
        bar_lines = [f"  {b[0]} c={b[1]} ef={b[2]} es={b[3]}" for b in bars]
        bar_text = "\n".join(bar_lines)
        if len(bar_text) > remaining:
            # Truncate to fit
            keep = max(6, remaining // 40)
            bar_lines = bar_lines[-keep:]
            bar_text = f"  ... ({len(bars) - keep} bars omitted)\n" + "\n".join(bar_lines)
        text += "\nBARS (M5):\n" + bar_text

    return text[:max_chars]
