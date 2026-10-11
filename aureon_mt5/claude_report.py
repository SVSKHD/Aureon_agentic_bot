"""Claude performance cards from graded detector/Claude/trader comparison rows."""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from . import compare
from .claude_usage import summarize

IST = timezone(timedelta(hours=5, minutes=30))
_history_lock = threading.Lock()

def summarize_rows(rows):
    graded = [r for r in rows if r.status == "GRADED"]
    decisions = [r for r in graded if r.claude in ("TAKE", "SKIP")]
    takes = [r for r in decisions if r.claude == "TAKE"]
    skips = [r for r in decisions if r.claude == "SKIP"]
    wins_avoided = [r for r in skips if r.points is not None and r.points < 0]
    def mean(a):
        return round(sum(float(r.points or 0) for r in a) / len(a), 2) if a else None
    disagreements = [r for r in decisions if (r.claude == "TAKE") != (r.me == "TAKEN")]
    disagreements = sorted(disagreements, key=lambda r: abs(r.points or 0), reverse=True)[:3]
    return {
        "graded": len(graded),
        "verdicts": {"TAKE": len(takes), "SKIP": len(skips)},
        "agreement_detector_pct": round(100 * len(takes) / len(decisions), 1) if decisions else None,
        "agreement_trader_pct": round(100 * sum((r.claude == "TAKE") == (r.me == "TAKEN") for r in decisions) / len(decisions), 1) if decisions else None,
        "skip_precision": round(len(wins_avoided) / len(skips), 3) if skips else None,
        "take_expectancy": mean(takes),
        "detector_expectancy": mean(graded),
        "top_disagreements": [{"symbol": r.symbol, "bar": r.bar, "points": r.points,
                               "claude": r.claude, "trader": r.me} for r in disagreements],
    }

def report(cfg, agents, journal, period="week", now=None):
    now = now or datetime.now(IST)
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError("period must be week or month")
    since, until = int(start.timestamp()), int(now.timestamp())
    result = compare.gather(cfg, agents, journal, since, until)
    stats = summarize_rows(result.rows)
    usage = summarize(cfg.log_dir, period)
    fields = [
        {"name": "Graded verdicts", "value": str(stats["verdicts"]), "inline": False},
        {"name": "Agreement", "value": f"detector TAKE {stats['agreement_detector_pct']}% · trader {stats['agreement_trader_pct']}%", "inline": False},
        {"name": "SKIP precision", "value": f"{stats['skip_precision']}" if stats["skip_precision"] is not None else "—", "inline": True},
        {"name": "Expectancy (pts)", "value": f"Claude TAKE {stats['take_expectancy']} · detector all {stats['detector_expectancy']}", "inline": True},
        {"name": "Usage", "value": f"{usage['calls']} calls · {usage['input_tokens']}/{usage['output_tokens']} tokens · CLI reported cost ${usage['cost_usd']:.4f}", "inline": False},
        {"name": "Top disagreements", "value": "\n".join(f"{d['symbol']} {d['bar']}: Claude {d['claude']} / trader {d['trader']} ({d['points']:+.1f}pts)" for d in stats["top_disagreements"]) or "None", "inline": False},
    ]
    return {"title": f"AUREON · CLAUDE REPORT · {period.upper()}", "fields": fields, "stats": stats, "usage": usage,
            "since": since, "until": until, "partial": True}

def read_history(log_dir):
    path = os.path.join(log_dir, "claude_report_history.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(s) for s in f if s.strip()]
    except (OSError, ValueError):
        return []

def append_history(log_dir, period, key):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "claude_report_history.jsonl")
    with _history_lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "period": period, "key": key}) + "\n")
