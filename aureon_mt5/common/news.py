"""Shared, strategy-neutral helpers: trading sessions and the news calendar (NFP first-Friday + user file)."""
from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ sessions (UTC hours)
SESSIONS = [  # name, start_utc, end_utc
    ("asia", 0, 7),
    ("london", 7, 12),
    ("ny", 12, 21),
]


def session_of(ts_server: int, server_offset_h: float) -> str:
    h = ((ts_server - server_offset_h * 3600) % 86400) / 3600
    for name, a, b in SESSIONS:
        if a <= h < b:
            return name
    return "off"


# ------------------------------------------------------------------ news blackout
def nfp_times(year: int, months: range = range(1, 13)) -> list[int]:
    """US Non-Farm Payrolls: first Friday of each month, 12:30 UTC (13:30 UTC in US summer? no — BLS releases 08:30 ET,
    which is 12:30 UTC during US daylight time and 13:30 UTC in winter)."""
    from datetime import datetime, timedelta, timezone
    out = []
    for m in months:
        d = datetime(year, m, 1, tzinfo=timezone.utc)
        while d.weekday() != 4:
            d += timedelta(days=1)
        # US DST: second Sunday of March to first Sunday of November
        dst = (m > 3 or (m == 3 and d.day >= 14)) and (m < 11 or (m == 11 and d.day < 7))
        rel = d.replace(hour=12 if dst else 13, minute=30)
        out.append(int(rel.timestamp()))
    return out


def load_news(path: str | None, year: int) -> tuple:
    """NFP first-Friday windows for the year, plus any UTC times listed one per line in `path`
    as 'YYYY-MM-DD HH:MM  name' (ISO date + 24h UTC time)."""
    from datetime import datetime, timedelta, timezone
    wins = list(nfp_times(year))
    if path:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ts = datetime.strptime(" ".join(line.split()[:2]), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            wins.append(int(ts.timestamp()))
    return tuple(sorted(set(wins)))




