"""v2.10.0 — zero-lag context for the EMA system (NO rule change).

EMA 50/80 lag is what makes a P late and a flip early. These columns give the advisor and the operator a faster, lower-lag
view of the same structure; the signal rules do not read them (except the optional AUREON_FLIP_USE_ZL evidence item).
Pure pandas/numpy, vectorised, NO LOOKAHEAD: every value uses bars <= its own index (tests assert it with a shifted frame).

  zl50, zl80   Ehlers zero-lag EMA: ema(2·close − close.shift(lag), n), lag = (n − 1) // 2
  hma55        Hull MA: wma(2·wma(n/2) − wma(n), √n)
  tema50       3·e1 − 3·e2 + e3
  adx5, adx14, di_plus, di_minus   Wilder ADX (+DI/−DI from the 14-bar one)
  atr14        Wilder ATR
  kama10       Kaufman adaptive MA (10, 2, 30) — fast when price is efficient, slow in chop
  vwap         session VWAP from the session-open bar (tick volume; equal weights when there is none)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ZL_COLS = ("zl50", "zl80", "hma55", "tema50", "adx5", "adx14", "di_plus", "di_minus", "atr14", "kama10", "vwap")


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def zlema(close: pd.Series, n: int) -> pd.Series:
    lag = (n - 1) // 2
    return ema(2 * close - close.shift(lag).fillna(close.iloc[0] if len(close) else 0.0), n)


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def hma(close: pd.Series, n: int) -> pd.Series:
    half = max(1, n // 2); root = max(1, int(np.sqrt(n)))
    return wma(2 * wma(close, half) - wma(close, n), root)


def tema(close: pd.Series, n: int) -> pd.Series:
    e1 = ema(close, n); e2 = ema(e1, n); e3 = ema(e2, n)
    return 3 * e1 - 3 * e2 + e3


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1).fillna(
        df["high"] - df["low"])


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff(); down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), n).replace(0, np.nan)
    di_p = 100 * _wilder(plus_dm, n) / tr
    di_m = 100 * _wilder(minus_dm, n) / tr
    dx = (100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)).fillna(0.0)
    return _wilder(dx, n), di_p.fillna(0.0), di_m.fillna(0.0)


def kama(close: pd.Series, n: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) == 0:
        return pd.Series(out, index=close.index)
    fsc, ssc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    out[0] = c[0]
    for i in range(1, len(c)):
        if i < n:
            out[i] = c[i]; continue
        change = abs(c[i] - c[i - n])
        vol = np.abs(np.diff(c[i - n:i + 1])).sum()
        er = change / vol if vol > 0 else 0.0
        sc = (er * (fsc - ssc) + ssc) ** 2
        out[i] = out[i - 1] + sc * (c[i] - out[i - 1])
    return pd.Series(out, index=close.index)


def session_vwap(df: pd.DataFrame, session_key) -> pd.Series:
    """session_key: a Series (same index) labelling each bar's session; VWAP restarts at each new label."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float) if "volume" in df and float(df["volume"].sum() or 0) > 0 else pd.Series(1.0, index=df.index)
    pv = (typical * vol).groupby(session_key).cumsum()
    vv = vol.groupby(session_key).cumsum()
    return pv / vv


def add_zl(df: pd.DataFrame, off_h: float = 3.0) -> pd.DataFrame:
    """Return a copy with the ZL_COLS added. Session = IST day (the operator's day) by default."""
    from datetime import timedelta
    out = df.copy()
    c = out["close"].astype(float)
    out["zl50"] = zlema(c, 50); out["zl80"] = zlema(c, 80)
    out["hma55"] = hma(c, 55); out["tema50"] = tema(c, 50)
    out["adx5"], _, _ = adx(out, 5)
    out["adx14"], out["di_plus"], out["di_minus"] = adx(out, 14)
    out["atr14"] = atr(out, 14)
    out["kama10"] = kama(c, 10, 2, 30)
    ist_shift = int((5.5 - off_h) * 3600)                     # server time → IST
    day = ((out["time"].astype("int64") + ist_shift) // 86400)
    out["vwap"] = session_vwap(out, day)
    return out


def zl_block(df: pd.DataFrame, side_sign: int = 0) -> dict | None:
    """The `zl` block for the Claude snapshot (None when the columns are absent)."""
    if df is None or "zl50" not in df or len(df) < 4:
        return None
    last = df.iloc[-1]
    g = (df["zl50"] - df["zl80"]).to_numpy()
    sg = np.sign(g)
    k = next((i for i in range(len(sg) - 1, 0, -1) if sg[i] != 0 and sg[i - 1] != 0 and sg[i] != sg[i - 1]), None)
    ago = (len(sg) - 1 - k) if k is not None else None
    r = lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)
    return {"zl50": r(last["zl50"]), "zl80": r(last["zl80"]), "zl_gap": r(g[-1]),
            "zl_cross_bars_ago": (ago if sg[-1] > 0 else -ago) if ago is not None else None,
            "hma55_slope": r(df["hma55"].iloc[-1] - df["hma55"].iloc[-4]), "adx5": r(last["adx5"]), "adx14": r(last["adx14"]),
            "di_plus": r(last["di_plus"]), "di_minus": r(last["di_minus"]), "atr14": r(last["atr14"]),
            "kama10_slope": r(df["kama10"].iloc[-1] - df["kama10"].iloc[-4]), "vwap_dist": r(last["close"] - last["vwap"])}


def zl_cross_against(df: pd.DataFrame, direction: str, bars: int = 2) -> bool:
    """zl50 crossed zl80 AGAINST the position within the last `bars` bars (optional flip evidence)."""
    if df is None or "zl50" not in df or len(df) < bars + 2:
        return False
    g = (df["zl50"] - df["zl80"]).to_numpy()
    s = 1 if direction == "long" else -1
    for i in range(len(g) - bars, len(g)):
        if g[i - 1] * s > 0 and g[i] * s < 0:
            return True
    return False
