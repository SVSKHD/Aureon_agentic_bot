# Aureon v3.0 — `ema921e` mode (auto-trade, demo first)

## Goal
Add a new strategy mode `ema921e` that runs the backtested "E" 9/21 rule set **autonomously** on the
demo account from the next trading session. No per-trade human approval. The user supervises
(pause, lot size, kill switch, news pause) — the bot decides and executes every entry, cut, floor
and exit at bar close.

Backtest reference (gold M30, Jan–Oct 2026, 1 lot, tools/e_strategy_backtest.py):
291 trades, 74% win, gross $119,346, PF 2.38, max DD −$12,815, 8/10 months positive, worst −$2,542.
With ATR gate 0.7: 214 trades, $101,206, PF 2.80, DD −$9,545.

## Files delivered (put under the repo)
- `strategies/ema921e/engine.py`  ← `ema921e_engine.py` (pure logic, no MT5/Discord; verified
  trade-for-trade against the backtester)
- `tools/e_strategy_backtest.py`  ← the backtester (already on the server as `e200.py`; keep one copy
  in-repo so replays can be re-run)

## The rules (do not reinterpret — the engine is the spec)
1. Timeframe M30, symbol XAUUSD only for this release (engine is symbol-agnostic; others later).
2. Every EMA 9/21 cross at the **closed** bar → enter at that bar's close, unless skipped:
   - `spike`: EMA 9 slope two bars before the cross > 0.15 ATR/bar toward the cross
   - `monday`: server weekday is Monday (no new entries; open trades still managed)
   - `hours`: outside configured window (default 0–24, i.e. off)
   - `low_atr`: ATR(14) < 0.7 × its 60-day (2880-bar) average
   - `month_stop`: month PnL ≤ −$2,000 × lot (hard safety, expected never to fire)
3. Bar +2 after entry: if close ≤ entry → exit at that close (`cut`).
4. Ladder, in ATR at entry: touch +0.25 → floor +0.05; +0.5 → +0.25; +1.0 → +0.5; then +0.5 per +0.5.
   Floor is computed at a bar's close from that bar's high/low and only acts from the next bar.
5. Exit priority each closed bar: floor touched (exit at floor price) → disaster stop 3×ATR
   (exit at stop price) → opposite 9/21 cross (exit at close) → bar-2 cut.
6. No fixed TP. No broker SL other than the disaster stop (see execution below).

## Execution mapping (MT5)
- Bar-close loop: on each new M30 bar open, treat the previous bar as closed and call
  `engine.on_bar(bar)`; execute returned `Action`s **in order, immediately** (market orders, IOC/RETURN
  filling as configured in 2.16, never FOK).
- `enter`: market order, `lot` from config. Place a **broker SL at the disaster stop** price
  (3×ATR) at entry — the only broker-side stop. Do not place TP.
- `floor`: keep the floor **bot-side** (it is ~1 pt at the first rung; a broker stop there would be
  filled by spread). The bot checks floor vs. the live tick between bar closes:
  if price trades through the floor intra-bar → market close immediately (matches the backtest,
  which exits at the floor price on the bar that touched it). Also move the broker SL up to the
  floor only once the floor ≥ +0.25 ATR (second rung), to protect against disconnects.
- `exit` with reason `cut` / `cross`: market close at that bar close.
- Reconnect/restart: on startup, rebuild the engine from the last 3,000 closed bars via `warmup()`,
  then reconcile with the open MT5 position (if one exists and the engine has none, adopt it with
  entry = position open price and bars_in from the deal time; if the engine expects one and MT5 has
  none, log and continue flat).
- Weekend: the engine only runs when the feed is live; Monday skip covers the open.

## Config (`config/ema921e.yaml`, all hot-reloadable via `/e set`)
```yaml
symbol: XAUUSD
timeframe: M30
lot: 0.3
spike: 0.15
no_monday: true
hours: [0, 24]
atr_gate: 0.7
month_stop_per_lot: 2000
cut_bars: 2
ladder: [[0.25, 0.05], [0.5, 0.25], [1.0, 0.5]]
step: 0.5
stop_atr: 3.0
news_pause_minutes: 30        # optional: pause entries around high-impact events if a calendar feed exists
paper: true                   # demo account; flips to live only by explicit /e live
```

## Discord
- `/e status` — engine `status()`: side, ATR, ATR ratio vs norm, in-position, entry, bars in, MFE,
  current floor, month PnL, today's skips by reason.
- `/e on` / `/e off` — enable/disable new entries (open position keeps being managed).
- `/e flat` — close now.
- `/e set <key> <value>` — hot config change, echo the new value.
- Cards (keep them short): `ENTER LONG 4012.3 | ATR 18.4 | cut@bar2 | floor rungs 4.6/9.2/18.4 | stop 3957`,
  `FLOOR +0.25 ATR → 4016.9`, `EXIT floor +9.1 pts +$273 (bars 6)`, `SKIP spike / monday / low_atr`.
- Daily 23:00 summary: trades, cut/floor/cross/stop counts, $ day, $ month, vs backtest expectation
  (≈ $400/trade at 1 lot, ~7 trades/week).

## Logging (this is the acceptance data)
`logs/ema921e_trades.csv` — one row per closed trade, same columns as the backtester's
`trades_<SYMBOL>_<TF>.csv` (time, dir, entry, exit, reason, bars, mfe, pnl_pts, pnl_usd) plus
`fill_slippage_pts` (requested close vs actual fill). `logs/ema921e_skips.csv` for every skipped cross.

## Acceptance tests (must pass before enabling auto-trade)
1. **Replay parity**: feed Jan–Oct 2026 M30 bars through `engine.py` and compare to
   `tools/e_strategy_backtest.py` with the same flags → identical trade count, exits and gross $
   (this was verified on synthetic data; verify on the real export).
2. **Restart parity**: kill the process mid-trade, restart, confirm the engine adopts the open position
   and the next action matches what an uninterrupted run would do.
3. **Paper week**: run on demo for 5 sessions; `/e status` and the daily summary must agree with the
   trades CSV; slippage median under 1 pt.

## Explicitly out of scope for v3.0
- Multi-symbol (silver/FX ladders need their own ATR validation runs)
- EMA 200 gates, chop gate, pre-cross angle/shape detection, analog scoring (tested, rejected)
- HTF (M45/H1) alignment (untested; later)
- Any change to the ladder or cut without a backtest run attached to the PR

## Guardrails that stay on top
Existing drawdown guard and regime gate remain active in **show** mode for this release; they log
but do not override `ema921e` decisions, so the first month's log can be compared 1:1 to the backtest.
