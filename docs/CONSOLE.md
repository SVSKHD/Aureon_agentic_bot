# Aureon action console (v2.11.0) — contract v1

The console is how a push notification's **TAKE / SKIP / CLOSE / READ** buttons reach the bot. It is a small FastAPI app on
its own uvicorn thread, started and stopped with the bot. It never talks to MetaTrader5: every action goes through
`aureon_mt5/actions.py`, the same code path as Discord's `/take`, `/skip`, `/close` and `/claude-read`.

## Turning it on
```
AUREON_CONSOLE_TOKEN=<32+ random chars>         # python -c "import secrets; print(secrets.token_urlsafe(32))"
AUREON_CONSOLE_BIND=127.0.0.1:8787              # default
AUREON_CONSOLE_URL=https://aureon-box.tail1234.ts.net
```
Expose it to your devices with **Tailscale** only: `tailscale serve --bg 8787` on the trading box publishes
`https://<box>.<tailnet>.ts.net` → `127.0.0.1:8787` inside your tailnet. Put that URL in `AUREON_CONSOLE_URL`; install
Tailscale on the phone. Never port-forward it.

A bind that is not loopback (`0.0.0.0`, a LAN IP) is **refused** unless `AUREON_CONSOLE_ALLOW_PUBLIC=1`; with it, `/env-check`
and the BOOT CHECK card show `⚠️ CONSOLE LISTENS ON …` every boot.

## Auth
`Authorization: Bearer <token>` on every request, one of:
- the **console token** (`AUREON_CONSOLE_TOKEN`), compared in constant time — all endpoints;
- a **scoped action token** `act.<payload>.<sig>` minted per push button (HMAC-SHA256 keyed by the console token):
  valid only for `POST /console/action` with exactly the action, symbol and ask key it was minted for, until the ask expires
  (`AUREON_ASK_TTL_MIN`), **once**. This is what ntfy / Pushover see — the console token never leaves the box.

Limits: 20 requests / minute per client (429 after). Every call — allowed or not — is journaled as
`console_call` with `source="push"`, `path`, `status`, and the action / symbol / key / reasons.

## `POST /console/action`
```json
{"action": "take", "symbol": "XAUUSD", "ask_key": "ema5080:XAUUSD:P_LONG:202610151235", "lots": 0.1}
```
| action | does | refused when |
|---|---|---|
| `take` | `ask_decision` / `alert_decision` → `execute_entry` (governor, live-account rule, lot cap) | no open ask · already answered · stale (older than `AUREON_ASK_TTL_MIN`) · regime gate blocks (reasons listed) · alert without a suggested side · anything `/take` refuses |
| `skip` | the `/skip` journal line; card edited | no open ask · already answered · stale |
| `close` | the `/close` path: `halt.close_all_positions([symbol], comment="aureon-close")` | flat · execution off / dry (lists what it would close) |
| `read` | `claude.read_now` in the background; the verdict arrives as a **high** push and in `last_reads` | Claude off |

`ask_key` omitted → the latest open ask. `symbol` omitted → the primary symbol.
Response: `200 {"ok": true, "text": "...", "reasons": []}` · `409 {"ok": false, "text": "refused — ...", "reasons": [...]}` ·
`400` bad body · `401` auth · `429` rate.

## `GET /console/state`  (console token only)
```json
{
  "v": 1,
  "version": "2.11.0",
  "t": "2026-10-15T12:40:05+05:30",
  "session": {"ist": "12:40", "window": "05:30-23:00", "in_session": true},
  "regime": {"XAUUSD": {"state": "TRENDING", "score": 0.71, "reasons": [], "gate": "off", "blocks": false}},
  "positions": [{"symbol": "XAUUSD", "ticket": 5512, "direction": "long", "entry": 4205.1, "now": 4211.4, "points": 6.3, "sl": 4199.1}],
  "pending_ask": {"key": "ema5080:XAUUSD:P_LONG:...", "symbol": "XAUUSD", "side": "LONG", "kind": "P", "title": "...",
                  "price": 4210.5, "age_s": 42.0, "expires_in_s": 1158.0, "take_refused": [],
                  "chart": "/console/chart/<key>.png", "actions": ["take", "skip", "read"]},
  "last_reads": [{"t": 1792049000.0, "symbol": "XAUUSD", "decision": "HOLD", "confidence": 0.7, "text": "...", "by": "console:push"}],
  "health": {"uptime_s": 5400, "discord_ok": true, "network_degraded": false, "push": ["ntfy"],
             "agents": {"XAUUSD": {"status": "ok", "last_bar": "12:35"}}}
}
```
`v` changes only on a breaking change; fields are only ever added within a version.

## `GET /console/chart/<key>.png`  (console token only)
The chart of the ask `<key>`, or a file by name in `logs/charts/`. Nothing outside `logs/charts/` is served.
