"""v2.3.5 — deterministic chop-day flag.

Pure function of the bar frame + offset. No AI, no advisor calls.
A server day is CHOP when the number of EMA fast/slow crosses before 11:00 server reaches the threshold.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))


def count_morning_crosses(df, fast_col: str, slow_col: str, off: float) -> int:
    """Count EMA fast/slow sign-change crosses on the current server day of the last bar, restricted to bars
    whose server time-of-day < 11:00.  Pure function of the frame (closed bars only)."""
    if df is None or len(df) < 2:
        return 0
    t = df["time"].to_numpy()
    last_bar = int(t[-1])
    # server day start = IST midnight + off (same as claude_advisor.build_snapshot)
    ist_midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = int(ist_midnight.timestamp() + off * 3600)
    # for replays: compute from the last bar's own server day
    if last_bar < day_start or last_bar > day_start + 86400:
        day_start = _server_day_start(last_bar, off)
    morning_end = day_start + 11 * 3600  # 11:00 server

    f_arr = df[fast_col].to_numpy()
    s_arr = df[slow_col].to_numpy()
    sgn = np.sign(f_arr - s_arr)
    count = 0
    for k in range(1, len(df)):
        if sgn[k] != 0 and sgn[k - 1] != 0 and sgn[k] != sgn[k - 1]:
            bar_t = int(t[k])
            if bar_t >= day_start and bar_t < morning_end:
                count += 1
    return count


def _server_day_start(bar_t: int, off: float) -> int:
    """Server-day start (00:00 server) for the day containing bar_t."""
    server_dt = datetime.fromtimestamp(bar_t, tz=timezone(timedelta(hours=off)))
    day_start_dt = server_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start_dt.timestamp())


def chop_state(df, fast_col: str, slow_col: str, off: float, threshold: int) -> dict:
    """Returns {"chop": bool, "crosses": int, "day": <server date str>, "threshold": int}.
    Pure function — caller is responsible for sticky behaviour (keep chop=True for the day once reached)."""
    crosses = count_morning_crosses(df, fast_col, slow_col, off)
    if df is not None and len(df) >= 1:
        last_bar = int(df["time"].to_numpy()[-1])
        server_dt = datetime.fromtimestamp(last_bar, tz=timezone(timedelta(hours=off)))
        day = server_dt.strftime("%Y-%m-%d")
    else:
        day = ""
    return {"chop": crosses >= threshold and threshold > 0, "crosses": crosses, "day": day, "threshold": threshold}
