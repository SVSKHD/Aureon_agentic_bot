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
