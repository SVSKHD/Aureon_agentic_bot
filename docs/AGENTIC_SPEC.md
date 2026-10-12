# Agentic specification — Aureon MT5 v2.4.0

## Control modes

| Mode | Who places | Who manages | Description |
|---|---|---|---|
| **MANUAL** (default) | Human places via buttons, /take, or directly in MT5 | Guardian manages: protect → secure → ride → close | Human decides every entry; Aureon is a tool |
| **AGENT** (v2.4.2+) | Aureon places on confirmed signals after a delay | Same guardian | Aureon acts autonomously within its rules; human supervises and can intervene |

`control_mode` is per-symbol, stored in `guardian_state.json` under `"control": {SYMBOL: {mode, since, by, until}}`.
Default MANUAL. Loaded at start, saved atomically, restart-safe.

## /override semantics

`/override` is a ONE-SHOT agent action on a missed setup. It does NOT flip the control mode.
It places one order through `execute_entry` (same governor, halt, demo/live gate) and returns to normal.

AGENT mode (v2.4.2+) is a sustained window where Aureon places on every confirmed signal automatically.

## NON-OVERRIDABLE invariants

These are enforced in code, not prompts. No button, command, agent action, or model output can bypass them.

| Invariant | Enforced in | Description |
|---|---|---|
| SL never loosened | `agent.py:_move_sl` | `ALREADY_BETTER` result accepted; SL only tightened |
| Governor rules | `governor.py:Governor.check` → `bot.py:execute_entry` | session_window, news_block, max_trades_day, daily_loss_cap, loss_multiple, one_position_per_direction, halted |
| 23:00–05:30 IST no entry | `governor.py:session_window` | Hardcoded IST hours |
| News blackout | `governor.py:news_block` | From news_blackout.txt / NFP auto |
| HALT | `halt.py:is_halted` → `governor.py:halted` | Persistent flag, survives restart |
| Demo-only unless ALLOW_LIVE | `bot.py:execute_entry` | `broker.is_demo() or cfg.allow_live` |
| Model output within allowed actions | `voice.py:ConfirmationManager` (voice), future `control.py` (AGENT) | Model can choose within action set, never bypass safety |
| Max lots cap | `bot.py:execute_entry` | `min(lots, cfg.max_lots)` always applied |

## Journal actor field (v2.4.0)

Every decision-type event carries:
- `actor`: `"human"` (button press, /take, /override), `"agent"` (v2.4.2+ autonomous entry), `"model"` (future), `"system"` (guardian, detector, watchdog)
- `rationale`: short text describing why the decision was made
- `p_win`: float 0–1 from advisors, or None

Old journals (pre-v2.4.0) load with `actor="human"` for button events and `"system"` for guardian/detector events (inferred from event type).
