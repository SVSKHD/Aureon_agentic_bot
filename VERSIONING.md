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
