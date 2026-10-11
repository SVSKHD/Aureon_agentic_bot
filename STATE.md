# Aureon MT5 State

## v2.2.0 operational work
- Canonical branch: master. Strategy rules changed: **NO**.
- Do not modify `aureon_mt5/strategies/ema5080/`.
- One process-wide strategy mode; agents run one thread per configured symbol.
- Market polling and deterministic position guardian must not await AI or Discord commands.
- Trading actions remain explicitly human driven unless AUREON_EXECUTION is enabled and validated.
- Existing Claude advisor offers off/review/advisory/manage; Codex is an independent read-only optional advisor.

## v2.3.0 advisors
- Claude and Codex share one code path (`CodexAdvisor` subclasses `ClaudeAdvisor`), one rules prompt (`prompts/advisor_rules.md`,
  alias `claude_rules.md`) and one reply contract. Both are CLI-login only; never add an API-key path.
- Codex modes: off · review · advisory. Codex verdicts are journaled and shown only — never applied (keep `CodexAdvisor.apply_pullback`
  free of `_move_sl` / `_close`; tests enforce it). Only Claude in `manage` may TIGHTEN / CLOSE-in-profit.
- Each advisor: own worker, budget, usage ledger (`logs/<source>_usage.jsonl`), journal rows (`<source>_verdict`), rule proposals file.
- Approved rule lines are appended to the tracked `prompts/advisor_rules.md`; with the v2.2.2 server-authoritative auto-update an
  approval made on the server is saved to a patch and discarded at the next update — commit approved lines to master.
- Operator functions require strict permission checks, position guards, and controlled process restart.
- Voice commands remain NOT IMPLEMENTED; see docs/VOICE_SCOPE.md.

## Unresolved validation
Full Windows/MT5 integration, restart behavior, authenticated CLI usage accounting and new Discord slash-command registration require a staging run before deployment.

## Operator release checks
- Configure `AUREON_ADMIN_ROLE_ID` as an environment variable on the machine hosting the Discord bot.
- Run `pytest tests/test_v220_ops.py` and the full regression suite, then verify slash-command sync.
- Test `/mode` and `/restart` only without open exposure, on a staging/demo MT5 instance.
- Confirm CLI usage ledger writes and weekend/month-end reports do not stall the 10-second market poll.
- Check commit hashes and crash-recovery notifications from a staged intentional exception.
