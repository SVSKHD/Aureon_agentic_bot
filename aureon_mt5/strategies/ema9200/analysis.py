"""EMA 9/200 on the exec TF (M30). One rule set (journeys.Rules), the guardian profile replayed from journeys.GUARDIANS."""
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from ...common.source import Bars
from .journeys import build as build_journeys, rules_for, unfiltered_for

FAST, SLOW = 9, 200


def add_emas(df: pd.DataFrame, fast: int = FAST, slow: int = SLOW) -> pd.DataFrame:
    """Columns ema9 / ema200 (the strategy's ema_cols) plus `spread` = ema9 − ema200."""
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=slow, adjust=False).mean()
    df["spread"] = df["ema9"] - df["ema200"]
    df.loc[: slow - 1, ["ema9", "ema200", "spread"]] = np.nan
    return df


def analyse(bars: dict[str, Bars], display_from: int | None, symbol: str, server_offset_h: float, news_times: tuple,
            trade_from: int | None, overrides: dict | None = None, day_end: int | None = None, **guardian_kw) -> dict:
    """EMAs on the full history (EMA200 needs >= 200 exec-TF bars of warm-up), then everything before `display_from` is dropped."""
    key = next(iter(bars))                              # the agent passes the exec-TF bars under the "M5" key (interface convention)
    df = add_emas(bars[key].df)
    if display_from is not None:
        df = df[df["time"] >= display_from].reset_index(drop=True)
    rules = dataclasses.replace(rules_for(symbol), news_times=news_times, **(overrides or {}))
    out = {"df": df, "crosses": [], "ema_periods": (FAST, SLOW)}
    out.update(build_journeys(df, rules, server_offset_h, trade_from, day_end, **guardian_kw))
    out["rules"] = rules
    out["trend"] = out["bar_states"]
    out["unfiltered_summary"] = build_journeys(df, dataclasses.replace(unfiltered_for(symbol), news_times=news_times),
                                               server_offset_h, trade_from, day_end)["summary"]
    out["m15_trend_now"] = out["trend"][-1] if len(out["trend"]) else "range"
    return {key: out}


def state_at(df: pd.DataFrame, i: int) -> dict:
    row = df.iloc[i]
    if np.isnan(row["spread"]):
        return {"direction": "n/a", "bars_since_cross": None}
    return {"direction": "bull" if row["spread"] > 0 else "bear", "bars_since_cross": None, "spread": float(row["spread"])}
