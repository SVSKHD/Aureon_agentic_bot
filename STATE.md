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
