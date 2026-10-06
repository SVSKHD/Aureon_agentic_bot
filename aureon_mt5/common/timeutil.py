"""Time helpers. MT5 encodes the broker's server clock as a UTC epoch; IST = server − offset + 5:30."""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_server(off_h: float) -> int:
    return int(_time.time() + off_h * 3600)


def ist_str(ts_server: int, off_h: float, fmt: str = "%d %b %H:%M") -> str:
    return datetime.fromtimestamp(ts_server - off_h * 3600, tz=IST).strftime(fmt)


def server_str(ts_server: int, fmt: str = "%d %b %H:%M") -> str:
    return datetime.fromtimestamp(ts_server, tz=timezone.utc).strftime(fmt)


def bar_key(ts_server: int, off_h: float) -> str:
    """Stable id for a bar in dedupe keys: IST ISO minute."""
    return datetime.fromtimestamp(ts_server - off_h * 3600, tz=IST).strftime("%Y-%m-%dT%H:%M")
