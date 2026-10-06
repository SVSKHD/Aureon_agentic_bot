"""EMA 20/50 crosses, the direction they are heading, and what move each
cross actually offered afterwards."""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .source import Bars
from .journeys import build as build_journeys, rules_for, Context, trend_series, UNFILTERED
import dataclasses

FAST, SLOW = 20, 50


def add_emas(df: pd.DataFrame, fast: int = FAST, slow: int = SLOW) -> pd.DataFrame:
    """Columns are named ema20/ema50 for historical reasons; they hold the FAST and SLOW EMA."""
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=slow, adjust=False).mean()
    df["spread"] = df["ema20"] - df["ema50"]
    df.loc[: slow - 1, ["ema20", "ema50", "spread"]] = np.nan
    return df


@dataclass
class Cross:
    index: int
    time: int
    direction: str          # "bull" (20 over 50) | "bear"
    price: float
    heading: str            # "expanding" | "fading" | "flat"
    slope20: float          # EMA20 slope over last 3 bars, price units
    mfe: float              # best move in cross direction within horizon (price units, from close)
    mae: float              # worst move against within horizon
    bars_to_mfe: int
    ended_by_opposite_cross: bool

    def to_dict(self):
        return asdict(self)


def detect_crosses(df: pd.DataFrame, horizon: int = 24) -> list[Cross]:
    s = df["spread"].to_numpy()
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    e20 = df["ema20"].to_numpy()
    time = df["time"].to_numpy()
    out: list[Cross] = []
    sign = np.sign(s)
    for i in range(1, len(df)):
        if np.isnan(sign[i - 1]) or np.isnan(sign[i]) or sign[i] == 0 or sign[i] == sign[i - 1]:
            continue
        direction = "bull" if sign[i] > 0 else "bear"
        # heading: is the spread widening over the next 3 bars + slope of EMA20
        j = min(i + 3, len(df) - 1)
        widening = abs(s[j]) - abs(s[i])
        slope = (e20[j] - e20[i]) if j > i else 0.0
        heading = "expanding" if widening > 0 and (slope > 0) == (direction == "bull") else ("fading" if widening < 0 else "flat")
        # outcome window
        k_end = min(i + horizon, len(df) - 1)
        ended = False
        for k in range(i + 1, k_end + 1):
            if not np.isnan(sign[k]) and sign[k] != sign[i]:
                k_end, ended = k, True
                break
        if k_end > i:
            if direction == "bull":
                fav = high[i + 1:k_end + 1] - close[i]
                adv = close[i] - low[i + 1:k_end + 1]
            else:
                fav = close[i] - low[i + 1:k_end + 1]
                adv = high[i + 1:k_end + 1] - close[i]
            mfe = float(fav.max()); mae = float(adv.max()); b2 = int(fav.argmax()) + 1
        else:
            mfe = mae = 0.0; b2 = 0
        out.append(Cross(i, int(time[i]), direction, float(close[i]), heading, float(slope),
                         round(mfe, 2), round(mae, 2), b2, ended))
    return out


def analyse(bars: dict[str, Bars], horizon: int = 24, display_from: int | None = None,
            symbol: str = "XAUUSD", server_offset_h: float = 3.0, m5_only: bool = False,
            news_blackout: tuple = (), max_extension: float | None = None,
            max_extension_atr: float | None = None, trade_from: int | None = None,
            fast: int = FAST, slow: int = SLOW, pyramid: int | None = None) -> dict:
    """Per-timeframe bars with EMAs and crosses. EMAs are computed on the full
    history, then everything before `display_from` (warm-up) is dropped."""
    result = {}
    tfs = ("M5",) if m5_only else ("M15", "M1", "M5")   # M5 last so it can use the others as context
    for tf in tfs:
        b = bars[tf]
        df = add_emas(b.df, fast, slow)
        if display_from is not None:
            df = df[df["time"] >= display_from].reset_index(drop=True)
        crosses = detect_crosses(df, horizon=horizon)
        result[tf] = {"df": df, "crosses": crosses}
        if tf == "M15":
            result[tf]["trend"] = trend_series(df)
        if tf == "M5":
            rules = dataclasses.replace(rules_for(symbol, m5_only), news_times=news_blackout)
            if max_extension is not None:
                rules = dataclasses.replace(rules, max_extension=max_extension)
            if max_extension_atr is not None:
                rules = dataclasses.replace(rules, max_extension_atr=max_extension_atr)
            if fast >= 40:
                # slow pairs: SIMPLE. Cross bar close = entry. No gap confirmation, no late entry, no slope filter,
                # no extension cap. Hold until the close through the slow EMA / opposite cross / +2 leg floor after +15.
                # Entry: pre-cross signal or the cross bar close. Exit: standard 20/50 exits — +15 locks +12, trail on a
                # close through the FAST EMA (50), stop on the SLOW EMA (80), breakeven +1 after +6. Leg mode only with --pyramid.
                rules = dataclasses.replace(rules, leg_mode=False, pre_entry=True, confirm_bars=0, late_entry_bars=0,
                                            min_slope50=0.0, min_gap=0.0, max_extension=0.0, max_extension_atr=0.0,
                                            reentry_max=0)
            if pyramid is not None:
                rules = dataclasses.replace(rules, pyramid_adds=pyramid, leg_mode=True)
            result[tf]["ema_periods"] = (fast, slow)
            m15 = Context(result["M15"]["df"], 15, result["M15"]["trend"]) if not m5_only else None
            m1 = Context(result["M1"]["df"], 1) if not m5_only else None
            result[tf].update(build_journeys(df, rules, server_offset_h, m15, m1, trade_from))
            result[tf]["rules"] = rules
            result[tf]["trend"] = trend_series(df, rules.slope_bars, rules.min_slope50)
            result[tf]["unfiltered_summary"] = build_journeys(df, UNFILTERED, server_offset_h, m15, m1, trade_from)["summary"]
            result[tf]["m15_trend_now"] = result["M15"]["trend"][-1] if not m5_only else result[tf]["trend"][-1]
    order = ("M5",) if m5_only else ("M15", "M5", "M1")
    return {tf: result[tf] for tf in order}


def state_at(df: pd.DataFrame, i: int) -> dict:
    """Current EMA state at bar i (used for the selection pointer)."""
    row = df.iloc[i]
    if np.isnan(row["spread"]):
        return {"direction": "n/a", "bars_since_cross": None}
    direction = "bull" if row["spread"] > 0 else "bear"
    sign = np.sign(df["spread"].to_numpy()[: i + 1])
    j = i
    while j > 0 and sign[j - 1] == sign[i]:
        j -= 1
    return {"direction": direction, "bars_since_cross": i - j, "spread": float(row["spread"])}
