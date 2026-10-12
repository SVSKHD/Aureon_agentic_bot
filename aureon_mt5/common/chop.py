"""v2.3.5 / v2.3.8 — deterministic chop-day flag.

Pure function of the bar frame. No AI, no advisor calls.
A server day is CHOP when the number of EMA fast/slow crosses before 11:00 server reaches the threshold.

Why `off` is NOT applied: MT5 bar "time" is the broker server clock encoded as a UTC epoch
(common/timeutil.py:server_str treats it as UTC). Applying `off` on top would double-shift and
count crosses in the wrong window. The `off` parameter is kept for signature compatibility but ignored.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np


def server_day_start(bar_t: int) -> int:
    """00:00 server on the day containing bar_t. Bar times are server-clock-as-UTC-epoch."""
    return int(bar_t) - int(bar_t) % 86400


def server_day(bar_t: int) -> str:
    """Server date as 'YYYY-MM-DD'."""
    return datetime.fromtimestamp(server_day_start(bar_t), tz=timezone.utc).strftime("%Y-%m-%d")


def _server_day_start(bar_t: int, off: float | None = None) -> int:
    """Alias for server_day_start (backward compat). `off` is ignored."""
    return server_day_start(bar_t)


def count_morning_crosses(df, fast_col: str, slow_col: str, off: float | None = None) -> int:
    """Count EMA fast/slow sign-change crosses on the server day of the LAST bar, restricted to bars
    whose bar time is >= day_start and < day_start + 11*3600 (before 11:00 server).
    `off` is kept for signature compat but ignored — bar times are already server clock.
    Vectorised with numpy. No datetime.now() — pure function of the frame."""
    if df is None or len(df) < 2:
        return 0
    t = df["time"].to_numpy().astype(np.int64)
    day_start = server_day_start(int(t[-1]))
    morning_end = day_start + 11 * 3600

    f_arr = df[fast_col].to_numpy(dtype=np.float64)
    s_arr = df[slow_col].to_numpy(dtype=np.float64)
    sgn = np.sign(f_arr - s_arr)

    # sign change: sgn[k] != sgn[k-1], both non-zero
    prev = sgn[:-1]; cur = sgn[1:]
    cross = (cur != prev) & (cur != 0) & (prev != 0)
    cross_t = t[1:]
    in_window = cross & (cross_t >= day_start) & (cross_t < morning_end)
    return int(np.sum(in_window))


def chop_state(df, fast_col: str, slow_col: str, off: float | None, threshold: int) -> dict:
    """Returns {"chop": bool, "crosses": int, "day": <server date str>, "threshold": int}.
    Pure function — caller is responsible for sticky behaviour (keep chop=True for the day once reached)."""
    crosses = count_morning_crosses(df, fast_col, slow_col, off)
    if df is not None and len(df) >= 1:
        day = server_day(int(df["time"].to_numpy()[-1]))
    else:
        day = ""
    return {"chop": crosses >= threshold and threshold > 0, "crosses": crosses, "day": day, "threshold": threshold}
