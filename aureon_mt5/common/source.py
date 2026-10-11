"""Bar sources. MT5 is the real one; CSV and synthetic exist so the renderer
can be developed and tested without a terminal running."""
from __future__ import annotations

import math
import random
from datetime import date as _date, datetime, timedelta, timezone
from dataclasses import dataclass

import pandas as pd

TIMEFRAMES = ("M1", "M5", "M15")
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15}


@dataclass
class Bars:
    symbol: str
    timeframe: str
    df: pd.DataFrame  # columns: time (server-time epoch seconds, int), open, high, low, close, volume


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={"tick_volume": "volume", "real_volume": "real_volume"})
    if "volume" not in df and "real_volume" in df:
        df["volume"] = df["real_volume"]
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    df["time"] = df["time"].astype("int64")
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- MT5
def previous_trading_day(d: _date) -> _date:
    """Previous weekday. Monday -> Friday; a weekend date itself is first pulled back to Friday."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def day_range(day: _date) -> dict:
    """Server-time epoch bounds for `day` and its previous trading day.
    MT5 bar times are server time encoded as UTC, so we build them with tz=UTC."""
    while day.weekday() >= 5:          # asked for Sat/Sun -> show Friday
        day -= timedelta(days=1)
    prev = previous_trading_day(day)
    to_epoch = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return {"day": day, "prev": prev, "prev_start": to_epoch(prev), "day_start": to_epoch(day),
            "day_end": to_epoch(day) + 86400, "warmup_from": to_epoch(prev) - 4 * 86400}


def trading_days(from_day: _date, to_day: _date) -> list[_date]:
    """Weekdays from `from_day` to `to_day` inclusive."""
    out, d = [], from_day
    while d <= to_day:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def range_bounds(from_day: _date, to_day: _date) -> dict:
    """One fetch window for a multi-day replay: warm-up before the first day, end of the last day."""
    first, last = day_range(from_day), day_range(to_day)
    return {"day": from_day, "prev": first["prev"], "prev_start": first["prev_start"], "day_start": first["day_start"],
            "day_end": last["day_end"], "warmup_from": first["warmup_from"]}


def fetch_mt5(symbol: str, timeframe: str, bars: int, rng: dict | None = None) -> Bars:
    import MetaTrader5 as mt5  # type: ignore

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {mt5.last_error()}")
        tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}[timeframe]
        if rng:
            rates = mt5.copy_rates_range(symbol, tf,
                                         datetime.fromtimestamp(rng["warmup_from"], tz=timezone.utc),
                                         datetime.fromtimestamp(rng["day_end"], tz=timezone.utc))
        else:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates_from_pos returned nothing: {mt5.last_error()}")
        df = pd.DataFrame(rates)
    finally:
        mt5.shutdown()
    # MT5 'time' is server time encoded as if it were UTC.
    return Bars(symbol, timeframe, _clean(df))


# --------------------------------------------------------------------------- CSV
def fetch_csv(path: str, symbol: str, timeframe: str) -> Bars:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    t = df[cols["time"]]
    if not pd.api.types.is_numeric_dtype(t):
        t = pd.to_datetime(t, utc=True).astype("int64") // 10**9
    df = df.rename(columns={cols[k]: k for k in cols})
    df["time"] = t
    if "volume" not in df:
        df["volume"] = df.get("tick_volume", 0)
    return Bars(symbol, timeframe, _clean(df))


def resample(bars: Bars, timeframe: str) -> Bars:
    """Build a higher timeframe from M1 bars (used when only one CSV is given)."""
    mins = TF_MINUTES[timeframe]
    df = bars.df.copy()
    df["bucket"] = (df["time"] // (mins * 60)) * (mins * 60)
    g = df.groupby("bucket")
    out = pd.DataFrame({
        "time": g["bucket"].first(),
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).reset_index(drop=True)
    return Bars(bars.symbol, timeframe, out)


# --------------------------------------------------------------------------- synthetic
def synthetic_m1(symbol: str, minutes: int, start_price: float = 2650.0, seed: int = 7,
                 end_time: int | None = None) -> Bars:
    """Trending/mean-reverting random walk with intraday session shape so EMA
    crosses actually happen. Deterministic for a given seed."""
    rng = random.Random(seed)
    end_time = end_time or 1_760_000_000  # ~Oct 2025 as server time
    end_time -= end_time % 60
    t0 = end_time - minutes * 60
    rows = []
    price = start_price
    drift = 0.0
    for i in range(minutes):
        t = t0 + i * 60
        if datetime.fromtimestamp(t, tz=timezone.utc).weekday() >= 5:   # markets closed, like MT5
            continue
        hour = (t // 3600) % 24
        session = 1.0 + 0.9 * math.exp(-((hour - 14) ** 2) / 8) + 0.5 * math.exp(-((hour - 9) ** 2) / 6)
        if rng.random() < 0.015:  # regime flips
            drift = rng.uniform(-0.25, 0.25)
        drift *= 0.995
        step = (drift + rng.gauss(0, 0.9)) * session * (price / 2650)
        o = price
        c = price + step
        wick = abs(rng.gauss(0, 0.6)) * session
        h = max(o, c) + wick * rng.random()
        l = min(o, c) - wick * rng.random()
        rows.append((t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(80 + 400 * session * rng.random())))
        price = c
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    return Bars(symbol, "M1", df)


def load_all(symbol: str, source: str, bars: int, csv: str | None = None,
             day: _date | None = None, tfs: tuple = TIMEFRAMES) -> tuple[dict[str, Bars], dict | None]:
    """Return ({'M1','M5','M15'} -> Bars, range-or-None).
    Without `day`: the last `bars` M5 bars (M1/M15 sized to the same span).
    With `day`: that trading day plus the previous trading day, with EMA warm-up before it."""
    rng = day_range(day) if day else None
    span_min = bars * 5
    if source == "mt5":
        out = {tf: fetch_mt5(symbol, tf, span_min // TF_MINUTES[tf] + 400, rng) for tf in tfs}
        return out, rng
    if source == "csv":
        if not csv:
            raise ValueError("--csv path required with --source csv")
        m1 = fetch_csv(csv, symbol, "M1")
        if rng:
            m1.df = m1.df[(m1.df.time >= rng["warmup_from"]) & (m1.df.time < rng["day_end"])].reset_index(drop=True)
    elif rng:
        end = min(rng["day_end"], int(datetime.now(timezone.utc).timestamp()) + 3 * 3600)  # synthetic "now" in server time
        m1 = synthetic_m1(symbol, (end - rng["warmup_from"]) // 60, end_time=end)
    else:
        m1 = synthetic_m1(symbol, span_min + 15 * 400)
    return {"M1": m1, "M5": resample(m1, "M5"), "M15": resample(m1, "M15")}, rng


def load_range(symbol: str, source: str, from_day: _date, to_day: _date, csv: str | None = None) -> tuple[dict[str, Bars], list[dict]]:
    """Bars for a multi-day replay (M5 only) plus one day_range() dict per trading day in [from_day, to_day]."""
    days = [day_range(d) for d in trading_days(from_day, to_day)]
    if not days:
        raise ValueError("no trading days in the range")
    rng = range_bounds(days[0]["day"], days[-1]["day"])
    if source == "mt5":
        return {"M5": fetch_mt5(symbol, "M5", 0, rng)}, days
    if source == "csv":
        if not csv:
            raise ValueError("--csv path required with --source csv")
        m1 = fetch_csv(csv, symbol, "M1")
        m1.df = m1.df[(m1.df.time >= rng["warmup_from"]) & (m1.df.time < rng["day_end"])].reset_index(drop=True)
    else:
        end = min(rng["day_end"], int(datetime.now(timezone.utc).timestamp()) + 3 * 3600)
        m1 = synthetic_m1(symbol, (end - rng["warmup_from"]) // 60, end_time=end)
    return {"M5": resample(m1, "M5")}, days
