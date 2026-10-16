"""v2.3.0 — two advisors on one card. Each advisor's verdict is written here under the card's key; the one that answers second
adds its own line plus a one-word AGREE / SPLIT tag. Display only: nothing here acts on a verdict.

  CLAUDE: TAKE · medium · "pullback to EMA 20, gap growing" · evidence gap, dist_to_fast_ema_pts
  CODEX : SKIP · low · "crosses_today 5, chop" · evidence crosses_today
"""
from __future__ import annotations

import threading
from collections import OrderedDict

TAGS = {"claude": "CLAUDE", "codex": "CODEX"}
_lock = threading.Lock()
_board: "OrderedDict[str, dict[str, dict]]" = OrderedDict()
KEEP = 500


def verdict_line(source: str, v: dict, stale: bool = False) -> str:
    tag = TAGS.get(source, source.upper())
    ev = ", ".join(v.get("evidence") or [])
    return (f"{tag:<6}: {'⌛ STALE · ' if stale else ''}{v.get('decision')} · {v.get('confidence')} · \"{v.get('reason', '')}\""
            + (f" · evidence {ev}" if ev else ""))


def agree(a: dict, b: dict) -> bool:
    """Same decision; two TAKEs must also be on the same side."""
    if a.get("decision") != b.get("decision"):
        return False
    return a.get("decision") != "TAKE" or (a.get("side") or None) == (b.get("side") or None)


def record(key: str, source: str, v: dict, stale: bool = False) -> dict[str, dict]:
    """Store this advisor's verdict for `key`; return the OTHER advisors' entries {source: {"v", "stale", "line"}}."""
    if not key:
        return {}
    with _lock:
        row = _board.setdefault(key, {})
        row[source] = {"v": v, "stale": stale, "line": verdict_line(source, v, stale)}
        _board.move_to_end(key)
        while len(_board) > KEEP:
            _board.popitem(last=False)
        return {k: dict(x) for k, x in row.items() if k != source}


def tag(v: dict, others: dict[str, dict]) -> str | None:
    """AGREE / SPLIT against the other advisor (None while it has not answered)."""
    if not others:
        return None
    return "AGREE" if all(agree(v, o["v"]) for o in others.values()) else "SPLIT"


def tag_text(t: str | None) -> str:
    return {"AGREE": "AGREE ✅", "SPLIT": "SPLIT ⚠️"}.get(t or "", "")


def clear():
    with _lock:
        _board.clear()


# ----------------------------------------------------------------------------- v2.8.0 READ grading by trigger
def read_grades(logs: list[dict], source: str = "claude") -> list[dict]:
    """Pair each READ verdict on a position (trigger open / drawdown / opposite P held) with the trade's final points from the
    `closed` journal row of the same ticket. Rows: {trigger, decision, p_win, points, graded, ticket}. Measurement only."""
    final = {r.get("ticket"): r.get("final_points") for r in logs if r.get("event") == "closed" and r.get("final_points") is not None}
    out = []
    for r in logs:
        if r.get("event") != f"{source}_verdict" or r.get(f"{source}_event") != "READ" or not r.get("decision"):
            continue
        pts = final.get(r.get("ticket"))
        out.append({"trigger": r.get("trigger") or "—", "decision": r.get("decision"), "p_win": r.get("p_win"),
                    "points": float(pts) if pts is not None else None, "graded": pts is not None, "ticket": r.get("ticket")})
    return out


def read_buckets(rows: list[dict]) -> dict[str, dict]:
    """Per trigger: n graded, p_win buckets (<0.4 / 0.4–0.6 / >=0.6) → n, win %, avg final points; EXIT vs HOLD avg points."""
    out: dict[str, dict] = {}
    for r in rows:
        if not r["graded"]:
            continue
        t = out.setdefault(r["trigger"], {"n": 0, "buckets": {}, "by_decision": {}})
        t["n"] += 1
        if r["p_win"] is not None:
            b = "<0.4" if r["p_win"] < 0.4 else "0.4-0.6" if r["p_win"] < 0.6 else ">=0.6"
            bb = t["buckets"].setdefault(b, {"n": 0, "wins": 0, "pts": 0.0})
            bb["n"] += 1; bb["wins"] += int(r["points"] > 0); bb["pts"] += r["points"]
        d = t["by_decision"].setdefault(r["decision"], {"n": 0, "pts": 0.0})
        d["n"] += 1; d["pts"] += r["points"]
    return out


def read_buckets_text(b: dict[str, dict]) -> str:
    lines = []
    for trig, t in sorted(b.items()):
        bk = " · ".join(f"p_win {k}: {v['n']} · {round(100 * v['wins'] / v['n'])}% win · {v['pts'] / v['n']:+.1f}"
                        for k, v in sorted(t["buckets"].items()))
        dec = " · ".join(f"{k} {v['n']} avg {v['pts'] / v['n']:+.1f}" for k, v in sorted(t["by_decision"].items()))
        lines.append(f"{trig} (n={t['n']}): {bk or 'no p_win'} | {dec}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- v2.12.0 pre-exit grading
def _entries(logs: list[dict]) -> dict:
    """ticket → (entry price, +1 long / −1 short) from position_seen rows."""
    out = {}
    for r in logs:
        if r.get("event") == "position_seen" and r.get("entry") is not None:
            out[r.get("ticket")] = (float(r["entry"]), 1 if str(r.get("side", "")).upper() in ("LONG", "BUY") else -1)
    return out


def pre_exit_grades(logs: list[dict], *, grade_bars: int = 6, price_at=None, source: str = "claude", bar_s: int = 300) -> list[dict]:
    """Every pre_exit verdict, graded against the price the rule would have closed at (the verdict's open_profit):
    deferral_gain_pts = points N bars later (or at the trade's exit if that came first) − points at the rule.
    For HOLD: > 0 the deferral earned it. For EXIT: > 0 holding would have paid (EXIT was early).
    price_at(symbol, bar_t) → close of that bar (bars / MT5); without it only trades that exited within N bars grade."""
    entries = _entries(logs)
    exits = {}
    for r in logs:
        if r.get("event") == "exit" and r.get("points") is not None:
            exits.setdefault(r.get("ticket"), r)
    out = []
    for r in logs:
        if r.get("event") != f"{source}_verdict" or r.get("trigger") != "pre_exit" or not r.get("decision"):
            continue
        tk, bar = r.get("ticket"), r.get("bar")
        at_rule = r.get("open_profit")
        row = {"ticket": tk, "rule": r.get("rule"), "decision": r.get("decision"), "deferral_n": r.get("deferral_n"),
               "points_at_rule": at_rule, "deferral_gain_pts": None, "graded": False}
        if at_rule is None or bar is None:
            out.append(row); continue
        ex = exits.get(tk)
        horizon = int(bar) + grade_bars * bar_s
        later = None
        if ex is not None and ex.get("bar") is not None and int(ex["bar"]) <= horizon:
            later = float(ex["points"])
        elif ex is not None and ex.get("t") is not None and r.get("t") is not None and float(ex["t"]) - float(r["t"]) <= grade_bars * bar_s:
            later = float(ex["points"])
        elif price_at is not None and tk in entries:
            px = price_at(r.get("symbol"), horizon)
            if px is not None:
                entry, s = entries[tk]
                later = (float(px) - entry) * s
        if later is not None:
            row.update(deferral_gain_pts=round(later - float(at_rule), 2), graded=True)
        out.append(row)
    return out


def pre_exit_summary(logs: list[dict], grades: list[dict], cap_pts: float = 6.0) -> dict:
    """/advisor-report pre-exit section: deferrals, avg gain per deferral, deferred trades that went on to hit the cap."""
    deferred = [r for r in logs if r.get("event") == "exit_deferred"]
    tickets = {r.get("ticket") for r in deferred}
    final = {r.get("ticket"): r.get("final_points") for r in logs if r.get("event") == "closed" and r.get("final_points") is not None}
    hit_cap = sum(1 for t in tickets if t in final and float(final[t]) <= -cap_pts + 0.5)
    hold = [g["deferral_gain_pts"] for g in grades if g["decision"] == "HOLD" and g["graded"]]
    ex = [g["deferral_gain_pts"] for g in grades if g["decision"] == "EXIT" and g["graded"]]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    return {"deferrals": len(deferred), "deferred_trades": len(tickets), "hold_graded": len(hold), "avg_gain_per_deferral": avg(hold),
            "deferral_wins": sum(1 for x in hold if x > 0), "exit_graded": len(ex), "avg_gain_had_held_after_exit": avg(ex),
            "deferred_hit_cap": hit_cap, "verdicts": len(grades)}


def pre_exit_text(s: dict) -> str:
    if not s["verdicts"] and not s["deferrals"]:
        return "no pre-exit reads in this period (`AUREON_PRE_EXIT_READ=off`?)"
    a = s["avg_gain_per_deferral"]; e = s["avg_gain_had_held_after_exit"]
    return (f"{s['deferrals']} deferral(s) on {s['deferred_trades']} trade(s) · HOLD graded {s['hold_graded']}: "
            f"avg {a:+.2f} pts per deferral ({s['deferral_wins']} gained)" if a is not None else
            f"{s['deferrals']} deferral(s) on {s['deferred_trades']} trade(s) · no HOLD graded yet") + \
        (f" · EXIT graded {s['exit_graded']}: holding would have made {e:+.2f}" if e is not None else "") + \
        f" · deferred trades that hit the cap: {s['deferred_hit_cap']}"
