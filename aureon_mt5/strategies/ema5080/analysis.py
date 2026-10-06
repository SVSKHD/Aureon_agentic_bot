"""EMA 50/80 on M5. One timeframe, one rule set."""
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from ...common.source import Bars
from .journeys import build as build_journeys, rules_for, trend_series, UNFILTERED

FAST, SLOW = 50, 80


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    """Columns ema20/ema50 are the FAST (50) and SLOW (80) EMA — names kept for the renderers."""
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=SLOW, adjust=False).mean()
    df["spread"] = df["ema_fast"] - df["ema_slow"]
    df.loc[: SLOW - 1, ["ema_fast", "ema_slow", "spread"]] = np.nan
    return df


def analyse(bars: dict[str, Bars], display_from: int | None, symbol: str, server_offset_h: float,
            news_times: tuple, trade_from: int | None, overrides: dict | None = None) -> dict:
    df = add_emas(bars["M5"].df)
    if display_from is not None:
        df = df[df["time"] >= display_from].reset_index(drop=True)
    rules = dataclasses.replace(rules_for(symbol), news_times=news_times, **(overrides or {}))
    out = {"df": df, "crosses": [], "ema_periods": (FAST, SLOW)}
    out.update(build_journeys(df, rules, server_offset_h, None, None, trade_from))
    out["rules"] = rules
    out["trend"] = trend_series(df, rules.slope_bars, 0.0)
    out["unfiltered_summary"] = build_journeys(df, dataclasses.replace(UNFILTERED, news_times=news_times),
                                               server_offset_h, None, None, trade_from)["summary"]
    out["m15_trend_now"] = out["trend"][-1]
    return {"M5": out}
