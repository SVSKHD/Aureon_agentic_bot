"""v2.14.0 — timeframes: the ladder, the execution timeframe, per-TF rule conversion, and the synthetic M45.

    TF_LADDER   M5, M10, M15, M20, M30, M45*, H1, H2, H3, H4, H6, H8, H12, D1   (* = built from M15; the rest are native MT5)
    confirm_tfs(exec)   the next two rungs above the execution TF (M5 → M10, M15 · M30 → M45, H1 · H1 → H2, H3 …)
    set_exec_tf / exec_tf / bar_s / bars_for(minutes)
                the process-wide execution TF (config AUREON_EXEC_TF, set once at start). Every bar-count rule is a number of
                MINUTES converted to bars of the exec TF; at M5 every conversion returns the v2.13.0 number.
    strategy_overrides(strategy, tf)
                the Rules overrides for a TF (TF_RULES). M5 → {} — the strategy objects are not touched at all.
    synth_m45(m15)  three consecutive M15 bars aligned to the server day (00:00, 00:45, 01:30, …): open of the first,
                high/low extremes, close of the last, time of the first. A group with a missing M15 bar is skipped (never
                merged across a gap); the last, possibly incomplete group is dropped — only CLOSED M45 bars."""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_LADDER = ("M5", "M10", "M15", "M20", "M30", "M45", "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1")
TF_MIN = {"M1": 1, "M5": 5, "M10": 10, "M15": 15, "M20": 20, "M30": 30, "M45": 45, "H1": 60, "H2": 120, "H3": 180,
          "H4": 240, "H6": 360, "H8": 480, "H12": 720, "D1": 1440}
SYNTHETIC = {"M45": ("M15", 3)}                      # built from 3 × M15 (MT5 has no M45)
EXEC_CHOICES = ("M5", "M10", "M15", "M20", "M30", "H1", "H2", "H4")
EXEC: dict = {"tf": "M5", "hold": 0, "gap_x": 0.0, "window": 0}


def tf_minutes(tf: str) -> int:
    return TF_MIN[str(tf).upper()]


def set_exec_tf(tf: str = "M5", cfg=None):
    """Called once at start (main / SymbolAgent / the replay harness). cfg carries the optional confirmation overrides."""
    tf = str(tf or "M5").upper()
    if tf not in TF_MIN:
        raise ValueError(f"unknown timeframe {tf!r}")
    EXEC["tf"] = tf
    EXEC["hold"] = int(getattr(cfg, "confirm_hold_bars", 0) or 0)
    EXEC["gap_x"] = float(getattr(cfg, "confirm_gap_x_range", 0.0) or 0.0)
    EXEC["window"] = int(getattr(cfg, "confirm_window_bars", 0) or 0)


def exec_tf() -> str:
    return EXEC["tf"]


def bar_s(tf: str | None = None) -> int:
    return tf_minutes(tf or EXEC["tf"]) * 60


def bars_for(minutes: float, tf: str | None = None) -> int:
    """A rule expressed in minutes → whole bars of `tf` (at least 1). M5: 60 min → 12, 120 → 24, 600 → 120."""
    return max(1, int(round(float(minutes) / tf_minutes(tf or EXEC["tf"]))))


def confirm_tfs(exec_tf_: str) -> tuple[str, str]:
    """The next two rungs above the execution TF."""
    i = TF_LADDER.index(str(exec_tf_).upper())
    if i + 2 >= len(TF_LADDER):
        raise ValueError(f"no two rungs above {exec_tf_}")
    return TF_LADDER[i + 1], TF_LADDER[i + 2]


# ----------------------------------------------------------------------------- per-TF rules (minutes → bars)
# Rules tuned per BAR keep their M5 bar count at M5 (no override at all). Elsewhere: confirmation by hold bars + a gap
# relative to the average range (the M15 study's 0.25×), windows converted from minutes.
TF_RULES = {
    # bar-count rules (not minute-based): hold bars / confirm window / gap × avg range
    "M5": {"hold": 3, "window": 18, "gap_x": 0.0},          # gap 1.5 points (Rules default) — unchanged
    "M10": {"hold": 3, "window": 9, "gap_x": 0.25},
    "M15": {"hold": 2, "window": 6, "gap_x": 0.25},
    "M20": {"hold": 2, "window": 6, "gap_x": 0.25},
    "M30": {"hold": 2, "window": 6, "gap_x": 0.25},
    "H1": {"hold": 2, "window": 6, "gap_x": 0.25},
    "H2": {"hold": 2, "window": 6, "gap_x": 0.25},
    "H4": {"hold": 2, "window": 6, "gap_x": 0.25},
}
# minute-based windows (the M5 bar counts × 5)
MINUTES = {
    "pullback_window": 60, "pre_sep_bars": 120, "pre_cooldown_bars": 60, "late_entry_bars": 60, "pullback_wait_bars": 120,
    "reentry_window": 180, "p_sep_bars": 120, "wz_bars": 60, "max_bars": 480, "pre_timeout_bars": 60, "regime_window": 120,
    "chart_bars": 600, "flip_hold_trend": 60, "trend_quiet": 30, "leg_slow": 240,
}


def rule_table(tf: str | None = None) -> dict:
    """Everything that depends on the exec TF, in one place (for /decide-timeframe and the README)."""
    tf = (tf or EXEC["tf"]).upper()
    t = TF_RULES.get(tf, TF_RULES["M30"])
    return {"tf": tf, "bar_s": bar_s(tf), "hold": EXEC["hold"] or t["hold"], "window": EXEC["window"] or t["window"],
            "gap_x": EXEC["gap_x"] or t["gap_x"], **{k: bars_for(m, tf) for k, m in MINUTES.items()}}


def strategy_overrides(strategy: str, tf: str | None = None) -> dict:
    """Rules overrides for the exec TF. M5 → {} (byte-identical)."""
    tf = (tf or EXEC["tf"]).upper()
    if tf == "M5" and not (EXEC["hold"] or EXEC["window"] or EXEC["gap_x"]):
        return {}
    r = rule_table(tf)
    if strategy == "ema2050":
        return {"confirm_bars": r["hold"], "confirm_window": r["window"], "confirm_gap": 0.0 if r["gap_x"] else 1.5,
                "confirm_gap_x_range": r["gap_x"], "pullback_window": r["pullback_window"], "pre_sep_bars": r["pre_sep_bars"],
                "pre_cooldown_bars": r["pre_cooldown_bars"]}
    if strategy == "ema5080":
        return {"late_entry_bars": 0, "pullback_wait_bars": r["pullback_wait_bars"], "reentry_window": r["reentry_window"],
                "p_sep_bars": r["p_sep_bars"], "wz_bars": r["wz_bars"], "pre_timeout": r["pre_timeout_bars"], "max_bars": r["max_bars"]}
    if strategy == "ema9200":
        return {}                       # v2.15.0: rules are native M30 bar counts; no minute conversion
    return {}


# ----------------------------------------------------------------------------- resampling + the synthetic M45
def resample_tf(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Native-TF bars from lower bars (harness / synthetic source): buckets aligned to the server day. M45 → synth_m45."""
    tf = tf.upper()
    if tf == "M45":
        return synth_m45(resample_tf(df, "M15") if _step(df) < 900 else df)
    sec = bar_s(tf)
    d = df.copy()
    d["bucket"] = (d["time"] // sec) * sec
    g = d.groupby("bucket", sort=True)
    out = pd.DataFrame({"time": g["bucket"].first(), "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last()}).reset_index(drop=True)
    if "volume" in d:
        out["volume"] = g["volume"].sum().to_numpy()
    return out


def _step(df) -> int:
    t = df["time"].to_numpy()
    return int(np.median(np.diff(t))) if len(t) > 1 else 900


def synth_m45(m15: pd.DataFrame) -> pd.DataFrame:
    """Closed M45 bars from M15. Groups start at server-day minute 0, 45, 90, … (32 per day). A group needs all three M15
    bars (t0, t0+15m, t0+30m); a gap skips it. The newest group is dropped when it is not complete."""
    if m15 is None or len(m15) == 0:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close"])
    d = m15.sort_values("time").reset_index(drop=True)
    t = d["time"].astype("int64").to_numpy()
    day = (t // 86400) * 86400
    g0 = day + ((t - day) // 2700) * 2700
    rows = []
    for start in pd.unique(g0):
        idx = np.where(g0 == start)[0]
        if len(idx) != 3 or not np.array_equal(t[idx], start + np.array([0, 900, 1800])):
            continue                                   # incomplete (the open group) or a missing M15 bar: skipped
        sub = d.iloc[idx]
        row = {"time": int(start), "open": float(sub["open"].iloc[0]), "high": float(sub["high"].max()),
               "low": float(sub["low"].min()), "close": float(sub["close"].iloc[-1])}
        if "volume" in sub:
            row["volume"] = float(sub["volume"].sum())
        rows.append(row)
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"] + (["volume"] if "volume" in d else []))


def closed_only(df: pd.DataFrame, tf: str, now_server: float) -> pd.DataFrame:
    """Drop the forming bar: keep bars whose end (time + tf) is <= now."""
    if df is None or len(df) == 0:
        return df
    return df[df["time"] + bar_s(tf) <= now_server].reset_index(drop=True)
