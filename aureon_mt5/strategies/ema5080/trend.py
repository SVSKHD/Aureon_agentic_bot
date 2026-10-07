"""EMA 50/80 trend state + re-entry detector (notifications only; the P / cross rules in journeys.py are unchanged).

Trend states, read on CLOSED M5 bars (your rule):
  BEARISH                 EMA 50 < EMA 80 and price closes below EMA 50
  BEARISH · PULLBACK      price closes between EMA 50 and EMA 80        → watch for a re-entry
  BEARISH · CHALLENGED    price closes above EMA 80 (lines not crossed)  → bullish turn forming
  (mirror for BULLISH)    the 50/80 cross itself flips the trend

Re-entry (trend continuation) — on bar i, in an established trend:
  * lines in order (bearish: 50 < 80) and EMA 80 sloping with the trend over the last 6 bars
  * within the last 3 bars price pulled back into the EMA 50 zone: high ≥ EMA50 − tol (bearish) / low ≤ EMA50 + tol (bullish)
  * no close beyond EMA 80 during that pullback (that is a CHALLENGE, not a pullback)
  * bar i closes back on the trend side of EMA 50 by at least 0.5 × avg range, as a trend-coloured candle, beyond the previous close
  * one re-entry per pullback; the next needs price to move ≥ 2× avg range away from EMA 50 and then a NEW pullback,
    and at least 6 bars (30 min) after the previous same-side re-entry
  tol = 0.25 × average bar range (20 bars)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TOL_ATR = 0.25
REARM_ATR = 2.0
CLOSE_BEYOND_ATR = 0.5   # the rejection bar must close decisively beyond EMA 50
MIN_GAP_BARS = 6        # at least 30 min between same-side re-entries
SLOPE_BARS = 6
PULLBACK_LOOKBACK = 3


def state_of(close: float, e50: float, e80: float) -> str:
    if e50 < e80:
        return "BEARISH" if close < e50 else ("BEARISH · PULLBACK" if close < e80 else "BEARISH · CHALLENGED")
    return "BULLISH" if close > e50 else ("BULLISH · PULLBACK" if close > e80 else "BULLISH · CHALLENGED")


def trend_states(df: pd.DataFrame, fast_col: str = "ema_fast", slow_col: str = "ema_slow") -> list[str]:
    c = df["close"].to_numpy(); f = df[fast_col].to_numpy(); s = df[slow_col].to_numpy()
    return [("—" if np.isnan(f[i]) or np.isnan(s[i]) else state_of(c[i], f[i], s[i])) for i in range(len(df))]


@dataclass
class ReEntry:
    index: int
    time: int
    side: str          # LONG | SHORT
    price: float
    ema50: float
    ema80: float
    pullback_extreme: float   # how far the pullback reached (high for shorts, low for longs)


def find_reentries(df: pd.DataFrame, fast_col: str = "ema_fast", slow_col: str = "ema_slow") -> list[ReEntry]:
    o = df["open"].to_numpy(); h = df["high"].to_numpy(); l = df["low"].to_numpy(); c = df["close"].to_numpy()
    f = df[fast_col].to_numpy(); s = df[slow_col].to_numpy(); t = df["time"].to_numpy()
    rng = pd.Series(h - l).rolling(20, min_periods=5).mean().shift(1).to_numpy()
    out: list[ReEntry] = []
    # after a re-entry, the next one needs: price away from EMA 50 (>= REARM_ATR x range) AND a NEW touch after that
    away_from = {1: 0, -1: 0}          # pullback touches must be on bars >= this index
    waiting = {1: False, -1: False}    # True = re-entry fired, waiting for price to move away
    for i in range(SLOPE_BARS + PULLBACK_LOOKBACK, len(df)):
        if np.isnan(f[i]) or np.isnan(s[i]) or np.isnan(rng[i]):
            continue
        d = -1 if f[i] < s[i] else 1
        tol = TOL_ATR * rng[i]
        if waiting[d]:
            if (c[i] - f[i]) * d >= REARM_ATR * rng[i]:
                waiting[d] = False; away_from[d] = i + 1
            continue
        if (s[i] - s[i - SLOPE_BARS]) * d <= 0:                   # EMA 80 must slope with the trend
            continue
        lb = range(max(i - PULLBACK_LOOKBACK, away_from[d]), i)
        if len(lb) == 0:
            continue
        if d < 0:
            touched = any(h[k] >= f[k] - tol for k in lb)
            challenged = any(c[k] > s[k] for k in lb)
            back = c[i] < f[i] - CLOSE_BEYOND_ATR * rng[i] and c[i] < o[i] and c[i] < c[i - 1]
            extreme = max(h[k] for k in lb)
        else:
            touched = any(l[k] <= f[k] + tol for k in lb)
            challenged = any(c[k] < s[k] for k in lb)
            back = c[i] > f[i] + CLOSE_BEYOND_ATR * rng[i] and c[i] > o[i] and c[i] > c[i - 1]
            extreme = min(l[k] for k in lb)
        last_same = next((r.index for r in reversed(out) if r.side == ("LONG" if d > 0 else "SHORT")), -10**9)
        if touched and not challenged and back and i - last_same >= MIN_GAP_BARS:
            out.append(ReEntry(i, int(t[i]), "LONG" if d > 0 else "SHORT", float(c[i]), float(f[i]), float(s[i]), float(extreme)))
            waiting[d] = True
    return out
