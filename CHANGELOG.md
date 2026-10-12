# Changelog — Aureon MT5

Format: `vMAJOR.MINOR.PATCH` · one entry per tag · strategy-rule changes are always called out explicitly.

## v2.3.14 — 2026-10-12  (PATCH: spread + slippage in replay and reports)
- **Live signals**: every `signal` journal event records `spread_pts` from the live tick at the signal bar.
- **Live orders**: `order_placed` journal event gains `slippage_pts` (fill − card price in trade direction; positive = worse),
  `spread_pts` (at fill), and `card_price`.
- **Replay** (`run.py`): `--spread PTS` (default 0.3, env `AUREON_REPLAY_SPREAD`). The research summary shows
  `gross +X · cost Y (assumed 0.3) · **net +Z**`. Each simulated trade's result is gross; net = gross − spread per trade.
- **Compare card**: new "Costs (taken trades)" field shows `gross · cost (spread + slippage) · net` for graded taken trades.
  Live rows use real costs from journal; replay rows use the assumption.
- **Compare Row**: `spread_pts` and `slippage_pts` fields (from signal and order_placed journal events).
- Old journals without cost fields load with None values (no error).
- Strategy rules changed: NO.

## v2.3.13 — 2026-10-12  (PATCH: /halt kill switch)
- **`/halt [reason]`**: sets persistent HALT flag in `logs/halt.json`; closes every open position on every symbol via
  `broker.close` (retried 3x); summary card with per-ticket retcodes. Dry mode / execution off: reports "would close" without
  touching MT5.
- **While HALT is on**: governor rule `halted` blocks all entries (buttons, `/take`, `/override`, future AGENT). Detectors still
  run. The guardian keeps managing any position that appears — never loosens SL. HALT survives restart.
- **`/unhalt confirm:UNHALT`**: requires the exact phrase. Journal `halt`/`unhalt` with user + reason.
- **`/status`**: first line shows `⛔ HALTED since 10:42 IST (by …: reason)` while on.
- Strategy rules changed: NO.

## v2.3.12 — 2026-10-12  (PATCH: enforced risk governor)
- **`aureon_mt5/governor.py`** (pure): `Governor(cfg).check(symbol, side, lots, context) -> Decision(allowed, rule, detail)`.
  Rules (each env-configurable, each with a stable name):
  `session_window` (23:00–05:30 IST), `news_block`, `max_trades_day` (AUREON_MAX_TRADES_DAY=4),
  `daily_loss_cap` (AUREON_DAILY_LOSS_CAP_PCT=2.0% of equity), `loss_multiple` (2x per-trade risk, AUREON_LOSS_MULTIPLE=2.0),
  `one_position_per_direction`. Counts from broker.closed_deals for the current server day.
- **Enforced in `execute_entry`**: every button, `/take`, `/override` passes through `Governor.check()` BEFORE `broker.place_market`.
  Blocked → no order, no attach_alert; returns `⛔ BLOCKED · <rule> · <detail>`, journals `governor_block`.
  No override flag — the governor is not bypassable.
- **`/status`**: shows `governor: 1/4 trades · loss 0.6%/2.0% · OK`.
- **`/governor`**: lists every rule with its current value and limit.
- **`_daily_limit`** in agent.py replaced with the governor's state (single source of truth).
- Strategy rules changed: NO (entry gating only).

## v2.3.11 — 2026-10-12  (PATCH: lot buttons — finish alert cards)
- ALREADY DONE: AskView lot buttons (v2.3.7), lot_button_spec, execute_entry lots param, /take lots, ORDER PLACED Size field,
  compare sizing groups.
- **TODO → DONE** alerts.py `reached_card` meta: added `lots` (from `_risk_lots`), `half_size`, `chop`, `chop_crosses` +
  `_lot_field` as the card's Size field.
- **TODO → DONE** `lot_button_spec`: `meta["lots"]` None → NO ★ (no max_lots fallback). `execute_entry`/`ask_decision`/`alert_decision`:
  `lots_suggested` = `meta["lots"]` or None (journals None); compare excludes None from `sized_up`/`sized_down` (already did).
- **TODO → DONE** AlertView with execution on: always both rows (LONG …, SHORT …) + SKIP row; `suggested_side`'s row is first and
  only that row gets ★.
- **TODO → DONE** AlertView._answer: colour GREEN if `r["ok"]`, RED if not ok, GREY for SKIP; ephemeral followup when card edit fails.
- **TODO → DONE** Decision line: `TAKEN LONG 1.0 lots (suggested 1.7★) · ticket … · filled …` (omitted when lots_suggested is None).
- **TODO → DONE** execute_entry rejection / not-placed lines include the size: `place 1.0 lots in MT5, I will manage it`.
- **PARTIAL** Volume validation at start: not implemented (requires live MT5 `symbol_info` which tests can't call).
- Strategy rules changed: NO.

## v2.3.10 — 2026-10-12  (PATCH: Discord watchdog on by default)
- **Watchdog** (`aureon_mt5/watchdog.py`): `Watchdog.tick()` called every ~30 s from the main/supervisor thread. Per symbol:
  `STALE:<SYMBOL>` (market open, no new M5 bar > `heartbeat_stale_min` min), `dead:<SYMBOL>` (agent thread not alive).
  Global: `mt5` (broker disconnected). When MT5 is down, per-symbol STALE cards are suppressed (root cause). STALE never fires
  while market is closed. Hysteresis: open after 2 consecutive failing checks, clear after 2 passing. ONE red card on open, no
  repeats, ONE green RECOVERED card on clear with duration. Journal `watchdog_open`/`watchdog_clear`.
- **Persistence** (`logs/watchdog.json`): open incidents survive restart — no re-post of the open card, RECOVERED still posted;
  incidents already clear at startup get one RECOVERED "cleared while Aureon was down".
- **Heartbeat**: one grey `💓 Aureon alive` card per `AUREON_HEARTBEAT_HOURS` (default 4) slot aligned to IST (00, 04, 08…).
  Dedupe key so restarts don't double-post. Skipped on a quiet weekend (market closed, no incidents).
- **Config**: `watchdog_ping` renamed to `supabase_ping` (env `AUREON_SUPABASE_PING`, had zero readers).
  `AUREON_WATCHDOG=1` (default on), `AUREON_HEARTBEAT_HOURS=4`.
- `/watchdog` command lists open incidents; `watchdog_status()` helper for /status.
- Strategy rules changed: NO.

## v2.3.9 — 2026-10-12  (MINOR: missed setup detection + /override)
- **Missed setup detection** (`agent.py`): `track_ask` records every signal ask card (ENTER / P / CROSS / RE). Every poll,
  `_check_misses` fires after the grace window (`AUREON_MISSED_GRACE_MIN`, default 5 min) if no position appeared in that direction.
  Not missed when: SKIP answered, order placed, position already existed, ask expired, market closed / news / 23:00–05:30 IST.
  A TAKE press that armed the guardian but no ticket appeared IS missed ("TAKE pressed, no ticket"). ONE `SETUP MISSED · LONG` card
  per ask with drift, suggested lots, SL, minutes since signal. Journal `setup_missed`. Open asks persisted in `logs/pending.json`.
- **`/override [symbol] [lots]`**: places the order at the current price via `execute_entry` within the override window
  (`AUREON_OVERRIDE_WINDOW_MIN`, default 10 min) AND drift against trade <= `AUREON_OVERRIDE_MAX_DRIFT` (default 3.0 pts) AND
  price above SL AND no blackout. Same demo/live gate. Journal `override` with `lots_source="override"`.
  `/ignore-miss` journals `override_ignored` and removes the miss.
- **Compare** (`compare.py`): `Row.missed`/`Row.overridden`; `groups()` adds `missed`/`overridden`/`missed_not_overridden`;
  card shows "Missed: N · overridden K (win%/pts) · let go M (win%/pts)" when any.
- Strategy rules changed: NO.

## v2.3.8 — 2026-10-12  (PATCH: chop-day window fix + restart duplicate)
- **FIX chop-day hour window**: MT5 bar `time` is already the broker server clock encoded as a UTC epoch (`server_str` uses `tz=utc`).
  The old `chop.py` applied `off` on top, shifting the 00:00–11:00 server window to ≈21:30–08:30 (live) or 21:00–08:00 (replay),
  missing 09:00–10:59 server crosses and counting previous-evening ones. Fixed: `server_day_start(bar_t) = bar_t - bar_t % 86400`;
  `count_morning_crosses` is now a pure numpy-vectorised function of the frame with no `datetime.now()` and `off` is ignored (kept
  for signature compat). The old tests couldn't catch it because `_choppy_frame` imported `_server_day_start` from the module under test.
  New tests use independent `_server_epoch` / `_frame_from_flips` helpers and assert correct behaviour for crosses at 09:00–10:40,
  previous-evening crosses, the 11:00 boundary, and day-label roll at midnight.
- **FIX repeat CHOP DAY card after /restart**: `Notifier.sent` (dedupe set) is in memory, so a restart on a chop day re-posted the
  card and re-journaled `chop_day`. Added `_chop_day_journaled(day)` that checks the journal for an existing `chop_day` event for this
  symbol and day (last 2 days). When chop is newly true but the journal already has the event, the card and journal line are suppressed;
  the flag itself (`self.chop`, `health["chop"]`) is still rebuilt from bars. New test: two agents on the same journal → exactly one
  CHOP DAY card and one journal line; second agent still has `chop=True`.
- Strategy rules changed: NO.

## v2.3.7 — 2026-10-12  (PATCH: lot-size buttons on entry cards)
- **Lot-size buttons** when `AUREON_EXECUTION=1`: Confirm/ENTER cards (AskView) show `[0.5] [1.0] [1.5] [SKIP]`; ALERT cards
  (AlertView) show per-side rows `LONG 0.5/1.0/1.5` and/or `SHORT 0.5/1.0/1.5` (one side row when `suggested_side` is set).
  `AUREON_LOT_BUTTONS=0.5,1.0,1.5` (up to 5 values, > 0, sorted, dedupe). The button nearest `meta["lots"]` (the risk-based
  suggested size, already halved by streak/chop) is marked with `★`. Buttons above `max_lots` render disabled with label
  `1.5 (>max)`. When execution is off, today's TAKE/LONG/SHORT/SKIP buttons are unchanged.
- **`execute_entry(…, lots=None)`**: when lots is given, uses `min(lots, max_lots)` instead of suggested. Journals `order_placed`
  with `lots_chosen`, `lots_suggested`, `lots_source` ("button"/"card"), `half_size`. ORDER PLACED card has a `Size` field.
  Return dict gains `"lots"`. `ask_decision` and `alert_decision` pass `lots` through and journal it in `decision`/`alert_decision`.
- **`/take lots=<float>`**: optional lots argument.
- **`/status`**: shows `exec: on · buttons 0.5/1.0/1.5 · max 2.0 · demo` or `exec: off`.
- **Compare**: `Row.lots_chosen`/`lots_suggested`; `groups()` adds `sized_up`/`sized_down`; card shows "Sizing: up N · down N ·
  as suggested N · avg chosen/suggested ratio" when any row has sizing data.
- `lot_button_spec(cfg, meta)`: pure testable helper returning button descriptors.
- Strategy rules changed: NO.

## v2.3.6 — 2026-10-12  (PATCH: restart-safe guardian resume)
- **Immediate state save** after every critical guardian mutation (early lock, cross confirmed, secure, ride step) — not just at
  poll end. A crash mid-poll no longer loses the last step.
- **Journal reconstruction fallback**: when a live position's ticket is NOT in `guardian_state.json`, rebuild state from journal
  events (`position_seen`, `secured`, `early_lock`, etc.): `secured` = max over journaled levels, `peak` = max, `pre` = False
  if any post-cross event, `alert_id` from `position_seen`. Then apply "live SL is the truth". Journal `state_rebuilt` with
  source `journal` or `none` (no events found → fresh PROTECT).
- **RESUMED card**: on (re)start, when positions are present, one card per symbol lists each ticket: direction, entry, current pts,
  phase (PROTECT / EARLY / SECURED / RIDING), SL, and the source (state file / journal / none). Includes armed alerts count.
- **Pending TAKE survives restart**: `attach_alert` writes to `logs/pending.json` (atomic); loaded in `SymbolAgent.__init__`;
  cleared when consumed or expired. A position appearing after restart within the TTL gets the alert_id and ATTACHED card.
- **Alert gap detection**: `AlertStore.check()` stores `last_tick` per symbol (throttled: on change > 0.5 pt). If the price
  crossed the level while Aureon was down (last known tick on one side, current on the other), fires with `gap_note`.
- **`/restart`** lists what will be resumed (open tickets with phase, armed alerts, pending TAKEs) before restarting. `/restart`
  is allowed with open positions since resume is now safe.
- Strategy rules changed: NO.

## v2.3.5 — 2026-10-12  (PATCH: deterministic chop-day flag)
- **Chop-day detection** (`common/chop.py`, pure function): `count_morning_crosses(df, fast_col, slow_col, off)` counts EMA fast/slow
  sign-change crosses on the current server day before 11:00 server (same cross-counting as `claude_advisor.build_snapshot`).
  `chop_state(…, threshold)` returns `{"chop", "crosses", "day", "threshold"}`. Works in dry mode and in replay.
- **Agent chop flag** (`agent.py`): `self.chop` recomputed once per closed bar; sticky per server day (once CHOP, stays for the day).
  `health["chop"]` set. When chop first becomes True: one CHOP DAY card (dedupe per day), journal `chop_day` event.
- **ENTER cards** (both ema2050 and ema5080): on a chop day, a `⚠️ Chop day` field is added (N crosses before 11:00 — half size
  suggested); `_risk_lots()` halves the risk (same mechanism as streak half, combined: halve once, not twice); the Size line appends
  `**half size** (chop day)` or `(chop day · N losses in a row)` when both bind. Ask meta includes `chop` and `chop_crosses`.
- **Signal journal**: `chop` and `chop_crosses` added to every `signal` event.
- **`/status`**: per-symbol line shows `🌀 CHOP (N crosses <11:00)` when active.
- **`/agents`**: per-agent line appends `· CHOP`.  `/parallel-status`: same token.
- **Compare** (`compare.py`): `Row.chop` field (default False, read from journal). `groups()` adds `chop`, `clear`, `chop_me_taken`,
  `clear_me_taken`. `/compare` card and Saturday COMPARE card show "Chop-flagged entries: N (taken K) · win% / avg pts" vs "Clear days"
  when any row is flagged.
- `AUREON_CHOP_CROSSES=5` (default); `config.py` `chop_crosses`.
- Strategy rules changed: NO (this is a size/warning overlay — entries are not blocked).

## v2.3.4 — 2026-10-12  (PATCH: Confirm card TAKE places orders + streak half size)
- **Confirm card [TAKE] button and `/take` now execute through the same path as the alert [LONG]/[SHORT] buttons**: `execute_entry`
  (extracted from `alert_decision`) does tick → SL from `g.pre_stop` → `place_market` when `AUREON_EXECUTION=1` on demo (or
  `AUREON_ALLOW_LIVE=1`), else arms the guardian and says why it did not place. `ask_decision` journals the `decision` event and
  delegates to `execute_entry`. `AskView._answer` defers, calls `ask_decision` in a thread, and edits the Decision field with the
  result (green on success, red on reject); if the edit fails, a followup is sent ephemeral. `/take` does the same via `ask_decision`
  + `notify.edit_card`. `alert_decision` also delegates to `execute_entry` (no duplicated placement code). All return dicts now
  include `"placed": True|False`.
- `attach_alert(alert_id, side, card_key=None)`: stores `card_key` in `pending_alert`. In `_guard`, when a first-seen position
  consumes `pending_alert`, the ATTACHED edit goes to `card_key` (the Confirm card) or falls back to `alerts_mod.key_for(…)`.
- **Streak half size**: `AUREON_STREAK_HALF_AFTER=0` (default off). When the count of consecutive losing closed deals >= threshold,
  `_risk_lots` uses `risk_pct / 2` for lot sizing. `_streak_n()` (cached per bar) counts the streak; `_half_size()` tests threshold.
  `_suggested` and `_lot_field` use `_risk_lots`; the Size line appends ` · **half size** (N losses in a row)` when active.
  The streak card says "half size applied to the next one" when the threshold is met. `AUREON_MAX_LOTS` still caps.
- Strategy rules changed: NO.

## v2.3.3 — 2026-10-11  (PATCH: Claude rules wording + shadow report fixes)
- `prompts/advisor_rules.md` (alias `claude_rules.md`), ema2050 section: the four operator paragraphs verbatim (detector already validated
  confirmation and the EMA20 touch · no ATR rule, swing matters only >= 8 · crosses_today >= 5 only before 11:00 server · default TAKE,
  cite a listed reason, p_win on every verdict). Listed reason 3 is now "crosses_today >= 5 before 11:00 server" (no session condition).
  Removed every other "1.5" and the gap / 3-bar confirmation detail. Reply contract asks for `p_win` on every verdict.
  **Not done: the base text of research/claude_rules_v2.md — it was not in the chat or the repo.**
- Snapshot `confirm_state` is words only (`confirmed` / `confirmed N bars ago` / `not confirmed` / `no cross`): the detector's own
  strings carried its thresholds ("gap 1.20 < 1.5").
- Shadow report (`run.py --claude/--codex`): win% counts result > 0 (was the scorecard's +10-and-better grades); calibration from
  p_win only, with the count of verdicts that had none; `rules hash changed -> N bars will be re-asked` printed first when the cache
  holds bars under another rules hash.
- `tests/test_claude_rules_replay.py`: the July snapshots through the current rules, answered live once (AUREON_PROMPT_TEST=1) and
  cached per rules hash in `tests/fixtures/claude_rules_replay_jul2026.jsonl`; asserts SKIP rate <= 35%, no reason containing "1.5"
  or "atr", every SKIP citing a listed reason, p_win present. A rules change without a refreshed cache fails. Needs the snapshot fixture.
- Strategy rules changed: NO.

## v2.3.2 — 2026-10-11  (PATCH: ema2050 advisor rules corrected)
- `prompts/advisor_rules.md`, ema2050 section (both advisors): the detector has already validated confirmation, the EMA20 touch
  (its 1.5-pt tolerance), the hour and news — the advisor does not re-test them. Default TAKE; SKIP only for a listed reason cited
  verbatim: `|dist_to_fast_ema_pts| > 5` · `swing_against_last6_pts >= 8` (no ATR rule) · `crosses_today >= 5 in Asia/London-early`
  (session asia, or london before 11:00 server). Base rate stated: ~80% of entries close >= 0; SKIP rate > 30% = misreading.
  Old "Good ENTER" / "Be sceptical" criteria (atr20, gap growing, trend_state) removed. "When unsure" is TAKE for ema2050
  (ema5080 unchanged: SKIP). Rules hash changes → shadow-replay caches re-ask once.
- Snapshot adds `bar_time_server` (HH:MM) for the London-early test.
- Shadow-replay cache rows now store the snapshot sent; `python -m aureon_mt5.claude_replay export <cache> <out> [YYYY-MM]` writes them
  as a fixture.
- `common/rules.py`: `SKIP_REASONS_2050`, `ema2050_skip_reasons()`, `cites_listed_reason()` (tests / measurement only).
- `tests/test_v232_prompt_rules.py`: rules-file content, reason boundaries, snapshot field, cache export (always run); July snapshots
  SKIP rate <= 35% by the listed reasons (needs `tests/fixtures/ema2050_jul2026_snapshots.jsonl`) and through the live prompt with
  every SKIP citing a listed reason (also `AUREON_PROMPT_TEST=1`, calls `claude -p`).
- Strategy rules changed: NO (advisor prompt only).

## v2.3.1 — 2026-10-11  (PATCH: ema2050 port = research/ema2050_scan.py)
- **Strategy rules changed: YES — ema2050 entry only** (the port deviated from the research; July 2026 replay 51 trades +113 vs research
  63 trades +222.5). ema5080: UNTOUCHED. Guardian / exits: unchanged.
  1. Pullback touch window starts the bar AFTER the cross: a touch on any bar in (cross, confirm] → enter at the confirm bar close
     ("pullback"); else the first touch in (confirm, confirm+12]; else the fallback at confirm+12 only if |close − EMA20| ≤ 5 and the order holds.
  2. No IST entry window (`entry_hours_ist=None`); the only time filter is server 21:00–23:59 (`no_entry_server_hours=(21, 24)`).
     News block kept. Setting `entry_hours_ist` to a tuple still re-enables a window.
  3. `max_chase=5` applies to the fallback entry only, never to a touch entry.
- `common.source.fetch_csv` (the one CSV reader; the ema2050 copy delegates to it): MT5 exports with `tick_volume` + `real_volume` (+ `spread`)
  → volume = tick_volume, real_volume / spread dropped (before: two `volume` columns). Text timestamps parsed resolution-independently
  (pandas 3 gives datetime64[us]; the old `// 10**9` divided every bar time by 1000) and MT5 `2026.07.01 00:05` dates are accepted.
- Live agent and run.py share `journeys.build` (no second implementation; a test checks `agent.py` holds none of the rule parameters).
  `run.replay_range()` is the `--from/--to` loop, shared with the tests. Card texts no longer say "never more than 5" for touches.
- `prompts/advisor_rules.md` ema2050 section updated to the same rules (its hash changes: shadow-replay caches re-ask once).
- `tests/test_v231_parity.py`: 13 rule tests; replay-vs-research entry-time parity and the July acceptance (63 trades, +222.5, 8 stops)
  run once `research/ema2050_scan.py` and `tests/fixtures/xauusd_m5_jul2026.csv` are committed (skipped until then).

## v2.3.0 — 2026-10-11  (MINOR: Codex parity with Claude)
- `codex_advisor.CodexAdvisor` subclasses `ClaudeAdvisor` (now parameterised by SOURCE / ATTR / budget / ledger / CLI call), so triggers,
  `build_snapshot` (no account data), validation, fail-closed, STALE and fast-exit are the same code. Own worker thread, own budget
  (`AUREON_CODEX_MAX_CALLS`, `logs/codex_budget.json`), own ledger `logs/codex_usage.jsonl`, journal `codex_verdict` / `codex_event`.
  `AUREON_CODEX`: off · review · advisory (anything else → off). In review and advisory a Codex verdict is **journaled and shown only** —
  `CodexAdvisor.apply_pullback` has no path to `_move_sl` / `_close` (property test over every verdict kind, price, staleness, closed trade;
  AST test on the module).
- `common/codex_cli.call`: per-event schema (pullback → HOLD / TIGHTEN / CLOSE with numeric `tighten_to`; entries / ALERT → TAKE / SKIP),
  nullable `p_win`, validated by `claude_cli.validate` (invalid → fail closed), free-text mode for reviews, `--json` token usage parsed
  (`parse_usage`; null when absent), fallback without `--json` for older CLIs. CLI login only: `OPENAI_API_KEY` / `CODEX_API_KEY` stripped.
- Shared rules prompt `prompts/advisor_rules.md` (`prompts/claude_rules.md` kept as a symlink alias; `common/rules.py`).
- Agents: `codex=` hook beside `claude=` at every call site (`SymbolAgent.advisors()`), drained after the guardian, Claude first.
  23:00 IST CODEX REVIEW (CLI default model); Saturday CODEX REVIEW (self-review of its wrong calls) after COMPARE.
- Cards: ENTER / pullback / ALERT REACHED carry `CLAUDE:` and `CODEX :` lines and an AGREE / SPLIT tag (`advisor_board.py`); ALERT card has
  a CODEX VERDICT block when Codex is on. `/claude` and `/codex` share `advisor_card` (per-agent ATTACHED / REVIEW / OFF); `/status` adds a
  Codex line; `/codex-status` has the same today / week / month usage block as `/claude-status`. New: `/codex-attach`, `/codex-detach`,
  `/codex-review`, `/codex-rules`, `/codex-rules-approve`. `/codex-test` and `/codex-alert` now go through the advisor (one budget, one lock).
- `compare.py`: Codex column (`Row.codex`, `codex_take` / `codex_skip`), Codex verdict line, `head_to_head()` (agreement %, SPLIT winner
  count and points), Codex pullback score, breakdown `Cx` column, chart series, batch summary; CODEX RULE PROPOSALS card; approvals append
  `- [claude|codex] [approved date · week] …` to the shared file.
- `/advisor-report [week|month]` (alias `/claude-report`): a section per advisor + head-to-head; scheduled Saturday after COMPARE and on the 1st.
- `run.py --codex entry|entry+pullback`, `--codex-max`, `--codex-sleep`, `--codex-model`, `--codex-refresh`; own cache
  `out/codex_replay_<mode>_<symbol>.jsonl`; with `--claude` too: Detector-all · filtered-by-Claude · filtered-by-Codex ·
  filtered-by-BOTH-agree, SKIP precision / calibration each (p_win buckets and by confidence), SPLIT list (`out/advisors_replay_*.md`).
- Usage ledger rows: token fields are null when the CLI reports none (was 0).
- 80 new tests (325 in this repository).
- Strategy rules changed: **NO**. ema5080 strategy files: **UNTOUCHED**.

## v2.2.2 — 2026-10-11  (PATCH: server-authoritative auto-update)
- `common/updater.update_to_origin`: when origin/master moved, the checkout ends exactly at origin/master or is left untouched.
  Modified tracked files → `git diff HEAD` saved to `logs/local_changes_<hash>_<ts>.patch` (no patch on disk → nothing discarded), then
  `git reset --hard origin/master`; a clean tree uses `git pull --ff-only`, a non-ff pull falls back to the reset.
- `main.check_for_updates`: `UPDATE · LOCAL EDITS DISCARDED · <files> · patch saved <path>` (once, the tree is clean afterwards), then
  RESTARTING → `execv` only after `HEAD == origin/master`. Any git failure (no network, fetch/reset error) → `UPDATE FAILED` at most once
  per hour, the current build keeps running. Replaces the v2.2.1 "UPDATE SKIPPED" behaviour.
- `/restart` and `/mode` run the same update before stopping agents; a failed update aborts the restart (`RESTART ABORTED`, agents keep
  running, journal `manual_restart_aborted`).
- `news_blackout.txt` is no longer tracked (added to `.gitignore`; template `news_blackout.example.txt`). The updater restores `.env` and
  `news_blackout.txt` byte-for-byte after an update; at start-up a missing `news_blackout.txt` is copied from the example (never overwritten).
- `/git-history`: status adds `· N local edits (discarded on next update)` next to `behind by N`.
- 12 new tests on real temporary git repos (245 in this repository).
- Strategy rules changed: NO.

## v2.2.1 — 2026-10-11  (PATCH: fixes only)
- `CliResult` gains `usage: dict | None` and `total_cost_usd: float | None`. `claude_cli.parse_output` already passed them, so every Claude
  verdict call raised `TypeError` (44 tests red). `claude_usage.append` reads them with `getattr` defaults.
- Auto-update (`main.check_for_updates`): checks `git status --porcelain --untracked-files=no` first; a modified tracked file posts one
  `UPDATE SKIPPED · <files>` card per IST day and nothing else (no pull, no restart). Pull is `git pull --ff-only`; never `execv` after a failed pull.
- `/restart` and `/mode` go through `run()` (defer → work off the loop → one followup) like every other command; the restart starts 2 s
  after the reply. Same role, position and `.env` gates.
- Tests brought in line with v2.2.0 behaviour: a crashing agent exits for the supervisor (isolation still asserted on the other agent);
  the `/mode` position gate test uses a fresh snapshot (stale snapshots fail closed, also asserted).
- `prompts/claude_rules.md`: test that the file loads and both mode sections exist. `research/claude_rules_v2.md` is **not in the repository**,
  so the placeholder text is unchanged.
- 5 new tests (236 in this repository).
- Strategy rules changed: NO.

## Codex read-only advisor — PR #7–#10, merged before v2.2.0 (no own tag; entry added in v2.2.1)
- `AUREON_CODEX=off` (default) | `advisory`. Off → no Codex process is ever started. Codex never touches MT5, positions, the guardian or
  alert buttons; verdicts are journaled (`codex_test`, `codex_alert`) and shown, never applied.
- `/codex` (mode, `codex --version`, login, local calls today), `/codex-status` (login, version, call count), `/codex-test` (one
  schema-constrained verdict, consumes one call), `/codex-alert [symbol] [side=BUY]` (frozen closed-bar snapshot + `claude_rules.md`, refused
  when the cached bar is stale).
- `codex exec --sandbox read-only --skip-git-repo-check` in `AUREON_CODEX_WORKDIR`; one process at a time, no queue; daily cap
  `AUREON_CODEX_MAX_CALLS` (30); `AUREON_CODEX_TIMEOUT` (30 s); `AUREON_CODEX_BIN`, `AUREON_CODEX_MODEL`.
- CLI login only (`codex login`): `OPENAI_API_KEY` / `CODEX_API_KEY` and Aureon secrets are stripped from the child environment.
- Strategy rules changed: NO.

## v2.2.0 — 2026-10-11 (operational diagnostics and control; draft)
- /agents: per-symbol phase, M5 bar server/IST, day counters, guardian, Claude, thread status and uptime; no MT5 reads in slash commands.
- /commands grouped live directory; command descriptions clarify automatic work versus operator actions.
- Admin /restart [reason] and /mode (ema2050|ema5080) [symbols] [force], safe .env rewrite, agent stop/join and re-exec; requires AUREON_ADMIN_ROLE_ID.
- Crash cards and journal agent_crash, bounded supervisor restart with three-strike pause in 10 minutes; process exit diagnostics.
- Claude usage JSONL accounting with token and cost metadata, status periods, and graded report endpoint; scheduled weekly/monthly report history.
- Voice scope documented in docs/VOICE_SCOPE.md; NOT IMPLEMENTED.
- This draft requires Windows/MT5 staging, unit tests, Discord command sync, and restart testing before merging.
- Strategy rules changed: **NO**. ema5080 strategy files: **UNTOUCHED**.

## v2.1.0 — 2026-10-11  (run.py --claude shadow replay)
- `run.py --claude entry|entry+pullback [--claude-max N=60] [--claude-sleep S=2] [--claude-model id] [--claude-refresh]` for both modes.
  Without `--claude` nothing changes. For every ENTER bar the replay produced (ema2050 ENTER; ema5080 P / CROSS / RE) — and, with
  `entry+pullback`, every pullback event inside a replayed trade (the live trigger, once per pullback, re-armed after a new peak) — the
  snapshot is built with `claude_advisor.build_snapshot` on `df.iloc[:bar+1]` only (asserted: the cut frame ends at the signal bar; for
  ema2050 the per-bar analysis is rebuilt on the cut frame), with crosses_today / day_pnl_pts / trades_today recomputed for the bar's own
  server day. The CLI is called exactly as live (`claude_cli.call`, same rules prompt, same reply contract with p_win / evidence,
  `AUREON_CLAUDE_BIN`, `ANTHROPIC_API_KEY` stripped). Verdicts are recorded, never applied; the "Detector-filtered-by-Claude" column drops
  Claude's SKIPs and, in the pullback variant, applies TIGHTEN (tighter, sane SL hit on the M5 bars) / CLOSE (in profit only).
- Cache `out/claude_replay_<mode>_<symbol>.jsonl` keyed by (bar_t, event, model, rules hash); re-runs make zero calls; `--claude-refresh`
  forces calls; `--claude-max` never exceeded, unscored bars counted and continued from the cache on the next run. The live daily budget
  file is not touched (the replay has its own cap).
- Report `out/claude_replay_<mode>_<symbol>.md` + console: groups in the compare.card() shape (Detector · all | Claude TAKE | Claude SKIP |
  Detector-filtered-by-Claude: n, win %, expectancy, total, stops, worst streak, max DD), SKIP precision, p_win calibration table, per-month
  Detector vs Claude-filtered, disagreement list (date, side, detector pts, verdict, reason, evidence), pullback verdict counts.
- `aureon_mt5/claude_replay.py` (ShadowAgent, cut_frame, snapshot_for_bar, ShadowReplay, calibration, report). 9 new tests (220 in this repository).
- Strategy rules changed: NO.

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
