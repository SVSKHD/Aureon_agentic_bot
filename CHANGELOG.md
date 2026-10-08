# Changelog — Aureon MT5

Format: `vMAJOR.MINOR.PATCH` · one entry per tag · strategy-rule changes are always called out explicitly.

## v1.10.0 — 2026-10-08  (Claude add-on)
- NEW Claude add-on: a second opinion from a local `claude -p --output-format json` call using your Claude Code login
  (no API key, no Anthropic SDK, no HTTP calls from Aureon). Claude never places a trade.
- Modes `AUREON_CLAUDE=off|review|advisory|manage` (default off = zero calls, zero behaviour change). Hooks into ema5080 only.
- Triggers: P / CROSS / RE at bar close (entry model, TAKE/SKIP on the card or a follow-up card) · pullback in an open trade
  (pullback model, HOLD/TIGHTEN/CLOSE, once per pullback, re-armed after a new peak) · 23:00 IST daily review (review model).
- Config: `AUREON_CLAUDE`, `_BIN`, `_ENTRY_MODEL`, `_PULLBACK_MODEL`, `_REVIEW_MODEL`, `_MAX_CALLS` (per IST day), `_TIMEOUT`, `_WORKDIR`.
- Safety: one worker thread, one call at a time, the poll never waits · fail closed on timeout / non-JSON / wrong decision / auth error /
  budget used / STALE · TIGHTEN only via the guardian's `_move_sl`, never loosens, announced after MT5 confirms · CLOSE only in
  `manage`, only in profit, after guardian exits · no account data in snapshots · `ANTHROPIC_API_KEY` stripped + warning card.
- Cards: CLAUDE (signal) · CLAUDE PULLBACK VERDICT · CLAUDE TIGHTENED · CLAUDE CLOSED · CLAUDE REVIEW · CLAUDE BUDGET USED ·
  CLAUDE UNAVAILABLE (health webhook, 5-min backoff). Commands `/claude`, `/claude-test`, `/claude-review [date]`.
- Journal `claude_verdict` (`source=claude`); `/report` Claude section; `reports.claude_rows_for_batch()` for the Saturday batch.
  `leg` events now carry `start_t` (for grading signals).
- 45 new tests (97 in this repository).
- Strategy rules changed: NO.

## v1.9.8 — 2026-10-08
- FIX intermittent 'application did not respond' on slash commands (defer + off-loop work + cached status).
  Case: 8 Oct 15:30 and 15:31 IST, `/status` and `/pull-history` timed out while guardian cards kept posting.
  Causes found: `/status`, `/parallel-status`, `/market`, `/symbol-present` called MT5 (through the shared RLock) and read the
  whole journal on the Discord event loop before answering; `/report` deferred but then ran MT5 history on the loop; the ask-card
  webhook fallback posted synchronously on the loop; handler exceptions were never reported.
- Every command now goes through one wrapper: defer first, work in a thread with a timeout, one followup, errors always answered
  (`⚠️ command failed: …`) + traceback in `logs/aureon.log` + telemetry. Per-command log line: name, ack ms, total ms, cache/live.
- `/status`, `/parallel-status`, `/agents`, `/market`, `/symbol-present` answer from a per-symbol snapshot the agent refreshes every
  poll ("as of N s ago"). `/report` waits at most 2 s for the MT5 lock, else answers from the journal ("MT5 busy — cached data").
- Bot health: event-loop lag and gateway latency in `/status` and `/agents`; `BOT UNRESPONSIVE` / `BOT RESPONSIVE` card when lag
  > 2 s or the gateway is down > 60 s (optional `DISCORD_HEALTH_WEBHOOK`, falls back to `DISCORD_WEBHOOK`).
- 12 new tests (52 in this repository).
- Strategy rules changed: NO.

## v1.8.3 — 2026-10-06
- Discord: dedupe key committed only after successful delivery; failed posts retried; gives up after 20 attempts. Console mode = delivered.
- Version strings aligned. 25 tests.
- Strategy rules changed: NO.

## v1.8.2 — 2026-10-06
- Shared MT5 RLock around every API call; symbol-aware dedupe keys; MT5 → state → journal → Discord ordering; EMA 80 follow-SL via safe path;
  shared helpers moved to `aureon_mt5/common/`; `/parallel-status`; supervisor restarts only dead agents; 7 parallel-safety tests.
- Strategy rules changed: NO.

## v1.8.1 — 2026-10-06
- Mode selector (`--mode ema5080|ema2050`, `AUREON_MODE`); isolated strategy packages; frozen EMA 20/50 included (signals only).
- Guardian completions: pre-abort, opposite-P flip (no auto-open), ALREADY_BETTER results, immediate state after ride, mode in state,
  mode-mismatch warning; nested per-symbol state; IST-day counters from journal; telemetry backoff; `detect.py`/`run.py` with `--mode`; 15 tests.
- Strategy rules changed: NO.

## v1.8 — 2026-10-06
- Persistent MT5 connection; SL truth (state from live SL, never loosen, SECURED only on confirm); cross moves SL to EMA 80;
  per-symbol guardian params; measured server offset; BrokerResult with retcodes; tracebacks; persisted state.

## v1.7 — 2026-10-06
- First Aureon MT5: agents, guardian, Discord bot commands, market calendar, weekly report, banner.
