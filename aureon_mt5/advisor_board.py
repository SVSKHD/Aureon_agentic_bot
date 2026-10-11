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
