"""v2.16.0 — ema921 live support: persisted state, the paper-position tracker, the weekly summary.

    logs/state_<SYMBOL>.json   {"equity_peak", "paper": [positions], "paper_realized_usd", "paper_last_exit_bar", "week_sent"}
        equity_peak survives restarts (AUREON_MAX_DD_USD must never reset to the current equity on boot).

    paper_step(pos, bar, rules)
        One closed bar for one paper position, in build()'s run_exit order: broker SL (−stop, or +lock_to once locked) on the
        bar's low/high, broker TP, peak from the bar's extreme, then journeys.on_bar_close (lock armed at the close / exit
        line / Friday flat). Same guardian, no broker calls — so a paper week is comparable to the replay and to live.

    week_summary(rows, ...)    trades, won, net pts, $ at the traded lots, best / worst, for the weekly card and /week.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .strategies.ema921 import journeys as J

USD_PER_POINT_PER_LOT = 100.0         # gold: $1 of price = $100 per lot
BACKTEST_REF = "backtest reference: +11 pts / week on average · 58% of weeks positive"


# ----------------------------------------------------------------------------- persisted state
def state_path(log_dir: str, symbol: str) -> str:
    return os.path.join(log_dir, f"state_{symbol.upper()}.json")


def load_state(log_dir: str, symbol: str) -> dict:
    try:
        with open(state_path(log_dir, symbol), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(log_dir: str, symbol: str, st: dict) -> None:
    os.makedirs(log_dir, exist_ok=True)
    path = state_path(log_dir, symbol); tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, default=str)
    os.replace(tmp, path)


def dd_check(st: dict, equity: float, limit: float) -> tuple[str | None, float, float]:
    """(block reason | None, peak, drawdown). Updates st["equity_peak"] (the caller saves it)."""
    peak = max(float(st.get("equity_peak") or 0.0), float(equity))
    st["equity_peak"] = peak
    dd = peak - float(equity)
    if limit and dd >= limit:
        return (f"drawdown guard: equity {equity:,.0f} is {dd:,.0f} below peak {peak:,.0f} (limit {limit:,.0f})", peak, dd)
    return None, peak, dd


# ----------------------------------------------------------------------------- paper tracker
def new_paper(side: str, entry: float, lots: float, sl: float, tp: float | None, bar: int) -> dict:
    s = 1 if side == "LONG" else -1
    return {"id": f"P{int(bar)}", "side": side, "entry": float(entry), "lots": float(lots), "sl": float(sl or 0.0),
            "tp": (float(tp) if tp else None), "bar": int(bar), "peak": 0.0, "mae": 0.0, "locked": False, "run": 0,
            # v2.16.1: the position's own SL / TP in points (a /place with sl= / tp=); None = the mode's rule
            "sl_pts": ((float(sl) - float(entry)) * s) if sl else None, "tp_pts": ((float(tp) - float(entry)) * s) if tp else None}


def paper_step(pos: dict, bar: dict, rules) -> dict | None:
    """Advance one closed bar (dict: time, high, low, close, ema9, ema200). Mutates pos. Returns None (still open), or
    {"event": "lock"} when only the lock armed, or the close {"event": "close", "reason", "points", "price"}.
    A lock and a close on the same bar return the close with "locked_now": True."""
    s = 1 if pos["side"] == "LONG" else -1; e = pos["entry"]
    hi = (bar["high"] - e) if s > 0 else (e - bar["low"]); lo = (bar["low"] - e) if s > 0 else (e - bar["high"])
    pos["mae"] = max(pos.get("mae", 0.0), -lo)
    base = pos.get("sl_pts") if pos.get("sl_pts") is not None else -rules.stop
    sl_pts = max(base, rules.lock_to) if pos.get("locked") else base          # the lock only ever tightens a manual SL
    if lo <= sl_pts:
        return {"event": "close", "reason": "locked" if sl_pts > 0 else "stop", "points": sl_pts, "price": e + s * sl_pts}
    tp_pts = pos.get("tp_pts") if pos.get("tp_pts") is not None else (rules.tp or None)
    if tp_pts and hi >= tp_pts:
        return {"event": "close", "reason": "tp", "points": tp_pts, "price": e + s * tp_pts}
    pos["peak"] = max(pos.get("peak", 0.0), hi)
    c = float(bar["close"]); cpts = (c - e) * s
    line = J.exit_line(rules, pos["peak"]); lv = float(bar[line])
    against = (c < lv) if s > 0 else (c > lv)
    lock, why, pos["run"] = J.on_bar_close(rules, peak=pos["peak"], locked=bool(pos.get("locked")), close_pts=cpts,
                                          against=against, run=int(pos.get("run", 0)), friday_last=J.friday_last_bar(int(bar["time"]), rules))
    if lock:
        pos["locked"] = True
    if why:
        return {"event": "close", "reason": why, "points": cpts, "price": c, "line": line, "locked_now": lock}
    return {"event": "lock"} if lock else None


# ----------------------------------------------------------------------------- the week
def week_start(now: datetime) -> datetime:
    """Monday 00:00 UTC of `now`'s ISO week."""
    now = now.astimezone(timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def closed_trades(rows: list[dict], symbol: str, *, since: float, paper: bool, default_lots: float) -> list[dict]:
    """ema921 exits this week: closed_by_rule (rule exits, live or paper) + closed (broker SL / lock on live)."""
    out = []
    for r in rows:
        if r.get("mode") != "ema921" or str(r.get("symbol", "")).upper() != symbol.upper() or float(r.get("t", 0)) < since:
            continue
        if bool(r.get("paper")) != paper:
            continue
        if r.get("event") == "closed_by_rule":
            pts = r.get("points")
        elif r.get("event") == "closed" and r.get("reason") in ("stop", "locked", "broker_sl"):
            pts = r.get("final_points")
        else:
            continue
        if pts is None:
            continue
        out.append({"points": float(pts), "lots": float(r.get("lots") or default_lots), "reason": r.get("reason"), "t": r.get("t")})
    return out


def week_summary(trades: list[dict]) -> dict:
    pts = [t["points"] for t in trades]
    usd = [t["points"] * t["lots"] * USD_PER_POINT_PER_LOT for t in trades]
    return {"trades": len(trades), "won": sum(1 for p in pts if p > 0), "net_pts": round(sum(pts), 1),
            "net_usd": round(sum(usd), 0), "best": round(max(pts), 1) if pts else 0.0, "worst": round(min(pts), 1) if pts else 0.0}


def week_text(summary: dict, *, dd: float | None, limit: float, paper: bool, lots_note: str = "") -> list[str]:
    s = summary
    lines = [f"{'PAPER · ' if paper else ''}trades {s['trades']} · won {s['won']} · net {s['net_pts']:+.1f} pts · "
             f"${s['net_usd']:+,.0f}{lots_note}",
             f"best {s['best']:+.1f} · worst {s['worst']:+.1f}",
             ("drawdown used " + (f"${dd:,.0f}" if dd is not None else "—")
              + (f" of ${limit:,.0f} (AUREON_MAX_DD_USD)" if limit else " (AUREON_MAX_DD_USD off)")),
             BACKTEST_REF]
    return lines
