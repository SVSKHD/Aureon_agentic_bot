# Running Aureon as a supervised service (v2.7.2)

Aureon must be visible from **outside** its own process: if it dies, freezes, or loses the Discord gateway, something
else has to notice and bring it back. Three layers:

| layer | what | notices |
|---|---|---|
| supervisor | `scripts/aureon_service.ps1` (NSSM service or logon Scheduled Task) | process exit, frozen process (stale `logs/heartbeat.txt`) |
| self-heal | `HealthGuard` thread inside Aureon (`aureon_mt5/selfheal.py`) | loop lag > 30 s, gateway down > 5 min → exit 75 |
| dead-man's switch | `AUREON_DEADMAN_URL` (healthchecks.io …) + optional Telegram fallback | everything above failed, machine off, network down |

## 1. Install the supervisor

Prerequisites: Python env with `requirements.txt`, `.env` filled in, MT5 terminal installed and logged in.

**Do not pass `--mode` / `--symbols`** on the service command line: the supervisor restarts with the same command line,
and CLI flags override `.env`, so `/mode` could not change them (it refuses with `RESTART ABORTED` if they are present).
Put `AUREON_MODE` and `AUREON_SYMBOLS` in `.env`.

```powershell
# optional but recommended: NSSM (https://nssm.cc) on PATH, e.g.  winget install nssm
cd C:\Aureon_agentic_bot
powershell -ExecutionPolicy Bypass -File .\scripts\aureon_service.ps1 -Action install      # -Python C:\path\python.exe if needed
powershell -ExecutionPolicy Bypass -File .\scripts\aureon_service.ps1 -Action status
```

- **With NSSM**: a Windows service `Aureon`, *Automatic* start at boot. NSSM runs the PowerShell launcher loop and restarts
  the launcher if it ever dies. Run the service as the Windows user that owns the MT5 terminal
  (`nssm edit Aureon` → *Log on* tab), otherwise `MetaTrader5.initialize()` cannot find/attach to the terminal.
- **Without NSSM**: a Scheduled Task `Aureon` that starts the launcher loop **at logon** (enable Windows auto-logon on a
  VPS so it also starts after a reboot). Task Scheduler restarts it every minute if it stops.

### The launcher loop (`-Action run`)
- Runs `python main.py` in the repo root; stdout/stderr appended to `logs/service.log`, rotated at 10 MB (5 kept).
  Supervisor events are in the same file, tagged `[supervisor]`.
- Any exit → restart. **Exit code 75** = requested restart (`/restart`, `/mode`, auto-update, self-heal) → back in 5 s.
- Crash backoff: 5 s → 30 s → 2 min (stays at 2 min; resets after 10 min of running).
- **Hang**: if `logs/heartbeat.txt` is older than 5 min (after a 5 min start grace) the process tree is killed and
  restarted — this covers a frozen interpreter where even the health thread cannot run.

### What `/restart` does now
Pulls origin/master (abort if that fails), posts RESTARTING, stops the agents, writes `logs/restart_marker.json`,
exits with code **75**. The supervisor starts the new process, which reads the marker and posts BACK ONLINE.
No `os.execv`. **Without a supervisor, `/restart` (and auto-update) stops the bot** — always run it under the service.

Guardian state is restart-safe (`guardian_state.json` + the journal): positions are re-adopted and SL is never loosened.

## 2. Self-heal inside the process
A plain thread (not the Discord loop) evaluates every 2 s:
- `logs/heartbeat.txt` every 30 s: `ISO time · version · loop lag · gateway state · health`. Shown on `/status`.
- loop lag > 3 s → every thread's stack written to `logs/stall-<time>.txt` (once per stall) — send that file when
  reporting a freeze; it names the blocking call.
- loop lag > 30 s, or gateway disconnected > 5 min (including a Discord thread that died or never connected) →
  journal `self_restart`, `logs/aureon.log` line, one webhook card **"⚠️ Aureon frozen — restarting"**, exit 75.
- every gateway disconnect / resume is logged with its duration in `logs/aureon.log`; the 4-hourly 💓 heartbeat card
  shows `reconnects last 4 h: N`.

## 3. Dead-man's switch
See README → *Dead-man's switch*. Short version: create a check at healthchecks.io (period 1 min, grace 5 min),
put its ping URL in `.env` as `AUREON_DEADMAN_URL`, add email/phone/Telegram integrations there.

Optional local fallback (works even if the dead-man service is not set up, but not if the machine is off):
```powershell
# .env: AUREON_DEADMAN_TELEGRAM=<bot token>   AUREON_DEADMAN_TELEGRAM_CHAT=<chat id>
powershell -ExecutionPolicy Bypass -File .\scripts\aureon_service.ps1 -Action install-fallback
```
Every 5 min `scripts/deadman_fallback.py` checks `logs/heartbeat.txt`; older than 5 min → "🔴 Aureon down" once,
then "🟢 Aureon back" when it is fresh again.

## Troubleshooting
- `logs/service.log` — process output + `[supervisor]` lines (exit codes, backoff, HANG kills).
- `logs/aureon.log` — `SELF-RESTART`, `discord gateway DOWN/UP … after N s`, `loop stall … stacks dumped`.
- `logs/stall-*.txt` — stacks at the moment the loop lagged.
- `.\scripts\aureon_service.ps1 -Action status` — service/task state + heartbeat age.

## v2.10.1 — unattended operation: the box belongs to Aureon

**Service account.** The service (NSSM *Log on* tab) or Scheduled Task must run as the **Windows user that owns the MT5
terminal** — `MetaTrader5.initialize()` attaches to the terminal in that user's session. The Scheduled-Task variant only starts
at logon, so **Windows auto-logon is required** for it to come back after a reboot (`aureon_server.ps1 -Action harden` sets it).

**MT5 before the bot.** `aureon_service.ps1 -Action run` starts `terminal64.exe` before `python main.py` when none is running
for `AUREON_MT5_PATH` (or the path the bot cached in `logs/mt5_path.txt` from `terminal_info()`). The bot then retries
`MetaTrader5.initialize()` with 5 s → 30 s backoff for `AUREON_MT5_BOOT_WAIT` (300 s), posts **MT5 NOT UP** after 5 min and
exits 75 if it never comes up (the launcher restarts it). While running, a terminal that is alive but disconnected for 60 s is
restarted and login + symbols re-verified (**MT5 RESTARTED** card).

**Harden once** (elevated PowerShell): `.\scripts\aureon_server.ps1 -Action plan` shows, `-Action harden` applies: Windows Update
active hours over `AUREON_SERVER_ACTIVE_HOURS`, no auto-reboot with a logged-on user, auto-logon, sleep/hibernate/display-off off,
High performance plan, `w32tm` resync at boot, Defender exclusions for MT5 and the repo. It prints what it changed and what it
could not.

**Maintenance window** (hourly check): only when **flat and outside the session** (05:30–23:00 IST, `AUREON_SESSION_IST`) — or a
reboot pending ≥ `AUREON_SERVER_MAINT_FORCE_AFTER_H` (48 h), still flat:
- pending Windows reboot → **MAINTENANCE · rebooting for Windows Update in 60 s**, marker `maintenance`, `shutdown /r /t 60`;
  a position opening during the countdown aborts it (`shutdown /a`) and reschedules. Back up: **BACK ONLINE · maintenance reboot**.
- code: `git` → origin/master, then the full suite; green → restart, red → stay on the old commit and skip that sha
  (the old every-5-minutes auto-update now runs here).
- housekeeping once a day: stall dumps / rotated service logs / replays older than `AUREON_LOG_KEEP_DAYS`, charts > 7 days
  (never the journal, state, incidents or features); `pip install -r requirements.txt` when it changed.

**Cold start.** A boot with no restart marker posts **COLD START**: boot time, last shutdown reason from the System log
(1074 planned / Windows Update, 6006 clean, 6008 unexpected), minutes down since the last heartbeat, and the positions re-adopted
across the gap. Every BACK ONLINE card now says `down X s`.

**Clock and network.** Clock drift is checked at boot and every 6 h against time.windows.com; > 2 s → resync + **CLOCK DRIFT**.
If Discord **and** the dead-man ping fail while MT5 still ticks, Aureon keeps polling and managing on the existing stops but opens
no new trades; missed cards are summarised in one **NETWORK BACK** card. It never flattens on a network loss.

**Remote access.** Use **Tailscale** for phone/device access to the box (and to the v2.11 console). Nothing listens on a public
interface.

**`/server`** shows uptime, last boot reason, pending updates + window, MT5 (pid, login, tick age), clock drift, disk, RAM, CPU,
repo commit vs origin/master, the last three restarts and open incidents. **`/env-check`** shows what the guardian actually runs on.

## v2.11.2 — network loss and where to install

**Install at `C:\Aureon`**, not under OneDrive / Dropbox / Google Drive / iCloud (Desktop and Documents are often redirected
into OneDrive). Sync clients lock and rewrite `guardian_state.json`, `features.json`, `restart_marker.json` and
`journal.jsonl` while the bot writes them. `config.validate` flags such a path on the BOOT CHECK card and in `/env-check`.
**Run the NSSM service from §1.** `python main.py` in a console is for tests only: on 2026-10-15 such a console process
ended during a DNS outage and nothing restarted it.

What a network loss does now (`aureon_mt5/netguard.py`):
- **Nothing network-related ends the process.** A gateway that stays down is no longer a self-restart reason (only a frozen
  event loop is). The Discord client is restarted forever (2 s → 60 s backoff) until a planned stop.
- **Degraded** (gateway down > 60 s, or 2 webhook failures in a row, or discord.com does not resolve): polling and the guardian
  are unchanged and there are no new entries. Cards are written to `logs/card_queue.jsonl` and flushed on reconnect behind a
  single BACK ONLINE summary. NETWORK DEGRADED / RESTORED cards carry a DNS self-check.
- **Every exit is recorded:** `process_exit` in the journal and in `restart_marker.json` / `logs/exit_marker.json`.
  Unplanned exits return 1, so NSSM restarts the process; planned exits return 0, restarts return 75.

## v2.15.0 — reboot sequence (no human)

MT5 runs in the **interactive desktop session** of the user who owns the terminal. The recommended install is now two
**logon Scheduled Tasks**, not an NSSM service. This replaces "run the NSSM service" above: a service runs outside that session,
so `MetaTrader5.initialize()` cannot reach the terminal.

```powershell
cd C:\Aureon
.\scripts\aureon_service.ps1 -Action uninstall           # only if an NSSM service 'Aureon' was installed before
powershell -ExecutionPolicy Bypass -File .\scripts\install_logon_tasks.ps1     # -Mt5Path "...\terminal64.exe" if not in .env
# once: Sysinternals Autologon for this user, and never sleep:
powercfg -change -standby-timeout-ac 0
powercfg -change -hibernate-timeout-ac 0
```

The sequence after a Windows reboot (or a Windows Update restart):

```
boot → auto-logon → task Aureon-MT5 (0 s: terminal64.exe) → task Aureon (+30 s: launcher loop) → python main.py
     → pre-flight → BACK ONLINE after restart
```

**Pre-flight** (`aureon_mt5/preflight.py`, first thing in `main.py` after the config, before any agent; every step is logged
to `logs/aureon.log` as `preflight: …`):
1. **Terminal.** If `terminal64.exe` is not running, start it from `AUREON_MT5_PATH` (else the path cached in `logs/mt5_path.txt`).
   It is started through `Start-Process`, not as a child, so a supervisor `taskkill /T` of the bot never takes the terminal
   with it. No known path → a warning, and the step is skipped.
2. **MT5 + login.** Retry `MetaTrader5.initialize()` **and** `account_info()` every 5 s for up to 90 s. `initialize()` alone
   succeeds against a terminal that has not logged in yet, and the old wait only checked that.
   - `logs/heartbeat.txt` is touched on every attempt. Before v2.15.0 the heartbeat only started after MT5 connected. The MT5
     wait (300 s) equalled the launcher's start grace (300 s), so a slow terminal was killed as a **HANG** and the supervisor
     looped.
   - Still down after 90 s → exit 75. The launcher (re)starts the terminal and the bot about 5 s later.
   - The first failure time is kept in `logs/preflight.json` across those restarts. **MT5 NOT UP** is posted once when the
     outage passes `AUREON_MT5_BOOT_WAIT` (300 s).
3. **Market closed** → nothing exits. The agents start and sleep until the open, keeping the position snapshot fresh (the
   `/mode` and restart gates read it).
4. **One card:** **BACK ONLINE after restart · MT5 build · account · open positions re-adopted** (plus how long MT5 took and the
   outage across restarts). It is posted after the existing COLD START / BACK ONLINE cards, which still give the reason for the
   restart.

Check it: `Restart-Computer`, then within about 2 min Discord shows BACK ONLINE after restart, `logs/aureon.log` has the
`preflight:` lines, and `.\scripts\aureon_service.ps1 -Action status` shows a fresh heartbeat.
