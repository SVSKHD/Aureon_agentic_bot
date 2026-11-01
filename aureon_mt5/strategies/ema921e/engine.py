"""
ema921e_engine.py — the "E" 9/21 rule set as a pure, side-effect-free engine.

Drop into Aureon as strategies/ema921e/engine.py. No MT5, no Discord, no I/O in here:
the mode wrapper feeds it closed bars and acts on the decisions it returns.
The logic is identical to tools/e_strategy_backtest.py (same functions, same order of
checks) so a replay through this engine must reproduce the backtest trade-for-trade.

Rules (gold M30 locked set, 1 Nov 2026):
  entry   every EMA 9/21 cross, at the cross bar's close, unless skipped by:
            spike      EMA 9 slope 2 bars before the cross > SPIKE ATR/bar (toward the cross)
            monday     weekday == 0 (server time)
            hours      hour outside [HOURS_FROM, HOURS_TO)
            low_atr    ATR < ATR_GATE x its 60-day average
            month_stop month PnL already <= -MONTH_STOP per lot
  cut     at the close of bar +CUT_BARS: close <= entry (+CUT_THR x ATR) -> exit
  ladder  LADDER = [(trigger, lock), ...] in ATR units; STEP continues beyond the last rung.
          Floor is set at a bar's close from its high/low and checked against the NEXT bar's range.
  exit    floor touched (exit at floor price), opposite cross (exit at that bar's close),
          disaster stop STOP_ATR x ATR (exit at stop price), in that priority.

Usage:
    eng = EngineE(symbol="XAUUSD", params=Params())
    eng.warmup(history_bars)               # list of closed bars (dicts with time, open, high, low, close)
    for bar in live_closed_bars:
        for action in eng.on_bar(bar):     # Action(kind=..., price=..., reason=...)
            execute(action)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import numpy as np


# ----------------------------------------------------------------------------- params
@dataclass
class Params:
    ema_fast: int = 9
    ema_slow: int = 21
    atr_len: int = 14
    slope_bars: int = 3
    spike: float = 0.15                 # ATR/bar; 0 disables the spike skip
    no_monday: bool = True
    hours_from: int = 0                 # server hours, [from, to)
    hours_to: int = 24
    atr_gate: float = 0.7               # 0 disables
    atr_ref_bars: int = 2880            # ~60 days of M30
    month_stop_per_lot: float = 2000.0  # $ per lot; 0 disables
    cut_bars: int = 2
    cut_thr_atr: float = 0.0
    ladder: tuple = ((0.25, 0.05), (0.5, 0.25), (1.0, 0.5))   # (trigger, lock) in ATR
    step: float = 0.5                   # ATR
    stop_atr: float = 3.0               # disaster stop; 0 disables
    lot: float = 0.3
    lot_value: float = 100.0            # $ per 1.0 price unit per 1 lot (gold 100, silver 5000, FX 100000, JPY 100000/price)
    spread: float = 0.25                # price units, deducted per trade in PnL accounting


@dataclass
class Action:
    kind: str                           # "enter" | "exit" | "skip" | "floor"
    side: int = 0                       # +1 long, -1 short
    price: float = 0.0                  # entry / exit / floor price
    reason: str = ""                    # skip reason or exit reason: cut | floor | cross | stop
    bar_time: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Position:
    i: int
    side: int
    entry: float
    atr: float
    mfe_atr: float = 0.0
    floor_atr: float | None = None
    opened: datetime | None = None

    @property
    def floor_price(self) -> float | None:
        return None if self.floor_atr is None else self.entry + self.side * self.floor_atr * self.atr


# ----------------------------------------------------------------------------- engine
class EngineE:
    def __init__(self, symbol: str, params: Params | None = None):
        self.symbol = symbol
        self.p = params or Params()
        self.t: list[datetime] = []
        self.o: list[float] = []
        self.h: list[float] = []
        self.l: list[float] = []
        self.c: list[float] = []
        self.ema9: list[float] = []
        self.ema21: list[float] = []
        self.atr: list[float] = []
        self.side: list[int] = []
        self.pos: Position | None = None
        self.month: tuple | None = None
        self.month_pnl: float = 0.0
        self.closed_trades: list[dict] = []

    # ---- indicators (incremental, identical to pandas ewm(adjust=False) and ATR ewm(alpha=1/n))
    def _push(self, bar: dict) -> None:
        tt = bar["time"]
        if isinstance(tt, (int, float)):
            tt = datetime.utcfromtimestamp(tt)
        self.t.append(tt)
        self.o.append(float(bar["open"]))
        self.h.append(float(bar["high"]))
        self.l.append(float(bar["low"]))
        self.c.append(float(bar["close"]))
        c = self.c[-1]
        k9, k21 = 2 / (self.p.ema_fast + 1), 2 / (self.p.ema_slow + 1)
        self.ema9.append(c if not self.ema9 else self.ema9[-1] + k9 * (c - self.ema9[-1]))
        self.ema21.append(c if not self.ema21 else self.ema21[-1] + k21 * (c - self.ema21[-1]))
        if len(self.c) == 1:
            tr = self.h[-1] - self.l[-1]
        else:
            pc = self.c[-2]
            tr = max(self.h[-1] - self.l[-1], abs(self.h[-1] - pc), abs(self.l[-1] - pc))
        a = 1 / self.p.atr_len
        self.atr.append(tr if not self.atr else self.atr[-1] + a * (tr - self.atr[-1]))
        self.side.append(int(np.sign(self.ema9[-1] - self.ema21[-1])))

    def _s9(self, i: int) -> float:
        n = self.p.slope_bars
        if i - n < 0 or self.atr[i] == 0:
            return 0.0
        return (self.ema9[i] - self.ema9[i - n]) / n / self.atr[i]

    def _atr_ratio(self, i: int) -> float | None:
        w = self.p.atr_ref_bars
        if i + 1 < 200:
            return None
        ref = float(np.mean(self.atr[max(0, i + 1 - w): i + 1]))
        return self.atr[i] / ref if ref else None

    def _floor_for(self, mfe: float) -> float | None:
        lock = None
        for trig, lk in self.p.ladder:
            if mfe >= trig:
                lock = lk
        last_t, last_l = self.p.ladder[-1]
        if lock is not None and self.p.step > 0 and mfe >= last_t + self.p.step:
            lock = last_l + np.floor((mfe - last_t) / self.p.step) * self.p.step
        return lock

    def _usd(self, pts: float, price: float) -> float:
        v = self.p.lot_value * self.p.lot
        if self.symbol.endswith("JPY"):
            v = v / price
        return pts * v

    # ---- public
    def warmup(self, bars: Iterable[dict]) -> None:
        """Feed historical closed bars without emitting actions (indicators + side history only)."""
        for b in bars:
            self._push(b)

    def on_bar(self, bar: dict) -> list[Action]:
        """Feed one newly CLOSED bar. Returns the actions to execute, in order."""
        self._push(bar)
        i = len(self.c) - 1
        if i < 1:
            return []
        out: list[Action] = []
        crossed = self.side[i] != 0 and self.side[i - 1] != 0 and self.side[i] != self.side[i - 1]
        hi, lo, cl, now = self.h[i], self.l[i], self.c[i], self.t[i]
        p = self.p

        # ---------- manage open position (same priority as the backtest)
        if self.pos is not None:
            ps = self.pos
            d, e = ps.side, ps.entry
            b = i - ps.i
            exit_px, reason = None, None
            fpx = ps.floor_price
            if fpx is not None and ((lo <= fpx) if d > 0 else (hi >= fpx)):
                exit_px, reason = fpx, "floor"
            if exit_px is None and p.stop_atr:
                spx = e - d * p.stop_atr * ps.atr
                if (lo <= spx) if d > 0 else (hi >= spx):
                    exit_px, reason = spx, "stop"
            if exit_px is None and crossed:
                exit_px, reason = cl, "cross"
            if exit_px is None and b == p.cut_bars and (cl - e) * d <= p.cut_thr_atr * ps.atr:
                exit_px, reason = cl, "cut"
            if exit_px is not None:
                pts = (exit_px - e) * d - p.spread
                usd = self._usd(pts, e)
                self.month_pnl += usd
                self.closed_trades.append({"symbol": self.symbol, "time": ps.opened, "exit_time": now,
                                           "dir": "LONG" if d > 0 else "SHORT", "entry": e, "exit": exit_px,
                                           "reason": reason, "bars": b, "mfe_atr": ps.mfe_atr,
                                           "pnl_pts": pts, "pnl_usd": usd})
                out.append(Action("exit", d, exit_px, reason, now, {"bars": b, "pnl_pts": pts, "pnl_usd": usd}))
                self.pos = None
            else:
                m = ((hi - e) if d > 0 else (e - lo)) / ps.atr
                if m > ps.mfe_atr:
                    ps.mfe_atr = m
                    f = self._floor_for(m)
                    if f is not None and (ps.floor_atr is None or f > ps.floor_atr):
                        ps.floor_atr = f
                        out.append(Action("floor", d, ps.floor_price, f"lock +{f:.2f} ATR", now,
                                          {"mfe_atr": m, "floor_pts": f * ps.atr}))

        # ---------- new entry on a cross
        if crossed and self.pos is None:
            d = self.side[i]
            if self.month != (now.year, now.month):
                self.month, self.month_pnl = (now.year, now.month), 0.0
            skip = None
            ratio = self._atr_ratio(i)
            if p.spike and self._s9(i - 2) * d > p.spike:
                skip = "spike"
            elif p.no_monday and now.weekday() == 0:
                skip = "monday"
            elif not (p.hours_from <= now.hour < p.hours_to):
                skip = "hours"
            elif p.atr_gate and ratio is not None and ratio < p.atr_gate:
                skip = "low_atr"
            elif p.month_stop_per_lot and self.month_pnl <= -p.month_stop_per_lot * p.lot:
                skip = "month_stop"
            if skip:
                out.append(Action("skip", d, cl, skip, now, {"atr_ratio": ratio, "s9_pre": self._s9(i - 2) * d}))
            else:
                self.pos = Position(i=i, side=d, entry=cl, atr=self.atr[i], opened=now)
                out.append(Action("enter", d, cl, "cross", now,
                                  {"atr": self.atr[i], "cut_at_bar": p.cut_bars,
                                   "ladder_pts": [(t * self.atr[i], l * self.atr[i]) for t, l in p.ladder],
                                   "stop_px": (cl - d * p.stop_atr * self.atr[i]) if p.stop_atr else None}))
        return out

    # ---- state for status cards
    def status(self) -> dict:
        i = len(self.c) - 1
        s = {"symbol": self.symbol, "bars": len(self.c), "month_pnl": self.month_pnl,
             "atr": self.atr[i] if i >= 0 else None, "atr_ratio": self._atr_ratio(i) if i >= 0 else None,
             "side": self.side[i] if i >= 0 else 0, "in_position": self.pos is not None}
        if self.pos:
            ps = self.pos
            s.update({"dir": "LONG" if ps.side > 0 else "SHORT", "entry": ps.entry, "bars_in": i - ps.i,
                      "mfe_pts": ps.mfe_atr * ps.atr, "floor_px": ps.floor_price})
        return s
