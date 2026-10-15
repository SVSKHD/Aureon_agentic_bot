"""v2.6.1 — bounded threshold tuning + /approve.

From nightly review flags, propose at most 2 changes per week, each within bounds and one step max.
A proposal is only posted if evidence supports it. Approved changes are written to logs/overrides.json
(loaded at start, hot-reloaded next bar). Anything not approved within 72 h is discarded."""
from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
YELLOW = 0xFEE75C

# Tunable parameters with min, max, and max step per change.
# Strategy ENTRY rules are NOT tunable.
BOUNDS = {
    "pre_stop":           {"min": 4.0,  "max": 15.0, "step": 1.0,  "type": "float"},
    "secure_at":          {"min": 6.0,  "max": 15.0, "step": 1.0,  "type": "float"},
    "ride_step":          {"min": 3.0,  "max": 8.0,  "step": 1.0,  "type": "float"},
    "early_at":           {"min": 2.0,  "max": 5.0,  "step": 0.5,  "type": "float"},
    "early_level":        {"min": 0.5,  "max": 3.0,  "step": 0.5,  "type": "float"},
    "chop_crosses":       {"min": 3,    "max": 8,    "step": 1,    "type": "int"},
    "missed_grace_min":   {"min": 3,    "max": 10,   "step": 1,    "type": "int"},
    "override_max_drift": {"min": 1.0,  "max": 5.0,  "step": 0.5,  "type": "float"},
    "heartbeat_stale_min":{"min": 10,   "max": 30,   "step": 5,    "type": "int"},
}

# Parameters that must NEVER be proposed (entry rules)
NOT_TUNABLE = {"confirm_bars", "confirm_gap", "confirm_window", "touch_tol", "pullback_window",
               "max_chase", "entry_hours_ist", "no_entry_server_hours"}

PROPOSALS_PER_WEEK = 2
EXPIRY_HOURS = 72


def _overrides_path(log_dir: str) -> str:
    return os.path.join(log_dir, "overrides.json")


def _proposals_path(log_dir: str) -> str:
    return os.path.join(log_dir, "tuning_proposals.json")


def load_overrides(log_dir: str) -> dict:
    """Load active overrides {param: value}."""
    try:
        with open(_overrides_path(log_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_overrides(log_dir: str, overrides: dict):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _overrides_path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=1)
    os.replace(tmp, _overrides_path(log_dir))


def load_proposals(log_dir: str) -> list[dict]:
    try:
        with open(_proposals_path(log_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_proposals(log_dir: str, proposals: list[dict]):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _proposals_path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(proposals, f, indent=1)
    os.replace(tmp, _proposals_path(log_dir))


def _next_id(proposals: list[dict]) -> int:
    return max((p.get("id", 0) for p in proposals), default=0) + 1


def propose(log_dir: str, param: str, old_value, new_value, evidence: str, journal=None) -> dict | str:
    """Create a tuning proposal. Returns the proposal dict or an error string."""
    if param in NOT_TUNABLE:
        return f"{param} is not tunable (entry rule)"
    bounds = BOUNDS.get(param)
    if bounds is None:                                   # v2.10.1: registry features (guardian / regime) with their own range
        try:
            from . import features as _F
            f = _F.get(param)
        except ValueError:
            f = None
        if f is None or f.group not in ("guardian", "regime") or f.type not in ("float", "int", "bool"):
            return f"{param} has no tuning bounds"
        bounds = {"min": f.min if f.min is not None else 0, "max": f.max if f.max is not None else 1,
                  "step": float("inf"), "type": f.type}
    # Check bounds
    if new_value < bounds["min"] or new_value > bounds["max"]:
        return f"{param} {new_value} outside bounds [{bounds['min']}, {bounds['max']}]"
    if abs(new_value - old_value) > bounds["step"] + 1e-9:
        return f"{param} step {abs(new_value - old_value)} exceeds max step {bounds['step']}"
    # Check weekly limit
    proposals = load_proposals(log_dir)
    now = _time.time()
    week_ago = now - 7 * 86400
    recent = [p for p in proposals if p.get("t", 0) > week_ago and p.get("status") != "expired"]
    if len(recent) >= PROPOSALS_PER_WEEK:
        return f"already {len(recent)} proposals this week (limit {PROPOSALS_PER_WEEK})"

    prop = {"id": _next_id(proposals), "param": param, "old": old_value, "new": new_value,
            "evidence": evidence[:500], "t": int(now), "status": "pending",
            "expires": int(now + EXPIRY_HOURS * 3600)}
    proposals.append(prop)
    save_proposals(log_dir, proposals)
    if journal:
        journal.log("tuning_proposed", param=param, old=old_value, new=new_value, evidence=evidence[:200])
    return prop


def approve(log_dir: str, proposal_id: int, journal=None) -> dict | str:
    """Approve a pending proposal. Writes to overrides.json."""
    proposals = load_proposals(log_dir)
    prop = next((p for p in proposals if p["id"] == proposal_id), None)
    if prop is None:
        return f"proposal #{proposal_id} not found"
    if prop["status"] != "pending":
        return f"proposal #{proposal_id} is {prop['status']}"
    if _time.time() > prop.get("expires", 0):
        prop["status"] = "expired"
        save_proposals(log_dir, proposals)
        return f"proposal #{proposal_id} expired"

    overrides = load_overrides(log_dir)
    overrides[prop["param"]] = prop["new"]
    save_overrides(log_dir, overrides)
    prop["status"] = "approved"; prop["approved_t"] = int(_time.time())
    save_proposals(log_dir, proposals)
    if journal:
        journal.log("tuning_applied", param=prop["param"], old=prop["old"], new=prop["new"])
    return prop


def reject(log_dir: str, proposal_id: int, journal=None) -> dict | str:
    proposals = load_proposals(log_dir)
    prop = next((p for p in proposals if p["id"] == proposal_id), None)
    if prop is None:
        return f"proposal #{proposal_id} not found"
    prop["status"] = "rejected"
    save_proposals(log_dir, proposals)
    if journal:
        journal.log("tuning_rejected", param=prop["param"])
    return prop


def revert(log_dir: str, param: str, journal=None) -> bool:
    overrides = load_overrides(log_dir)
    if param not in overrides:
        return False
    old_val = overrides.pop(param)
    save_overrides(log_dir, overrides)
    if journal:
        journal.log("tuning_reverted", param=param, was=old_val)
    return True


def expire_stale(log_dir: str):
    """Discard proposals older than EXPIRY_HOURS."""
    proposals = load_proposals(log_dir)
    now = _time.time()
    changed = False
    for p in proposals:
        if p["status"] == "pending" and now > p.get("expires", 0):
            p["status"] = "expired"; changed = True
    if changed:
        save_proposals(log_dir, proposals)


def format_proposal(p: dict) -> str:
    return (f"#{p['id']} **{p['param']}** {p['old']} → {p['new']} · "
            f"{p.get('evidence', '')[:100]} · {p['status']}")
