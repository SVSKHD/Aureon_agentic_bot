# Versioning & branches

**Tags**: `vMAJOR.MINOR.PATCH`
- PATCH — infrastructure, reliability, tests, docs. No behaviour change in signals or guardian. (v1.8.2 → v1.8.3)
- MINOR — guardian behaviour, new tooling, new commands, new mode. Strategy thresholds unchanged. (v1.8 → v1.8.1)
- MAJOR — a change to detection thresholds or exit rules of the ACTIVE strategy, or a new active strategy.
  Requires: the data it was decided on (which days, which trades) written into the CHANGELOG entry.

**Branches**
- `main` — what runs on the demo/live machine. Only tagged versions are deployed from it.
- `dev` — integration; merges to `main` as a tag.
- `exp/<topic>` — strategy experiments (e.g. `exp/p-one-bar`, `exp/floor-12`). Never merged without a week of data in the PR.

**Rules we keep**
- Every tag's CHANGELOG entry ends with `Strategy rules changed: YES/NO` (and what, if YES).
- `logs/`, `out/`, `.env` are never committed.
- A deployed machine runs `git describe --tags` on start; the banner shows it.

**Pins rule** (v2.10.0)
- `tests/test_strategy_pins.py` runs the ema5080 analysis and the live guardian (every feature at its default) over a fixed
  2-day fixture and asserts the exact signals, entries, stops and close reasons. Any change — human or repair agent — that
  moves a pin fails the suite. A deliberate strategy change updates the pins in the same commit, and its CHANGELOG entry says
  `Strategy rules changed: YES` with the evidence.

**Repair-agent boundary** (v2.10.0)
- The repair agent only *proposes*: a patch on a `repair/<n>` git worktree branched from `origin/master`, tested, carded.
  Nothing is applied, committed, pushed or restarted without an admin's `/repair-apply n` in Discord.
- Forbidden to the agent (a patch touching any of these is refused before it is offered): `aureon_mt5/strategies/**`, the
  guardian functions in `aureon_mt5/agent.py` (`_guard*`, `_close`, `_move_sl`), `aureon_mt5/broker.py`, any `journeys.py`,
  `aureon_mt5/leg_guardian.py`, `aureon_mt5/setup.py`, `.env*`, `VERSIONING.md`, and the `VERSION` line. Strategy and
  guardian changes go through `tuning.propose` or a human.
- `AUREON_REPAIR_AGENT=auto` (v2.10.1) may apply a *plumbing* patch by itself only when every guard holds: no forbidden path,
  suite green in the worktree and on the live checkout, flat (no open or unverifiable position), outside the session (unless the
  bot is already unhealthy: heartbeat / gateway / journal integrity), at most `AUREON_REPAIR_AUTO_PER_DAY`. It is verified after
  `AUREON_REPAIR_VERIFY_S` of healthy running; otherwise it is reverted once, the incident goes to `needs_human` and that
  fingerprint is never auto-applied again. Forbidden paths are never applied automatically under any setting.

**Strategy-autonomy rule** (v2.10.1)
- A strategy or guardian knob changes by itself only with evidence: `AUREON_TUNING_AUTO=shadow|apply` replays the proposal over
  `AUREON_TUNING_REPLAY_DAYS` (more points AND fewer losing trades AND the regime sign-off where relevant AND the pins green), then
  shadows it for `AUREON_TUNING_SHADOW_SESSIONS` sessions (every session must agree), then — `apply` only — sets it through the
  feature registry with a STRATEGY CHANGE APPLIED card and `/veto`. At most `AUREON_TUNING_AUTO_PER_WEEK` per week.
- Never automatic under any setting: lot size, loosening `pre_stop` / `SETUP_MAX_DD`, `claude_mode → manage`, `exit_losing`,
  symbols, mode. Only feature-registry knobs can apply automatically; Guardian-dataclass values need a human.
