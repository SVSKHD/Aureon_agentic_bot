# Aureon MT5 State

## v2.2.0 operational work
- Canonical branch: master. Strategy rules changed: **NO**.
- Do not modify `aureon_mt5/strategies/ema5080/`.
- One process-wide strategy mode; agents run one thread per configured symbol.
- Market polling and deterministic position guardian must not await AI or Discord commands.
- Trading actions remain explicitly human driven unless AUREON_EXECUTION is enabled and validated.
- Existing Claude advisor offers off/review/advisory/manage; Codex is an independent read-only optional advisor.
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
