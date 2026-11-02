# Status export (read-only) — for Aureon_Agentic

Off by default. With `AUREON_STATUS_EXPORT=1` the bot writes one JSON document, `logs/status.json`, every
`AUREON_STATUS_INTERVAL_S` seconds (default 15). Aureon_Agentic's `MT5Adapter` reads it, either from the file or from
an optional GET-only HTTP endpoint.

It **only exports what the bot already holds**: the per-symbol agent snapshot (the same cache the slash commands read),
agent health, the latest signal, the read-only part of `/console/state`, allowlisted journal events, `halt.json`, the
`ema921e_trades.csv` tail and the `summary` block of `out/*.json` backtests. It makes **no MT5 calls**,
places/modifies/cancels nothing, and the module never imports `broker` or `MetaTrader5`
(`tests/test_status_export.py` checks the AST). Strategies, the guardian, Discord cards and slash commands are unchanged.
A failure in the export is printed at most once a minute and never reaches the agents.

## Settings (env only)

| Variable | Default | Meaning |
|---|---|---|
| `AUREON_STATUS_EXPORT` | `0` | `1` turns the export on |
| `AUREON_STATUS_FILE` | `logs/status.json` | where the document is written (tmp + `os.replace`) |
| `AUREON_STATUS_INTERVAL_S` | `15` | 5–300 |
| `AUREON_STATUS_EVENTS` | `500` | max journal events in `events` |
| `AUREON_STATUS_JOURNAL_HOURS` | `48` | how far back `events` goes |
| `AUREON_STATUS_TRADES` | `200` | max `ema921e_trades.csv` rows |
| `AUREON_STATUS_BACKTEST_DIR` | `out` | where `run.py` writes backtest JSON |
| `AUREON_STATUS_TOKEN` | *(empty)* | 32+ chars turns on the HTTP endpoint; empty = file only |
| `AUREON_STATUS_BIND` | `127.0.0.1:8788` | loopback only unless `AUREON_STATUS_ALLOW_PUBLIC=1` |
| `AUREON_STATUS_ALLOW_PUBLIC` | `0` | allow a non-loopback bind (prefer `tailscale serve` instead) |

The status token is separate from `AUREON_CONSOLE_TOKEN`: it can only read, so leaking it never lets anyone trade.

## HTTP

```
GET /status          Authorization: Bearer <AUREON_STATUS_TOKEN>   → 200 the document | 401 | 503 not ready
anything else        404        any non-GET method → 405
```

Reach it from the Aureon server over Tailscale, e.g. on the bot machine `tailscale serve --bg --https=8788 http://127.0.0.1:8788`.

## Document (`schema: "aureon.mt5.status/v1"`)

```json
{
  "schema": "aureon.mt5.status/v1",
  "generated_at": "2026-11-02T10:00:15+00:00", "generated_ts": 1793613615,
  "bot": {"version": "3.0.0", "mode": "ema5080", "symbols": ["XAUUSD"], "exec_tf": "M5", "dry": false,
          "server_utc_offset": 3.0, "uptime_s": 5400, "pid": 4242},
  "agents": {"XAUUSD": {
      "symbol": "XAUUSD", "strategy": "ema5080", "exec_tf": "M5", "alive": true,
      "snapshot_at": "2026-11-02T10:00:10+00:00", "market_open": true, "connected": true, "mt5": "CONNECTED",
      "tick_age": 1.4,
      "positions": [{"ticket": 101, "symbol": "XAUUSD", "direction": "long", "price_open": 2650.5, "time": 1793620800,
                     "sl": 2640.0, "tp": 0.0, "volume": 0.1, "current": 2655.25, "comment": "", "magic": 0, "points": 4.75}],
      "today": {"signals": 1, "secured": 0.0, "errors": 0, "closed": 0},
      "health": {"status": "ok", "last_bar": 1793620500, "offset_h": 2.0, "...": "..."},
      "offset_h": 2.0,
      "latest_signal": {"bar_time": 1793620500, "symbol": "XAUUSD", "strategy": "ema5080", "kind": "cross",
                        "direction": "LONG", "consumed": false}}},
  "halt": null,
  "console": {"version": "3.0.0", "t": "...", "session": {}, "regime": {}, "timeframe": {}, "network": {}, "health": {}},
  "ema921e": null,
  "events": [{"t": 1793613500, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": "LONG",
              "signal_kind": "cross", "price": 2650.1, "bar": 1793620500, "...": "..."}],
  "ema921e_trades": [],
  "backtests": [{"file": "XAUUSD_jan_ema5080.json", "mode": "ema5080", "symbol": "XAUUSD",
                 "summary": {"trades": 10, "wins": 6, "win_rate": 0.6, "net": 55.5, "avg": 5.55},
                 "modified_at": "..."}],
  "errors": {}
}
```

Times: `generated_*`, `snapshot_at` and journal `t` are real UTC. **Bar times** (`bar`, `bar_time`, `last_bar`) and
position `time` are MT5 **server-clock** epoch seconds, exactly as the bot keeps them; subtract
`offset_h × 3600` (per agent) for UTC. Prices are JSON numbers as MT5 returns them; consumers should parse them as
decimals from their text.

`events` is an allowlist: `signal`, `position_seen`, `exit`, `order_placed`, `pending_placed`, `secured`, `partial`,
`closed_by_rule`, `cross_confirmed`, `regime_change`, `error`, `process_start`, `process_exit`, `watchdog_open`,
`watchdog_clear`. Fields named token/auth/password/secret/webhook/url are dropped. `errors` names any section that
failed this round (the rest of the document is still valid).

Not exported (the bot does not compute them without new MT5 calls): account balance/equity, closed deal history,
pending orders.
