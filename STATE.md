# Aureon MT5 State

## v2.18.1 — ops
- `/update force [notests]` never closes a position; the guardian re-adopts on boot. `/update status` for the updater's state.
- `/repair` is diagnosis + a fixed allowlist of named actions (no shell, no orders, never `.env` / `logs/` /
  `guardian_state.json`, 3 runs an hour). Claude is consulted only through the CLI login — keep it that way.
- `/attach` / `/detach` change the live symbol set; only `AUREON_SYMBOLS=` in `.env` is ever written by the bot.

## v2.18.0 — ema921 multi-symbol pullback (M15)
- Strategy rules changed: **YES, ema921 only** (ema5080 / ema2050 / ema9200 untouched). Six symbols: XAUUSD (first — it drives
  market open and the server offset), XAGUSD, EURUSD, GBPUSD, AUDUSD, USDJPY.
- Rules per symbol in POINTS (`strategies/ema921/journeys.py` SYMBOLS; `to_price` is the only conversion). A symbol without a row
  raises and is never started.
- Entry `ema9touch`: cross arms → first later EMA 9 touch within 16 bars → TRADE card. Detect-and-alert: `AUREON_EXECUTION=0`,
  a human places the trade. Exits bracket (SL / TP / lock / Friday flat).
- Claude only on `AUREON_CLAUDE_SYMBOLS` (XAUUSD, XAGUSD). A gateway-only Discord outage no longer restarts the process for
  `AUREON_GATEWAY_ONLY_RESTART_S` (30 min) while MT5 is up.
- Before live alerts: `python main.py --dry --mode ema921 --symbols <six>` for 30 min (six RUNNING lines in `/parallel-status`),
  `tools/replay_921.py --symbol XAUUSD` on the Oct 2026 M15 CSV, then one week of `AUREON_PAPER=1`.

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
