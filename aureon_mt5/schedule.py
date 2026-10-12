"""v2.4.3 — scheduled AGENT windows.

/agent-schedule add <symbol> HH:MM-HH:MM [days=Mon-Fri]
Stored in logs/agent_schedule.json. Scheduler runs from the supervisor thread.
"""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_SHORTS = {v: k.title() for k, v in DAY_NAMES.items()}


def _path(log_dir: str) -> str:
    return os.path.join(log_dir, "agent_schedule.json")


def _load(log_dir: str) -> list[dict]:
    try:
        with open(_path(log_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(log_dir: str, schedules: list[dict]):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(schedules, f, indent=1)
    os.replace(tmp, _path(log_dir))


def _next_id(schedules: list[dict]) -> int:
    return max((s.get("id", 0) for s in schedules), default=0) + 1


def parse_time_range(time_range: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Parse 'HH:MM-HH:MM' → ((start_h, start_m), (end_h, end_m))."""
    parts = time_range.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM-HH:MM, got '{time_range}'")
    def _p(s):
        h, m = s.strip().split(":")
        return int(h), int(m)
    return _p(parts[0]), _p(parts[1])


def parse_days(days_str: str) -> list[int]:
    """Parse 'Mon-Fri' or 'Mon,Wed,Fri' → list of weekday ints (0=Mon)."""
    days_str = days_str.strip().lower()
    if "-" in days_str and "," not in days_str:
        parts = days_str.split("-")
        start = DAY_NAMES.get(parts[0].strip()[:3], 0)
        end = DAY_NAMES.get(parts[1].strip()[:3], 4)
        return list(range(start, end + 1))
    return [DAY_NAMES.get(d.strip()[:3], 0) for d in days_str.split(",") if d.strip()]


def validate_window(start: tuple[int, int], end: tuple[int, int]) -> str | None:
    """Return an error message if the window overlaps 23:00–05:30 IST, else None."""
    s = start[0] + start[1] / 60
    e = end[0] + end[1] / 60
    if s >= 23 or s < 5.5 or e > 23 or e <= 5.5:
        return "window overlaps 23:00–05:30 IST (no-entry hours)"
    if s >= e:
        return "start must be before end"
    return None


def add(log_dir: str, symbol: str, time_range: str, days: str = "Mon-Fri", today_only: bool = False) -> dict | str:
    """Add a schedule. Returns the schedule dict or an error string."""
    try:
        start, end = parse_time_range(time_range)
    except ValueError as e:
        return str(e)
    err = validate_window(start, end)
    if err:
        return err
    day_list = parse_days(days) if not today_only else [datetime.now(IST).weekday()]
    schedules = _load(log_dir)
    sched = {"id": _next_id(schedules), "symbol": symbol.upper(), "start": list(start), "end": list(end),
             "days": day_list, "today_only": today_only, "created": datetime.now(IST).strftime("%Y-%m-%d %H:%M")}
    schedules.append(sched)
    _save(log_dir, schedules)
    return sched


def remove(log_dir: str, sched_id: int) -> bool:
    schedules = _load(log_dir)
    before = len(schedules)
    schedules = [s for s in schedules if s.get("id") != sched_id]
    if len(schedules) < before:
        _save(log_dir, schedules)
        return True
    return False


def list_schedules(log_dir: str, symbol: str | None = None) -> list[dict]:
    schedules = _load(log_dir)
    if symbol:
        return [s for s in schedules if s["symbol"] == symbol.upper()]
    return schedules


def active_window(log_dir: str, symbol: str, now: datetime | None = None) -> dict | None:
    """Returns the schedule dict if now is inside an active window for this symbol, else None."""
    now = now or datetime.now(IST)
    weekday = now.weekday()
    now_minutes = now.hour * 60 + now.minute
    for s in _load(log_dir):
        if s["symbol"] != symbol.upper():
            continue
        if weekday not in s.get("days", []):
            continue
        start_min = s["start"][0] * 60 + s["start"][1]
        end_min = s["end"][0] * 60 + s["end"][1]
        if start_min <= now_minutes < end_min:
            return s
    return None


def tick(log_dir: str, journal, notify, symbol: str, agents: dict, cfg, now: datetime | None = None) -> str | None:
    """Called every supervisor iteration. Manages window start/end transitions. Returns action taken or None."""
    from . import control
    from .halt import is_halted

    now = now or datetime.now(IST)
    ctrl = control.current_mode(journal, symbol)
    window = active_window(log_dir, symbol, now)

    if window and ctrl["mode"] == "MANUAL":
        # HALT suspends windows
        if is_halted(cfg.log_dir):
            return None
        end_min = window["end"][0] * 60 + window["end"][1]
        now_min = now.hour * 60 + now.minute
        remaining = max(1, end_min - now_min)
        control.to_agent(journal, notify, symbol, f"schedule {window['id']}", remaining)
        return f"schedule {window['id']}: AGENT ON"

    if not window and ctrl["mode"] == "AGENT" and ctrl.get("by", "").startswith("schedule"):
        control.to_manual(journal, notify, symbol, "system", "schedule window ended")
        return "schedule window ended"

    # Clean up today-only schedules at end of day
    if now.hour == 23 and now.minute >= 55:
        schedules = _load(log_dir)
        cleaned = [s for s in schedules if not s.get("today_only")]
        if len(cleaned) < len(schedules):
            _save(log_dir, cleaned)

    return None


def format_schedule(s: dict) -> str:
    days = ", ".join(DAY_SHORTS.get(d, str(d)) for d in s.get("days", []))
    return (f"#{s['id']} {s['symbol']} {s['start'][0]:02d}:{s['start'][1]:02d}–{s['end'][0]:02d}:{s['end'][1]:02d} "
            f"· {days}" + (" · today only" if s.get("today_only") else ""))
