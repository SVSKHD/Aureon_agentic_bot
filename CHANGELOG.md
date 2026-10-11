# Changelog — Aureon MT5

Format: `vMAJOR.MINOR.PATCH` · one entry per tag · strategy-rule changes are always called out explicitly.

## v2.0.2 — 2026-10-11  (/alert: two verdicts · placement feedback · /pull-history · /git-history · command sync)
- `/alert` card (`alerts.reached_card`) rebuilt as five blocks: PRICE · EMA · TREND · **AGENT VERDICT** (deterministic, the mode's own rules on
  the last closed bar, with the SL the guardian would set / early lock / secure) · **CLAUDE VERDICT** (placeholder `⏳ asking Claude (<model>) …`,
  `— not attached (mode=off)`, `— budget used`, `— unavailable`). The agent block always exists before the Claude call is queued. When the
  add-on answers (`request_alert` → `process`) the same card is edited (message id stored on the alert): `CLAUDE: <side> · TAKE|SKIP ·
  p_win · confidence`, the reason, `evidence: …`, `AGREES with agent ✅` / `DISAGREES with agent ⚠️ (agent X, Claude Y)`; if the edit fails
  (webhook only) a follow-up `ALERT VERDICT · <id>` card repeats both blocks. Optional `p_win` (0–1) accepted in the reply contract.
- `/alert <price>` replies with a PREVIEW card of the same blocks for the current bar (zero Claude calls). The alert snapshot is
  `build_snapshot(event="alert", side=agent side or None)` + alert price/note + the agent verdict; entry model; counts toward the budget;
  skipped silently when the budget is used.
- `alert_decision` journals `agent_verdict, claude_verdict, claude_side, agreed_agent, agreed_claude`; `/report` and the Saturday COMPARE card
  show an ALERTS table (fired · taken · agent✓ · claude✓ · pts taken).
- Placement feedback: ORDER PLACED card after a successful `place_market` (ticket, side, lots, fill, slippage vs the card price, SL as set, spread
  at fill, time) then PROTECTED (also when the SL came with the order); a requote / price-changed rejection (10004 / 10020 / 10021 or the
  comment) is retried once after 2 s with a fresh tick, then ❌ with the reason. `BrokerResult` gains `ticket, price, volume`. The decision is
  edited into the card's fields (`TAKEN SHORT · ticket … · filled …` / `SKIPPED`); with execution off the card shows the exact guardian SL and
  "waiting for your ticket…", then `ATTACHED · ticket …` on the first poll that sees the position (`notify.edit_card` → bot card-edit hook).
- Start-up command sync for the configured guild (`DISCORD_GUILD`): `copy_global_to` + `sync(guild=…)`, stale commands not defined in bot.py
  dropped, final list logged; `/discord-bot sync` (admin) forces it at runtime.
- `/pull-history [days=7] [symbol]`: MT5 closed deals of the window (shared RLock, 2 s wait else "MT5 busy — try again"), matched to journal
  `position_seen` / `closed` rows by ticket, missing `final_points` filled as journal `final_points` rows (read by the alerts stats), one
  `history_pull` row; card with deals found / matched / newly graded, net points, per-day lines, unmatched tickets; `reports.graded_signals()` line.
- `/git-history [n=10] [tags]`: running build (`git describe`, HEAD, up to date / behind by N vs origin/master after a 5 s fetch, "fetch failed —
  showing local"), merges on master newest first in IST and the commit's own offset with `#N` linked to the PR; `tags=true` lists the last 10
  tags with dates; "not a git checkout" when git is missing — never raises. The start-up banner adds the merge date of HEAD.
- 15 new tests (211 in this repository).
- Strategy rules changed: NO.

## v2.0.1 — 2026-10-11  (thread stop fix · /claude attachment card)
- FIX `SymbolAgent` set `self._stop = threading.Event()`, shadowing `threading.Thread._stop()`: `join()` raised `TypeError: 'Event' object is
  not callable` (`tests/test_claude_advisor.py::test_off_means_zero_calls`; the supervisor restart / shutdown path live). Renamed to
  `_stop_evt` everywhere. New `tests/test_v201_thread.py`: start → stop → join(timeout=2) returns cleanly in dry mode.
- `/claude` now answers "is Claude Code attached to this agent?": header (mode · resolved bin path · `claude --version`, cached at start and
  refreshed by `/claude-test` · login state ok / auth error / not found — red with what to run when the binary is missing or the login failed) ·
  models (configured ids and the id the CLI reported in its last JSON result) · budget (calls used / max, entry / pullback / alert calls,
  fast-exit cancellations = pullback verdicts that arrived after the trade had closed, last latency, last error) · one line per running agent:
  `XAUUSD · EMA 20/50 · ATTACHED (advisory) · triggers: ENTER bar, pullback-in-trade · last verdict 11:40 IST TAKE`, or
  `NOT ATTACHED (advisor.advises()=False)`, `DETACHED (mode=off)`, `DETACHED (/claude-detach)`, `REVIEW (cards only, nothing applied)`.
- `/claude-attach <symbol>` / `/claude-detach <symbol>`: toggle `agent.claude` at runtime without a restart (detach sets it to None and cancels
  the symbol's queued jobs and inbox; attach re-binds only when advises() and the mode allow). Journal `claude_attach` / `claude_detach`.
  `/status` gets a `Claude: ATTACHED (mode) | REVIEW | OFF | NOT ATTACHED | DETACHED` field.
- `claude_cli.version()` and `CliResult.model` (model id reported by the CLI). 8 new tests (196 in this repository).
- Strategy rules changed: NO.

## v2.0.0 — 2026-10-10  (ema2050 un-frozen: a NEW ACTIVE strategy · /alert price alerts · Claude add-on for both modes)
- **ema2050 is ACTIVE** with its own rules and live guardian profile; `ema5080` stays the default and is untouched (thresholds, guardian
  values and all its tests unchanged; a test pins its Guardian dataclass byte-identical).
- Detection (`strategies/ema2050/journeys.py`, GOLD): EMA 20/50 on M5 · cross = sign change on a closed bar · **confirm** = order holds ≥ 3 bars
  AND |gap| ≥ 1.5 pts within 18 bars (first bar with both); a flip before → **multi cross**, no trade · **pullback entry**: touch of EMA20 within
  1.5 (touch between cross and confirm → enter at the confirm bar; else first touch after confirm up to confirm+12; fallback at confirm+12 only if
  |close − EMA20| ≤ 5 and the order holds; never chase beyond 5) · no entries server 21:00–23:59 / outside 05:30–23:00 IST (stricter wins) ·
  news 60/30 via `common/news.py` as ema5080 · pre-cross shoot (40 bars) reported only · reentry_max=0, skip_sessions=(), m15_align=False,
  min_slope50=0, max_extension=0, min_shoot=0. Removed: late_entry, target/protect/trail_arm/let_run, leg_mode, pyramid, pre_entry/PB/PS.
- Guardian profile (`GUARDIANS["XAU"]`, XAG disabled): pre_stop **12** · **early lock** +3 seen → SL entry +1 (new `Guardian.early_at/early_level`,
  defaults None/0 keep ema5080 unchanged) · secure +10 → SL +10 (announced after MT5 confirms) · ride +5 (2 pts air) · ema_slow_sl_buffer 0 and
  `slow_ema_sl=False` (**no EMA50 follow-SL, no close through EMA50**) · close on the EMA20 turn once secured · exit on the next **confirmed**
  opposite cross (`exit_on_confirmed_cross`) · `p_phase=False`: P timeout / pre-stop / separation-abort paths skipped · SL never loosened ·
  live SL is the truth · `respect_manual_sl` as configured.
- Cards: CROSS (unconfirmed) · CROSS CONFIRMED · MULTI CROSS (ignored) · ENTER LONG/SHORT (pullback | no-pullback, TAKE/SKIP) · NO ENTRY (too far
  from EMA20 | no-entry hour | news) · EARLY LOCK · OPPOSITE CROSS · CLOSED; each with cross time, confirm time, shoot, session, EMA20/50, bars since cross.
- `run.py`: `--from/--to` day-by-day replay (entries inside the server day, open trades closed at the day end), `--lot`, `--point-value`, `--trades`,
  and the research summary block (profit pts and $, moves offered, available at entry, captured %, trades/winners/losers/stops, goal days ≥ 10,
  losing days, best/worst day, max drawdown, month table). `detect.py --mode ema2050`: per bar EMAs, cross, confirm state, pullback touch,
  entry allowed, reason (`common/verdict.py`, shared with the alert card).
- Data it was decided on: XAUUSD M5 2026-07-01..2026-10-09 (guardian rules: +475.7 pts, 211 trades, 39 losers, 34 stops, max DD −35.0;
  Jul +222.5 / Aug +33.5 / Sep +134.8 / Oct +85.0; 2026-10-07: 03:30 short +10 secured, 19:20 long +13, 22:50 short −6.2 day end) and the
  2025 out-of-sample set 2025-06..2025-12 (guardian rules **+297.8 pts / 143 days**; the trend-engine variant **−33 pts, rejected**; trend / hold /
  filter variants failed 2025 and were not added). Research scripts (`research/ema2050_scan.py`, `research/harness.py`) are not in this
  repository, so the replay could not be diffed against the scan in this change; run `python run.py --mode ema2050 --from 2026-07-01 --to 2026-10-09`
  on the MT5 machine to confirm (±2 pts, same trade count). Known convention choices to check first if it differs: SL levels armed by bar k apply
  from bar k+1 (as the 50/80 replay); news_flat (15 min, ≥ +1) is replayed because the live guardian does it; crosses before the server day are
  context only.
- **/alert price alerts** (both modes): `/alert <price> [symbol] [note]`, `/alerts`, `/alert-cancel <id>`, `/alert-clear`; `logs/alerts.json`;
  fires once on the guardian's tick (ask from below, bid from above), never on a stale tick; ALERT REACHED card with EMA behaviour, trend
  behaviour (ema5080/trend.py state machine on the mode's lines), guardian context and the SUGGEST line = the mode's own entry verdict for the
  bar (never a new rule); [LONG] [SHORT] [SKIP] buttons → `alert_decision` journal; order placed only with `AUREON_EXECUTION=1` on demo (or
  `AUREON_ALLOW_LIVE=1`) through the new `broker.place_market`, otherwise the guardian is armed for the trade you place (alert id attached);
  Alerts section in `/report` and the Saturday card.
- Claude add-on: advises both modes (ema2050: ENTER bar + pullbacks only); snapshot fields `crosses_today, bars_since_cross, confirm_state,
  dist_to_fast_ema_pts, swing_against_last6_pts, atr20, day_pnl_pts, trades_today` + position `mae_so_far, retrace_from_peak_pts, retrace_pct,
  bars_since_peak, closed_through_slow_ema, dist_to_sl` (no account data); `"evidence": [...]` in the reply contract, journaled with the compact
  snapshot fields; mode-aware prompt (`prompts/claude_rules.md` rewritten with one section per mode — placeholder until
  `research/claude_rules_v2.md` is supplied); Saturday CLAUDE RULE PROPOSALS card (up to 3 lines with support) + `/claude-rules`,
  `/claude-rules-approve <n>` (appends a dated line; nothing changes without approval). Alert add-on line in review/advisory/manage.
- Broker: `tick`, `account`/`is_demo`, `place_market`, plus the helpers the agent already referenced (`spread`, `lot_for_risk`, `bars_tf`, `close_partial`).
- 53 new tests (188 in this repository).
- Strategy rules changed: YES (ema2050 un-frozen: confirm 3 bars/1.5 gap, pullback entry, guardian pre_stop 12, early lock +3->+1, no EMA50 follow-SL).
  /alert and the Claude add-on changes: strategy rules changed: NO (they read rules, never add one).

## v1.11.0 — 2026-10-08  (weekly compare: Detector vs Claude vs Me)
- NEW `compare.py`: on the same detector signals (P / CROSS / RE, ema5080) — Detector · all, Claude TAKE / SKIP, Me TAKEN / SKIPPED,
  both disagreement groups; n, win rate, expectancy, total, STOP count, worst losing streak, biggest loss; OPEN / UNGRADED /
  no-verdict counted, never scored. Verdict line (≥30 signals), your filtering line, disagreements line.
- Pullbacks: Claude HOLD / TIGHTEN / CLOSE vs the guardian's actual result (TIGHTEN checked on M5 bars); applied verdicts separate.
- Grading reused from the scorecard through `reports.graded_signals()` (a field-renaming wrapper; no second grading method).
  If the scorecard is missing or its fields are not recognised, the card says so.
- `/compare [days] [private]`, `/compare-breakdown [days]` (v1.9.8 wrapper). Saturday 10:00 IST COMPARE card for Mon–Fri, once per
  week (restart-safe), with a cumulative-points chart, 4-week expectancy trend, models and `claude_rules.md` hash.
- `logs/compare_weekly.jsonl`; `compare.latest_summary()` gives the `compare` field for the Saturday `aureon_weekly` batch row.
- Journal additions (measurement only): `position_seen` (first sight of a trade) and `final_points` on `closed`.
- Optional `AUREON_COMPARE_CLAUDE_REVIEW` (default 0): Claude's 5-line self-review of its wrong calls; never edits the rules.
- 38 new tests (135 in this repository).
- Strategy rules changed: NO.

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
