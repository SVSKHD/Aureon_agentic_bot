# Aureon MT5 v2.16.0

You place the trade. Aureon detects (per selected EMA mode), fires a Discord gunshot, then manages what you placed:
protect → secure +10 → ride in +5 steps → close on the fast-EMA turn → news safeguard. Reports and slash commands.

## Modes — two INDEPENDENT systems
| mode | status | what runs |
|---|---|---|
| `ema5080` (default) | ACTIVE | EMA 50/80 detector + Gold guardian profile (unchanged in v2.0.0) |
| `ema2050` | ACTIVE (v2.0.0) | EMA 20/50: confirmed cross (3 bars + 1.5 gap) + pullback entry, own Gold guardian profile (−12 / +3→+1 / +10 / ride +5) |
| `ema921` | ACTIVE (v2.16.0) | EMA 9/21 on **M30**, **mechanical**: every cross taken at the bar close · broker SL −30 · +20 → SL +15 · exit on the first close through EMA 9 (or EMA 200 after +20) · Friday flat · drawdown guard |
| `ema9200` | ACTIVE (v2.15.0) | EMA 9/200 on **M30**: candidates only (pullback to EMA9 with the bias / momentum bar) → TAKE / SKIP, own Gold guardian profile (−30 / +40→+30 / ride +10 / EMA9 turn / Friday flat) |

Nothing is ported between them. Resolution: `--mode` → `AUREON_MODE` → `ema5080`. Aliases `5080`, `2050`, `9200`, `921`.
The mode is fixed for the life of the process; a change needs a restart. Positions managed under another mode are kept
protected (SL never loosened) but receive no strategy rules; a `⚠️ MODE MISMATCH` warning is posted.

## Run
    pip install -r requirements.txt
    set DISCORD_WEBHOOK=...            set DISCORD_TOKEN=...   (commands)
    python main.py --mode ema5080 --symbols XAUUSD            # gold, live (demo account first!)
    python main.py --dry --mode ema5080 --symbols XAUUSD      # synthetic bars, no MT5 modifications
    python main.py --mode ema5080 --symbols XAUUSD,XAGUSD --enable-silver   # silver guardian is EXPERIMENTAL; off unless enabled

Production: run it under the supervisor (auto-restart, hang kill, boot start) — **[docs/SERVICE.md](docs/SERVICE.md)**:

    powershell -ExecutionPolicy Bypass -File .\scripts\aureon_service.ps1 -Action install

## v2.16.0 — `ema921`: the cross is the order

Researched Jan–Oct 2026 on XAUUSD M30 (43 weeks; Jan–Apr held out, never used for tuning). Fully mechanical — no asks.

    AUREON_MODE=ema921
    AUREON_EXEC_TF=M30
    AUREON_EXECUTION=1 AUREON_AUTO=1      # both needed before an order is placed; demo, or AUREON_ALLOW_LIVE=1
    AUREON_PAPER=1                        # first week: real feed, real cards, fills journaled only
    AUREON_MAX_DD_USD=7000                # no new entries once equity is this far below its peak (your funded-account rule)
    AUREON_PRE_CARDS=1                    # 0 silences the pre-cross cards

| piece | rule |
|---|---|
| entry | every EMA 9/21 cross, market order at the bar close |
| broker SL | entry −30 (safety net; the exit rule is the real stop) · no TP (every cap tested cost money) |
| lock | once +20 has printed (checked at the bar close) → broker SL to entry +15 |
| exit | first M30 bar closing through EMA 9 against the trade (`exit_mode=e9`) · or EMA 9 until +20, then EMA 200 (`e9>e200`) |
| days / hours | Mon–Fri · no entries Friday from 16:00 server · news 60/30 · flat at Friday's last bar |
| pre-cross | EMA 9 within 0.35 ATR of EMA 21 and converging 2 bars → informational card, no action |
| cards | PRE · ENTER (with SL/TP/lock/exit lines and the backtest reference) · 🔒 LOCK · CLOSED (reason + points) |

Backtest, 1 lot, lock armed at the bar close, $0.20 spread: May–Oct **+$53k** (16 / 10 weeks, worst −$8.2k, max DD −$17.5k);
Jan–Oct (your server run, `backtest.py`) **+$54k**, Jan–Apr unseen +$1.4k, **max DD −$28k**. Size the lot from the drawdown:
`lot ≈ (allowed max DD ÷ 3) ÷ $28,000` → 0.24 per $100k on a 10 % rule.

    python tools/replay_921.py --csv xauusd_m30_20260101_20261021.csv --from 2026-01-01
    python tools/replay_921.py --csv ... --set exit_mode=e9>e200 --set stop=30

Journal rows: `pre_cross`, `signal`, `cross_skipped` (why), `order_placed` / `paper_fill`, `locked`, `closed_by_rule`.

**The order gate.** An order is sent only when every condition holds; anything else posts a SIGNAL card saying which one
failed:
- `AUREON_EXECUTION=1` and `AUREON_AUTO=1`;
- a demo account, or `AUREON_ALLOW_LIVE=1` (an account type that can't be read fails closed);
- not `AUREON_PAPER=1`;
- no position on the symbol (a paper position counts in paper mode);
- the drawdown guard is clear.

**The lock is armed at the bar close, never intrabar.** This is the research assumption. On each M30 close, if +20 has
printed:
- the close is ≥ +15 → the broker SL goes to entry +15;
- the close is below +15 → out at the close (`lock_close`).

After a restart the lock is rebuilt from the live SL, and nothing is re-sent. A broker SL or lock exit posts a CLOSED card,
never "CLOSED BY YOU".

**Paper mode** (`AUREON_PAPER=1`): `paper_fill` rows carry `side lots price sl tp bar`. No `order_send` is reached. The paper
tracker (`aureon_mt5/ema921_live.py`) runs the same guardian on paper positions, one closed bar at a time:
- order: SL on the bar's low/high, TP, peak, then the bar-close lock / exit line / Friday flat;
- output: the same 🔒 LOCK and CLOSED cards and `closed_by_rule` rows as live, with `paper: true`;
- a test checks it matches the replay trade for trade.

Paper positions persist in `logs/state_<SYMBOL>.json`, and block a new cross while open.

**Drawdown guard** (`AUREON_MAX_DD_USD`): the equity peak is kept in `logs/state_<SYMBOL>.json`, so a restart never resets it to
the current equity. In paper mode the equity is the account equity when paper trading started plus the paper P&L.

**The week card** is posted after the Friday flat bar, once per week, and on `/week`. It shows trades, won, net points, $ at
the traded lots, best / worst, the drawdown used vs `AUREON_MAX_DD_USD`, and the backtest reference (+11 pts / week, 58% of
weeks positive).

**Live vs the replay:**
- **Friday flat:** live flattens at the close of the first Friday bar opening at or after 22:00 server. The replay uses the
  last Friday bar in the data, which can be later; live can't trade after the market closes.
- **Exit bar:** when the broker SL fills intrabar, live sees no position at that bar's close and can take a cross on the
  same bar. The replay counts that bar as "in position".

**First-class mode:** `/mode ema921`, `ops.write_mode`, the Claude snapshot EMA names (`ema9` / `ema21`), the alias docs. The
`/alert` context uses the ema921 day / Friday / news rule; it raised `AttributeError` the same way ema9200 did.

## v2.15.0 — `ema9200`: you decide, Aureon executes

Researched on XAUUSD M30, Apr–Oct 2026 (26 weeks) plus three blind replay weeks traded by hand. Every mechanical version of
these entries loses over 26 weeks; the hand-picked subset won all three weeks. So this mode never takes a trade on its own:
it finds **candidates**, posts each once with the context the research widget showed, and you answer TAKE / SKIP.

    AUREON_MODE=ema9200
    AUREON_EXEC_TF=M30          # the research timeframe — the rules are M30 bar counts
    AUREON_ASK_TTL_MIN=30       # an ask lives until the next M30 close, then EXPIRED (= skip)

| piece | rule |
|---|---|
| bias | sign(EMA 9 − EMA 200) on the closed M30 bar |
| candidate · pullback | close within ±8 of EMA 9 on the bias side, bar reached back to EMA 9 ± 8 |
| candidate · momentum | bar body ≥ 30 in the bias direction, close beyond EMA 9 |
| one ask per cluster | cooldown 4 bars, re-armed after $10 of travel |
| quiet | 21:00–24:00 server · Friday from 17:00 server · news 60/30 |
| card | side · setup · price · EMA 9 (distance) · EMA 200 (distance) · last 3 candles (colour, body) · session · stop · manage line · chart |
| guardian (XAU) | −30 stop · +40 seen → SL +30 · ride +10 steps · close on an EMA 9 turn once secured · Friday last bar flat |

Journal rows: `signal` (candidate offered, with `dist9`, `dist200`, `last3`), `candidate_skipped` (matched, not offered: cooldown /
in position / hour / news), then the usual `ask_*` / `take` / `skip` / `expired` rows — that log is the dataset the next rules come from.

Replay (offline, CSV from `fetch_bars.py`):

    python tools/replay_9200.py --csv xauusd_m30_20260403_20261020.csv --from 2026-05-01
    python tools/export_decisions_9200.py --from 2026-10-19 --to 2026-10-24     # journal → logs/decisions_9200.json
    python tools/replay_9200.py --csv ... --decisions logs/decisions_9200.json      # replay a recorded TAKE/SKIP log

`tools/export_decisions_9200.py` reads `logs/journal.jsonl` (mode `ema9200` only) and writes `{bar_time_epoch: true|false}`.
- `true` = TAKE.
- `false` = SKIP or EXPIRED, and also a `signal` nobody answered. EXPIRED is journaled only by the Discord view's timeout, so
  without the bot running, an unanswered ask leaves no decision row.
- The last decision on a bar wins.

The TAKE / SKIP / EXPIRED answers are `decision` rows (`decision=TAKE|SKIP|EXPIRED`, `bar`, `side`, `signal_kind`, `by`).

**First-class mode:** `/mode ema9200` and `ops.write_mode` accept it. `/alert`'s guardian context uses the ema9200 hour /
Friday / news rule; before, it read the ema5080-only `entry_hours_ist` and raised. The Claude snapshot labels its EMAs `ema9` / `ema200`.
`AUREON_ASK_TTL_MIN` is read from the environment; it was hard-coded to 20 before v2.15.0. Every ask expiry counts wall-clock
minutes, not bars.

**Not yet in this mode** (the replay has them, the live agent does not):
- **Friday flat:** no live Friday close. The guardian's −30 / +40→+30 / ride +10 / EMA 9 turn are live.
- **Claude / Codex entry reads:** the advisors' mode list is still `ema5080`, `ema2050`, so `request_entry` is a no-op here.
- **`/alert`:** the entry window and news block use this mode's rule. The cross / confirm read on the alert card uses the
  generic path (no ema9200-specific candidate check yet).

The take-all baseline on Apr–Oct 2026 is **−$19.6k / lot** (218 candidates, 40% win, 11 / 15 weeks). That is the point: the
value is in the selection. Three hand-traded weeks: +$12.3k, +$14.6k blind (and +$13.0k / +$24.2k on a replayed week).

## v2.14.0 — execution timeframe, alignment gate, /decide-timeframe

**The rule in plain words:** trade on one timeframe; take the entry only when the two timeframes above it have already
crossed our way, or are about to.

**Execution timeframe** — `AUREON_EXEC_TF` (default `M5`, exactly v2.13.0). Every bar-count rule is a number of minutes
converted to bars of the exec TF (`common/tf_synth.py`: `TF_RULES` + `MINUTES`). Session, news and IST logic are unchanged.
`exec_tf` is not hot (a restart applies it); it is on the boot card and in the journal's `process_start` row.

| exec TF | confirm TFs (default) | confirm: hold · window · gap | regime windows | P timeout |
|---|---|---|---|---|
| M5  | M10, M15  | 3 · 18 · 1.5 pts (v2.13.0) | 24 / 12 bars | 12 bars |
| M10 | M15, M20  | 3 · 9 · 0.25× avg range | 12 / 6 | 6 |
| M15 | M20, M30  | 2 · 6 · 0.25× | 8 / 4 | 4 |
| M20 | M30, M45* | 2 · 6 · 0.25× | 6 / 3 | 3 |
| M30 | M45*, H1  | 2 · 6 · 0.25× | 4 / 2 | 2 |
| H1  | H2, H3    | 2 · 6 · 0.25× | 2 / 1 | 1 |
| H2  | H3, H4    | 2 · 6 · 0.25× | 1 / 1 | 1 |
| H4  | H6, H8    | 2 · 6 · 0.25× | 1 / 1 | 1 |

\* M45 is synthetic: three M15 bars aligned to the server day (00:00, 00:45, … 32 per day; none straddles midnight). The
open group is dropped (closed bars only), and a group with a missing M15 bar is skipped, never merged across the gap.
The confirm rule can be overridden with `AUREON_CONFIRM_HOLD_BARS`, `AUREON_CONFIRM_GAP_X_RANGE` and
`AUREON_CONFIRM_WINDOW_BARS` (0 = the table).

**HTF alignment gate** — `AUREON_HTF_ALIGN=off|show|block` (default `off`). The confirm TFs are
`AUREON_HTF_CONFIRM_TFS` (default: the next two rungs). On each confirm TF's closed bars, with the strategy's EMA pair:

| state | meaning |
|---|---|
| `with` | EMA fast is on our side, and the cross happened ≤ `AUREON_HTF_WITH_MAX_BARS` (8) bars ago |
| `with_stale` | on our side, older cross. Counts as `with` unless `AUREON_HTF_REQUIRE_FRESH=1` |
| `pre` | still against, but converging (gap 3 bars ago was wider) and \|gap\| ≤ `AUREON_HTF_PRE_MAX_GAP` (0.35) × that TF's 20-bar avg range |
| `against` | anything else: wide against, or widening against |
| `unknown` | no data. Fails closed: never counts as `with` / `pre` |

`AUREON_HTF_ALIGN_MIN=all` (default) needs every confirm TF to be `with` / `pre`. `any` needs at least one `with` and
none `against`.
- `show`: the entry card gets an `HTF` line (`HTF · M45 pre (gap -0.21× rng, converging) · H1 with (crossed 2 bars ago)`)
  and an HTF chart panel. Claude's snapshot gets an `htf_align` block and one prompt line.
- `block`: an entry that fails is posted as `… · HTF against · not tradeable`. It gets no buttons, no tracked ask and no
  Claude entry read, and it is journaled `entry_blocked_htf`. A TAKE from the pad or the push layer on an ask that failed
  the gate is refused with the reasons.
- Open positions are never touched by the gate.

**Replay:**

    python tools/replay_guardian.py --from 2026-08-01 --to 2026-10-15 --compare \
        --set exec_tf=M30 --set htf_align=block --set htf_confirm_tfs=M45,H1 \
        --set guardian_profile=follow --set follow_stop_x_range=1.5

The baseline in `--compare` is the same set with `htf_align=off`. The table adds `asks_blocked_by_htf` (would have won /
lost, blocked points) and the results of entries made on `pre` vs on `with`. The bars come from MT5 (M15 when the exec TF
is a multiple of 15, else M5). They are cached in `logs/bars/`.

**`/decide-timeframe tf [apply:false] [days:30] [force:false]`** (admin):
- Shows the plan: exec, confirm TFs, the confirm rule, and the follow knobs rescaled from the exec TF's average range over
  `days`:
  - stop = max(`FOLLOW_STOP`, 1.5 × avg range);
  - step ≈ 1.5 × avg range, rounded to 5, floor 10;
  - secure at ≈ 0.5 × avg range, floor 3.
- Then the replay with the gate `block` and with it `off`, cached per (tf, days, date).
- `apply:true` writes exactly those overrides (source `discord`), journals `timeframe_decided`, posts a loud card and
  restarts. It is refused while a position is open (or flat cannot be proven) unless `force:true`.
- The gate goes in as `show`. Promoting it is a separate `/feature-set htf_align block`.
- With no `tf`: the ladder and the last decision.
- `GET /console/state` has `timeframe: {exec, confirm, htf_align}`.

## v2.13.0 — follow profile

**The rule in plain words:** once you are in, only the stop can take you out. Secure at +3. Then lock +10 behind each +10
the trade makes.

`AUREON_GUARDIAN_PROFILE=follow` (or per symbol: `AUREON_GUARDIAN_PROFILE_XAUUSD=follow`). `classic` stays the default.
- **Adopt:** the stop goes to entry − `AUREON_FOLLOW_STOP` (25). With `AUREON_FOLLOW_STOP_X_RANGE` > 0 it is the larger of
  that and that multiple of the 20-bar average range. A tighter stop that is already there is kept.
- **Secure:** at live +`FOLLOW_SECURE_AT` (3) the stop moves to entry + `FOLLOW_SECURE_LEVEL` (3).
- **Lock:** with peak +12 the stop is +3 (+0 floored to the secure level), +22 → +10, +32 → +20, +42 → +30.
  `FOLLOW_CLEARANCE` (2) is the wick margin; at +11.9 nothing moves. `FOLLOW_TRAIL_MODE=continuous` instead keeps the stop
  at peak − 10, ratcheted.
- **Exits:**
  - the broker stop;
  - flat before high-impact news while not secured (`FOLLOW_NEWS_FLAT`);
  - an opposite **confirmed** cross only when the structure confirms it (`FOLLOW_EXIT_ON_FLIP=structure`, ≥
    `FLIP_MIN_EVIDENCE`), through the v2.12 pre-exit read when that is on;
  - `FOLLOW_MAX_HOLD_BARS` (off by default): only when never secured **and** the setup is dead.
- **No other exits:** no P timeout, EMA turn, plain P flip, giveback, slow-EMA close or setup stop.

Where it comes from: the operator's study of 91 M15 crosses (Aug–Oct 2026): +500 pts, max drawdown 97. Those numbers are
from that study; this repo does not reproduce them. `FOLLOW_STOP=25` is the M15 value. For `ema2050` on M5, start from
`AUREON_FOLLOW_STOP_X_RANGE=1.5` with `FOLLOW_STOP` as the floor, and check it with
`python tools/replay_guardian.py --set guardian_profile=follow` (every `follow_*` knob takes `--set`).

The profile is not hot. A ticket keeps the profile it opened with until it closes. `/governor` shows the profile and the
knobs, `/features follow` lists them, and `/mode` warns which open tickets keep their profile.

## v2.12.0 — guardian-aware reads

**Claude now sees what the guardian is about to do.** Every READ on a position (open, drawdown, flip-held, pullback,
`/claude-read`) carries a `guardian` block: phase (pre / protect / secured / ride), secured, peak, MAE, the trail /
giveback / structure-stop / cap levels, the last three guardian actions, and `next_exit`, the closest exit rule with its
level, distance in points, bars left and whether it fires on a close or a tick. The prompt tells Claude that HOLD means
holding through that rule.

**A vote before a close-based exit** (`AUREON_PRE_EXIT_READ`, default `off`):
- `show`: the guardian closes as before, and a `pre_exit` read is journaled next to the close (grading only).
- `defer`: for `ema_fast_turn`, `giveback`, `setup_invalidated`, `pre_timeout` and a structure-confirmed `p_flip`, the
  guardian asks first. The card says **EXIT PENDING · rule · asking Claude**.
  - Claude EXIT, no answer in `AUREON_PRE_EXIT_WAIT_S` (45 s), budget used or a CLI error → it closes now by the
    original rule.
  - Claude HOLD → **EXIT DEFERRED · 1/2**, and the rule is checked again on the next close. After
    `AUREON_PRE_EXIT_MAX_DEFER` (2) HOLDs it closes without asking.
- **Never deferred:** the broker / pre-stop, `ema_slow_stop`, news, an opposite confirmed cross, the ride staircase, and any
  trade within 2 pts of its cap. A deferral never moves the SL.

`/advisor-report` gains a **pre-exit** section: deferrals, average gain per deferral (graded
`AUREON_PRE_EXIT_GRADE_BARS` = 6 bars later), and how often a deferred trade went on to hit the cap.
`python tools/exit_audit.py --days 7` shows which software exits close trades early. Per rule it lists the count, the
average points at the close, the average MFE over the 12 bars after the close, and how often price went ≥ +10 further.
Use it to pick the rules that deserve `defer`.

## v2.11.2 — a network loss never ends the process

**Run it as the service, from `C:\Aureon`.** Install the repo at `C:\Aureon` (never under OneDrive, Dropbox or Google Drive:
they lock and sync `guardian_state.json`, `features.json`, `restart_marker.json` and the journal while the bot writes them;
the BOOT CHECK warns), and run it as the NSSM service from `docs/SERVICE.md §1`. Never use `python main.py` by hand for live
trading: when that process ends for any reason, nothing brings it back.

When the box loses its network (DNS, Wi-Fi, ISP), Aureon now keeps polling MT5 and managing open trades on their existing
stops. It opens no new entries: an ask that fires is journaled `ask_suppressed_network` and listed on the console
(`AUREON_NETWORK_DEGRADED_ENTRIES=1` allows entries anyway). Trade cards queue in `logs/card_queue.jsonl`. The Discord
client reconnects forever, with backoff up to 60 s. When the network returns, one `BACK ONLINE · network · down N min ·
K cards queued` card is posted, followed by the queued cards in order. Every process exit is recorded (`process_exit` in the
journal, `logs/exit_marker.json`), and the next boot card says `last exit: …`. The 4-hourly heartbeat shows a DNS self-check.

## v2.11.0 — notifications that arrive when the trade does

Nothing about trading changes. A card that matters now reaches the phone as a notification, and the phone can answer it.

**1 · Discord, set up so it actually pings** (no extra service):
- `AUREON_DISCORD_USER_ID` = your user id (Settings → Advanced → Developer Mode, then right-click yourself → *Copy User ID*).
  Trade cards (`AUREON_DISCORD_MENTION_EVENTS`, default `ASK,FILLED,CLOSED,STOP`) carry `<@you>`, so the phone notifies
  even when the channel is muted for everything else. Only you can be pinged (`allowed_mentions`).
- Optional `AUREON_DISCORD_ASKS_WEBHOOK`: make a `#aureon-asks` channel, give it a webhook, and set the channel to
  **All messages** on the phone (long-press the channel → Notification settings). It gets only trade cards; TAKE / SKIP and
  Claude's verdict are mirrored into that copy too.
- Phone **Focus / Do Not Disturb**: add Discord (or the push app below) to the allowed apps. Otherwise iOS/Android hold it.

**2 · Phone push with buttons** (`AUREON_PUSH=ntfy|pushover|telegram`, comma list):
- **ntfy** (free, recommended): install the ntfy app, subscribe to a long random topic, put it in `AUREON_NTFY_TOPIC`.
  An ask arrives as an *urgent* notification with **TAKE · SKIP · READ** buttons; a fill as *high*; a close or stop as
  *normal* (a stop losing more than `AUREON_PUSH_LOSS_ALERT_PTS` is *urgent*). `AUREON_PUSH_IMAGES=1` attaches the chart.
- **Pushover**: `AUREON_PUSHOVER_TOKEN` / `USER`; asks are emergency priority (repeats every 30 s for 10 min until seen).
- **Telegram**: `AUREON_TELEGRAM_TOKEN` / `CHAT`; buttons under the message, answered by a long-poll thread.

**3 · The buttons need the console** (`docs/CONSOLE.md`): set `AUREON_CONSOLE_TOKEN` (32+ chars). It listens on
`127.0.0.1:8787` only. Install **Tailscale** on the box and the phone, run `tailscale serve --bg 8787` on the box, and put
the `https://<box>.<tailnet>.ts.net` address in `AUREON_CONSOLE_URL`. Each button carries a one-time token for that one
action on that one ask, so the push service never sees the console token. A TAKE from the phone is refused exactly when
`/take` would be, and also when the ask is stale or the regime gate blocks it. The reason comes back on the phone.

**New command:** `/close [symbol]`, the same close path the phone's CLOSE uses. `/features` lists `push`, `push_images` and
`mention_events` (all hot; switching push on posts a FEATURE CHANGED card). `/env-check` flags a non-numeric user id, a
short token, a public bind and a backend with missing credentials.

## v2.10.1 — unattended operation; the box belongs to Aureon
The operator is often not at the server. Details and setup: [docs/SERVICE.md](docs/SERVICE.md#v2101--unattended-operation-the-box-belongs-to-aureon).
- **Cold start**: a boot without a restart marker posts **COLD START** (boot time, last shutdown reason from the System log,
  minutes down, positions re-adopted). Every BACK ONLINE card says `down X s`.
- **MT5 before the bot**: the launcher starts `terminal64.exe` first, and a hung terminal is restarted.
  - v2.15.0 restart pre-flight (`aureon_mt5/preflight.py`) waits for initialize() **and** the account login (every 5 s, up to
    90 s), keeps the heartbeat fresh so the supervisor never kills the wait as a hang, then exits 75 if MT5 is still down.
  - **MT5 NOT UP** is posted once per outage (after `AUREON_MT5_BOOT_WAIT`, across restarts). Then
    **BACK ONLINE after restart · MT5 build · account · open positions re-adopted**.
  - Reboot recovery: `scripts/install_logon_tasks.ps1` installs the logon tasks `Aureon-MT5` and `Aureon`. It needs auto-logon
    and never-sleep; see [docs/SERVICE.md](docs/SERVICE.md#v2150--reboot-sequence-no-human).
- **Env check**: `config.validate()` — unparsable numbers, values outside the feature registry's ranges, Discord plumbing,
  contradictions (`EXIT_LOSING` without manage, `SETUP_MAX_DD` < `pre_stop`, regime thresholds out of order). Guardian
  problems fall back to the safe default and post **BOOT CHECK**; `/env-check` shows what the guardian runs on.
- **Repair agent `auto`**: plumbing fixes apply themselves only when flat, outside the session, suite green twice, ≤ 2/day;
  **REPAIR AUTO-APPLY** → **REPAIR VERIFIED** after 10 min healthy, or **REPAIR ROLLED BACK** (once) and `needs_human`.
- **Maintenance window** (flat + outside the session): Windows reboots (60 s countdown, aborted if a position opens), code updates
  with the suite (the old 5-minute auto-update moved here), log rotation, pip. `aureon_server.ps1 -Action harden` sets the OS up.
- **Clock / network**: drift > 2 s → resync + card; Discord and dead-man down while MT5 ticks → no new entries, guardian
  untouched, one NETWORK BACK summary on reconnect. **`/server`** shows the box.
- **Evidence-gated strategy autonomy** (`AUREON_TUNING_AUTO=off|shadow|apply`, default off): a proposal must win the replay
  (more points AND fewer losing trades AND pins green), then shadow 3 sessions, then — apply only — it is set through the registry
  with **STRATEGY CHANGE APPLIED** and `/veto <id>`. Never automatic: lot size, loosening stops, claude manage, exit_losing,
  symbols, mode.

## v2.10.0 — live toggles, full /commands, gated self-repair, zero-lag context, regime gate
Nothing here changes a trade by default; `tests/test_strategy_pins.py` pins the strategy and the guardian at defaults.

**Feature toggles** (`aureon_mt5/features.py`). Every env-gated feature has one registry row (env name, type, range, group,
hot, loud). `/features [group]` shows each value with its source (`discord` > `env` > `default`); `/feature-set name value
[force]` (admin) changes it live — `logs/features.json`, applied in place on `Config`, so the guardian sees it on its next bar;
non-hot features (`setup_stop`, `setup_max_dd`) are refused with a position open unless `force:true`; loud changes post
`FEATURE CHANGED`. `/feature-reset`, `/feature-help`. The boot card and the daily plan show a `Features` line per group
(`*` = set from Discord). `/commands` now documents every registered command (a test enforces it).

**Repair agent** (`aureon_mt5/repair.py`) — detect → propose → a human applies. Incidents (unhandled exceptions, a command
failing twice in 10 min, Claude verdicts failing to parse ≥ 3/day, unreadable journal lines, chart failures ≥ 3/day, stale frames
with MT5 connected) get one card each (`INCIDENT #n`). `/repair n` (admin, or automatically with `AUREON_REPAIR_AGENT=propose`)
drafts a fix in a `repair/<n>` git worktree from origin/master with `claude -p` (or `codex exec`), runs the tests, refuses any
patch touching strategies / guardian functions / broker / journeys / leg_guardian / setup / .env / VERSIONING.md / VERSION, and
posts `REPAIR #n` with the stat. `/repair-apply n` (admin) applies, tests, commits, pushes and restarts; any failure reverts.
`/repairs`, `/repair-discard n`. Broker rejects, MT5 disconnects and losing trades are never incidents.

**Zero-lag context** (`AUREON_ZL_CONTEXT=1`, `aureon_mt5/common/zl.py`): ZLEMA 50/80, HMA 55, TEMA 50, ADX 5/14 ± DI, ATR 14,
KAMA 10, session VWAP — no lookahead. Claude's snapshot gets a `zl` block, the chart gets dotted ZL lines and an ADX(5) strip.
`AUREON_FLIP_USE_ZL=1` adds "ZL cross against" as flip evidence. No signal rule reads them.
**Backtest**: `python tools/replay_guardian.py --bars <file> [--trade MM-DDTHH:MM,long] [--signal MM-DDTHH:MM,short] --compare
--set giveback_pct=50 --set flip_require_structure=1` — the live `_guard` bar by bar, stops honoured intrabar, no Claude, no
Discord; per-combination trades, win rate, avg win/loss, total, max drawdown, close reasons → `logs/replay/<date>.json`.

**Regime gate** (`aureon_mt5/regime.py`). Every entry must lead away from drawdown. There is no max-trades cap — if the market is
clean all day, every valid setup is tradeable; if it whipsaws all day, there are no trades that day. The gate is a regime,
computed on every closed bar, not a counter. Score = min(whipsaw half: EMA crosses in 2 h, flat gap, failed P's; energy half:
ADX 14, DI spread, range compression) → `CLEAN | CHOPPY | DEAD` with hysteresis. `AUREON_REGIME_GATE=off` (journal only) ·
`show` (REGIME line on entry cards) · `block` (CHOPPY/DEAD: the signal is carded "CHOP · not tradeable", no ask, no Claude entry
read, `/take` refused; `NO-TRADE REGIME` / `NO-TRADE DAY` cards). Open positions are never touched by the regime. Backtest with
`--set regime_gate=block`: `entries_blocked` → would-have-lost / would-have-won; the gate is acceptable when blocked losers ≥ 3 ×
blocked winners. Thresholds are tuned through `tuning.propose`, citing the replay JSON.

## v2.8.0 — Claude sees the chart, the guardian sees the setup
Everything that can change a trade is behind an env knob that defaults to OFF (v2.7.2 behaviour); see `.env.example`.

- **Chart to Claude**: live P / CROSS calls (and every READ) attach the card's chart (`AUREON_CLAUDE_IMAGE_EVENTS`). The CLI gets
  `--allowedTools Read` and the prompt ends with a CHART trailer: the picture is for structure only, every number comes from the
  SNAPSHOT. Without an image the command and prompt are byte-identical to v2.7.2. `claude_replay.py` (run.py --claude) stays text-only.
  Codex never gets a chart.
- **`/claude-read [symbol]`**: Claude reads the chart + the open position now — HOLD/EXIT in a trade, ENTER/SKIP when flat. Shown
  only; journaled `claude_read`; positions come from the agent's cached snapshot (no broker call).
- **Structure-gated flip** (`AUREON_FLIP_REQUIRE_STRUCTURE=1`): an opposite P closes the trade only with ≥ `FLIP_MIN_EVIDENCE` of:
  close through EMA80 · EMA50 slope against · M15 against · gap ≥ `FLIP_GAP_X_RANGE` × avg range. Otherwise `OPPOSITE P · HOLDING`
  (journal `opposite_p_ignored`), the guardian keeps managing, and Claude gets a flip-held READ.
- **Giveback** (`AUREON_GIVEBACK_PCT`, armed at `GIVEBACK_ARM`): close on a bar CLOSE that gave back that share of the peak.
- **Setup stop** (`AUREON_SETUP_STOP=1`, classic profile): initial SL = swing low/high ∓ `SETUP_BUFFER`, capped at −`SETUP_MAX_DD`
  (or × range); raised with the swing while under water; closes on a bar CLOSE that invalidates the setup (EMA order after the cross,
  close beyond EMA80 ∓ buffer, M15 against, prior swing broken — unknown checks never fail); P timeout paused while the lines converge.
  After SECURE the staircase takes over unchanged. The `leg` profile gets only the invalidation exit.
- **Claude votes**: one READ when a position is first seen (`AUREON_CLAUDE_OPEN_READ`, shown only, p_win graded in `/advisor-report`)
  and event-driven READs under water (`AUREON_DD_READ_AT` / `_STEP` / `_READS_PER_TRADE`). A drawdown EXIT may cut a losing trade
  only in `manage` with `AUREON_CLAUDE_EXIT_LOSING=1`. Budget: +1 call per trade at open, +0–3 in drawdown — set
  `AUREON_CLAUDE_MAX_CALLS=100`.

## Dead-man's switch (v2.7.2) — know when Aureon is dead, from outside
Aureon pings `AUREON_DEADMAN_URL` every 60 s from a background thread (not the Discord loop), **only while** the event-loop
lag is < 5 s, the Discord gateway is connected, every agent thread is alive (and refreshed its snapshot in the last 5 min)
and, while the market is open, a closed bar was processed in the last 15 min. Dead, frozen or disconnected → pings stop →
the external service alerts you.

1. healthchecks.io → *Add Check* → period **1 minute**, grace **5 minutes** (any URL that alerts on missed GETs works).
2. Integrations: email, SMS/phone call, Telegram, Discord (a channel the bot does not own) …
3. `.env`: `AUREON_DEADMAN_URL=https://hc-ping.com/<uuid>` → restart. Startup prints `dead-man's switch: pinging …`.
4. Test: stop the service → the check goes *down* after ~6 min.

Fallback without an external service: `AUREON_DEADMAN_TELEGRAM=<bot token>`, `AUREON_DEADMAN_TELEGRAM_CHAT=<chat id>`, then
`scripts\aureon_service.ps1 -Action install-fallback` — Task Scheduler runs `scripts/deadman_fallback.py` every 5 min and posts
"Aureon down" when `logs/heartbeat.txt` is older than 5 min. `/status` shows the heartbeat line.

## Verify
    python detect.py --mode ema5080 --date 2026-10-05         # every P / cross / skip of the day: raw ts, server, IST, EMAs
    python detect.py --mode ema5080 --live                     # per closed bar: close, EMAs, P, cross, whipsaw, news, entry allowed
    python run.py   --mode ema5080 --date 2026-10-05 --news news_blackout.txt   # static replay: PNG, HTML, JSON, per-trade console
    python -m pytest -q tests

## EMA 20/50 (v2.0.0 — see strategies/ema2050/journeys.py Rules / GUARDIANS; researched Jul–Oct 2026 + Jun–Dec 2025 out of sample)
cross = sign change of EMA20 − EMA50 on a closed bar · **confirmed** when the order has held ≥ 3 bars AND |gap| ≥ 1.5 pts, within 18 bars
(first bar that satisfies both); a flip before that = **multi cross**, no trade · **entry = pullback**: price touches EMA20 within 1.5
(v2.3.1, = research/ema2050_scan.py: a touch on any bar after the cross up to the confirm bar → enter at the confirm bar close; else the
first touch in (confirm, confirm+12]; else a fallback at confirm+12 only if |close − EMA20| ≤ 5 and the order holds — the 5-point cap
applies to the fallback only) · the only time filter: no entries server 21:00–23:59 (no IST window) ·
news block 60/30 (NFP auto + `news_blackout.txt`) · one trade per cross · pre-cross shoot (40 bars) reported, not filtered.
Not added (tested, failed 2025): trend/hold/slope/EMA200/re-entry/extension filters.

Guardian (ema2050, Gold profile, `AUREON_GUARDIAN_PROFILE`):

**Classic** (`classic`, default): no SL → **−12** (`PROTECTED`) · **+3 seen → SL entry +1** (`EARLY LOCK +1`, never loosened) ·
+10 → SL +10 (announced when MT5 confirms) · ride +5 steps (2 pts air) · **no EMA50 follow-SL** ·
close on the **EMA20 turn once secured** · exit at the next **confirmed** opposite cross · no P phase ·
news safeguard as 50/80 · live SL is the truth, SL never loosened.

**Leg** (`leg`, v2.7.0): the move detector (`self.leg`) drives exits.
- Phase A (peak < `secure_at`): broker SL stays at −`pre_stop`. Soft TRAILING lock: `max(early_level, peak − giveback)`
  where `giveback = max(AUREON_SOFT_GIVEBACK_PTS, AUREON_SOFT_GIVEBACK_FRAC × peak)`. Exit on bar close through the lock.
- Phase B (peak >= `secure_at`): broker SL set to floor `AUREON_RIDE_FLOOR` (+4 default, never +10). Trail on CLOSED bars:
  `max(floor, swing trail = lowest low of last RIDE_SWING_BARS bars − RIDE_BUFFER, ATR trail = peak − RIDE_ATR_MULT × avg range)`.
  SL updated once per bar, never loosened. Card only when trail rises >= 3 pts.
- Phase B exits: a) broker SL hit (trail/floor), b) bar closes through EMA SLOW → close (`ema_slow_close`),
  c) leg ends (EMAs crossed back → `leg_end`), d) news/day-end. EMA FAST close only tightens the trail.
- News in Phase B: tighten SL to `max(current trail, points − 3)`, never close.
- Position without a same-side leg → classic exits.

Replay: `python run.py --guardian classic|leg|grid` · grid searches floor {2,4,6} × swing {4,6,10} × ATR mult {2,3,4}.

Cards: `CROSS LONG|SHORT (unconfirmed)` · `CROSS CONFIRMED` · `MULTI CROSS (ignored)` · `ENTER LONG|SHORT (pullback | no-pullback)` (TAKE/SKIP) ·
`NO ENTRY (too far from EMA20 | no-entry hour | news)` · then PROTECTED / EARLY LOCK or SOFT TRAIL / MOVE PROVEN / TRAIL / CLOSED. Every card: cross time,
confirm time, pre-cross shoot, session, EMA20/50, bars since cross.

Replay / verify:
    python run.py --mode ema2050 --date 2026-10-07                              # trades + events + the research block for the day
    python run.py --mode ema2050 --from 2026-07-01 --to 2026-10-09 --lot 1       # day by day (entries inside the server day, open trades
                                                                                #  closed at the day end) + research summary + month table
    python detect.py --mode ema2050 --date 2026-10-07 | --live                   # per bar: EMAs, cross, confirm state, touch, entry allowed, reason
Replay SL convention (same as the 50/80 replay): a level armed by bar k's extreme is tested from bar k+1. Exit reasons: stop · early ·
secured · ema20_turn · opposite_cross · news_flat · day_end · open.

## Claude shadow replay (v2.1.0) — `run.py --claude`
    python run.py --mode ema2050 --from 2026-07-01 --to 2026-10-09 --claude entry --claude-max 60 --claude-sleep 2 [--claude-model opus] [--claude-refresh]
    python run.py --mode ema5080 --date 2026-10-07 --claude entry+pullback
Without `--claude` nothing changes. With it, Claude is asked at every ENTER bar the replay produced (and, with `entry+pullback`, at every
pullback event inside a replayed trade — the live trigger: trend PULLBACK/WEAKENING/CHALLENGED or ≤ 60 % of a ≥ +5 peak, once per pullback)
**exactly as live**: `claude_advisor.build_snapshot` on `df.iloc[:bar+1]` only (asserted — no future bars; crosses/day P&L recomputed for the
bar's day), the same rules prompt and reply contract (TAKE/SKIP, `p_win`, `evidence`), `claude_cli.call` with `AUREON_CLAUDE_BIN` and
`ANTHROPIC_API_KEY` stripped. Verdicts are **recorded, never applied**; a second column **Detector-filtered-by-Claude** = the same trades with
Claude's SKIPs removed (pullback variant: TIGHTEN / CLOSE-in-profit applied on the M5 bars). Cache `out/claude_replay_<mode>_<symbol>.jsonl`
keyed by (bar, event, model, rules hash): a re-run makes zero calls; `--claude-max` caps calls per run and the report says how many ENTER
bars are still unscored. Report `out/claude_replay_<mode>_<symbol>.md` (printed too): Detector · all | Claude TAKE | Claude SKIP |
Detector-filtered-by-Claude (n, win %, expectancy, total, stops, worst streak, max DD), SKIP precision, p_win calibration table, per-month
Detector vs Claude-filtered, disagreement list (date, side, detector pts, verdict, reason, evidence). Strategy rules changed: NO.

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
`/claude` (v2.0.1: is Claude Code attached to each agent? header mode · bin · `claude --version` · login; models as configured and as the CLI reported;
budget calls/max, entry/pullback/alert calls, fast-exit cancellations, last latency/error; one line per agent ATTACHED / REVIEW / NOT ATTACHED / DETACHED) ·
`/claude-attach <symbol>` · `/claude-detach <symbol>` (runtime toggle, no restart; detach cancels queued jobs; journaled `claude_attach` / `claude_detach`) ·
`/pull-history [days=7] [symbol]` (v2.0.2: MT5 closed deals ↔ journal by ticket, fills missing `final_points`, per-day lines, unmatched = manual trades) ·
`/git-history [n=10] [tags]` (v2.0.2: running build, origin/master status, merges on master with PR links in IST + own offset; `tags=true` = last 10 tags) ·
`/discord-bot sync` (admin: re-sync the slash commands for the guild, stale ones dropped) ·
`/status` (version, mode, market, tick age, MT5, agent, guardian, position phase/SL/secured/peak, IST-day counters, last broker action, Claude ATTACHED/REVIEW/OFF) ·
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

## Price alerts (v2.0.2) — two verdicts, clearly labelled
`/alert <price>` answers with the armed line and a **PREVIEW** card (PRICE · EMA · TREND · AGENT VERDICT for the current bar, "if it hit right
now", no Claude call). When the level is hit the **🔔 ALERT REACHED** card has five blocks: **PRICE** (hit, server/IST time, approach, pts from
EMA fast/slow) · **EMA** (values, gap and 6-bar trend, 4-bar fast slope, price position, last cross side/time/confirmed/bars ago/multi, crosses
today, session, ATR20) · **TREND** (state, pre-cross shoot) · **AGENT VERDICT** — deterministic, the mode's own rules on the last closed bar
(`AGENT: SHORT — confirmed cross 09:00, pullback touch now, 0.8 pts from EMA20, window open` + the SL the guardian would set, or `WAIT — …`,
`NO TRADE — 6.4 pts from EMA20 (chase) | no-entry hour | news in 42 min | multi cross`) · **CLAUDE VERDICT** (`⏳ asking Claude (opus) …`,
`— not attached (mode=off)`, `— budget used`, `— unavailable`). The agent block is always on the card before the Claude call is queued; when
Claude answers the same card is **edited**: `CLAUDE: SHORT · TAKE · p_win 0.68 · confidence medium`, the reason, `evidence: …`, and
`AGREES with agent ✅` / `DISAGREES with agent ⚠️ (agent SHORT, Claude SKIP)`. If the card cannot be edited (webhook only) a follow-up
`ALERT VERDICT · <id>` card repeats both blocks. Buttons `[LONG] [SHORT] [SKIP]`; `alert_decision` journals `agent_verdict, claude_verdict,
agreed_agent, agreed_claude`; `/report` and the Saturday card show an **ALERTS** table (fired · taken · agent✓ · claude✓ · pts taken).
Placement feedback: after a successful `place_market` an **ORDER PLACED** card (ticket, side, lots, fill, slippage vs the card price, SL as set,
spread at fill, time), then PROTECTED; a requote / price-changed rejection is retried once after 2 s, then ❌. The decision is edited into the
card itself (`TAKEN SHORT · ticket 1234567 · filled 4120.3` / `SKIPPED`); with execution off the card shows the exact guardian SL and
"waiting for your ticket…", then `ATTACHED · ticket …` when the position appears (≤ 1 poll).

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
snapshot fields. The prompt is mode-aware (first line names the fast/slow EMA; `prompts/advisor_rules.md` — v2.3.0, alias
`prompts/claude_rules.md` — has one section per mode, a placeholder until `research/claude_rules_v2.md` is supplied). Saturday, after the COMPARE card: **CLAUDE RULE PROPOSALS** — wrong calls and their
snapshot fields → up to 3 proposed rule lines with the supporting count (`logs/claude_rule_proposals.json`). `/claude-rules` lists them;
`/claude-rules-approve <n>` appends the line, dated, to `claude_rules.md`. Nothing changes without approval.


## Auto-update (v2.2.2) — the server follows origin/master
Every 5 min, and before `/restart` / `/mode`: if origin/master moved, local edits to tracked files are saved to
`logs/local_changes_<hash>_<ts>.patch` and discarded (`git reset --hard origin/master`), otherwise `git pull --ff-only`; restart only once
HEAD == origin/master. Never edit tracked files on the server — commit them. Server-local, never touched: `.env`, `logs/`, `out/`,
`news_blackout.txt` (copied from `news_blackout.example.txt` when missing). No network → `UPDATE FAILED` once per hour, current build keeps running.

## Advisors (v2.3.0) — Claude and Codex, same second opinion, CLI login only
Both advisors are the same code path (`CodexAdvisor` subclasses `ClaudeAdvisor`): same triggers (ema2050: ENTER bar + pullback-in-trade;
ema5080: P / CROSS / RE + pullback), same `build_snapshot` (market data only — no account, balance, login or token), same rules prompt
`prompts/advisor_rules.md` (`prompts/claude_rules.md` is an alias), same reply contract `{decision, side, tighten_to, confidence,
reason ≤ 20 words, evidence[], p_win?}` validated the same way, same fail-closed and STALE handling, one job per pullback,
fast exit identical (guardian first, the queued verdict becomes "position no longer open", a late verdict is STALE).
Each has its own worker thread, daily budget, usage ledger and journal rows. **Never an API-key path:** `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `CODEX_API_KEY` are stripped from the child process; a startup card warns if one is set.

| | Claude (`AUREON_CLAUDE`) | Codex (`AUREON_CODEX`) |
|---|---|---|
| CLI | `claude -p --output-format json` | `codex exec --sandbox read-only --json --output-schema …` (workdir outside the repo) |
| modes | off · review · advisory · manage | off · review · advisory |
| `review` | 23:00 IST CLAUDE REVIEW + `/alert` line | 23:00 IST CODEX REVIEW + `/alert` line |
| `advisory` | verdicts on ENTER / pullback / ALERT cards, journaled | verdicts on ENTER / pullback / ALERT cards, journaled |
| **applies to your trade** | **TIGHTEN and CLOSE-in-profit, only in `manage`** (through the guardian's `_move_sl` / `_close`) | **never** — journaled and shown only, in every mode (a test walks every verdict kind) |
| budget | `AUREON_CLAUDE_MAX_CALLS` → `logs/claude_budget.json` | `AUREON_CODEX_MAX_CALLS` → `logs/codex_budget.json` |
| usage ledger | `logs/claude_usage.jsonl` | `logs/codex_usage.jsonl` (tokens when `codex exec --json` reports them, else null; cost null unless reported) |
| journal | `claude_verdict` (`claude_event`) | `codex_verdict` (`codex_event`) |
| models | entry / pullback / review env vars | `AUREON_CODEX_MODEL` for verdicts, CLI default for reviews |
| Saturday | COMPARE column · CLAUDE RULE PROPOSALS · optional self-review | COMPARE column · CODEX RULE PROPOSALS · CODEX REVIEW (self-review) |

**Two verdicts on one card.** ENTER, pullback and ALERT REACHED cards carry one line per attached advisor and a one-word tag once both
have answered:

    CLAUDE: TAKE · medium · "pullback to EMA 20, gap growing" · evidence gap, dist_to_fast_ema_pts
    CODEX : SKIP · low · "crosses_today 5, chop" · evidence crosses_today          Advisors: SPLIT ⚠️

AGREE = same decision (two TAKEs on the same side). The second advisor to answer edits the card (bot) or posts its card with both lines (webhook).

**Compare / reports.** `/compare`, `/compare-breakdown` and the Saturday COMPARE card show Detector · Claude · Codex · You with the same
metrics, Claude-vs-Codex agreement %, and *when they SPLIT, who was right* (the TAKE side when the if-taken result was a gain, the SKIP side
when it was a loss; count and points). Codex rows appear once Codex has judged a signal. `/advisor-report [week|month]` (alias
`/claude-report`): one card, a section per advisor (verdict counts, agreement with detector / you, SKIP precision, TAKE expectancy vs
detector-all, calibration by confidence, pullbacks, usage) plus head-to-head; auto-posted Saturday after COMPARE and on the 1st.
Rule proposals: separate CLAUDE / CODEX RULE PROPOSALS cards; `/claude-rules-approve <n>` · `/codex-rules-approve <n>` append to the shared
`advisor_rules.md` as `- [claude] [approved YYYY-MM-DD · week] …` / `- [codex] …`.

**Replay.** `run.py --codex entry|entry+pullback [--codex-max 60] [--codex-sleep 2] [--codex-model id] [--codex-refresh]` — the same
no-look-ahead shadow replay as `--claude`, own cache `out/codex_replay_<mode>_<symbol>.jsonl`. With both flags: Detector-all ·
filtered-by-Claude · filtered-by-Codex · filtered-by-BOTH-agree (kept only when neither said SKIP), SKIP precision and calibration each, and
the SPLIT list → `out/advisors_replay_<mode>_<symbol>.md`.

Codex commands: `/codex` (health, login, per-agent ATTACHED / REVIEW / OFF) · `/codex-status` (today / week / month usage) · `/codex-test` ·
`/codex-review [date]` · `/codex-alert [symbol] [side]` · `/codex-attach` / `/codex-detach <symbol>` · `/codex-rules` · `/codex-rules-approve <n>`.
Codex settings: `AUREON_CODEX_BIN`, `AUREON_CODEX_MODEL`, `AUREON_CODEX_TIMEOUT` (30 s; reviews ≥ 120 s), `AUREON_CODEX_MAX_CALLS` (30/day),
`AUREON_CODEX_WORKDIR`. Run `codex login` once on the server.

## v2.2.0 operations (staging)
See `STATE.md`. Voice commands are **NOT IMPLEMENTED**; planned intents and safety controls: `docs/VOICE_SCOPE.md`. Existing trading strategy rules are unchanged. Do not operate deployment from unreviewed branches.

### v2.2.0 operator command reference (draft)
| Group | Commands | Operator versus automatic behavior |
| --- | --- | --- |
| Monitoring | `/status`, `/agents`, `/parallel-status`, `/market`, `/chart`, `/symbols`, `/symbol-present` | Read-only snapshots; bot polls automatically |
| Trading | `/take`, `/skip`, `/alert`, `/alerts`, `/alert-cancel`, `/alert-clear` | Alerts monitored automatically after arming; trading decision belongs to operator |
| Claude | `/claude`, `/claude-status`, `/claude-test`, `/claude-review`, `/claude-rules`, `/claude-rules-approve`, `/claude-report` | Status/report read-only; CLI calls consume usage; rule approval manual |
| Codex | `/codex`, `/codex-status`, `/codex-test`, `/codex-review`, `/codex-alert`, `/codex-attach`, `/codex-detach`, `/codex-rules`, `/codex-rules-approve` | Same as Claude, but verdicts are never applied; CLI calls consume the Codex budget |
| Reports | `/report`, `/compare`, `/compare-breakdown`, `/advisor-report`, `/git-history`, `/pull-history` | On-demand historical diagnostics |
| Control | `/restart`, `/mode`, `/discord-bot sync` | Administrator actions only where implemented; restart/mode require configured role ID |

Set `AUREON_ADMIN_ROLE_ID` in your private environment to the Discord server administrator role ID; when missing, privileged restart/mode commands are denied. Changes are not live until the draft branch is merged and validated. `/commands` lists live registered commands. Voice command interface is **NOT IMPLEMENTED**.
