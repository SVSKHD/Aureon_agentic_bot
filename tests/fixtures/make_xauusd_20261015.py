"""Builds tests/fixtures/xauusd_20261015_m5.json — a SYNTHETIC reconstruction of the 2026-10-15 02:05 (server) XAUUSD trade.

No journal / MT5 history was available where v2.8.0 was built, so the bars are generated from the numbers in the incident
report: longs from 4103.5 (01:30 server), a slow pullback with the worst close ≈ −3 around 02:00, a P SHORT bar closing 4102.3
at 02:05 that never closed through EMA 80 (EMA 50 rising, M15 bull, gap ≈ 1 vs avg range ≈ 3), then the run to ≈ 4150.
Replace with the real bars (broker.bars_tf on the trading machine) when available: same JSON shape."""
import json, os
from datetime import datetime, timezone
import numpy as np, pandas as pd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xauusd_20261015_m5.json")


def build():
    t0 = int(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc).timestamp())       # server time as epoch (MT5 convention)
    closes = []
    closes += list(np.linspace(4090.0, 4097.0, 96))           # 12:00–19:55 slow uptrend
    closes += list(np.linspace(4097.0, 4098.6, 54))           # 20:00–00:25 flat-ish above EMA 80
    closes += list(np.linspace(4098.6, 4103.5, 13))           # 00:30–01:30 — entry bar closes 4103.5
    closes += [4102.9, 4102.2, 4101.6, 4101.0, 4100.8, 4100.5]  # 01:35–02:00 slow pullback (worst close 4100.5 = −3.0)
    closes += [4102.3]                                         # 02:05 the P SHORT bar
    closes += list(np.linspace(4103.0, 4150.0, 23))           # 02:10–04:00 the move
    n = len(closes)
    c = np.array(closes)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + 1.2; l = np.minimum(o, c) - 1.2
    t = t0 + np.arange(n) * 300
    df = pd.DataFrame({"time": t, "open": o, "high": h, "low": l, "close": c})
    df["ema_fast"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=80, adjust=False).mean()
    return df


def bar_index(df, hhmm: str) -> int:
    hh, mm = map(int, hhmm.split(":"))
    for i, t in enumerate(df["time"]):
        d = datetime.fromtimestamp(int(t), tz=timezone.utc)
        if d.day == 15 and d.hour == hh and d.minute == mm:
            return i
    raise KeyError(hhmm)


if __name__ == "__main__":
    df = build()
    meta = {"synthetic": True, "symbol": "XAUUSD", "entry": 4103.5, "entry_bar": "01:30", "p_short_bar": "02:05", "note": __doc__}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "bars": df.round(4).to_dict(orient="list")}, f)
    for hhmm in ("01:30", "02:00", "02:05", "04:00"):
        i = bar_index(df, hhmm); r = df.iloc[i]
        rng = (df["high"] - df["low"]).to_numpy()
        print(hhmm, "close", r.close, "e50", round(r.ema_fast, 2), "e80", round(r.ema_slow, 2), "gap", round(r.ema_fast - r.ema_slow, 2),
              "avg_rng", round(rng[i - 20:i].mean(), 2), "slope", round(df.ema_fast[i] - df.ema_fast[i - 3], 3))
