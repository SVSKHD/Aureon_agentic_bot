# Trade origins: my trades vs the bot's

The bot labels every order it sends with its **magic number** and pushes the account's deal history and open positions
to the Aureon server, so manual trades and bot trades can be told apart, stored and compared. The server side (tables,
API, dashboard, Discord) is documented in Aureon_agents_Assembler `docs/trade-origins.md`.

Nothing here changes how the bot decides or places trades: the magic is one extra field on the order request, and the
history is read with `history_deals_get` / `history_orders_get` / `positions_get` / `account_info` only.

## The magic number

| | |
|---|---|
| Default | **`261103`** |
| Setting | `AUREON_MAGIC` in the bot's `.env` (positive integer; anything else falls back to 261103) |
| Where | `broker.bot_magic()`, on every order the bot sends: market entries, pending (limit) entries, closes, partial closes |

Keep it fixed: changing it later makes older bot trades fall back to the `reason == EXPERT` rule (still "bot", see below).
Don't give a manual EA on the same account the same magic.

## Classification

Who executed one deal (`actor`):

| magic | reason | actor |
|---|---|---|
| = the bot's magic | any | `bot` |
| any | `DEAL_REASON_EXPERT` (3) | `bot` (API orders; covers bot trades from before the magic existed) |
| 0 | `CLIENT` 0 / `MOBILE` 1 / `WEB` 2 | `manual` |
| other | `SL` 4 / `TP` 5 / `SO` 6 | `broker` (the server hit a stop / target / stop-out) |
| anything else | anything else | `unknown` (raw `reason` kept) |

A **position's origin** is the actor of its opening deal (or, for an open position whose opening deal is outside the
window, the position's own magic / reason). Every deal of the position carries that origin, so a manual trade closed
by its SL is still `manual`. `broker` can't open a position, so that becomes `unknown`.

**Guardian-managed**: a `manual` position the bot acted on keeps `origin: manual` and gets `guardian_touched: true`
when the bot closed or partially closed it (a bot deal on it), the journal has a guardian event for its ticket
(`protected`, `secured`, `trail_update`, `structure_sl`, `locked`, `early_lock`, `soft_lock_exit`, `partial`,
`closed_by_rule`, `exit`, `pre_cross`), or the guardian holds state for it right now. SL moves (`TRADE_ACTION_SLTP`)
leave no deal in MT5, which is why the journal and guardian state are used too. The server keeps the flag once set.

## What is pushed (`trades` in `aureon.mt5.push/v1`)

```json
"trades": {
  "acct": "9f2c4b1a7d3e5f60",   "server": "Broker-Demo", "trade_mode": "demo", "currency": "USD",
  "magic": 261103, "server_offset_h": 2.0,
  "window": {"from": 1730592000, "to": 1730678400, "kind": "incremental", "deal_count": 3, "truncated": false},
  "deals": [{"ticket": 3, "order": 13, "position_id": 300, "symbol": "XAUUSD", "side": "buy", "entry": "in",
             "volume": 0.1, "price": 2650.0, "sl": 2640.0, "tp": 0.0, "profit": 0.0, "commission": -0.5, "swap": 0.0,
             "fee": 0.0, "time": 1730676600, "time_msc": 1730676600007, "magic": 0, "comment": "", "reason": 1,
             "reason_name": "mobile", "actor": "manual", "origin": "manual", "guardian_touched": true}],
  "positions": [{"ticket": 400, "position_id": 400, "symbol": "XAUUSD", "side": "sell", "volume": 0.2,
                 "price_open": 2654.0, "price_current": 2653.0, "sl": 2664.0, "tp": 2630.0, "profit": 20.0, "swap": 0.0,
                 "time": 1730678100, "magic": 261103, "comment": "aureon-alert", "reason": 3, "reason_name": "expert",
                 "origin": "bot", "guardian_touched": false, "initial_sl": 2664.0}]
}
```

- `acct` = `sha256("<server>|<login>")[:16]`: the account key the server stores deals under. The login itself is
  never sent (the bridge drops any key containing `account` / `login`).
- Times are **UTC** epochs (server time minus the agents' measured `offset_h`).
- Only buy / sell deals; balance, credit and other non-trade deals are left out.
- `sl` / `tp` on a deal come from the order that made it; `initial_sl` on a position is the SL of its opening deal.
- **Windows.** `backfill` = the last 7 days, on the first push after a start and then every 24 h. `incremental` = from
  the newest deal the server acknowledged minus one hour, so nothing is lost across restarts or failed pushes (the
  window moves only on a 2xx, like the journal cursor; both live in `logs/push_cursor.json`). The server upserts on
  `(acct, ticket)`, so re-sent deals are harmless, and it flags gaps when a backfill brings deals it never had.
- Every push carries **all open positions**; a position missing from the list is closed.
- Absent when MT5 is not connected. Max 5000 deals per push (oldest first; the rest follows).

## Code

`aureon_mt5/trade_history.py` (classification + collector; reads only), `aureon_mt5/push_bridge.py` (the `trades`
section, still with no MT5 import: main.py injects the collector), `aureon_mt5/broker.py` (`bot_magic()` on the
four order builders), `tests/test_trade_history.py`.

## After updating

`systemctl --user restart aureon-bot` (Linux, docs/LINUX_SETUP.md). The first push sends the last 7 days.
