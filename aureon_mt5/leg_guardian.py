"""v2.7.0 — leg-based guardian profile: trail on closed bars, ride with the leg, exit on EMA slow close.

Pure functions operating on guardian state dicts. Called from agent._guard() when cfg.guardian_profile == "leg".
The classic profile (pre-v2.7.0) is the else branch in agent._guard() — unchanged.
"""
from __future__ import annotations


def soft_trail_level(peak: float, early_at: float, early_level: float,
                     giveback_pts: float, giveback_frac: float) -> float:
    """Phase A soft trailing lock: max(early_level, peak - giveback).
    giveback = max(giveback_pts, giveback_frac * peak)."""
    giveback = max(giveback_pts, giveback_frac * peak)
    return max(early_level, peak - giveback)


def phase_b_trail(bars_close: list[float], bars_low: list[float], bars_high: list[float],
                  direction: str, peak_price: float, entry: float,
                  floor_pts: float, current_trail_pts: float,
                  swing_bars: int, buffer: float, atr_mult: float,
                  avg_range: float) -> float:
    """Compute the Phase B trailing SL level in POINTS FROM ENTRY (positive = in profit).
    Uses the higher of swing trail and ATR trail, floored at ride_floor.
    bars_close/low/high are the last N closed bars (most recent last). Never loosens."""
    s = 1 if direction == "long" else -1

    # swing trail: lowest low of last swing_bars closed bars - buffer (longs)
    #              highest high of last swing_bars closed bars + buffer (shorts)
    swing_pts = floor_pts
    if len(bars_low) >= 1 and len(bars_high) >= 1:
        window_low = bars_low[-swing_bars:] if len(bars_low) >= swing_bars else bars_low
        window_high = bars_high[-swing_bars:] if len(bars_high) >= swing_bars else bars_high
        if s > 0:
            swing_price = min(window_low) - buffer
            swing_pts = swing_price - entry
        else:
            swing_price = max(window_high) + buffer
            swing_pts = entry - swing_price

    # ATR trail: peak_price - atr_mult * avg_range (longs) → in points from entry
    atr_pts = floor_pts
    if avg_range > 0:
        if s > 0:
            atr_price = peak_price - atr_mult * avg_range
            atr_pts = atr_price - entry
        else:
            atr_price = peak_price + atr_mult * avg_range
            atr_pts = entry - atr_price

    new_trail = max(floor_pts, swing_pts, atr_pts)
    # never loosen
    return max(current_trail_pts, new_trail)


def leg_alive(leg: dict | None, direction: str, ef: float, es: float) -> bool:
    """True when a same-side leg exists and EMA fast/slow order is intact."""
    if leg is None or leg["side"].lower() != direction.lower():
        return False
    s = 1 if direction == "long" else -1
    return (ef - es) * s > 0


def bar_closes_through_ema(close: float, ema: float, direction: str) -> bool:
    """True when a bar closes on the wrong side of the given EMA for this direction."""
    if direction == "long":
        return close < ema
    return close > ema
