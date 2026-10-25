"""EMA 9/21 (+200 for the e9>e200 exit) on the exec TF (M30)."""
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from ...common.source import Bars
from .journeys import build as build_journeys, rules_for, with_overrides

FAST, SLOW = 9, 21


def add_emas(df: pd.DataFrame, fast: int = FAST, slow: int = SLOW) -> pd.DataFrame:
    df = df.copy()
    for n in (9, 21, 200):
        df[f"ema{n}"] = df["close"].ewm(span=n, adjust=False).mean()
    df["spread"] = df["ema9"] - df["ema21"]
    df.loc[:199, ["ema200"]] = np.nan
    return df


def analyse(bars: dict[str, Bars], display_from: int | None, symbol: str, server_offset_h: float, news_times: tuple,
            trade_from: int | None, overrides: dict | None = None, day_end: int | None = None, **guardian_kw) -> dict:
    key = next(iter(bars)); df = add_emas(bars[key].df)
    if display_from is not None:
        df = df[df["time"] >= display_from].reset_index(drop=True)
    rules = with_overrides(dataclasses.replace(rules_for(symbol), news_times=news_times), overrides)
    out = {"df": df, "crosses": [], "ema_periods": (FAST, SLOW)}
    out.update(build_journeys(df, rules, server_offset_h, trade_from, day_end, **guardian_kw))
    out["rules"] = rules; out["trend"] = out["bar_states"]; out["unfiltered_summary"] = out["summary"]
    out["m15_trend_now"] = out["trend"][-1] if len(out["trend"]) else "range"
    return {key: out}


def state_at(df: pd.DataFrame, i: int) -> dict:
    row = df.iloc[i]
    return {"direction": "bull" if row["spread"] > 0 else "bear", "bars_since_cross": None, "spread": float(row["spread"])}
