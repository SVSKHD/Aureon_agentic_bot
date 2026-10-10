"""EMA 20/50 on M5. One timeframe, one rule set (journeys.Rules), the guardian profile replayed from journeys.GUARDIANS."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from ...common.source import Bars
from .journeys import build as build_journeys, rules_for, unfiltered_for, trend_series

FAST, SLOW = 20, 50


def add_emas(df: pd.DataFrame, fast: int = FAST, slow: int = SLOW) -> pd.DataFrame:
    """Columns ema20 / ema50 hold the FAST and SLOW EMA (names kept for the renderers)."""
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
    slope20: float
    mfe: float
    mae: float
    bars_to_mfe: int
    ended_by_opposite_cross: bool

    def to_dict(self):
        return asdict(self)


def detect_crosses(df: pd.DataFrame, horizon: int = 24) -> list[Cross]:
    """Raw crosses with what each offered afterwards (renderer context only; the rules live in journeys.build)."""
    s = df["spread"].to_numpy(); close = df["close"].to_numpy(); high = df["high"].to_numpy(); low = df["low"].to_numpy()
    e20 = df["ema20"].to_numpy(); time = df["time"].to_numpy()
    out: list[Cross] = []
    sign = np.sign(s)
    for i in range(1, len(df)):
        if np.isnan(sign[i - 1]) or np.isnan(sign[i]) or sign[i] == 0 or sign[i] == sign[i - 1]:
            continue
        direction = "bull" if sign[i] > 0 else "bear"
        j = min(i + 3, len(df) - 1)
        widening = abs(s[j]) - abs(s[i]); slope = (e20[j] - e20[i]) if j > i else 0.0
        heading = "expanding" if widening > 0 and (slope > 0) == (direction == "bull") else ("fading" if widening < 0 else "flat")
        k_end = min(i + horizon, len(df) - 1); ended = False
        for k in range(i + 1, k_end + 1):
            if not np.isnan(sign[k]) and sign[k] != sign[i]:
                k_end, ended = k, True; break
        if k_end > i:
            fav = (high[i + 1:k_end + 1] - close[i]) if direction == "bull" else (close[i] - low[i + 1:k_end + 1])
            adv = (close[i] - low[i + 1:k_end + 1]) if direction == "bull" else (high[i + 1:k_end + 1] - close[i])
            mfe = float(fav.max()); mae = float(adv.max()); b2 = int(fav.argmax()) + 1
        else:
            mfe = mae = 0.0; b2 = 0
        out.append(Cross(i, int(time[i]), direction, float(close[i]), heading, float(slope), round(mfe, 2), round(mae, 2), b2, ended))
    return out


def analyse(bars: dict[str, Bars], display_from: int | None, symbol: str, server_offset_h: float, news_times: tuple,
            trade_from: int | None, overrides: dict | None = None, day_end: int | None = None) -> dict:
    """EMAs on the full history, then everything before `display_from` (warm-up) is dropped. M5 only."""
    df = add_emas(bars["M5"].df)
    if display_from is not None:
        df = df[df["time"] >= display_from].reset_index(drop=True)
    rules = dataclasses.replace(rules_for(symbol), news_times=news_times, **(overrides or {}))
    out = {"df": df, "crosses": detect_crosses(df), "ema_periods": (FAST, SLOW)}
    out.update(build_journeys(df, rules, server_offset_h, trade_from, day_end))
    out["rules"] = rules
    out["trend"] = trend_series(df, rules.slope_bars, rules.min_slope50)
    out["unfiltered_summary"] = build_journeys(df, dataclasses.replace(unfiltered_for(symbol), news_times=news_times),
                                               server_offset_h, trade_from, day_end)["summary"]
    out["m15_trend_now"] = out["trend"][-1] if len(out["trend"]) else "range"
    return {"M5": out}


def state_at(df: pd.DataFrame, i: int) -> dict:
    """Current EMA state at bar i (selection pointer in the HTML replay)."""
    row = df.iloc[i]
    if np.isnan(row["spread"]):
        return {"direction": "n/a", "bars_since_cross": None}
    direction = "bull" if row["spread"] > 0 else "bear"
    sign = np.sign(df["spread"].to_numpy()[: i + 1]); j = i
    while j > 0 and sign[j - 1] == sign[i]:
        j -= 1
    return {"direction": direction, "bars_since_cross": i - j, "spread": float(row["spread"])}
