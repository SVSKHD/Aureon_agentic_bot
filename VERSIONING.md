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
