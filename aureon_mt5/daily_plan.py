"""v2.3.15 — daily plan card at 05:00 IST on weekdays.

Runs from the main/supervisor thread (same scheduler as the watchdog heartbeat).
Dedupe key plan:<date> — restart-safe. /plan posts on demand."""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

from . import broker
from .common.news import load_news, session_of
from .config import VERSION
from .halt import is_halted

IST = timezone(timedelta(hours=5, minutes=30))
GREY = 0x95A5A6


def plan_card(cfg, agents: dict, journal, now: datetime | None = None) -> dict:
    """Build the daily plan card content. Returns {title, lines, fields}."""
    now = now or datetime.now(IST)
    date_str = now.strftime("%a %d %b")

    # News today
    news_times = load_news(cfg.news_file if cfg.news_file and __import__("os").path.exists(cfg.news_file) else None, now.year)
    today_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today_end = today_start + 86400
    today_news = sorted([t for t in news_times if today_start <= t < today_end])
    if today_news:
        news_line = " · ".join(f"{datetime.fromtimestamp(t, tz=IST):%H:%M} IST" for t in today_news[:5])
        news_line += f" (entry block ±60/30 min each)"
    else:
        news_line = "no high-impact events today"

    # Sessions
    sessions_line = "Asia 05:30–12:00 · London 12:00–20:30 · NY 17:30–01:30 IST · no-entry 23:00–05:30"

    # Yesterday: net pts, trades, win/loss
    yesterday = now - timedelta(days=1)
    yday_start = int(yesterday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    yday_recs = journal.read(yday_start, today_start)
    yday_trades = [r for r in yday_recs if r.get("event") == "closed" and r.get("final_points") is not None]
    if yday_trades:
        yday_net = sum(float(r["final_points"]) for r in yday_trades)
        yday_wins = sum(1 for r in yday_trades if float(r["final_points"]) > 0)
        yday_losses = sum(1 for r in yday_trades if float(r["final_points"]) <= 0)
        yesterday_line = f"net {yday_net:+.1f} pts · {len(yday_trades)} trades · win {yday_wins} / loss {yday_losses}"
    else:
        yesterday_line = "no closed trades"

    # Week to date
    monday = now - timedelta(days=now.weekday())
    week_start = int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    week_recs = journal.read(week_start, today_start)
    week_trades = [r for r in week_recs if r.get("event") == "closed" and r.get("final_points") is not None]
    week_net = sum(float(r["final_points"]) for r in week_trades)
    week_line = f"net {week_net:+.1f} pts · {len(week_trades)} trades"
    if cfg.week_goal_pts > 0:
        week_line += f" · goal {cfg.week_goal_pts:g} pts ({week_net / cfg.week_goal_pts * 100:.0f}%)" if cfg.week_goal_pts else ""

    # Chop
    chop_lines = []
    for sym, ag in agents.items():
        ch = getattr(ag, "chop", None)
        if ch and ch.get("chop"):
            chop_lines.append(f"{sym}: CHOP ({ch['crosses']} crosses)")
        elif ch:
            chop_lines.append(f"{sym}: not set ({ch['crosses']} crosses, threshold {ch['threshold']})")
    chop_line = " · ".join(chop_lines) if chop_lines else "not set"

    # Governor + halt
    from .governor import Governor
    gov = Governor(cfg)
    gov_line = f"{cfg.max_trades_day} trades allowed · loss cap {cfg.daily_loss_limit_pct:g}%"
    halt = is_halted(cfg.log_dir)
    if halt:
        gov_line = f"⛔ HALTED ({halt.get('reason', '')}) · " + gov_line

    fields = [
        {"name": "News", "value": news_line, "inline": False},
        {"name": "Sessions", "value": sessions_line, "inline": False},
        {"name": "Yesterday", "value": yesterday_line, "inline": False},
        {"name": "Week to date", "value": week_line, "inline": False},
        {"name": "Chop", "value": chop_line, "inline": False},
        {"name": "Governor", "value": gov_line, "inline": False},
    ]
    return {"title": f"🗓 DAILY PLAN · {date_str}", "lines": [], "fields": fields,
            "footer": f"v{VERSION} · {now:%H:%M} IST"}


def tick(cfg, agents, journal, notify, now: datetime | None = None) -> bool:
    """Called from the supervisor loop. Posts the plan once per IST day at the configured time.
    Returns True if posted."""
    if not cfg.daily_plan:
        return False
    now = now or datetime.now(IST)
    if now.weekday() >= 5:  # skip weekends
        return False
    hh, mm = (int(x) for x in cfg.daily_plan_at.split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    key = f"plan:{now.strftime('%Y-%m-%d')}"
    if key in notify.sent:
        return False
    card = plan_card(cfg, agents, journal, now)
    notify.send(key, card["title"], card["lines"], fields=card["fields"], color=GREY, footer=card["footer"])
    journal.log("daily_plan", date=now.strftime("%Y-%m-%d"))
    return True
