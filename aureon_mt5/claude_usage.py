"""Append-only Claude CLI usage ledger (IST windows, restart-safe)."""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_lock = threading.Lock()
KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

def append(log_dir, kind, result):
    os.makedirs(log_dir, exist_ok=True)
    usage = getattr(result, "usage", None) or {}
    row = {"ts": time.time(), "kind": kind, "model": getattr(result, "model", "") or "",
           "latency": float(result.latency_s), "ok": bool(result.ok),
           "error": (result.error or "")[:160], "timed_out": bool(result.timed_out),
           "decision": (result.verdict or {}).get("decision"),
           "total_cost_usd": getattr(result, "total_cost_usd", None),
           **{k: int(usage.get(k, 0) or 0) for k in KEYS}}
    with _lock, open(os.path.join(log_dir, "claude_usage.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return row

def summarize(log_dir, period="today", now=None):
    now = now or datetime.now(IST)
    since = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "week": (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0),
        "month": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
    }[period].timestamp()
    totals = {"calls": 0, "ok": 0, "errors": 0, "timeouts": 0, "cost_usd": 0.,
              "by_kind": {}, "verdicts": {}, "avg_latency": 0., "max_latency": 0.,
              **{k: 0 for k in KEYS}}
    latency_sum = 0.
    try:
        with open(os.path.join(log_dir, "claude_usage.jsonl"), encoding="utf-8") as f:
            for line in f:
                try: r = json.loads(line)
                except (ValueError, TypeError): continue
                if float(r.get("ts", 0)) < since: continue
                totals["calls"] += 1
                totals["ok"] += bool(r.get("ok"))
                totals["errors"] += not bool(r.get("ok"))
                totals["timeouts"] += bool(r.get("timed_out"))
                totals["cost_usd"] += float(r.get("total_cost_usd") or 0)
                k = str(r.get("kind") or "unknown")
                totals["by_kind"][k] = totals["by_kind"].get(k, 0) + 1
                decision = r.get("decision")
                if decision: totals["verdicts"][decision] = totals["verdicts"].get(decision, 0) + 1
                for key in KEYS: totals[key] += int(r.get(key) or 0)
                latency = float(r.get("latency") or 0)
                latency_sum += latency
                totals["max_latency"] = max(totals["max_latency"], latency)
    except FileNotFoundError:
        pass
    if totals["calls"]: totals["avg_latency"] = latency_sum / totals["calls"]
    totals["cost_usd"] = round(totals["cost_usd"], 5)
    return totals
