"""v2.8.0 — the stop is the setup. Pure functions; no MT5, no state.

While the setup that justified the trade is alive, the stop is the structure (swing low/high ± buffer), bounded by a hard
drawdown cap. A bar CLOSE that invalidates the setup ends the trade; a wick does not. The trail maths is
leg_guardian.phase_b_trail (atr_mult=0 → only the swing and the cap/floor remain) — no second trail implementation.
Used by agent._guard / _guard_classic when AUREON_SETUP_STOP=1 (and for the Claude snapshot when Claude is attached)."""
from __future__ import annotations

from . import leg_guardian


def setup_checks(direction: str, close: float, ef: float, es: float, es_buffer: float,
                 lows: list[float], highs: list[float], swing_bars: int, m15_state: str | None) -> dict[str, bool | None]:
    """Per-check truth on the latest CLOSED bar. None = unknown (never counts as a failure).
    lows/highs are the bars BEFORE the latest one (the prior swing the close must hold)."""
    s = 1 if direction == "long" else -1
    if lows and highs:
        swing = (close > min(lows[-swing_bars:])) if s > 0 else (close < max(highs[-swing_bars:]))
    else:
        swing = None
    return {
        "ema_order": (ef - es) * s > 0,                                                  # leg_guardian.leg_alive without the leg dict
        "ema80_close": not leg_guardian.bar_closes_through_ema(close, es - s * es_buffer, direction),
        "m15": None if not m15_state else (m15_state.lower().startswith("bull") if s > 0 else m15_state.lower().startswith("bear")),
        "swing": swing,
    }


def setup_alive(checks: dict | None) -> bool:
    """Alive while nothing is False; None is ignored."""
    return not any(v is False for v in (checks or {}).values())


def failed_checks(checks: dict | None) -> list[str]:
    return [k for k, v in (checks or {}).items() if v is False]


def cap_points(max_dd: float, x_range: float, avg_range: float) -> float:
    """Hard drawdown cap in points: SETUP_MAX_DD, or x_range × avg range when that is larger (scales with volatility)."""
    if x_range > 0 and avg_range > 0:
        return max(float(max_dd), x_range * avg_range)
    return float(max_dd)


def structure_stop_pts(direction: str, entry: float, lows: list[float], highs: list[float], swing_bars: int, buffer: float,
                       cap_pts: float, current_pts: float) -> float:
    """Points from entry (negative = below entry for a long). max(−cap, swing − buffer), never looser than current_pts."""
    return leg_guardian.phase_b_trail([], list(lows), list(highs), direction, peak_price=entry, entry=entry, floor_pts=-cap_pts,
                                      current_trail_pts=current_pts, swing_bars=swing_bars, buffer=buffer, atr_mult=0.0,
                                      avg_range=0.0)
