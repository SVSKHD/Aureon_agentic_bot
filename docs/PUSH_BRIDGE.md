# Push bridge (read-only) — bot → Aureon server

Off by default. With `AUREON_PUSH_ENABLED=1` the bot POSTs one JSON document (`schema: "aureon.mt5.push/v1"`) to the
Aureon server every `AUREON_PUSH_INTERVAL_S` seconds, so the Aureon dashboard can show the bot's status, the guardian,
the latest signals, journal events, crashes and recent candles. The bot connects *out*; nothing on the Windows PC has to
listen or be exposed.

It **only sends what the bot already holds**. It makes **no MT5 calls** (candles come from the bars the agents already
cached), places / modifies / cancels nothing, and `aureon_mt5/push_bridge.py` never imports `broker` or `MetaTrader5`
(`tests/test_push_bridge.py` checks the AST). It runs on its own daemon thread with a 10 s timeout, so a slow or dead
server never blocks an agent, the guardian or Discord. A failure is printed at most once a minute. Strategies, the
guardian, cards and slash commands are unchanged. It is independent of the status export (`docs/STATUS_EXPORT.md`):
either, both or neither can be on.

## Settings (env or `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AUREON_PUSH_ENABLED` | `0` | `1` turns the bridge on |
| `AUREON_PUSH_URL` | `https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest` | where to POST |
| `MT5_INGEST_TOKEN` | *(empty)* | **required** when enabled, 32+ chars, the same value as the server's `.env`. Never logged or printed |
| `AUREON_PUSH_INTERVAL_S` | `AUREON_STATUS_INTERVAL_S` (15) | 5–300 |
| `AUREON_PUSH_GZIP` | `1` | `0` sends plain JSON |
| `AUREON_PUSH_CANDLES` | `200` | bars per symbol per timeframe, 0–300 |

With the bridge enabled and the token missing or shorter than 32 characters the bridge does not start and the console
prints `MT5_INGEST_TOKEN not set or shorter than 32 chars — push bridge off`. The bot itself keeps running.

The only file the bridge writes is `logs/push_cursor.json` (the acknowledged journal position).

## Transport

```
POST <AUREON_PUSH_URL>
Authorization: Bearer <MT5_INGEST_TOKEN>
Content-Type: application/json
Content-Encoding: gzip                 (unless AUREON_PUSH_GZIP=0)
X-Aureon-Push-Id: <uuid4>              (== body.push_id)
```

Any `2xx` (`202 accepted`, `200 duplicate`) is an acknowledgement: the journal cursor moves past the events in that
push. Anything else (`401`, `413`, `422`, `503`, timeout, no network) leaves the cursor where it was, so the same events
(with the same ids) go again on the next poll; the server de-duplicates on the event id.

## Document

A superset of `aureon.mt5.status/v1` (every v1 key, same meaning — see `docs/STATUS_EXPORT.md`), with:

| Key | Content |
|---|---|
| `schema` | `"aureon.mt5.push/v1"` |
| `push_id`, `seq` | uuid4 per push; `seq` is monotonic per bot process (starts at 1) |
| `health` | `status` `ok|degraded|error`; `mt5` `{connected, terminal, last_error}`; `discord` `{ok, last_error, last_error_at}`; `market` `{SYM: {open, tick_age_s, session}}` |
| `guardian` | `{SYM: [{ticket, phase, leg_phase, profile, sl, secured_level, peak, secured_pts, peak_pts, direction, entry, bars, frozen, updated_at}]}` |
| `signals_latest` | `{SYM: {kind: <latest journal row>}}`, kinds `P`, `cross`, `reentry`, …, `protected`, `secured`, `exit`, `closed`, `error`, `partial`, `locked`, `pre_cross`, `cross_confirmed` |
| `events` | journal rows since the last **acknowledged** push, oldest first, each with a stable `id` |
| `tracebacks` | exceptions since the last acknowledged push (max 20, text ≤ 8 KiB): `{id, t, where, type, message, traceback}` |
| `strategy` | `{mode, symbols, exec_tf, timeframes, ema: {fast, slow}, params}` |
| `candles` | `{SYM: {TF: [{t, o, h, l, c, v, ema: {"<fast>": x, "<slow>": y}}]}}` |
| `trades` | deal history (incremental + a daily 7-day backfill) and all open positions, each with `origin` (`bot`/`manual`/`unknown`), `actor`, `magic`, `reason`, `guardian_touched`: docs/TRADE_ORIGINS.md. The one section read from MT5 (`aureon_mt5/trade_history.py`, read-only calls, injected by main.py) |

Notes:

- `events[].id` = `sha1(t|event|symbol|ticket|bar|offset)` where `offset` is the row's byte offset in
  `logs/journal.jsonl` (append-only), so it is stable across retries and restarts. Events are the status/v1 allowlist
  plus the guardian / crash rows: `protected`, `pre_cross`, `locked`, `early_lock`, `soft_lock_exit`,
  `soft_lock_cleared`, `setup_invalidated`, `trail_update`, `structure_sl`, `pending_filled|expired|cancelled`,
  `agent_crash`, `signal_suppressed`, `cross_skipped`, `pre_timeout_hold`, `self_restart`, `state_rebuilt`,
  `entry_blocked_htf`. The first push of a fresh install sends at most the last 48 h (`AUREON_STATUS_JOURNAL_HOURS`).
- `tracebacks[].where`: `agent:<SYM>` (an agent crash, from the journal), `guardian` (a failure whose title / action
  names the guardian), `discord`, or `main`. Source: the in-process failure ring `telemetry.RECENT` and `agent_crash` rows.
- `guardian`: one row per position the agent guards. `sl` is the broker stop, `secured_level` / `peak` are **prices**
  (entry ± the guardian's points; the raw points are in `secured_pts` / `peak_pts`). `phase` is the guardian's own
  wording (`pre`, `protect`, `secured`, `ride`; leg profile: `pre`, `protect`, `ride`).
- `candles`: the exec-TF frame each agent keeps (`last_df`, EMAs already added) and the HTF confirm frames it cached
  (only when `AUREON_HTF_ALIGN` is on and the agent has fetched them). EMA keys are the strategy's periods. Bar times
  are MT5 server-clock epochs — subtract `agents[SYM].offset_h × 3600` for UTC.
- **Size cap**: if the body would exceed 1.5 MiB, candles are cut first (halved until they fit, then dropped), then the
  newest events are held back; the cursor stops before them so they go on the next push, in order. Nothing is lost.
  `errors.candles` / `errors.events` say when this happened.
- **Secrets**: any key containing `token`, `auth`, `password`, `secret`, `webhook`, `url`, `login` or `account` is
  dropped at every depth before sending. No account number, balance or login is in the document (`trades.acct` is a
  hash of server + login).

## Linux (the Aureon server itself)

When the bot runs on the Aureon server under Wine, follow [LINUX_SETUP.md](LINUX_SETUP.md) and push
over loopback, not Tailscale:

```
AUREON_PUSH_URL=http://127.0.0.1:8000/api/trading/mt5/ingest
```

## Windows setup

Do these on the Windows PC that runs the bot (PowerShell as the same Windows user that runs the bot / NSSM service).

1. **Tailscale.** Install it (`winget install tailscale.tailscale`, or https://tailscale.com/download/windows), sign in
   with the account that owns the `taila439d5` tailnet, and check the PC shows as *Connected*:
   ```powershell
   tailscale status
   ```
   In the Tailscale admin console, disable key expiry for this machine so the bridge does not stop after 180 days.

2. **Reach the server.**
   ```powershell
   Invoke-WebRequest -UseBasicParsing https://svskhd8.taila439d5.ts.net/api/health | Select-Object StatusCode, Content
   ```
   Expect `StatusCode 200`. A name-resolution error means Tailscale is not connected or MagicDNS is off.

3. **The token.** Get `MT5_INGEST_TOKEN` from the server's `.env` (the same value on both sides, 32+ characters). If you
   have to create one: `python -c "import secrets; print(secrets.token_urlsafe(48))"` and put it in both places.
   Optional check that the token is accepted (expect `422` for an empty body, `401` means the token is wrong):
   ```powershell
   $t = "<paste token>"
   try { Invoke-WebRequest -UseBasicParsing -Method Post -Uri https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest `
         -Headers @{Authorization="Bearer $t"} -ContentType application/json -Body '{}' } catch { $_.Exception.Response.StatusCode.value__ }
   ```

4. **Settings — in `.env` (recommended).** The bot loads `.env` from the repo root (`load_dotenv()` in `main.py` and
   `aureon_mt5/config.py`), the service runs `python main.py` there, and `.env` is never committed or touched by
   auto-update. Add:
   ```
   AUREON_PUSH_ENABLED=1
   MT5_INGEST_TOKEN=<the token>
   # optional (defaults shown)
   # AUREON_PUSH_URL=https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest
   # AUREON_PUSH_INTERVAL_S=15
   # AUREON_PUSH_GZIP=1
   # AUREON_PUSH_CANDLES=200
   ```
   **Or with `setx`** (persistent user environment; only processes started *after* this see it, and a real environment
   variable wins over `.env`):
   ```powershell
   setx AUREON_PUSH_ENABLED 1
   setx MT5_INGEST_TOKEN "<the token>"
   ```
   If the NSSM service runs under a different account than yours, use `setx /M` (admin PowerShell, machine-wide), or
   simply use `.env`. Do not set both with different values.

5. **Run the bot with the bridge.** Normally the bot runs as the NSSM service / Scheduled Task from `docs/SERVICE.md`,
   which starts `python main.py` in the repo root — no command-line change is needed; restart it so it reads the new
   settings (`/restart` in Discord, or):
   ```powershell
   cd C:\Aureon_agentic_bot
   Restart-Service Aureon            # NSSM service; with the Scheduled Task: Stop-ScheduledTask Aureon; Start-ScheduledTask Aureon
   ```
   For a one-off foreground test (not for live use — see README):
   ```powershell
   cd C:\Aureon_agentic_bot
   $env:AUREON_PUSH_ENABLED = "1"; .\.venv\Scripts\python.exe main.py
   ```

6. **Verify a push landed.**
   - Bot side: `logs\service.log` (or the console) shows
     `aureon push: POST every 15 s to https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest (gzip)` at start and no
     `aureon push failed: …` line after it. `logs\push_cursor.json` appears after the first acknowledged push and its
     `journal_offset` grows as new journal rows are delivered.
     ```powershell
     Select-String -Path logs\service.log -Pattern "aureon push" | Select-Object -Last 5
     Get-Content logs\push_cursor.json
     ```
   - Server side: the Aureon MT5 dashboard shows the bot with a fresh "last push" time (it turns `stale_heartbeat` after
     90 s without a push).
   - `aureon push failed: HTTP 401` = token mismatch; `HTTP 503` = the server has no `MT5_INGEST_TOKEN`; `URLError` /
     `timeout` = Tailscale or the server is down. The bot keeps trading normally in every case and retries every poll.

To turn it off: `AUREON_PUSH_ENABLED=0` (or remove the line) and restart.

## Not sent

Account balance / equity / login, closed-deal history, pending orders (not held without new MT5 calls), candles for a
timeframe no agent has cached, anything from `.env`.
