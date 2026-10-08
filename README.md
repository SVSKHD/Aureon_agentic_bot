# Aureon MT5 v1.9.8

You place the trade. Aureon detects (per selected EMA mode), fires a Discord gunshot, then manages what you placed:
protect → secure +10 → ride in +5 steps → close on the fast-EMA turn → news safeguard. Reports and slash commands.

## Modes — two INDEPENDENT systems
| mode | status | what runs |
|---|---|---|
| `ema5080` (default) | ACTIVE | EMA 50/80 detector + Gold guardian profile |
| `ema2050` | FROZEN | the original EMA 20/50 implementation, signals only (no live guardian profile) |

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
