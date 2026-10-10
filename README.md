# Aureon MT5 v2.0.0

You place the trade. Aureon detects (per selected EMA mode), fires a Discord gunshot, then manages what you placed:
protect → secure +10 → ride in +5 steps → close on the fast-EMA turn → news safeguard. Reports and slash commands.

## Modes — two INDEPENDENT systems
| mode | status | what runs |
|---|---|---|
| `ema5080` (default) | ACTIVE | EMA 50/80 detector + Gold guardian profile (unchanged in v2.0.0) |
| `ema2050` | ACTIVE (v2.0.0) | EMA 20/50: confirmed cross (3 bars + 1.5 gap) + pullback entry, own Gold guardian profile (−12 / +3→+1 / +10 / ride +5) |

Nothing is ported between them. Resolution: `--mode` → `AUREON_MODE` → `ema5080`. Aliases `5080`, `2050`.
The mode is fixed for the life of the process; a change needs a restart. Positions managed under another mode are kept
protected (SL never loosened) but receive no strategy rules; a `⚠️ MODE MISMATCH` warning is posted.

## Run
    pip install -r requirements.txt
    set DISCORD_WEBHOOK=...            set DISCORD_TOKEN=...   (commands)
    python main.py --mode ema5080 --symbols XAUUSD            # gold, live (demo account first!)
    python main.py --dry --mode ema5080 --symbols XAUUSD      # synthetic bars, no MT5 modifications
    python main.py --mode ema5080 --symbols XAUUSD,XAGUSD --enable-silver   # silver guardian is EXPERIMENTAL; off unless enabled

## Verify
    python detect.py --mode ema5080 --date 2026-10-05         # every P / cross / skip of the day: raw ts, server, IST, EMAs
    python detect.py --mode ema5080 --live                     # per closed bar: close, EMAs, P, cross, whipsaw, news, entry allowed
    python run.py   --mode ema5080 --date 2026-10-05 --news news_blackout.txt   # static replay: PNG, HTML, JSON, per-trade console
    python -m pytest -q tests

## EMA 20/50 (v2.0.0 — see strategies/ema2050/journeys.py Rules / GUARDIANS; researched Jul–Oct 2026 + Jun–Dec 2025 out of sample)
cross = sign change of EMA20 − EMA50 on a closed bar · **confirmed** when the order has held ≥ 3 bars AND |gap| ≥ 1.5 pts, within 18 bars
(first bar that satisfies both); a flip before that = **multi cross**, no trade · **entry = pullback**: price touches EMA20 within 1.5
(touches between cross and confirm → enter at the confirm bar; else the first touch after confirm, up to confirm+12; fallback at confirm+12
only if |close − EMA20| ≤ 5 and the order holds; never chase beyond 5) · no entries server 21:00–23:59 nor outside 05:30–23:00 IST ·
news block 60/30 (NFP auto + `news_blackout.txt`) · one trade per cross · pre-cross shoot (40 bars) reported, not filtered.
Not added (tested, failed 2025): trend/hold/slope/EMA200/re-entry/extension filters.

Guardian (ema2050, Gold profile): no SL → **−12** (`🛡 PROTECTED`) · **+3 seen → SL entry +1** (`EARLY LOCK +1`, never loosened) ·
+10 → SL +10 (announced when MT5 confirms) · ride +5 steps (2 pts air) · **no EMA50 follow-SL** (tested: wicked on normal bounces) ·
close on the **EMA20 turn once secured** · exit at the next **confirmed** opposite cross (`🔁 OPPOSITE CROSS`) · no P phase (timeout /
pre-stop / separation paths are skipped) · news safeguard as 50/80 · live SL is the truth, SL never loosened, `respect_manual_sl` as configured.
Cards: `CROSS LONG|SHORT (unconfirmed)` · `CROSS CONFIRMED` · `MULTI CROSS (ignored)` · `ENTER LONG|SHORT (pullback | no-pullback)` (TAKE/SKIP) ·
`NO ENTRY (too far from EMA20 | no-entry hour | news)` · then PROTECTED / EARLY LOCK / SECURED / RIDE / CLOSED. Every card: cross time,
confirm time, pre-cross shoot, session, EMA20/50, bars since cross.

Replay / verify:
    python run.py --mode ema2050 --date 2026-10-07                              # trades + events + the research block for the day
    python run.py --mode ema2050 --from 2026-07-01 --to 2026-10-09 --lot 1       # day by day (entries inside the server day, open trades
                                                                                #  closed at the day end) + research summary + month table
    python detect.py --mode ema2050 --date 2026-10-07 | --live                   # per bar: EMAs, cross, confirm state, touch, entry allowed, reason
Replay SL convention (same as the 50/80 replay): a level armed by bar k's extreme is tested from bar k+1. Exit reasons: stop · early ·
secured · ema20_turn · opposite_cross · news_flat · day_end · open.

## EMA 50/80 (unchanged thresholds — see strategies/ema5080/journeys.py RULES)
P: price closes through EMA 50 against the 50/80 order for 2 bars · gap ≤ 2× avg range · gap ≥ 1.5× range within 24 bars ·
shrinking 2 of 3 · no slope test. Whipsaw zone (≥3 flips in 12 bars): no P; cross confirmed by next bar beyond both lines.
Cross entry at bar close, no extension cap. No entries 23:00–05:30 IST. News block 60/30 min (NFP auto + news_blackout.txt).

## Guardian (ema5080, Gold profile)
no SL → −6 (`🛡 PROTECTED`, never replaces an existing SL) · P phase: −6 stop, 12-bar timeout, separation abort (`pre_abort`) ·
opposite P → close (`🔁 FLIP CLOSE`; the new trade is yours to place) · cross confirmed → SL to EMA 80 − 1.0 buffer
(configurable `ema_slow_sl_buffer`) · **+10 → SL entry+10, announced only when MT5 confirms** · ride +5 steps (2 pts air),
state updated immediately · close on EMA 50 turn once secured · news: bank in profit / cap loss.
Invariants: live SL is the truth (state rebuilt every poll, restart-safe) · SL never loosened (`ALREADY_BETTER` is fine) ·
rejected modifications → `❌ AUREON GUARDIAN FAILURE` with retcode, retried, state unchanged.

## Commands
`/alert <price> [symbol] [note]` · `/alerts` · `/alert-cancel <id>` · `/alert-clear [symbol]` (v2.0.0, see below) · `/claude-rules` · `/claude-rules-approve <n>` ·
`/status` (version, mode, market, tick age, MT5, agent, guardian, position phase/SL/secured/peak, IST-day counters, last broker action) ·
`/parallel-status` · `/agents` (components ✅/❌) · `/symbols` (mode, profile) · `/symbol-present` · `/market` · `/report [current]` (mode, signals by kind, secure steps, exits).
Every command is acknowledged at once and answered by a followup. Status commands read a per-symbol snapshot the agent refreshes
each poll ("as of N s ago"), so a busy MT5 never delays them. `/status` and `/agents` also show event-loop lag and gateway latency.

## Files
`logs/journal.jsonl` (events: signal/secured/exit/closed/error, with `signal_kind`), `logs/guardian_state.json` (per symbol, with mode),
`logs/aureon.log` (tracebacks). Telemetry dedupes the same failure to one Discord post per 5 minutes.

## v1.8.2 — parallel safety & runtime hardening
- One shared process lock around every direct MT5 API call (rates, ticks, positions, SL/close, history, symbol info);
  strategy loops stay independent — only the API call is locked.
- Discord dedupe keys are symbol-aware: `mode:symbol:event:ticket|bar` (P/CROSS/PROTECTED/SECURED/CLOSED/NEWS/ERROR/MARKET). Gold never suppresses Silver.
- Order of operations everywhere: MT5 action → confirm result → guardian state → journal → Discord. Discord failures are logged and ignored.
- Post-cross EMA 80 follow-SL now goes through the same safe `_move_sl` path (retcode, telemetry, retry, never-loosen).
- Shared helpers moved to `aureon_mt5/common/` (`source`, `news`, `timeutil`); strategy rules stay inside their packages.
- `/parallel-status`: one glanceable line per symbol (`XAUUSD | EMA 50/80 | RUNNING | LONG #… · +15 secured | … | ✅`).
  One symbol's error never marks another unhealthy. Supervisor restarts only the dead agent.
- Tests added: MT5 lock, symbol-aware dedupe, Discord isolation, XAU/XAG state concurrency (200×2 threaded saves, atomic),
  follow-SL telemetry path, agent failure isolation, supervisor restart scope. 22 tests pass.
- Time alignment check: `python detect.py --mode ema5080 --live` prints raw MT5 bar time, server time and IST per bar — compare one candle with MT5 before trusting signals (expected IST = server + 2h30 on the current broker).
Validation order: `detect.py --live` → `main.py --mode ema5080 --symbols XAUUSD` → `main.py --mode ema5080 --symbols XAUUSD,XAGUSD` (silver guardian stays off without `--enable-silver`).

## v1.8.3 — Discord delivery
Dedupe keys are committed only after a successful webhook post (HTTP 2xx). A failed post is retried on the next poll;
after 20 failed attempts the key is committed with a log line so a dead webhook cannot loop forever. Console mode
(no webhook) counts as delivered. Guardian ordering (MT5 → state → journal → Discord) is unchanged. Version strings aligned.

## Claude add-on (v1.10.0)
Aureon stays the data layer, the rules, and the only thing that talks to MT5 and Discord. Claude is a second opinion,
called as a local program (`claude -p … --output-format json`) with your Claude Code login — no API key, no SDK,
no HTTP calls from Aureon. **You place every trade. Claude never places a trade.**

| `AUREON_CLAUDE` | what happens |
|---|---|
| `off` (default) | no Claude calls at all — v1.9.x behaviour |
| `review` | 23:00 IST `CLAUDE REVIEW` card of the day's journal |
| `advisory` | review + Claude's TAKE/SKIP on P / CROSS / RE cards + `CLAUDE PULLBACK VERDICT` cards; nothing applied |
| `manage` | advisory + pullback TIGHTEN and CLOSE-in-profit applied to your open trades |

When it is asked: a P PRE-CROSS, CONFIRMED CROSS or RE-ENTRY at bar close (entry model) · a pullback in an open trade —
trend state PULLBACK / WEAKENING / CHALLENGED, profit ≤ 60% of a peak ≥ +5, or a SHOULD WE CLOSE? card — once per
pullback, re-armed after a new peak (pullback model) · 23:00 IST (review model). ema5080 only.

Safety: the poll never waits (one background worker, one call at a time, timeout) · fail closed — timeout, bad JSON,
wrong decision, auth error, budget used or a verdict after the next bar closed (STALE) are never acted on · TIGHTEN goes
through the guardian's `_move_sl` (never loosens, announced after MT5 confirms) · CLOSE only in `manage` and only in profit,
after the guardian's own exits · snapshots carry market data only (no account, balance, login, webhook, token) ·
`ANTHROPIC_API_KEY` is removed from the subprocess environment and a warning card is posted if it is set.

Commands: `/claude` (mode, login, calls today/limit, last verdict, avg latency) · `/claude-test` · `/claude-review [YYYY-MM-DD]`.
Journal: `claude_verdict` (`claude_event, symbol, model, latency_s, decision, side, confidence, reason, my_action, acted, stale`,
`source=claude`). `/report` has a Claude section (TAKE vs SKIP outcomes, you vs Claude, pullback verdicts vs the guardian).
`reports.claude_rows_for_batch()` returns the rows for the Saturday `aureon_decisions` batch (`source='claude'`).
Rules prompt: `prompts/claude_rules.md`. Daily budget: `logs/claude_budget.json`.

Check on your PC: `claude -p "Reply only: OK"` from `C:\aureon_claude` prints OK → `AUREON_CLAUDE=review` → `/claude-test`.

## Weekly compare (v1.11.0) — Detector vs Claude vs Me
Measurement only: nothing here changes detection, the guardian, Claude calls or trades.

Every detector signal (P / CROSS / RE, ema5080) in the window is one row. The same rows are split into groups:
Detector · all · Claude TAKE · Claude SKIP (what Claude avoided) · Me TAKEN · Me SKIPPED · the two disagreement groups.
- **Points** are the scorecard's if-taken result (`reports.scorecard`, read through `reports.graded_signals`). The compare never
  grades a signal itself. OPEN and UNGRADED signals are counted, not scored.
- **No verdict** (Claude off, timeout, bad JSON, budget, STALE) is its own count and is never treated as TAKE or SKIP.
- **Me · TAKEN** = your TAKE button, or a trade the guardian saw in MT5 on the same side within 6 bars of the signal.
- **Metrics:** n · win rate · expectancy (points per signal) · total · STOP count · worst losing streak · biggest loss.
- **Verdict:** under 30 Claude-graded signals it only says "Not enough data yet". After that: Claude helps (its skips lost,
  its takes beat the detector and kept ≥80% of the points) · Claude is skipping good trades · Mixed. Plus your own filtering line and
  "when we disagreed, Claude right X, you right Y".
- **Pullbacks:** Claude's HOLD / TIGHTEN / CLOSE vs what actually happened. Applied (`manage`) verdicts are counted separately.

Commands: `/compare [days] [private]` · `/compare-breakdown [days]` (by P / CROSS / RE and Asia / London / New York).
Saturday at `AUREON_WEEKLY_IST` (10:00 IST): the COMPARE card for Mon–Fri, once per week (restart-safe), with a chart of cumulative
points and the last 4 weeks' expectancy. Footer: models used that week and the `claude_rules.md` hash.
History: `logs/compare_weekly.jsonl`. For the Saturday Supabase batch, `compare.latest_summary(log_dir)` returns the `compare`
field for the `aureon_weekly` row (no extra request). Optional `AUREON_COMPARE_CLAUDE_REVIEW=1`: Claude's 5-line "what I got wrong"
card after the COMPARE card (counts toward the budget; never edits `claude_rules.md`).

## Price alerts (v2.0.0) — `/alert`, both modes
`/alert 4120 [XAUUSD] [note]` arms an alert (`logs/alerts.json`, restart-safe; `side_hint` from below / from above by the current price) and
answers at once: `ALERT armed · XAUUSD 4120.00 · current 4132.40 · 12.4 pts away`. `/alerts` lists armed alerts with the distance,
`/alert-cancel <id>`, `/alert-clear`. Checked every poll on the guardian's tick (ask for an approach from below, bid from above); fires **once**;
never on a stale tick (> `heartbeat_stale_min`) or a closed market.
**🔔 ALERT REACHED** card (dedupe `alert:<symbol>:<id>`): hit price, server + IST time, approach, bars since set · EMA behaviour (fast/slow values,
gap and its 6-bar trend, fast-EMA 4-bar slope, where price sits, last cross: side/time/confirmed/bars ago/multi) · trend behaviour
(`ema5080/trend.py` state machine on the mode's lines, slow-EMA 10-bar slope, pre-cross shoot, session, ATR20, crosses today) · guardian
context (position or flat, entry window, news block, daily counters) · **SUGGEST** line = the mode's own entry verdict for the last closed bar
(`common/verdict.py`, the same function `detect.py` prints — never a new rule) with the SL the guardian would set (−12 ema2050, −6 ema5080) ·
Claude add-on line when `AUREON_CLAUDE` ≠ off (entry model, counts toward the budget, silent when used).
Buttons `[LONG] [SHORT] [SKIP]` (TTL `ask_ttl_min`): LONG/SHORT journal `alert_decision {alert_id, side, suggested_side, agreed}`; with
`AUREON_EXECUTION=1` on a demo account (or `AUREON_ALLOW_LIVE=1`) the market order is placed through `broker.place_market` with the guardian's SL,
otherwise "noted — place it in MT5, I will manage it" and the next position seen on the symbol carries the alert id. SKIP journals `side=skip`;
expired cards are edited, nothing journaled. `/report` and the Saturday COMPARE card get an **Alerts** section (fired, taken, agreed, win/loss).

## Claude add-on (v2.0.0 additions)
Advises **both modes**. ema2050 is asked at the ENTER bar (confirmed cross + pullback touch) and on pullbacks in a trade only — never at a raw
CROSS or a MULTI CROSS. Snapshots add `crosses_today, bars_since_cross, confirm_state, dist_to_fast_ema_pts, swing_against_last6_pts, atr20,
day_pnl_pts, trades_today` and, for positions, `mae_so_far, retrace_from_peak_pts, retrace_pct, bars_since_peak, closed_through_slow_ema, dist_to_sl`
(still no account data). The reply contract adds `"evidence": [field names used]`, journaled with every verdict together with the compact
snapshot fields. The prompt is mode-aware (first line names the fast/slow EMA; `prompts/claude_rules.md` has one section per mode — a
placeholder until `research/claude_rules_v2.md` is supplied). Saturday, after the COMPARE card: **CLAUDE RULE PROPOSALS** — wrong calls and their
snapshot fields → up to 3 proposed rule lines with the supporting count (`logs/claude_rule_proposals.json`). `/claude-rules` lists them;
`/claude-rules-approve <n>` appends the line, dated, to `claude_rules.md`. Nothing changes without approval.
