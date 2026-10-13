"""v2.5.1 — advisor calibration: bucket entries by p_win, measure win% and avg NET pts.

Determines whether each advisor's p_win ranking is "useful" (top half beats bottom half on avg NET pts
with n >= AUREON_CALIB_MIN_N). Result stored in logs/advisor_calibration.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class CalibResult:
    source: str           # "claude" | "codex"
    n: int
    top_half_avg: float
    bottom_half_avg: float
    useful: bool
    buckets: list[dict]   # [{range, n, win_pct, avg_pts}]
    brier: float | None


def calibrate(rows: list[dict], source: str = "claude", min_n: int = 30) -> CalibResult:
    """rows: [{p_win, points, graded}] — from the shadow replay or journal.
    Only graded rows with p_win not None are used."""
    scored = [r for r in rows if r.get("p_win") is not None and r.get("graded")]
    if len(scored) < min_n:
        return CalibResult(source, len(scored), 0.0, 0.0, False, [], None)

    scored.sort(key=lambda r: r["p_win"])
    mid = len(scored) // 2
    bottom = scored[:mid]; top = scored[mid:]

    top_avg = sum(r["points"] for r in top) / len(top) if top else 0.0
    bot_avg = sum(r["points"] for r in bottom) / len(bottom) if bottom else 0.0
    useful = top_avg > bot_avg and len(scored) >= min_n

    # Decile buckets
    buckets = []
    for d in range(10):
        lo, hi = d / 10, (d + 1) / 10
        bucket = [r for r in scored if lo <= r["p_win"] < hi] if d < 9 else [r for r in scored if lo <= r["p_win"] <= 1.0]
        if not bucket:
            continue
        wins = sum(1 for r in bucket if r["points"] > 0)
        avg = sum(r["points"] for r in bucket) / len(bucket)
        buckets.append({"range": f"{lo:.1f}-{hi:.1f}", "n": len(bucket), "win_pct": round(wins / len(bucket) * 100),
                        "avg_pts": round(avg, 2)})

    # Brier score
    brier = None
    try:
        brier = round(sum((r["p_win"] - (1.0 if r["points"] > 0 else 0.0)) ** 2 for r in scored) / len(scored), 4)
    except Exception:
        pass

    return CalibResult(source, len(scored), round(top_avg, 2), round(bot_avg, 2), useful, buckets, brier)


def save(log_dir: str, result: CalibResult):
    path = os.path.join(log_dir, "advisor_calibration.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[result.source] = {"useful": result.useful, "n": result.n, "top_half_avg": result.top_half_avg,
                            "bottom_half_avg": result.bottom_half_avg, "brier": result.brier,
                            "buckets": result.buckets}
    os.makedirs(log_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load(log_dir: str, source: str = "claude") -> dict | None:
    try:
        with open(os.path.join(log_dir, "advisor_calibration.json"), encoding="utf-8") as f:
            return json.load(f).get(source)
    except Exception:
        return None


def is_useful(log_dir: str, source: str = "claude") -> bool:
    d = load(log_dir, source)
    return bool(d and d.get("useful"))


def summary_line(result: CalibResult) -> str:
    return (f"top half {result.top_half_avg:+.1f} pts avg vs bottom half {result.bottom_half_avg:+.1f} "
            f"(n={result.n}) → {'useful' if result.useful else 'not useful'}")
