"""Advisor performance cards (v2.3.0: Claude and Codex) from graded detector / advisor / trader comparison rows."""
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

def summarize_rows(rows, source="claude"):
    """Graded verdict stats for one advisor column of compare rows (Row.claude / Row.codex)."""
    col = lambda r: getattr(r, source, None)
    graded = [r for r in rows if r.status == "GRADED"]
    decisions = [r for r in graded if col(r) in ("TAKE", "SKIP")]
    takes = [r for r in decisions if col(r) == "TAKE"]
    skips = [r for r in decisions if col(r) == "SKIP"]
    wins_avoided = [r for r in skips if r.points is not None and r.points < 0]
    def mean(a):
        return round(sum(float(r.points or 0) for r in a) / len(a), 2) if a else None
    disagreements = [r for r in decisions if (col(r) == "TAKE") != (r.me == "TAKEN")]
    disagreements = sorted(disagreements, key=lambda r: abs(r.points or 0), reverse=True)[:3]
    return {
        "graded": len(graded),
        "verdicts": {"TAKE": len(takes), "SKIP": len(skips)},
        "agreement_detector_pct": round(100 * len(takes) / len(decisions), 1) if decisions else None,
        "agreement_trader_pct": round(100 * sum((col(r) == "TAKE") == (r.me == "TAKEN") for r in decisions) / len(decisions), 1) if decisions else None,
        "skip_precision": round(len(wins_avoided) / len(skips), 3) if skips else None,
        "take_expectancy": mean(takes),
        "detector_expectancy": mean(graded),
        "top_disagreements": [{"symbol": r.symbol, "bar": r.bar, "points": r.points,
                               source: col(r), "trader": r.me} for r in disagreements],
    }


def period_window(period, now=None):
    now = now or datetime.now(IST)
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.day == 1:
            # The 1st-of-month scheduled report covers the completed previous month.
            now = start
            start = (start - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError("period must be week or month")
    return start, now


def calibration(rows, logs, source="claude"):
    """Directional correctness by stated confidence (low / medium / high) — not a probability forecast."""
    conf_by_signal = {(r.get("symbol"), r.get(f"{source}_event"), r.get("bar")): r.get("confidence")
                      for r in logs if r.get("event") == f"{source}_verdict" and not r.get("stale")}
    out = {}
    for row in rows:
        mine = getattr(row, source, None)
        if row.status != "GRADED" or mine not in ("TAKE", "SKIP"):
            continue
        level = conf_by_signal.get((row.symbol, row.kind, row.bar))
        if level not in ("low", "medium", "high"):
            continue
        b = out.setdefault(level, {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += int((mine == "TAKE" and row.points > 0) or (mine == "SKIP" and row.points < 0))
    return out


def advisor_fields(label, stats, conf, usage, pullback, applies):
    """The per-advisor section of the /advisor-report card."""
    by = pullback.by_decision
    return [
        {"name": f"{label} · verdicts", "value": f"TAKE {stats['verdicts']['TAKE']} · SKIP {stats['verdicts']['SKIP']} (graded)"
                                               + ("" if applies else " · shown only, never applied"), "inline": False},
        {"name": f"{label} · agreement", "value": f"with detector (TAKE) {stats['agreement_detector_pct']}% · with you {stats['agreement_trader_pct']}%", "inline": False},
        {"name": f"{label} · SKIP precision · TAKE expectancy",
         "value": f"SKIP precision {stats['skip_precision'] if stats['skip_precision'] is not None else '—'} · "
                  f"TAKE {stats['take_expectancy']} vs detector-all {stats['detector_expectancy']} pts", "inline": False},
        {"name": f"{label} · calibration by confidence", "value": " · ".join(f"{k} {v['correct']}/{v['n']}" for k, v in sorted(conf.items()))
                                                                    or "no graded confidence evidence", "inline": False},
        {"name": f"{label} · pullbacks", "value": " · ".join(f"{k} {by.get(k, {}).get('right', 0)}/{by.get(k, {}).get('n', 0)} right"
                                                            for k in ("HOLD", "TIGHTEN", "CLOSE")) + f" · {pullback.ungraded} ungraded", "inline": False},
        {"name": f"{label} · usage", "value": f"{usage['calls']} calls · {usage['input_tokens']}/{usage['output_tokens']} tokens · "
                                             f"CLI reported cost ${usage['cost_usd']:.4f}", "inline": False},
    ]


def advisor_report(cfg, agents, journal, period="week", now=None, gather=None):
    """v2.3.0 /advisor-report (alias /claude-report): one card, a section per advisor plus head-to-head. Measurement only."""
    start, now = period_window(period, now)
    since, until = int(start.timestamp()), int(now.timestamp())
    result = (gather or compare.gather)(cfg, agents, journal, since, until)
    logs = journal.read(since, until)
    usage_now = now - timedelta(seconds=1) if period == "month" else now
    fields, stats, usage = [], {}, {}
    for src, label, applies in (("claude", "Claude", True), ("codex", "Codex", False)):
        st = summarize_rows(result.rows, src)
        st["confidence"] = calibration(result.rows, logs, src)
        stats[src] = st
        usage[src] = summarize(cfg.log_dir, period, now=usage_now, source=src)
        if src == "codex" and not st["verdicts"]["TAKE"] + st["verdicts"]["SKIP"] and not usage[src]["calls"]:
            fields.append({"name": "Codex", "value": "no Codex verdicts or calls in this period (`AUREON_CODEX=off`?)", "inline": False})
            continue
        pb = result.pullback if src == "claude" else result.codex_pullback
        fields += advisor_fields(label, st, st["confidence"], usage[src], pb, applies)
        if src == "claude":                                       # v2.8.0: open / drawdown / flip-held READs vs the trade result
            from .advisor_board import read_buckets, read_buckets_text, read_grades
            rb = read_buckets(read_grades(logs, src))
            if rb:
                fields.append({"name": f"{label} · READ votes by trigger (final points)", "value": read_buckets_text(rb)[:1024],
                               "inline": False})
    h = result.h2h or {}
    if h.get("both"):
        fields.append({"name": "Head-to-head · Claude vs Codex",
                       "value": (f"both judged {h['both']} · agree {h['agree_pct']:.0f}% · split {h['split']}\n"
                                 f"when they SPLIT: Claude right {h['claude_right']} ({h['claude_pts']:+.1f} pts) · "
                                 f"Codex right {h['codex_right']} ({h['codex_pts']:+.1f} pts)"
                                 + (f" · tie {h['tie']}" if h["tie"] else "") + (f" · ungraded {h['ungraded']}" if h["ungraded"] else "")),
                       "inline": False})
    else:
        fields.append({"name": "Head-to-head · Claude vs Codex", "value": "no signal judged by both yet", "inline": False})
    return {"title": f"AUREON · ADVISOR REPORT · {period.upper()}", "fields": fields, "stats": stats, "usage": usage, "h2h": h,
            "description": "Graded advisory performance; ungraded signals excluded. Claude may apply TIGHTEN / CLOSE in manage; "
                           "Codex is shown only. No rules changed.",
            "since": since, "until": until, "partial": True}


def report(cfg, agents, journal, period="week", now=None):
    """Kept for callers of v2.2: the advisor report (Claude section first)."""
    return advisor_report(cfg, agents, journal, period, now=now)

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
