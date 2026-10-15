"""Builds tests/fixtures/xauusd_2day_m5.json for tests/test_strategy_pins.py: the SYNTHETIC 15-Oct bars
(make_xauusd_20261015.py) followed by one more SYNTHETIC day (16-Oct, seeded) that swings through several EMA 50/80
crosses, so the pins cover P, crosses, stops and close reasons. Deterministic: same file every run."""
import json, os
import numpy as np, pandas as pd
from make_xauusd_20261015 import build

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xauusd_2day_m5.json")


def build2():
    d1 = build()
    rs = np.random.RandomState(7)
    n = 288
    t = np.arange(n)
    wave = 18.0 * np.sin(2 * np.pi * t / 140.0) + 6.0 * np.sin(2 * np.pi * t / 37.0)
    walk = np.cumsum(rs.normal(0, 0.6, n))
    start = float(d1["close"].iloc[-1])
    c = start + wave + walk - wave[0] - walk[0]
    o = np.concatenate([[start], c[:-1]])
    h = np.maximum(o, c) + rs.uniform(0.3, 1.5, n); l = np.minimum(o, c) - rs.uniform(0.3, 1.5, n)
    times = int(d1["time"].iloc[-1]) + 300 * (1 + t)
    d2 = pd.DataFrame({"time": times, "open": o, "high": h, "low": l, "close": c})
    df = pd.concat([d1[["time", "open", "high", "low", "close"]], d2], ignore_index=True)
    df["ema_fast"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=80, adjust=False).mean()
    return df


if __name__ == "__main__":
    df = build2()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"meta": {"synthetic": True, "symbol": "XAUUSD", "note": __doc__}, "bars": df.round(4).to_dict(orient="list")}, f)
    print(len(df), "bars")
