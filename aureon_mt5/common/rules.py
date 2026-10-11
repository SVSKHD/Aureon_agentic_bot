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
