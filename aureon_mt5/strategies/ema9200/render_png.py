"""EMA 9/200 — report chart (replay / reports). Candles, EMA9 (white), EMA200 (orange), candidates (triangles), trades (dashed)."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def render_png(res: dict, symbol: str, path: str, title: str = "", last_bars: int = 240, **_):
    df = res["df"].tail(last_bars).reset_index(drop=True); off = len(res["df"]) - len(df)
    fig, ax = plt.subplots(figsize=(18, 7), facecolor="#121417"); ax.set_facecolor("#121417")
    for i, r in df.iterrows():
        c = "#2fb380" if r.close >= r.open else "#e2574b"
        ax.plot([i, i], [r.low, r.high], color=c, lw=0.7)
        ax.add_patch(plt.Rectangle((i - 0.3, min(r.open, r.close)), 0.6, abs(r.close - r.open) + 0.01, color=c))
    ax.plot(df.index, df["ema9"], color="#f2f2f2", lw=1.1, label="EMA 9")
    ax.plot(df.index, df["ema200"], color="#f0a742", lw=1.4, label="EMA 200")
    for e in res.get("events", []):
        i = e.index - off
        if i < 0 or e.label not in ("EB", "ES"):
            continue
        up = e.label == "EB"
        ax.scatter([i], [df.low[i] - 3 if up else df.high[i] + 3], marker="^" if up else "v", s=60, color="#5aa9ff", zorder=5)
    for j in res.get("journeys", []):
        i0 = j.entry_index - off; i1 = (j.exit_index if j.exit_index is not None else len(res["df"]) - 1) - off
        if i1 < 0:
            continue
        ax.plot([max(i0, 0), i1], [j.entry_price, j.exit_price or df.close[i1]], ls="--", lw=1,
                color="#2fb380" if j.result > 0 else "#e2574b")
    s = res.get("summary", {})
    ax.set_title(f"{symbol} · EMA 9/200 · {title} · candidates {s.get('candidates', 0)} · trades {s.get('trades', 0)} · "
                 f"win {s.get('win_rate', 0) * 100:.0f}% · net {s.get('net', 0):+.1f}", color="white", fontsize=12)
    ax.tick_params(colors="#ddd"); ax.grid(color="#2a2f37", lw=0.4); ax.legend(facecolor="#121417", labelcolor="white", loc="upper left")
    for sp in ax.spines.values():
        sp.set_color("#444")
    plt.tight_layout(); plt.savefig(path, dpi=100, facecolor="#121417"); plt.close(fig)
    return path
