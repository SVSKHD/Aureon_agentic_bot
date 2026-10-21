"""v2.10.0 — run the LIVE guardian (`SymbolAgent._guard`) bar by bar on recorded M5 bars. No MT5, no Claude, no Discord.

A fake broker fills at the bar close and honours stops intrabar (a bar whose low/high touches the SL closes the trade at the
SL). P signals from the strategy's own analysis are published on their bar exactly like the live agent does, so flips,
the structure gate, giveback and the setup stop behave as live. Used by tests/test_strategy_pins.py, tools/replay_guardian.py
and the regime backtest."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time as _time
import types
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402


class FakeBroker:
    """The MetaTrader5 surface broker.py uses, for ONE position."""
    POSITION_TYPE_BUY = 0; TRADE_ACTION_SLTP = 6; TRADE_ACTION_DEAL = 1; TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1; ORDER_TYPE_BUY = 0; ORDER_FILLING_IOC = 1; ORDER_FILLING_FOK = 0; DEAL_ENTRY_OUT = 1; DEAL_TYPE_SELL = 1
    SYMBOL_TRADE_MODE_DISABLED = 0

    def __init__(self, direction: str, entry: float, price: float):
        self.pos = types.SimpleNamespace(ticket=1, type=0 if direction == "long" else 1, price_open=entry, sl=0.0, tp=0.0,
                                         volume=0.1, time=0, comment="", magic=0)
        self.price = price; self.closed = False
    def initialize(self): return True
    def last_error(self): return (0, "")
    def terminal_info(self): return object()
    def symbol_info(self, s): return types.SimpleNamespace(digits=2, trade_mode=4)
    def symbol_info_tick(self, s): return types.SimpleNamespace(bid=self.price, ask=self.price, time=int(_time.time() + 3 * 3600))
    def positions_get(self, symbol=None, ticket=None): return [] if self.closed else [self.pos]
    def history_deals_get(self, a, b): return []
    def order_send(self, req):
        if req["action"] == self.TRADE_ACTION_SLTP:
            self.pos.sl = req["sl"]; return types.SimpleNamespace(retcode=10009, comment="done")
        self.closed = True; return types.SimpleNamespace(retcode=10009, comment="done")


def hhmm(t: int) -> str:
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%m-%d %H:%M")


def p_signals(bars: pd.DataFrame, mode: str = "ema5080", symbol: str = "XAUUSD") -> dict[int, str]:
    """{bar_time: 'LONG'|'SHORT'} for every P the strategy's analysis finds on these bars."""
    from aureon_mt5.common.source import Bars
    from aureon_mt5.strategies import get_strategy
    df = bars[["time", "open", "high", "low", "close"]].copy(); df["volume"] = 1
    out = get_strategy(mode).analyse({"M5": Bars(symbol, "M5", df)}, symbol, 3.0, ())["M5"]
    return {int(p.time): ("SHORT" if p.label.endswith("S") else "LONG") for p in out.get("pre") or []}


def simulate(bars: pd.DataFrame, entry_i: int, direction: str, entry: float | None = None, *, signals: dict | None = None,
             m15: str | None = None, mode: str = "ema5080", symbol: str = "XAUUSD", max_bars: int | None = None,
             cfg_over: dict | None = None) -> dict:
    """Open `direction` at bar `entry_i` (price = its close unless `entry`) and run _guard on every later closed bar.
    bars needs time/open/high/low/close and the strategy's EMA columns. Returns
    {entry, initial_sl, exit: {i, time, price, points, reason} | None, worst_close, peak_close, journal}."""
    from aureon_mt5 import broker
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.config import Config
    from aureon_mt5.journal import Journal
    from aureon_mt5.notify import Notifier
    from aureon_mt5.strategies import get_strategy

    S = get_strategy(mode); cf, cs = S.ema_cols
    s = 1 if direction == "long" else -1
    entry = float(bars["close"].iloc[entry_i]) if entry is None else float(entry)
    fake = FakeBroker(direction, entry, float(bars["close"].iloc[entry_i]))
    old = (broker._m, broker._offset_h)
    broker._m, broker._offset_h = fake, 3.0
    tmp = tempfile.mkdtemp(prefix="aureon-sim-")
    out = {"entry": entry, "initial_sl": None, "exit": None, "worst_close": 0.0, "peak_close": 0.0}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = Config(); cfg.dry = False; cfg.log_dir = tmp; cfg.claude_open_read = False
            for k, v in (cfg_over or {}).items():
                setattr(cfg, k, v)
            ag = SymbolAgent(symbol, cfg, S, Notifier(None), Journal(tmp), ())
            ag._m15_state = lambda: m15
            last = len(bars) if max_bars is None else min(len(bars), entry_i + max_bars + 1)
            for i in range(entry_i, last):
                row = bars.iloc[i]
                sl = fake.pos.sl
                if i > entry_i and sl and ((s > 0 and float(row["low"]) <= sl) or (s < 0 and float(row["high"]) >= sl)):
                    out["exit"] = {"i": i, "time": hhmm(row["time"]), "price": round(sl, 2), "points": round((sl - entry) * s, 2),
                                   "reason": "sl_hit"}
                    break
                c = float(row["close"]); fake.price = c
                df = bars.iloc[: i + 1].reset_index(drop=True)
                ef, es = float(row[cf]), float(row[cs])
                t = int(row["time"])
                if signals and t in signals:
                    ag.latest_signal = {"bar_time": t, "symbol": symbol, "strategy": mode, "kind": "P", "direction": signals[t],
                                        "consumed": False}
                ag.last_bar = t
                ag._guard(True, t, df, c, ef, es, 1 if ef > es else -1)
                if out["initial_sl"] is None and fake.pos.sl:
                    out["initial_sl"] = round(fake.pos.sl, 2)
                pts = round((c - entry) * s, 2)
                out["worst_close"] = min(out["worst_close"], pts); out["peak_close"] = max(out["peak_close"], pts)
                if fake.closed:
                    ex = [r for r in ag.journal.read(0) if r["event"] in ("exit", "closed_by_rule")]   # v2.16.2: ema921 logs closed_by_rule
                    out["exit"] = {"i": i, "time": hhmm(t), "price": c, "points": pts, "reason": ex[-1]["reason"] if ex else "closed"}
                    break
            out["journal"] = ag.journal.read(0)
            out["last_sl"] = round(fake.pos.sl, 2)
            out["last_close"] = float(bars["close"].iloc[last - 1])
            out["last_time"] = hhmm(bars["time"].iloc[last - 1])
        return out
    finally:
        broker._m, broker._offset_h = old
