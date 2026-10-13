"""v2.6.0 — deterministic nightly review (no LLM needed).

After the EOD review, score the last 20 trading days from journal.jsonl:
entries and exits by session × mode × actor, protect-stop hit rate, secured-then-closed-early
pts left on the table, chop vs clear, missed vs overridden.

Pattern flags with fixed thresholds. Each flag names the parameter it concerns."""
from __future__ import annotations

import json
import os
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
GREY = 0x95A5A6
SESSIONS = ("asia", "london", "ny", "off")
SESSION_NAMES = {"asia": "Asia", "london": "London", "ny": "New York", "off": "Off-hours"}

# Fixed thresholds for pattern flags
STOP_RATE_HIGH = 0.50       # flag if protect stop hit rate >= 50%
STOP_RATE_MIN_N = 10        # need at least this many trades in the slice
EARLY_CLOSE_AVG = 5.0       # flag if avg pts left on table after secure > this
CHOP_WIN_DIFF = 15.0        # flag if chop win% is this many pp below clear
MISSED_OVERRIDE_RATIO = 0.5 # flag if overridden-then-won > 50%


@dataclass
class SliceMetrics:
    """Metrics for one slice (session × actor)."""
    n: int = 0
    wins: int = 0
    losses: int = 0
    net_pts: float = 0.0
    stops: int = 0
    avg_mae: float = 0.0
    avg_mfe: float = 0.0


@dataclass
class Flag:
    """A pattern flag — names the parameter it concerns."""
    text: str
    parameter: str
    severity: str = "info"   # info | warning


def _session_of_time(t: int, off: float) -> str:
    from .common.news import session_of
    return session_of(t, off)


def compute(journal, off: float = 3.0, days: int = 20) -> dict:
    """Score the last `days` trading days. Returns {slices, flags, left_on_table, chop_vs_clear, missed}."""
    cutoff = int(_time.time()) - days * 86400
    recs = journal.read(cutoff)

    # Build per-trade records from closed events
    trades = []
    for r in recs:
        if r.get("event") != "closed" or r.get("final_points") is None:
            continue
        trades.append({
            "ticket": r.get("ticket"),
            "symbol": r.get("symbol"),
            "mode": r.get("mode"),
            "direction": r.get("direction"),
            "pts": float(r["final_points"]),
            "peak": float(r.get("peak", 0)),
            "secured": float(r.get("secured", 0)),
            "actor": r.get("actor", "human"),
            "session": "unknown",
            "t": r.get("t", 0),
            "alert_id": r.get("alert_id"),
        })
    # Match sessions from position_seen
    seen_map = {}
    for r in recs:
        if r.get("event") == "position_seen" and r.get("ticket"):
            seen_map[r["ticket"]] = r
    for t in trades:
        ps = seen_map.get(t["ticket"])
        if ps and ps.get("open_time"):
            t["session"] = _session_of_time(int(ps["open_time"]), off)
        elif ps:
            t["session"] = _session_of_time(ps["t"], off)

    # Match exit reasons
    exit_map = {}
    for r in recs:
        if r.get("event") == "exit" and r.get("ticket"):
            exit_map[r["ticket"]] = r.get("reason", "unknown")
    for t in trades:
        t["exit_reason"] = exit_map.get(t["ticket"], "unknown")

    # ---- slices: session × actor
    slices: dict[str, SliceMetrics] = {}
    for t in trades:
        for key in [f"{t['session']}", f"{t['actor']}", f"{t['session']}_{t['actor']}", "all"]:
            s = slices.setdefault(key, SliceMetrics())
            s.n += 1
            s.net_pts += t["pts"]
            if t["pts"] > 0:
                s.wins += 1
            else:
                s.losses += 1
            if t["exit_reason"] in ("stop", "pre_stop", "soft_early_lock"):
                s.stops += 1

    # ---- flags
    flags: list[Flag] = []

    # Protect stop hit rate per session
    overall = slices.get("all", SliceMetrics())
    overall_stop_rate = overall.stops / overall.n if overall.n else 0
    for sess in SESSIONS:
        s = slices.get(sess)
        if s and s.n >= STOP_RATE_MIN_N:
            rate = s.stops / s.n
            if rate >= STOP_RATE_HIGH:
                flags.append(Flag(
                    f"protect stop hit {rate:.0%} in {SESSION_NAMES.get(sess, sess)} (n={s.n}) vs {overall_stop_rate:.0%} overall",
                    "pre_stop"))

    # Secured then closed early: pts left on the table
    early_closed = [t for t in trades if t["secured"] > 0 and t["pts"] < t["peak"] and t["peak"] > 0]
    if early_closed:
        avg_left = sum(t["peak"] - t["pts"] for t in early_closed) / len(early_closed)
        if avg_left >= EARLY_CLOSE_AVG:
            flags.append(Flag(
                f"early close left avg +{avg_left:.1f} pts after secure (n={len(early_closed)})",
                "ride_step"))

    # Chop vs clear
    chop_trades = [t for t in trades if any(r.get("event") == "signal" and r.get("chop") and r.get("bar") and
                                            abs(r["t"] - t["t"]) < 3600 for r in recs)]
    clear_trades = [t for t in trades if t not in chop_trades]
    if chop_trades and clear_trades:
        chop_win = sum(1 for t in chop_trades if t["pts"] > 0) / len(chop_trades) * 100
        clear_win = sum(1 for t in clear_trades if t["pts"] > 0) / len(clear_trades) * 100
        if clear_win - chop_win >= CHOP_WIN_DIFF:
            flags.append(Flag(
                f"chop day win {chop_win:.0f}% vs clear {clear_win:.0f}% (diff {clear_win - chop_win:.0f}pp)",
                "chop_crosses"))

    # Missed vs overridden
    missed = [r for r in recs if r.get("event") == "setup_missed"]
    overridden = [r for r in recs if r.get("event") == "override"]
    if missed:
        flags.append(Flag(
            f"missed {len(missed)} setups, overridden {len(overridden)}",
            "missed_grace_min"))

    return {"slices": {k: {"n": v.n, "wins": v.wins, "losses": v.losses, "net_pts": round(v.net_pts, 2),
                            "stops": v.stops, "win_pct": round(v.wins / v.n * 100) if v.n else 0}
                        for k, v in slices.items()},
            "flags": [{"text": f.text, "parameter": f.parameter, "severity": f.severity} for f in flags],
            "trades": len(trades), "days": days}


def card(result: dict) -> dict:
    """Build the NIGHTLY REVIEW card."""
    now = datetime.now(IST)
    flags = result.get("flags", [])[:5]
    fields = []
    # Summary
    all_s = result.get("slices", {}).get("all", {})
    fields.append({"name": "Summary", "value": f"{result['trades']} trades · {result['days']} days · "
                   f"net {all_s.get('net_pts', 0):+.1f} pts · win {all_s.get('win_pct', 0)}% · stops {all_s.get('stops', 0)}",
                   "inline": False})
    # Per session
    for sess in SESSIONS:
        s = result.get("slices", {}).get(sess)
        if s and s["n"] > 0:
            fields.append({"name": SESSION_NAMES.get(sess, sess),
                           "value": f"n={s['n']} · win {s['win_pct']}% · net {s['net_pts']:+.1f} · stops {s['stops']}",
                           "inline": True})
    # Flags
    if flags:
        flag_text = "\n".join(f"{'⚠️' if f['severity'] == 'warning' else '📌'} {f['text']} → `{f['parameter']}`" for f in flags)
        fields.append({"name": "Pattern flags", "value": flag_text, "inline": False})
    else:
        fields.append({"name": "Pattern flags", "value": "none", "inline": False})

    return {"title": f"🔍 NIGHTLY REVIEW", "description": f"Last {result['days']} trading days",
            "fields": fields, "footer": f"Aureon MT5 · review · {now:%H:%M} IST"}


def save(log_dir: str, result: dict, date_str: str):
    os.makedirs(os.path.join(log_dir, "review"), exist_ok=True)
    path = os.path.join(log_dir, "review", f"{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)


def latest(log_dir: str) -> dict | None:
    review_dir = os.path.join(log_dir, "review")
    if not os.path.isdir(review_dir):
        return None
    files = sorted(f for f in os.listdir(review_dir) if f.endswith(".json"))
    if not files:
        return None
    with open(os.path.join(review_dir, files[-1]), encoding="utf-8") as f:
        return json.load(f)
