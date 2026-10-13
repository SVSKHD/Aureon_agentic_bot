"""v2.5.3 — Codex end-of-day review, read-only.

At AUREON_EOD_AT (default 23:15 IST) on trading days: build a day pack from journal.jsonl
and run `codex exec` with a read-only prompt. No parameter changes, no code edits."""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
GREY = 0x95A5A6

EOD_PROMPT = """You are Aureon's end-of-day reviewer. Summarise today's trading in XAUUSD.

Below is the journal of today's events (signals, decisions, orders, guardian actions, misses, governor blocks, costs, chop flag).

RULES:
- What went well? (cite specific trades/actions)
- What cost points? (cite specific trades/actions)
- Any rule violations or near-misses?
- Three concrete observations about today's trading.
- NO parameter changes. NO code edits. NO file modifications. Read only.

Respond in JSON:
{"summary": "<1 sentence>", "wins": ["..."], "costs": ["..."], "violations": ["..."], "observations": ["..."]}

TODAY'S JOURNAL:
"""


def build_day_pack(journal, date_str: str | None = None) -> str:
    """Build a text pack of today's journal events for the Codex prompt."""
    now = datetime.now(IST)
    if date_str:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    else:
        d = now
    day_start = int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    day_end = day_start + 86400
    recs = journal.read(day_start, day_end)
    # Filter to decision-relevant events
    relevant = [r for r in recs if r.get("event") in (
        "signal", "decision", "alert_decision", "order_placed", "governor_block",
        "setup_missed", "override", "override_ignored", "halt", "unhalt",
        "protected", "secured", "early_lock", "exit", "closed", "chop_day",
        "pre_cross", "soft_lock_exit", "agent_entry", "control_mode",
    )]
    lines = []
    for r in relevant:
        # Compact: event, time, key fields
        t = datetime.fromtimestamp(r["t"], tz=IST).strftime("%H:%M")
        ev = r["event"]
        parts = [f"{t} {ev}"]
        for k in ("symbol", "side", "decision", "price", "lots", "points", "rule", "reason", "ticket", "level"):
            if r.get(k) is not None:
                parts.append(f"{k}={r[k]}")
        lines.append(" · ".join(parts))
    return "\n".join(lines[-100:])  # last 100 events, capped


def validate_response(text: str) -> dict | None:
    """Parse and validate the Codex JSON response. Returns the dict or None."""
    try:
        # Strip markdown fences
        import re
        t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip()).strip()
        if not t.startswith("{"):
            a, b = t.find("{"), t.rfind("}")
            if a < 0 or b <= a:
                return None
            t = t[a:b + 1]
        obj = json.loads(t)
        if not isinstance(obj, dict):
            return None
        # Must have at least summary
        if "summary" not in obj:
            return None
        for key in ("wins", "costs", "violations", "observations"):
            if key not in obj:
                obj[key] = []
            if not isinstance(obj[key], list):
                obj[key] = [str(obj[key])]
        return obj
    except Exception:
        return None


def run_review(cfg, journal, notify, date_str: str | None = None, runner=None) -> dict:
    """Run the EOD review. Returns {"ok", "card", "response"}."""
    pack = build_day_pack(journal, date_str)
    if not pack.strip():
        return {"ok": False, "card": None, "response": None, "error": "no journal events today"}

    prompt = EOD_PROMPT + pack
    now = datetime.now(IST)
    date_label = date_str or now.strftime("%Y-%m-%d")
    day_name = now.strftime("%a %d %b") if not date_str else date_str

    if runner is not None:
        # Test/mock runner
        result = runner(prompt)
    else:
        try:
            from .common.codex_cli import call
            result = call(prompt, event="ALERT", binary=cfg.codex_bin, workdir=cfg.codex_workdir,
                          timeout=cfg.codex_timeout, model=cfg.codex_model or None, free_text=True)
            if not result.ok:
                return {"ok": False, "card": None, "response": None, "error": result.error or "codex call failed"}
            result = result.text
        except Exception as e:
            return {"ok": False, "card": None, "response": None, "error": str(e)[:200]}

    parsed = validate_response(result if isinstance(result, str) else "")
    if parsed is None:
        card = {"title": f"📓 EOD REVIEW · {day_name}", "description": "EOD review unavailable (invalid response)",
                "fields": [], "footer": f"Aureon MT5 · EOD · {now:%H:%M} IST"}
        return {"ok": False, "card": card, "response": result, "error": "invalid JSON"}

    # Build card
    fields = []
    if parsed.get("wins"):
        fields.append({"name": "Wins", "value": "\n".join(f"✅ {w}" for w in parsed["wins"][:5]), "inline": False})
    if parsed.get("costs"):
        fields.append({"name": "Costs", "value": "\n".join(f"💸 {c}" for c in parsed["costs"][:5]), "inline": False})
    if parsed.get("violations"):
        fields.append({"name": "Violations", "value": "\n".join(f"⚠️ {v}" for v in parsed["violations"][:3]), "inline": False})
    if parsed.get("observations"):
        fields.append({"name": "Observations", "value": "\n".join(f"📝 {o}" for o in parsed["observations"][:3]), "inline": False})
    card = {"title": f"📓 EOD REVIEW · {day_name}", "description": parsed.get("summary", ""),
            "fields": fields, "footer": f"Aureon MT5 · EOD · {now:%H:%M} IST"}
    return {"ok": True, "card": card, "response": parsed, "error": None}


def tick(cfg, journal, notify, now: datetime | None = None) -> bool:
    """Called from the supervisor loop. Posts the EOD review once per day at the configured time.
    Returns True if posted."""
    if cfg.codex_mode == "off":
        return False
    now = now or datetime.now(IST)
    if now.weekday() >= 5:  # skip weekends
        return False
    hh, mm = (int(x) for x in cfg.eod_at.split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    key = f"eod:{now.strftime('%Y-%m-%d')}"
    if key in notify.sent:
        return False
    notify.sent.add(key)  # dedupe before the call so restarts/retries don't double-post
    result = run_review(cfg, journal, notify)
    if result["card"]:
        notify.send(key, result["card"]["title"], [result["card"]["description"]],
                    fields=result["card"]["fields"], color=GREY, footer=result["card"]["footer"])
    journal.log("eod_review", date=now.strftime("%Y-%m-%d"), ok=result["ok"],
                error=result.get("error", "")[:200])
    return True
