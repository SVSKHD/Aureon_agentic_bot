"""v2.3.13 — /halt kill switch. Persistent flag in logs/halt.json.

While HALT is on: governor blocks all entries; detectors still run; the guardian
keeps managing any position that appears. /unhalt requires the exact phrase."""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

from . import broker

IST = timezone(timedelta(hours=5, minutes=30))


def _path(log_dir: str) -> str:
    return os.path.join(log_dir, "halt.json")


def load(log_dir: str) -> dict | None:
    """Returns the halt record if on, else None."""
    try:
        with open(_path(log_dir), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("on"):
            return d
    except Exception:
        pass
    return None


def is_halted(log_dir: str) -> bool:
    return load(log_dir) is not None


def set_halt(log_dir: str, by: str, reason: str) -> dict:
    os.makedirs(log_dir, exist_ok=True)
    d = {"on": True, "by": str(by)[:64], "reason": (reason or "manual")[:120],
         "t": int(_time.time()), "ist": datetime.now(IST).strftime("%H:%M")}
    tmp = _path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _path(log_dir))
    return d


def clear_halt(log_dir: str) -> dict | None:
    prev = load(log_dir)
    try:
        os.remove(_path(log_dir))
    except FileNotFoundError:
        pass
    return prev


def close_all_positions(symbols: list[str], dry: bool = False, retries: int = 3, comment: str = "aureon-halt") -> list[dict]:
    """Close every open position on every symbol. Returns per-ticket results."""
    results = []
    for sym in symbols:
        if dry:
            pos = []
        else:
            pos = broker.positions(sym) or []
        for p in pos:
            ok = False; last_r = None
            for attempt in range(retries):
                r = broker.close(p["ticket"], sym, comment=comment)
                last_r = r
                if r.ok:
                    ok = True; break
            results.append({"ticket": p["ticket"], "symbol": sym, "direction": p["direction"],
                            "points": p["points"], "ok": ok,
                            "retcode": getattr(last_r, "retcode", None),
                            "comment": getattr(last_r, "comment", "")})
    return results


def halt_summary(results: list[dict]) -> str:
    closed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    pts = sum(r["points"] for r in closed)
    lines = [f"⛔ HALT · {len(closed)} positions closed · net {pts:+.1f} pts"]
    for r in closed:
        lines.append(f"  #{r['ticket']} {r['direction'].upper()} {r['points']:+.1f} pts")
    for r in failed:
        lines.append(f"  ❌ #{r['ticket']} FAILED ({r['retcode']} {r['comment']})")
    return "\n".join(lines)
