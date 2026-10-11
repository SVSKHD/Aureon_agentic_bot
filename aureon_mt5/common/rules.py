"""v2.3.0 — one rules prompt for both advisors: prompts/advisor_rules.md.

prompts/claude_rules.md is kept as an alias (a symlink in git; on a Windows checkout without symlinks it is a one-line
pointer file). Code always reads advisor_rules.md and falls back to the legacy name only when the new file is missing."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_PATH = os.path.join(ROOT, "prompts", "advisor_rules.md")
LEGACY_RULES_PATH = os.path.join(ROOT, "prompts", "claude_rules.md")


def rules_path() -> str:
    if os.path.isfile(RULES_PATH) or not os.path.isfile(LEGACY_RULES_PATH):
        return RULES_PATH
    return LEGACY_RULES_PATH


def read_rules(default: str = "Reply with one JSON object only.") -> str:
    try:
        with open(rules_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


# ----------------------------------------------------------------------------- v2.3.2 ema2050 entry SKIP reasons (the rules file lists
# exactly these strings; tests check the file and this list agree). Measurement / tests only — nothing in the live path calls this.
SKIP_REASONS_2050 = ("|dist_to_fast_ema_pts| > 5", "swing_against_last6_pts >= 8", "crosses_today >= 5 before 11:00 server")


def ema2050_skip_reasons(snap: dict) -> list[str]:
    """The listed SKIP reasons that apply to an ema2050 ENTER snapshot ([] = TAKE, the default)."""
    out = []
    dist = snap.get("dist_to_fast_ema_pts")
    if isinstance(dist, (int, float)) and abs(dist) > 5:
        out.append(SKIP_REASONS_2050[0])
    swing = snap.get("swing_against_last6_pts")
    if isinstance(swing, (int, float)) and swing >= 8:
        out.append(SKIP_REASONS_2050[1])
    hhmm = str(snap.get("bar_time_server") or "")
    if (snap.get("crosses_today") or 0) >= 5 and hhmm[:2].isdigit() and int(hhmm[:2]) < 11:
        out.append(SKIP_REASONS_2050[2])
    return out


def cites_listed_reason(reason: str) -> bool:
    return any(r in (reason or "") for r in SKIP_REASONS_2050)
