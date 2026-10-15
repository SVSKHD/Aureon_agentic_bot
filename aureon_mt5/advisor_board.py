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
