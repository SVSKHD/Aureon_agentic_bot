# Changelog — Aureon MT5

Format: `vMAJOR.MINOR.PATCH` · one entry per tag · strategy-rule changes are always called out explicitly.

## Unreleased — restart: pre-flight + reboot recovery
- **Pre-flight** (`aureon_mt5/preflight.py`, `main.py` before any agent):
  1. start `terminal64.exe` when it is not running (`AUREON_MT5_PATH`, else the cached path; else a warning);
  2. wait for `initialize()` + `account_info()` every 5 s, up to 90 s, touching `logs/heartbeat.txt` on every attempt; still
     down → exit 75;
  3. market closed → nothing exits, the agents sleep until the open;
  4. one **BACK ONLINE after restart** card (MT5 build, account, open positions re-adopted).
  - Every step is logged to `logs/aureon.log`. `logs/preflight.json` keeps the outage across restarts, so **MT5 NOT UP** is
    posted once. The COLD START card reads the heartbeat from before the pre-flight touched it.
  - Fixes: after a reboot, a slow terminal was killed as a HANG by the launcher (the 300 s MT5 wait equalled the 300 s start
    grace, with no heartbeat yet) and the supervisor looped. A terminal that was not yet logged in passed the old check.
- **`scripts/install_logon_tasks.ps1`**: Scheduled Tasks at log on, in the interactive session (no NSSM):
  - `Aureon-MT5` starts the terminal (0 s);
  - `Aureon` runs the launcher loop (+30 s, restarted every minute).
  It prints the auto-logon (Sysinternals Autologon) and `powercfg -change -standby-timeout-ac 0` reminders.
- `docs/SERVICE.md`: the reboot sequence (boot → auto-logon → terminal task → bot task → pre-flight → BACK ONLINE).
- Tests: `tests/test_v2150_preflight.py`.
- Strategy rules changed: NO

## v2.14.0 — 2026-10-16  (MINOR: configurable execution timeframe, HTF alignment gate, /decide-timeframe; defaults unchanged)
- **Execution timeframe** (`AUREON_EXEC_TF`, default `M5`). `common/tf_synth.py` holds the ladder, `TF_RULES` and the
  minutes table. `BAR_S`, the confirm rule and every bar-count window come from it:
  - confirm windows, pullback windows, P separation, whipsaw zone, P timeout, max bars;
  - the regime windows, the chart length, the leg "slow" age;
  - Claude/Codex staleness, `exit_audit --tf`.
  At M5 every conversion returns the v2.13.0 constant and `strategy_overrides` is `{}`: byte-identical.
  - M15 and up: confirm = hold 2 bars + gap ≥ `AUREON_CONFIRM_GAP_X_RANGE` (0.25) × the 20-bar average range, window 6.
  - `broker.bars(tf=…)` / `bars_tf` take every ladder TF, and the fetch scales.
  - `exec_tf` is on the boot card and in the journal's `process_start` row. It is applied after the Discord overrides.
- **Synthetic M45** (`synth_m45`): 3 × M15 aligned to the server day. The open group is dropped, and a group with a gap is
  skipped. It is cached per exec bar with the other HTF frames.
- **HTF alignment gate** (`AUREON_HTF_ALIGN=off|show|block`, `htf_align.py`). For each confirm TF: gap, gap_x_range,
  gap_slope3, bars_since_cross → `with` / `with_stale` / `pre` / `against` / `unknown`. `AUREON_HTF_ALIGN_MIN` all | any.
  - `show`: an `HTF` card line, an HTF chart panel (`common/htf_chart.py`), `htf_align` in the Claude snapshot (also
    compact) and one prompt line.
  - `block`: `… · HTF against · not tradeable`. No ask and no Claude read, journaled `entry_blocked_htf`. A TAKE from the
    pad or push is refused with the reasons. Open positions are never touched.
- **Replay:** `--set exec_tf=… htf_align=… htf_confirm_tfs=… confirm_*`. The `--compare` baseline is the same set with the
  gate off. Prints `asks_blocked_by_htf` (would have won / lost, blocked points) and the `pre` vs `with` entry results.
  Bars are fetched at M15 when the exec TF is a multiple of 15.
- **`/decide-timeframe tf [apply:false] [days:30] [force]`** (admin, `decide_tf.py`): the plan with rescaled follow knobs,
  and the replay block vs off, cached per (tf, days, date). `apply:true` writes the overrides, journals
  `timeframe_decided`, posts a loud card and restarts. It is refused with an open position unless `force`, and the gate is
  set to `show`, never `block`. With no tf: the ladder and the last decision. `/console/state` has `timeframe`.
- **Registry group `timeframe`:**
  - `exec_tf` (not hot, loud), `confirm_hold_bars`, `confirm_gap_x_range`, `confirm_window_bars`;
  - `htf_align` (hot; `block` is loud), `htf_confirm_tfs`, `htf_pre_max_gap`, `htf_with_max_bars`, `htf_require_fresh`,
    `htf_align_min`.
- Tests: `tests/test_v2140_exec_tf.py`, `tests/test_v2140_htf_align.py`, `tests/test_v2140_decide_tf.py`.
- Strategy rules changed: NO (defaults) — YES with AUREON_EXEC_TF≠M5 or AUREON_HTF_ALIGN=block

## v2.13.0 — 2026-10-16  (MINOR: new guardian profile, selectable; defaults unchanged)
- **`follow` guardian profile** (`AUREON_GUARDIAN_PROFILE=follow`, or per symbol `AUREON_GUARDIAN_PROFILE_<SYMBOL>`).
  `SymbolAgent._guard_follow` is dispatched per ticket and sticky like `classic` / `leg`. It does five things:
  - **Adopt:** stop at entry − `FOLLOW_STOP` (or range-scaled), never loosened.
  - **Secure:** +`SECURE_LEVEL` at live +`SECURE_AT`.
  - **Step ladder:** k = floor((peak − CLEARANCE) / STEP); the stop is (k − 1)·STEP, floored at the secure level. Each k
    gets a `follow_lock` journal row; a card is posted only when the stop moves. `continuous` mode keeps the stop at
    peak − STEP. A lock is never placed above the live price; after a restart the ladder value for the live points is used.
  - **Exits:** the broker stop (`closed` row `reason="stop"`, no "why?" ask); news flat while unsecured; an opposite
    confirmed cross with ≥ `FLIP_MIN_EVIDENCE` (reason `flip`, through the v2.12 pre-exit read when on); `MAX_HOLD_BARS`
    (unsecured AND setup dead).
  - **Not used:** P timeout, EMA turn, P flip, giveback, slow-EMA close, setup stop. `setup_alive` is still computed.
- Journal rows carry `profile="follow"`, `lock_level`, `peak`. `rebuild_state_for_ticket` restores the profile with peak
  and secured; the live SL stays the truth.
- **Guardian block for follow:** `next_exit` is the stop (`stop` / `ride_step`), plus `next_lock` (level and the trigger
  peak). The v2.12 "within 2 pts of the cap" check uses the follow stop.
- **Registry group `follow`:** `guardian_profile`, `follow_stop`, `follow_stop_x_range`, `follow_secure_at`,
  `follow_secure_level`, `follow_step`, `follow_clearance`, `follow_trail_mode`, `follow_exit_on_flip`, `follow_news_flat`,
  `follow_max_hold_bars` (not hot) and `follow_pre_exit_read` (hot).
- **Commands:** `/governor` shows the profile, its knobs and each open ticket's profile. `/mode` warns that open tickets
  keep theirs.
- **Replay:** `tools/replay_guardian.py --set guardian_profile=follow` (and every `follow_*` knob). A broker-stop exit is
  reported as `stop`, with the sequence per trade. The 15-Oct fixture: `classic` p_flip −1.20 → `follow`
  `secured +3 → stop` +3.00.
- Tests: `tests/test_v2130_follow.py` (24). `classic` and `leg` suites unchanged.
- Strategy rules changed: NO (default profile) — YES when AUREON_GUARDIAN_PROFILE=follow

## v2.12.0 — 2026-10-16  (MINOR: guardian intent in reads; pre-exit read with bounded deferral behind env; defaults unchanged)
- **Part A, guardian intent** (`SymbolAgent.guardian_intent`): a pure read of the guardian's state (no MT5 call).
  - Fields: phase pre / protect / secured / ride (both profiles), bars in trade, secured, peak, MAE, the trail / giveback /
    structure-stop / cap levels, flip evidence, setup_alive, and the last 3 guardian actions (`st["recent_actions"]`,
    written by `_close`, `_move_sl`, structure SL, FLIP_HOLD and exit deferrals).
  - `next_exit` is the candidate with the smallest distance: the broker stop (tick), ema_fast_turn, pre_stop, pre_timeout
    (bars_left), ema_slow_stop, giveback (same `_giveback_fires` / `_giveback_floor_pts` helpers the guardian uses),
    setup_invalidated, and the leg profile's soft_trail and ema_slow_close.
  - The block goes in `snap["guardian"]` of every position snapshot (open, drawdown, flip-held, pullback, `/claude-read`)
    and in `_compact`. The prompt gains a GUARDIAN paragraph.
- **Part B, pre-exit read** (`AUREON_PRE_EXIT_READ=off|show|defer`, registry: `pre_exit_read` loud on `defer`,
  `pre_exit_max_defer`, `pre_exit_wait_s`). The five close-based exits go through `_close_or_defer`:
  - `show`: close plus a `pre_exit` read.
  - `defer`: `pending_exit` + EXIT PENDING card. The verdict is applied in `drain()` on the agent thread:
    - EXIT, timeout or error → close by the original rule (`claude_pre_exit` on the `exit` row).
    - HOLD → `exit_deferred` + EXIT DEFERRED card; asked again only while `deferrals < MAX`, then the close is unconditional.
  - Never deferred: `pre_stop`, `ema_slow_stop`, news, an opposite confirmed cross, the staircase, or a trade within 2 pts
    of the cap. The SL is never touched (tested).
  - `pending_exit` is cleared on any close, when the rule is no longer true on the next close (`exit_cancelled`), and
    on restart.
  - Pre-exit `claude_verdict` rows carry `trigger="pre_exit"`, `rule`, `deferral_n`.
- **B3 grading:** `advisor_board.pre_exit_grades` computes `deferral_gain_pts` (points `AUREON_PRE_EXIT_GRADE_BARS` = 6
  bars later, or at the exit, minus points at the rule). `/advisor-report` gets a pre-exit section.
- **B4** `tools/exit_audit.py --days 7`. Exit audit for the last 7 days: this build ran on a copy of the repo with no live
  journal (`logs/journal.jsonl` lives on the trading box), so the table is empty here:
  ```
  exit audit · last 7 days · MFE over 12 bars after the close · early = ≥ +10 pts
  rule                    n  avg pts @close  avg MFE after  closed early
  (no software exits in the journal for this window)
  ```
  Run `python tools/exit_audit.py --days 7` on the box. Bars come from `logs/bars/*.parquet`, else MT5.
- Tests: `tests/test_v2120_guardian_reads.py` (20 tests). The rest of the suite is unchanged with the default `off`.
- Strategy rules changed: NO (defaults) — YES with AUREON_PRE_EXIT_READ=defer (close-based exits may be delayed up to 2 bars; broker stop unchanged)

## v2.11.2 — 2026-10-15  (PATCH: process survival on network loss)
Root cause: with DNS gone the Discord login raised, `bot._runner` let the `discord-bot` thread end, and once the gateway had
been down for 300 s `HealthGuard._maybe_restart` treated that as a hang and called `ops.hard_exit(75)` → `os._exit(75)`
(after posting "⚠️ Aureon frozen — restarting", which itself failed with "discord post failed"); with no service installed
nothing restarted the process.
- **`aureon_mt5/netguard.py`**: network failures become state, never an exit. `degraded()` = gateway down > 60 s OR 2 webhook
  failures in a row OR discord.com does not resolve; mirrored into the v2.10.1 entry gate. Failure logs at most once a
  minute (`rlog`).
- **Gateway supervisor** (`netguard.supervise_client`, `bot._runner`): any exit of `client.start()` — DNS failure at login,
  ConnectionClosed, discord.py's reconnect giving up, a clean return — backs off 2 s → 60 s and starts again (closed client
  `clear()`ed, its aiohttp connector reset); only `ops.STOP` (/restart, /mode, maintenance, repair-apply, Ctrl-C) ends it.
  `reconnect=True` stays on.
- **HealthGuard**: a disconnected gateway is no longer a restart condition (loop lag > 30 s still is). The v2.7.2 test
  `test_self_restart_on_gateway_down_over_5_min` is replaced by `test_gateway_down_never_restarts_the_process`.
- **Degraded**: the poll loop and guardian unchanged; entry asks journaled `ask_suppressed_network` and listed on the console
  (`/console/state` → `network.suppressed_asks`), never auto-taken; registry flag `network_degraded_entries` (bool, default
  off, loud when on) allows them. Trade cards go to `logs/card_queue.jsonl` (append-only, cap 500) and no blocking POST is
  attempted while the webhook is down; the main loop retries every 30 s (independent of the gateway) and flushes with one
  `BACK ONLINE · network · down N min · K cards queued` card followed by the queued cards in order (replaces NETWORK BACK).
- **Push / console best-effort**: a push backend failing 5× in a row is paused 10 min with one PUSH PAUSED card; the uvicorn
  thread restarts itself (5 s → 60 s); Telegram poll and dead-man ping failures are rate-limited logs.
- **exit_reason**: `main.run()` wraps `main()`; every path out (normal stop, exception, SIGINT, SIGTERM/SIGBREAK, SystemExit,
  and `ops.hard_exit`) journals `process_exit {reason, exc_type, where, uptime_s, positions_open}` and writes it into
  `restart_marker.json` (restart requested) or `logs/exit_marker.json`. COLD START / BACK ONLINE cards show `last exit: …`.
  `python main.py` exits 1 on any unplanned exit (0 planned, 75 restart). `ops.hard_exit` resolves `os._exit` at call time.
- **Service-safe paths**: `config.validate` warns (BOOT CHECK, /env-check) when the repo or `log_dir` resolves under a
  OneDrive / Dropbox / Google Drive / iCloud folder.
- **DNS self-check** (`netguard.dns_check`, 5 s bound per host): discord.com, the ntfy host (api.ntfy.sh by default), the MT5
  host (optional `AUREON_MT5_HOST`) — on the 4-hourly heartbeat card and on every degraded flip (NETWORK DEGRADED / RESTORED).
- Tests: `tests/test_v2112_netguard.py` (the reproduction fails on v2.11.0 with `ops.hard_exit(75)`; unpatched it killed
  pytest with exit 75). `.env.example`: no new required variables.
- Strategy rules changed: NO

## v2.11.0 — 2026-10-15  (MINOR: notifications + console contract; no trading change)
- **Part A — immediate Discord mentions** (`aureon_mt5/notify.py`): `EVENT_CLASS` by event prefix (trade / ops, default
  info); every `send` / `ask` / `card` resolves an event (`event=` kwarg, else the key's event segment, else an ops title) and a
  class (`klass=` overrides). Trade events in `AUREON_DISCORD_MENTION_EVENTS` (default `ASK,FILLED,CLOSED,STOP`, registry
  enum-set, hot) carry `<@AUREON_DISCORD_USER_ID>` in `content` with `allowed_mentions: {"users": [id]}` (webhook and bot-posted
  asks). `AUREON_DISCORD_ASKS_WEBHOOK`: trade-class copies posted with `?wait=true`; `edit_card` / button decisions PATCH every
  tracked copy (`Notifier.mirror_edit`). Events tagged at the source: ORDER PLACED → FILLED; guardian closes and broker-side
  closes → CLOSED / STOP with points; SHOULD WE CLOSE? → POSITION_ASK. With the new variables unset the payloads are
  byte-identical to v2.10.1 (tested).
- **Part B — push layer** (`aureon_mt5/push.py`): `Push.send(PushEvent)`; backends `AUREON_PUSH=off|ntfy|pushover|telegram`
  on a 3-worker pool, 10 s timeout, failures journaled (`push_failed`), never raised; one push per dedupe key, also when the
  Discord post failed. Mapping: ASK → urgent + TAKE/SKIP/READ · FILLED → high · CLOSED/STOP → normal (STOP worse than
  `AUREON_PUSH_LOSS_ALERT_PTS` → urgent) · FLIP_HOLD → high + READ · POSITION_ASK → high + CLOSE/READ · MAINTENANCE /
  COLD START / MT5 NOT UP / REPAIR AUTO-APPLY → high · Claude READ verdict → high. ntfy JSON publish (PUT upload with
  `AUREON_PUSH_IMAGES=1`, RFC 2047 headers), Pushover (urgent = priority 2, retry 30, expire 600), Telegram (`telegram_send`
  from `scripts/deadman_fallback.py` gains `reply_markup`; inline keyboard; `getUpdates` long-poll 25 s → console actions,
  configured chat only).
- **Scoped action tokens**: push buttons carry `act.<payload>.<sig>` (HMAC-SHA256 keyed by the console token), valid only for
  that action, symbol and ask key, until the ask expires, once. The console token is never sent to ntfy / Pushover / Telegram.
- **Console** (`aureon_mt5/console.py`, `docs/CONSOLE.md`, contract v1): FastAPI on its own uvicorn thread, started with the
  bot; `AUREON_CONSOLE_BIND=127.0.0.1:8787`, non-loopback refused without `AUREON_CONSOLE_ALLOW_PUBLIC=1` (flagged loudly);
  Bearer `AUREON_CONSOLE_TOKEN` (32+ chars, constant-time compare) or a scoped token; 20 req/min per client; every call
  journaled `console_call` with `source="push"`. `POST /console/action` (take / skip / close / read), `GET /console/state`
  (`{"v": 1, …}`), `GET /console/chart/<key>.png` (only `logs/charts/`). Never imports `broker`.
- **Shared actions** (`aureon_mt5/actions.py`): `/take`, `/skip`, the new **`/close`**, the console and Telegram use one path;
  console refusals: no open ask, already answered, stale (> `AUREON_ASK_TTL_MIN`), regime gate (with reasons), alert without a
  side, plus everything `execute_entry` refuses. `halt.close_all_positions` gains `comment=`.
- Config: `AUREON_DISCORD_USER_ID`, `AUREON_DISCORD_ASKS_WEBHOOK`, `AUREON_DISCORD_MENTION_EVENTS`, `AUREON_PUSH`,
  `AUREON_PUSH_IMAGES`, `AUREON_PUSH_LOSS_ALERT_PTS`, `AUREON_NTFY_URL/TOPIC/TOKEN`, `AUREON_PUSHOVER_TOKEN/USER`,
  `AUREON_TELEGRAM_TOKEN/CHAT`, `AUREON_CONSOLE_BIND/TOKEN/ALLOW_PUBLIC/URL`; `validate()` rules for all of them (BOOT CHECK
  applies the `notify` group's fallbacks). Registry group `notify`: `mention_events`, `push` (loud when switched on),
  `push_images`. `requirements.txt`: fastapi, uvicorn (imported only when the console is on).
- Tests: `test_v2110_notify.py`, `test_v2110_push.py`, `test_v2110_console.py`.
- Strategy rules changed: NO

## v2.10.1 — 2026-10-15  (operations: unattended operation + server ownership; defaults unchanged for trades)
Parts E and F of the v2.10.0 brief (v2.10.0 shipped A–D). New tooling and commands, so MINOR-sized; numbered 2.10.1 to keep
the v2.11.0 number for the notifications release.
- **E1 cold start** (`aureon_mt5/server.py`): boot without a marker → **COLD START** card + journal `cold_start` (Windows boot
  time, last 1074/6006/6008 event and its reason, minutes down since the last heartbeat, positions re-adopted from
  guardian_state). BACK ONLINE cards: `down X s` (marker time); new marker kinds `maintenance`, `repair-auto`, `repair-rollback`.
- **E2 MT5 before the bot**: `aureon_service.ps1` starts `terminal64.exe` (`AUREON_MT5_PATH` / `logs/mt5_path.txt`);
  `server.connect_mt5` retries 5 s → 30 s for `AUREON_MT5_BOOT_WAIT`, **MT5 NOT UP** after 5 min, exit 75; `broker.reset()`.
- **E3 env check**: every numeric `Config` field parses through `_num` (an unparsable value no longer crashes the import: default
  + recorded); `config.validate()` / `apply_fallbacks()` / `env_check_table()`; **BOOT CHECK** card; `/env-check`.
- **E4 repair `auto`**: `repair.maybe_auto_apply` (guards: clean patch, flat, outside session unless the bot is unhealthy,
  daily cap `AUREON_REPAIR_AUTO_PER_DAY`, fingerprint not rolled back before) → **REPAIR AUTO-APPLY** → `repair.Verifier` on the
  new process → **REPAIR VERIFIED** after `AUREON_REPAIR_VERIFY_S`, or `git revert` + restart once → **REPAIR ROLLED BACK**,
  `needs_human`, fingerprint blocked from auto.
- **F1 maintenance** (`server.Maintenance`, `scripts/aureon_server.ps1 -Action plan|harden`): hourly pending-reboot check;
  reboot / code update (+ suite; red → stay on the old commit) / housekeeping only flat and outside the session (or pending ≥
  `AUREON_SERVER_MAINT_FORCE_AFTER_H`, flat); 60 s countdown aborted by a new position; `rotate_logs` never touches the journal or
  state; pip on a changed requirements.txt. **The 5-minute auto-update moved into this window.**
- **F2** `server.Mt5Manager`: disconnected 60 s → restart terminal, verify login + symbols. **F3** `check_clock` (boot + 6 h,
  > 2 s → resync + card); `NetworkWatch` → `network_degraded` blocks new entries (guardian unchanged, tested); Notifier backlog →
  one **NETWORK BACK** card. **F4** `/server`; heartbeat card line `disk · drift · updates pending · MT5 age`.
- **F5** `aureon_mt5/tuning_auto.py` (`AUREON_TUNING_AUTO=off|shadow|apply`): replay gate (points ↑, losing trades ↓, regime
  sign-off, pins) → shadow sessions (each must agree) → apply through the registry (weekly cap) with **STRATEGY CHANGE
  APPLIED** + `/veto`; never-list. `tuning.propose` accepts guardian/regime registry features (registry range).
- Registry: `repair_agent` gains `auto`; group `server`: `tuning_auto`, `maint_force_after_h`, `log_keep_days`.
- Known gap (unchanged, pre-existing): `logs/overrides.json` written by `/approve` is shown by `/tuning` but not read by the
  guardian, so only registry knobs can change automatically.
- Tests: `test_v2101_unattended.py`, `test_v2101_server.py` (all OS calls through a faked `run_ps`).
- Strategy rules changed: NO — pins test unchanged and green

## v2.10.0 — 2026-10-15  (MINOR: operations + context; defaults unchanged)
Built on v2.8.0 — there is no v2.9.0: the "v2.9.0" pieces this release builds on (structure-gated flip, giveback,
`_flip_evidence`, `request_flip_read`, the 15-Oct fixture) shipped in v2.8.0.
- **Feature registry** `aureon_mt5/features.py`: one row per env-gated feature (claude_mode, image_events, open_read, dd_read_at,
  exit_losing, max_calls, flip_require_structure, flip_use_zl, giveback_pct/arm, setup_stop, setup_max_dd, daily_plan,
  repair_agent, zl_context, regime_gate + 5 thresholds). Resolution discord (`logs/features.json`) > env > default; `apply()`
  writes onto `Config` in place (no caller changes). `ClaudeAdvisor.set_mode()` (+ `rebind`) for live claude_mode, journal
  `claude_mode_changed`; max_calls updates the budget. `/features`, `/feature-set` (admin; non-hot refused with a position open
  unless force; journal `feature_set`; FEATURE CHANGED card for loud changes), `/feature-reset`, `/feature-help`.
  Boot card + daily plan card: `Features` field, one line per group, `*` = from Discord.
- **/commands**: `bot.HELP` (module level) documents all 66 registered commands; `tests/test_v2100_commands.py` fails on any
  undocumented root command.
- **Repair agent** `aureon_mt5/repair.py`: `Detector` on the HealthGuard tick (no new thread; `telemetry.RECENT`,
  `context_chart.FAILURES`), one open incident per fingerprint (`logs/incidents.json`, `INCIDENT #n` card). `Repairer.propose`
  (Claude worker, `_call_lock`, daily budget, usage kind `repair`): worktree `repair/<n>` from origin/master, REPAIR_BRIEF.md,
  fixer (`claude -p` with Read/Edit/Write/pytest or `codex exec`), pytest, forbidden-path + guardian-function (AST) + VERSION
  check, patch file, worktree reset, `REPAIR #n` card. `apply`: `git apply --check --index` → apply → pytest → commit → push →
  `ops.restart_process(reason "repair #n")`; any failure reverts. Commands `/repairs`, `/repair`, `/repair-apply`,
  `/repair-discard` (admin). `AUREON_REPAIR_AGENT=off|propose`, `_ENGINE`, `_MODEL`, `_TIMEOUT`.
- **Pins** `tests/test_strategy_pins.py` on `tests/fixtures/xauusd_2day_m5.json` (synthetic 15-Oct + one synthetic day): P
  signals, analysis entries/exit reasons, live-guardian stops/close reasons at defaults, the 15-Oct trade. VERSIONING.md: pins
  rule + repair-agent boundary.
- **Zero-lag context** `aureon_mt5/common/zl.py` (`AUREON_ZL_CONTEXT=0`): ZLEMA 50/80, HMA 55, TEMA 50, ADX 5/14 ± DI, ATR 14,
  KAMA 10, session VWAP — no lookahead (tested). Snapshot `zl` block + ZL_RULES paragraph; chart: dotted ZL lines + ADX(5) strip;
  `AUREON_FLIP_USE_ZL=0` → optional "ZL cross against" flip evidence (adds only).
- **Backtest** `tools/replay_guardian.py` + `tools/guardian_sim.py`: live `_guard` bar by bar, fills at close, stops intrabar;
  `--trade`, `--signal` (a P seen live that the analysis misses), `--set`, `--compare`; summary → `logs/replay/<date>.json`.
- **Regime gate** `aureon_mt5/regime.py` (`AUREON_REGIME_GATE=off`): features crosses_2h, flat_gap_bars, adx14 (+rising),
  di_spread, failed_p_2h, range_compression, zl_crosses_2h; score = min(whipsaw half, energy half) (weights in
  `Config.regime_weights`); CLEAN/CHOPPY/DEAD with hysteresis; `no_trade_day`. Computed + journaled (`regime_change`) every bar
  at any setting; `show` adds a Regime field to P/cross/RE/ENTER cards; `block` cards the signal "CHOP · not tradeable" (no ask,
  no tracked ask, no Claude entry read), refuses TAKE (`take_refused_regime`), posts NO-TRADE REGIME / NO-TRADE DAY. Open
  positions untouched (the v2.8.0 flip/giveback tests re-run with block + DEAD). Regime block in the Claude snapshot only when
  the gate is not `off` (defaults keep Claude's prompt unchanged). Backtest: entries_blocked → would_have_lost / would_have_won,
  no_trade_days, sign-off rule.
- Registry notes: `max_calls` is an int; `flip_use_zl` added to the registry; `daily_plan` keeps its existing default **on**
  (the daily plan card has existed since v2.3.15 — `.env.example` keeps `AUREON_DAILY_PLAN=1`, not 0).
- Replay (synthetic bars, see v2.8.0): `--trade 10-15T01:30,long,4103.5 --signal 10-15T02:05,short --compare
  --set giveback_pct=50 --set flip_require_structure=1` → defaults total +8.30 (01:30 long −1.2 p_flip) vs +19.50 (held, +10.0);
  `--set regime_gate=block` on the same window → entries_blocked 0 (every entry CLEAN: the synthetic days trend).
- Strategy rules changed: NO — pins test added

## v2.8.0 — 2026-10-15  (MINOR: Claude chart/reads + guardian rules behind env; defaults unchanged)
Why: 2026-10-15 02:05 server — five XAUUSD longs from 4103.5 were closed at 4102.3 (−1.2) by rule 1 in `_guard` ("opposite P →
close (flip)") on a short P that never closed through EMA 80, with EMA 50 still rising and M15 bull. Price then ran to 4150. The exit
rule had less context than the entry rule (`journeys._entry_checks` needs session, M15, slope, news), and there was no giveback-from-
peak exit anywhere (only `claude_advisor.pullback_reason`, which only asks).
- **Chart to Claude** (`claude_cli.call(image=)`, `stage_image`, `CliResult.image`): the card's PNG is copied to `<workdir>/snaps/`,
  the CLI gets `--allowedTools Read` and a CHART trailer (structure only; numbers from the SNAPSHOT). No image → command and prompt
  byte-identical (tested). `AUREON_CLAUDE_IMAGE_EVENTS=P,CROSS,READ`. Ledger and verdict journal carry `image`. Codex `_invoke`
  accepts and drops `image`.
- **`READ` decisions** `HOLD|EXIT|ENTER|SKIP` (ENTER needs a side; TIGHTEN rejected). **`/claude-read [symbol]`** → `read_now()`:
  frozen frame, cached position (no broker call), chart, `READ_RULES`; contradictions dropped (ENTER/SKIP in a trade, HOLD/EXIT flat);
  journal `claude_read` (`acted=False`). Shown only.
- **Structure-gated flip** `AUREON_FLIP_REQUIRE_STRUCTURE=0|1`, `FLIP_MIN_EVIDENCE=2`, `FLIP_GAP_X_RANGE=0.5`: `_flip_evidence()`
  (close through EMA80 · EMA50 slope against · M15 against via the new cached `_m15_state()` · gap wide). Held → journal
  `opposite_p_ignored`, `OPPOSITE P · HOLDING` card, `request_flip_read()`. Closed → the `exit` row carries `flip_evidence`.
- **Giveback** `AUREON_GIVEBACK_PCT=0` (off), `GIVEBACK_ARM=10`: bar CLOSE kept ≤ (1 − pct) of the peak → `giveback` close.
- **Setup stop** `AUREON_SETUP_STOP=0` (+ `SETUP_MAX_DD=10`, `SETUP_MAX_DD_X_RANGE=0`, `SETUP_SWING_BARS=6`, `SETUP_BUFFER=1.0`):
  new pure `aureon_mt5/setup.py` (`setup_checks`, `setup_alive`, `structure_stop_pts` = `leg_guardian.phase_b_trail` with
  `atr_mult=0`). Classic: PROTECT at the structure stop, raised each bar while `secured == 0` (journal `structure_sl`),
  `setup_invalidated` exit on a CLOSE before `pre_stop` (journal `setup_invalidated` with the failed checks), `pre_timeout` paused while
  the lines converge (`pre_timeout_paused`, once). After SECURE the staircase/giveback/`ema_fast_turn` run unchanged. `leg`: only the
  invalidation exit. `st["setup"|"setup_alive"|"cap_pts"|"dist_to_cap"|"bars_in_dd"]` (computed when the setup stop is on or Claude is
  attached) ride into the Claude snapshot.
- **Claude votes** (Claude only; Codex no-op): `request_open_read` at first sight of a NEW ticket (`AUREON_CLAUDE_OPEN_READ=1`, shown
  only in every mode, edits the ask card's `CLAUDE VERDICT` when there is one) and `maybe_drawdown` (`DD_READ_AT=3`, `DD_READ_STEP=2`,
  last chance within 2 pts of the cap, `DD_READS_PER_TRADE=3`). `apply_pullback` gets an `EXIT` branch: in profit → close (manage);
  losing → close only for `trigger == "drawdown"` in manage with `AUREON_CLAUDE_EXIT_LOSING=1` and above the cap (`claude_cut_loss`);
  otherwise shown. All closes go through one `_guardian_close` → `agent._close` (the hard-rule test still counts one call site).
- `/advisor-report`: "READ votes by trigger" — p_win buckets and HOLD/EXIT vs the trade's `final_points` (`advisor_board.read_grades`).
- Decisions taken while building (worth a look):
  - In the P phase a long's EMA fast is below EMA slow by definition, so the `ema_order` check is `None` (not applicable) until the
    cross — otherwise every P-phase trade would be "invalidated" at once under `SETUP_STOP=1`.
  - A held opposite P does not skip the rest of that poll's guardian rules (stops, timeout, invalidation still run); only a flip close
    ends the ticket's poll.
  - The `swing` check compares the close with the PRIOR bars' lows/highs (the current bar's own low is always ≤ its close).
- Tests: `test_v280_claude_read.py`, `test_v280_flip_giveback.py`, `test_v280_setup_stop.py`, `test_v280_claude_votes.py`;
  `tests/conftest.py` starts tests with the open READ off (older rigs pop the advisor queue); v2.8.0 tests turn it on.
- **Replay of the 02:05 trade** (`python tests/fixtures/replay_20261015.py`). ⚠️ The M5 bars are SYNTHETIC
  (`tests/fixtures/make_xauusd_20261015.py`, rebuilt from the incident numbers: EMA80 ≈ 4098.7, gap ≈ 1.0 vs avg range ≈ 2.8, M15 bull):
  the machine this was built on has no `logs/journal*.jsonl` and no MT5. Re-run with the real bars before trusting these numbers.
  - defaults (v2.7.2): initial SL 4097.5 · worst close −3.0 · closed 02:05 at 4102.30 (−1.2, `p_flip`) — the incident, reproduced.
  - `AUREON_FLIP_REQUIRE_STRUCTURE=1`: P SHORT held (0/2 evidence) · worst close −3.0 · SECURE +10 at 02:35, stopped at 4113.50
    (+10.0) by the next bar's wick on the synthetic bars.
  - `AUREON_GIVEBACK_PCT=50`: no effect on this trade — with the default flip it is closed at 02:05 before the giveback can arm; with
    `FLIP_REQUIRE_STRUCTURE=1` the SECURE +10 lock exits first (peak +10.2: a 50 % giveback level, +5.1, sits below the +10 lock).
  - `AUREON_SETUP_STOP=1`: initial SL 4098.85 (swing low 4099.9 − 1.0, cap −10) · worst close −2.5 · **stopped at 01:55 at 4099.67
    (−3.8)** — the per-bar raise (as specified) lifts the stop with the 6-bar swing low during the grind into the entry, and the
    pullback's low touches it. Without the per-bar raise the initial 4098.85 would have held (lowest low 4099.3). Not what the brief
    expected ("held, worst close ≈ −3"); left as specified and flagged — options: raise only once the trade is above entry, or raise
    only on a confirmed higher swing low.
- Strategy rules changed: NO (defaults) — YES with AUREON_FLIP_REQUIRE_STRUCTURE=1, AUREON_GIVEBACK_PCT>0 or AUREON_SETUP_STOP=1 — Claude cut-loss only with AUREON_CLAUDE_EXIT_LOSING=1

## v2.7.2 — 2026-10-14  (PATCH: self-healing + outside-visible liveness)
Symptom: `/status` and `/restart` → "The application did not respond", bot offline, no 08:00 heartbeat after the 04:00 card.
`respond()` already defers first, so the process was dead, frozen or off the gateway — and nothing outside it could tell.
- **Supervisor** (`scripts/aureon_service.ps1`, `docs/SERVICE.md`): NSSM service (boot start) or, without NSSM, a logon
  Scheduled Task, running a PowerShell launcher loop: restart on any exit, crash backoff 5 s → 30 s → 2 min (reset after
  10 min up), kill + restart when `logs/heartbeat.txt` is > 5 min old (hang), stdout/stderr → `logs/service.log` (10 MB × 5).
- **Exit 75 = restart**: `/restart`, `/mode` and auto-update stop agents, write `logs/restart_marker.json`, exit 75; the
  supervisor restarts. No `os.execv`. The marker drives the BACK ONLINE card. `/mode` refuses when the process was started
  with `--mode/--symbols` (the supervisor would restart with the old flags). Fixed a literal `\n` in the manual BACK ONLINE card.
- **`aureon_mt5/selfheal.py` `HealthGuard`** (plain thread, never the Discord loop, no MT5 calls):
  - `logs/heartbeat.txt` every 30 s (ISO time · version · lag · gateway · health); shown on `/status`.
  - **Dead-man's switch** `AUREON_DEADMAN_URL`: GET every 60 s only while loop lag < 5 s, gateway connected, every agent
    thread alive and its snapshot < 5 min old, and a closed bar < 15 min old while the market is open.
  - **Self-restart**: loop lag > 30 s, or gateway down > 5 min (incl. never connected / Discord thread died) → journal
    `self_restart` + `logs/aureon.log` + webhook card "⚠️ Aureon frozen — restarting" → exit 75 (once).
  - **Stall dump**: loop lag > 3 s → every thread's stack to `logs/stall-<time>.txt`, once per stall.
- **Fallback alarm** `scripts/deadman_fallback.py` (Task Scheduler every 5 min, `-Action install-fallback`): Telegram
  "Aureon down" / "Aureon back" from `logs/heartbeat.txt` age. `AUREON_DEADMAN_TELEGRAM` + `AUREON_DEADMAN_TELEGRAM_CHAT`.
- **Loop blockers removed** (found by an AST scan of the async code in `bot.py`):
  - a failed command's `telemetry.failure` posted the webhook (requests, 15 s timeout) **on the event loop** — now in a worker;
  - card-edit hooks waited 10 s on a loop future — would freeze the loop if called from the loop thread; now scheduled there;
  - `alerts.set_message` (store lock + file write) and `mark_ask_decided` (file write) ran on the loop — now off it;
  - dedicated `ThreadPoolExecutor(max_workers=16)` for `off_loop` / all former `asyncio.to_thread` calls, so stuck MT5 calls
    cannot starve command work (asyncio's default pool is cpu+4 = 6 on a 2-core VPS).
  Charts, replays, compare, reports already run in `work()` off the loop; Claude/Codex subprocesses on their own worker
  threads; nightly review / calibration on the supervisor thread — none on the Discord thread.
- **Gateway**: every disconnect / resume logged with its duration; first connect logged; Discord `client.run` exit logged and
  marks the gateway down; 💓 heartbeat card shows `gateway up · reconnects last 4 h: N`; `/status` Bot line too.
- Tests: `tests/test_v272_selfheal.py`.
- Strategy rules changed: NO

## v2.7.1 — 2026-10-13  (PATCH: leg guardian fixes)
- **Default**: `AUREON_GUARDIAN_PROFILE=""` → auto: `leg` on demo (`broker.is_demo()`), `classic` on live.
  `AUREON_GUARDIAN_PROFILE_DEMO=leg`. `/status` and RESUMED card show profile + phase per ticket.
- **Phase A news**: do NOT close; cap broker SL at `−pre_stop`; if soft lock armed, convert to hard SL at that level.
- **Sticky leg**: once a ticket enters `leg` profile it stays. `st["leg"]` snapshot persisted in `guardian_state.json`.
  `leg_alive()` reads `st["leg"]`, not `self.leg` — opposite unconfirmed cross no longer switches to classic mid-ride.
- **SHORT ATR trail**: `peak_px = entry + s * peak` (was `entry - s * peak` for shorts — double negative).
- **Phase B entry guard**: transition A→B only when **current** points >= `ride_floor + ride_buffer` (not just peak).
  If floor SL move rejected, stay A and retry. Never leave B without a floor.
- **Soft trail arm**: `max(early_at, AUREON_SOFT_ARM_PTS=5)`; giveback default `3.0` (was 2.5).
- **Restart**: `st["leg"]` rebuilt from journal; RESUMED card shows `leg A soft +2.1` / `leg B floor +4 trail +8.3`.
- **CLOSED card**: shows `[leg · soft_trail]` / `[classic · ema_fast_turn]` for attribution.
- **Replay parity**: `journeys.py` updated for all fixes above. `soft_arm_pts` in build/grid.
- Strategy rules changed: YES — exits (leg profile only). Classic unchanged. SL never loosened.

## v2.7.0 — 2026-10-13  (MINOR: leg-based guardian profile)
- **`aureon_mt5/leg_guardian.py`** (new, pure): `soft_trail_level()`, `phase_b_trail()`, `leg_alive()`, `bar_closes_through_ema()`.
- **`aureon_mt5/agent.py`**: guardian logic split into `_guard_classic()` (exact pre-v2.7.0 behaviour) and `_guard_leg()` (move-detector based).
  Selected by `AUREON_GUARDIAN_PROFILE` (`classic` default, `leg` for demo).
- **Leg profile Phase A**: broker SL stays at `−pre_stop`. Soft TRAILING lock = `max(early_level, peak − giveback)` where
  `giveback = max(AUREON_SOFT_GIVEBACK_PTS=2.5, AUREON_SOFT_GIVEBACK_FRAC=0.5 × peak)`. Armed at `early_at`, exit on bar close.
- **Leg profile Phase B** (peak >= `secure_at`): broker SL set to floor `AUREON_RIDE_FLOOR=+4` (NOT +secure_level=+10).
  Trail on CLOSED bars: `max(floor, swing trail = lowest low of AUREON_RIDE_SWING_BARS=6 bars − AUREON_RIDE_BUFFER=1.0,
  ATR trail = peak − AUREON_RIDE_ATR_MULT=3.0 × avg range)`. Never loosens. Card when trail rises >= 3 pts.
- **Phase B exits**: SL hit (`trail_hit`), bar closes through EMA slow (`ema_slow_close`), leg ends (`leg_end`).
  EMA fast close only tightens the trail. News in Phase B tightens, doesn't close.
- **Guardian ↔ leg link**: on `position_seen`, attach/create `self.leg` for the same side. No same-side leg → classic exits.
- **Replay**: `run.py --guardian classic|leg|grid`. Grid over floor {2,4,6} × swing {4,6,10} × ATR mult {2,3,4}.
  Compare table per config: n, win%, net pts (with --spread), avg winner, avg loser, max drawdown, % >=+20/+30, avg give-back.
- **`compare.py`**: `Row` gains `exit_rule` and `guardian_profile` fields (appended last).
- **`Journey`** dataclass: `exit_rule` and `guardian_profile` fields (serialized in `to_dict()`).
- Strategy rules changed: **YES — exits only**. Classic profile keeps exact old rules. Entry rules unchanged. SL never loosened.

## v2.6.2 — 2026-10-13  (PATCH: event publisher for the connector)
- **`aureon_mt5/publisher.py`**: when `AUREON_PUBLISH=1` and `REDIS_URL` set, appends to Redis Stream `aureon:events`
  (XADD, MAXLEN ~10000): open, sl_change, partial, close, heartbeat, halt, unhalt. Payload: event_id (uuid4, idempotent),
  ts, master_ticket, symbol, side, lots, price, sl, actor, mode, version.
- Publishing happens AFTER MT5 confirm + state + journal. Redis failure → spool to `logs/outbox.jsonl`, replayed when
  Redis returns. Reconciliation snapshot every tick: `aureon:positions` hash.
- `redis` is an optional dependency. `AUREON_PUBLISH=0` default.
- Strategy rules changed: NO.

## v2.6.1 — 2026-10-13  (PATCH: bounded threshold tuning + /approve)
- **`aureon_mt5/tuning.py`**: for each tunable parameter (`pre_stop`, `secure_at`, `ride_step`, `early_at`, `early_level`,
  `chop_crosses`, `missed_grace_min`, `override_max_drift`, `heartbeat_stale_min`) — min, max, and max step per change defined
  in `BOUNDS`. Strategy entry rules (`confirm_bars`, `confirm_gap`, etc.) are `NOT_TUNABLE`.
- **Proposals**: at most 2 per week, each within bounds and one step max. `propose()` creates a pending proposal stored in
  `logs/tuning_proposals.json`. Not approved within 72 h → expired. `/approve-tuning <id>` writes to `logs/overrides.json`
  (loaded at start, hot-reloaded). `/reject-tuning <id>`, `/revert-tuning <param>`, `/tuning` shows active overrides + pending.
- Journal: `tuning_proposed`, `tuning_applied`, `tuning_rejected`, `tuning_reverted`.
- Strategy rules changed: NO.

## v2.6.0 — 2026-10-13  (MINOR: nightly review)
- **`aureon_mt5/review.py`** (deterministic, no LLM): after the EOD review, scores the last 20 trading days from journal:
  entries and exits by session (Asia/London/NY) × actor (human/agent): n, win%, net pts, stops. Protect-stop hit rate per
  session, "secured then closed early" pts left on the table, chop vs clear, missed vs overridden.
- **Pattern flags** with fixed thresholds: e.g. "protect stop hit 58% in Asia (n=19) vs 31% overall" → `pre_stop`;
  "early close left avg +6.2 pts after +10 secure" → `ride_step`. Each flag names the parameter it concerns.
- One card `🔍 NIGHTLY REVIEW` with summary + per-session metrics + top 5 flags. Full results to
  `logs/review/<date>.json`. `/review` shows the latest.
- Strategy rules changed: NO.

## v2.5.3 — 2026-10-13  (PATCH: Codex end-of-day review)
- **`aureon_mt5/eod_review.py`**: at `AUREON_EOD_AT` (default 23:15 IST) on trading days, builds a day pack from journal
  (signals, decisions, orders, guardian actions, misses, governor blocks, costs, chop) and runs `codex exec` with a read-only
  prompt: what went well, what cost points, rule violations, three observations. NO parameter changes, NO code edits.
- Output: schema-validated JSON `{summary, wins[], costs[], violations[], observations[]}`; posted as one card
  `📓 EOD REVIEW · Tue 13 Oct`. Invalid/timeout → card "EOD review unavailable (reason)".
- `/eod [date]` runs on demand. Dedupe per date. Budget respected like other Codex calls.
- Strategy rules changed: NO.

## v2.5.2 — 2026-10-13  (PATCH: ema2050 PRE-CROSS heads-up + p_win floor)
- **ema2050 PRE-CROSS detection** (`strategies/ema2050/journeys.py`): ported from ema5080's P logic using EMA 20/50.
  CrossEvent label `"PRE"` emitted when: price closes through EMA20 for 2 bars, gap shrinking, gap <= 1.5× ATR, lines
  were separated within 24 bars. One PRE per approach, re-arms after a cross or cooldown. PRE never creates a trade.
- **Agent card**: `notify.send` (NOT ask — no TAKE buttons, no track_ask, no missed-setup, no AGENT entry). Lines: "EMA 20 is
  closing on EMA 50 — a LONG cross may form · not a trade". Journal `pre_cross`.
- **`AUREON_PRE_ALERT_2050=1`** (default on); `AUREON_PWIN_FLOOR=0` (off by default, for v2.5.2 AGENT mode integration).
  `AUREON_PWIN_WAIT_S=15`.
- Strategy rules changed: NO — notification only.

## v2.5.1 — 2026-10-13  (PATCH: advisors as p_win rankers + calibration)
- **Advisor output schema**: entry events now accept ranker mode — `p_win` (0–1), `rationale` (<=200 chars), `risks` (short strings).
  `decision` (TAKE/SKIP) is optional for entry events; when absent, `decision="RANK"`. Pullback HOLD/TIGHTEN/CLOSE unchanged.
  Invalid/timeout → `p_win` None (fail closed = no ranking, never a block). Old verdict records still load.
- **`validate()`** in `claude_cli.py` updated: accepts `rationale` and `risks` fields; `confidence` defaults to "medium" when absent.
  No advisor can block, skip, or delay an entry — they only rank.
- **`aureon_mt5/calibration.py`**: `calibrate(rows, source, min_n)` → `CalibResult(useful, top_half_avg, bottom_half_avg, buckets, brier)`.
  "Useful" = top half beats bottom half on avg NET pts with n >= `AUREON_CALIB_MIN_N` (default 30). Stored in
  `logs/advisor_calibration.json`. `is_useful(log_dir, source)` for downstream use (v2.5.2).
- `AUREON_CALIB_MIN_N=30`.
- Strategy rules changed: NO.

## v2.5.0 — 2026-10-13  (MINOR: context packer)
- **`aureon_mt5/context_pack.py`** (pure): `pack(agent, df, signal, journal) -> dict` + `to_prompt(dict) -> str`.
  Contents: last N closed bars (AUREON_CTX_BARS default 48: close, ema fast/slow), cross/morning counts, chop flag, session,
  minutes to/since news, HTF bias, spread, today's trades/results, last 10 closed trades, governor state, control mode,
  position data (entry, SL, secured, soft_lock, peak, MAE). Fixed key order, rounded numbers, deterministic truncation order
  (bars dropped first) to fit AUREON_CTX_MAX_CHARS (default 6000).
- **`build_snapshot`** in `claude_advisor.py` kept as backwards-compat wrapper (comment directs new callers to `context_pack.pack`).
- Strategy rules changed: NO.

## v2.4.5 — 2026-10-13  (PATCH: soft early lock)
- **Soft early lock** (`AUREON_EARLY_LOCK_MODE=soft`, default): when peak >= `early_at`, do NOT move the broker SL. Instead set
  `st["soft_lock"]` and close only when a NEW CLOSED M5 bar's close is at or beyond `entry + level` against the trade. Intrabar
  wicks never trigger it. Broker SL remains at `−pre_stop` until the SECURE step moves it. Card: "EARLY LOCK (soft) +1".
- **Hard mode** (`AUREON_EARLY_LOCK_MODE=hard`): today's behaviour unchanged — broker SL moved on peak trigger.
- **Off mode** (`AUREON_EARLY_LOCK_MODE=off`): no early lock at all.
- **State separation**: `st["secured"]` = locked by broker SL. `st["soft_lock"]` = soft level. Once SECURE moves the real SL above
  the soft level, `soft_lock` is cleared (journaled). `_locked(p)` and "live SL is the truth" don't touch soft_lock.
- **Soft lock exit**: `soft_lock_exit` journal event. Reason key `soft_early_lock` in guardian close.
- **Restart**: `soft_lock` persisted in `guardian_state.json`, restored on restart.
- `AUREON_SOFT_FALLBACK_MIN=5` (safety fallback when bot can't watch — deferred to a follow-up).
- Strategy rules changed: **YES** — early lock is now bar-close based by default; hard mode keeps the old rule.

## v2.4.4 — 2026-10-13  (PATCH: override / AGENT dry-run harness)
- **`scripts/rehearse.py`**: scripted rehearsal against fake MT5 + console notifier. Scenarios: missed → /override placed,
  override refused (drift), AGENT window → agent takes, HALT mid-trade, governor blocks. Pass/fail table.
- **`tests/test_v244_transitions.py`**: parametrised pytest coverage for EVERY control-mode transition (to_agent, to_manual,
  timeout, HALT, disconnect, restart preserve/drop) and EVERY governor rule rejection (session_window, news_block, max_trades_day,
  daily_loss_cap, one_position_per_direction, halted) with edge cases. Rehearse script runs as a test.
- Strategy rules changed: NO.

## v2.4.3 — 2026-10-12  (PATCH: scheduled AGENT windows)
- **`aureon_mt5/schedule.py`**: `/agent-schedule add <symbol> HH:MM-HH:MM [days=Mon-Fri]`, `list`, `remove <id>`.
  Stored in `logs/agent_schedule.json`. Windows inside 23:00–05:30 IST rejected at creation. Overlaps merge
  (the later window extends the AGENT until). Today-only windows cleaned up at end of day.
- **Scheduler**: `tick()` from the supervisor loop. At window start → MANUAL→AGENT (reason "schedule <id>", until = window end).
  At window end → AGENT→MANUAL. HALT suspends all windows. Restart mid-window: if now is inside a window, enter AGENT
  with remaining time.
- **`/manual`** during a window: ends the window for today only (the agent entry from the schedule won't re-trigger until
  the scheduler sees the window has passed).
- Strategy rules changed: NO.

## v2.4.2 — 2026-10-12  (PATCH: AGENT entry executor)
- **Autonomous entry in AGENT mode**: when a symbol is in AGENT mode and a confirmed entry signal posts its ask
  (ENTER / RE-ENTRY / P / CROSS), after `AUREON_AGENT_DELAY_S` (default 20 s, gives the human first refusal) and
  if the ask is still unanswered: `execute_entry(actor="agent")` — the SAME path as buttons: governor, HALT, demo/live gate,
  order → confirm → guardian state → journal → Discord. Card Decision edited `🤖 AGENT TOOK LONG 0.85 lots`.
- **Sizing**: the card's risk-based suggestion (equity × risk_pct / stop, streak/chop half applied), capped by max_lots.
- **Human wins**: a SKIP before the delay → agent does nothing; a button press → human wins (double-tap guard + `decided` flag).
- **Journal**: `agent_entry` event with `actor="agent"`, `rationale="rule: <signal kind> confirmed"`, `p_win` from advisor if present.
  `order_placed` uses `actor="agent"` when placed by the agent.
- **Missed-setup detector**: skips asks the agent took (`placed=True` in mark_ask_decided).
- `AUREON_AGENT_DELAY_S=20`. `agent_executor.py`: `schedule_agent_entry` / `maybe_schedule`.
  `track_ask` calls `maybe_schedule` if in AGENT mode.
- Strategy rules changed: NO.

## v2.4.1 — 2026-10-12  (PATCH: control-mode state machine)
- **`aureon_mt5/control.py`** (pure): transitions MANUAL→AGENT (`/agent-on [symbol] [minutes]`, default `AUREON_AGENT_DEFAULT_MIN=60`),
  AGENT→MANUAL (`/manual`, timeout, HALT, governor daily-loss-cap hit, MT5 disconnect > 5 min). Every transition: guardian_state saved,
  journal `control_mode` {from, to, by, reason, until}, one card.
- **`/agent-on`** and **`/manual`** commands in bot.py.
- **`/status`**, **`/parallel-status`**: show mode per symbol with time left (`🤖 AGENT (42m left)` / `🧑 MANUAL`).
- **Auto-transitions**: `check_auto_transitions()` called every supervisor loop iteration for each symbol.
  AGENT→MANUAL forced by: timeout (`until` expired), HALT active, governor `daily_loss_cap` hit, MT5 disconnect incident > 5 min.
- **`/override`** remains a one-shot agent action — does NOT flip the mode (documented in AGENTIC_SPEC.md).
- Still NO autonomous entry in this version — AGENT mode only changes labels and arms the state. v2.4.2 adds the executor.
- Strategy rules changed: NO.

## v2.4.0 — 2026-10-12  (MINOR: agentic foundation)
- **`docs/AGENTIC_SPEC.md`**: control modes MANUAL (default) and AGENT; /override semantics (one-shot, not a mode flip);
  NON-OVERRIDABLE invariants table (SL never loosened, governor, session window, news, HALT, demo-only, max lots) with the
  file/function that enforces each.
- **`control_mode`** per symbol in `guardian_state.json` (top-level `"control": {SYMBOL: {mode, since, by, until}}`).
  Default MANUAL. `journal.load_control(symbol)` / `journal.save_control(symbol, mode, by, until)`.
  Loaded at start, saved atomically, restart-safe. Not yet switchable (v2.4.1 adds commands).
- **Journal `actor` field**: `journal.decision(event, actor=, rationale=, p_win=, ...)` helper. Migrated call sites:
  `decision`, `alert_decision`, `order_placed`, `governor_block`, `halt`, `unhalt`. Actor values: `"human"` (button),
  `"agent"` (v2.4.2+), `"model"` (future), `"system"` (guardian/detector). Old journals load with actor inferred from
  event type (button events → human, system events → system).
- No behaviour change for MANUAL users.
- Strategy rules changed: NO.

## v2.3.16 — 2026-10-12  (PATCH: voice command scope)
- **`aureon_mt5/voice.py`**: deterministic keyword/grammar intent parser (`parse_intent(transcript) -> ParsedIntent`).
  Intents: status, alerts, plan (read-only, execute immediately), take, skip, alert, halt, unhalt, override
  (order-affecting, require typed confirmation). No LLM for intent parsing.
- **Confirmation system** (`ConfirmationManager`): order-affecting intents from voice create a challenge
  `"Reply CONFIRM 7F3 within 60 s to take LONG 1.0 XAUUSD"`. Only a typed message with the exact code executes,
  through the same bot functions. Governor, demo/live gate, halt all apply. Expired/mismatched → nothing happens.
- **`aureon_mt5/voice_transport.py`**: `VoiceHandler` handles audio (transcribe → parse → execute/challenge) and
  typed confirmation. Telegram bot + HTTP endpoint are transport options (both optional).
- **Transcription**: whisper/faster-whisper (optional dep). `transcribe(audio_path)` returns text or None when not installed.
  Audio is never stored after transcription.
- **Journal**: every voice command logged (transcript, intent, confirmed or not, result).
- `AUREON_VOICE=0` default, `AUREON_VOICE_CHATS`, `AUREON_VOICE_SECRET`.
- **Difference from docs/VOICE_SCOPE.md**: the doc lists candidate intents and says "NOT IMPLEMENTED". This version implements
  them with the doc's safety rule: "Never execute an order solely from speech — require typed confirmation."
- Strategy rules changed: NO.

## v2.3.15 — 2026-10-12  (PATCH: daily plan card 05:00 IST)
- **`aureon_mt5/daily_plan.py`**: at 05:00 IST every weekday (Mon–Fri), one card `🗓 DAILY PLAN · Tue 13 Oct` with:
  News (today's high-impact events + entry blocks), Sessions (IST windows), Yesterday (net pts, trades, win/loss),
  Week to date vs goal (`AUREON_WEEK_GOAL_PTS`, default 0 = hidden), Chop flag, Governor state + HALT.
- Dedupe key `plan:<date>` (restart-safe). `/plan` posts on demand. Journal `daily_plan`.
- Runs from the supervisor thread. Skips weekends.
- `AUREON_DAILY_PLAN=1` (default on), `AUREON_DAILY_PLAN_AT=05:00`, `AUREON_WEEK_GOAL_PTS=0`.
- Strategy rules changed: NO.

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
