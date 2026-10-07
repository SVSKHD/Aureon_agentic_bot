"""Compact market-context chart for Discord cards (phone-friendly, ~1200x700).

    path = context_chart(df, symbol, mode_display, fast, slow, ema_cols, off_h, out_dir,
                         marker={"index": i, "side": "LONG", "label": "P"}, bars=120)

Draws the last `bars` closed M5 candles, EMA fast (red) and EMA slow (white), session bands, the current price line,
and an optional entry marker with stop/secure lines. Pure matplotlib (Agg); never raises into the caller."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np

from .news import session_of

IST = timezone(timedelta(hours=5, minutes=30))
BG, PANEL, GRID, TEXT, MUTED = "#141922", "#1b2230", "#263042", "#d6dde8", "#7e8a9c"
UP, DOWN, EMA_F, EMA_S = "#2fbf9a", "#e0635a", "#ff3b3b", "#ffffff"
SESS = {"asia": "#f2c14e", "london": "#5aa9ff", "ny": "#ff5a5a"}


def context_chart(df, symbol: str, mode_display: str, fast: int, slow: int, ema_cols: tuple, off_h: float, out_dir: str,
                  marker: dict | None = None, bars: int = 120, title_extra: str = "", regions: list | None = None,
                  day_start_ts: int | None = None, reentries: list | None = None) -> str | None:
    """regions: [{"start": idx, "end": idx, "side": "LONG"|"SHORT", "label": "...", "move": float, "peak_idx": idx}] in df indexes."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        cf, cs = ema_cols
        d = df.tail(bars).reset_index(drop=True)
        shift = len(df) - len(d)
        n = len(d); x = np.arange(n)
        o, h, l, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
        fig, ax = plt.subplots(figsize=(12, 7), dpi=100, facecolor=BG)
        ax.set_facecolor(PANEL)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.grid(True, color=GRID, lw=0.6); ax.tick_params(colors=MUTED, labelsize=11, length=0)
        # session bands (thin strip at the bottom when legs are highlighted, full height otherwise)
        sess = [session_of(int(t), off_h) for t in d["time"]]; k0 = 0
        for k in range(1, n + 1):
            if k == n or sess[k] != sess[k0]:
                if SESS.get(sess[k0]):
                    if regions:
                        ax.axvspan(k0 - 0.5, k - 0.5, ymin=0, ymax=0.025, color=SESS[sess[k0]], alpha=0.8, lw=0)
                    else:
                        ax.axvspan(k0 - 0.5, k - 0.5, color=SESS[sess[k0]], alpha=0.10, lw=0)
                k0 = k
        # session dividers: vertical line + label at each session start
        sess_names = {"asia": "ASIA", "london": "LONDON", "ny": "NEW YORK"}
        for k in range(1, n):
            if sess[k] != sess[k - 1] and sess[k] in sess_names:
                ax.axvline(k - 0.5, color=SESS[sess[k]], lw=1.4, alpha=0.75, zorder=1)
                ax.text(k - 0.2, 0.975, sess_names[sess[k]], transform=ax.get_xaxis_transform(), color=SESS[sess[k]],
                        fontsize=10, weight="bold", va="top", ha="left", zorder=8)
        # re-entry markers
        for re_ in (reentries or []):
            ri = re_["index"] - shift
            if 0 <= ri < n:
                long = re_["side"] == "LONG"; colour = UP if long else DOWN
                ax.plot(ri, c[ri], marker="^" if long else "v", ms=12, color=colour, mec="#ffffff", mew=1.2, zorder=7)
                ax.annotate("RE", (ri, c[ri]), xytext=(0, 14 if long else -14), textcoords="offset points", ha="center",
                            va="bottom" if long else "top", fontsize=10, weight="bold", color=colour, zorder=7)
        # day-start line
        if day_start_ts is not None:
            ds = int(np.searchsorted(d["time"].to_numpy(), day_start_ts))
            if 0 < ds < n:
                ax.axvline(ds - 0.5, color=MUTED, lw=1.2, ls=(0, (4, 3)))
                ax.text(ds - 0.3, 0.035, "day start (IST)", transform=ax.get_xaxis_transform(), color=MUTED, fontsize=10, va="bottom")
        # highlighted legs: cross -> peak of the move (shaded, labelled with points moved)
        for rg in regions or []:
            a, b = rg["start"] - shift, rg["end"] - shift
            if b < 0 or a >= n:
                continue
            a, b = max(a, 0), min(b, n - 1)
            colour = UP if rg["side"] == "LONG" else DOWN
            ax.axvspan(a - 0.5, (pk_rel if (pk_rel := (rg.get("peak_idx", b + shift) - shift)) <= b else b) + 0.5,
                       color=colour, alpha=0.18, lw=0, zorder=0.5)                         # shade cross -> peak
            ax.axvline(a, color=colour, lw=1.3, alpha=0.9)
            if rg["start"] - shift >= 0:
                ax.plot(a, c[a], marker="^" if rg["side"] == "LONG" else "v", ms=11, color=colour, mec=BG, mew=1, zorder=6)
            pk = rg.get("peak_idx")
            if pk is not None and 0 <= pk - shift < n:
                pk -= shift
                y_pk = h[pk] if rg["side"] == "LONG" else l[pk]
                ax.plot(pk, y_pk, marker="o", ms=7, color=colour, mec=BG, mew=1, zorder=6)
                ax.annotate(f"{'▲' if rg['side'] == 'LONG' else '▼'} {rg.get('label', '')}  {rg['move']:+.1f}", (pk, y_pk), xytext=(0, 12 if rg["side"] == "LONG" else -12),
                            textcoords="offset points", ha="center", va="bottom" if rg["side"] == "LONG" else "top", fontsize=11,
                            weight="bold", color=BG, bbox=dict(boxstyle="round,pad=0.25", fc=colour, ec="none"), zorder=7)
        # candles
        col = np.where(c >= o, UP, DOWN)
        ax.vlines(x, l, h, color=col, lw=1)
        for xi, oi, ci, cc in zip(x, o, c, col):
            ax.add_patch(Rectangle((xi - 0.32, min(oi, ci)), 0.64, max(abs(ci - oi), 1e-6), color=cc, lw=0))
        # EMAs + labels
        ax.plot(x, d[cf], color=EMA_F, lw=2.2); ax.plot(x, d[cs], color=EMA_S, lw=2.2)
        for col_name, colour, per in ((cf, EMA_F, fast), (cs, EMA_S, slow)):
            yv = float(d[col_name].iloc[-1])
            ax.annotate(f"EMA {per}  {yv:.2f}", (n - 1, yv), xytext=(8, 0), textcoords="offset points", color=colour,
                        fontsize=11, weight="bold", va="center", bbox=dict(boxstyle="round,pad=0.25", fc=PANEL, ec="none"))
        # price line
        last = float(c[-1])
        ax.axhline(last, color=MUTED, lw=1, ls=(0, (3, 3)))
        # marker
        if marker and marker.get("index") is not None:
            mi = marker["index"] - shift
            if 0 <= mi < n:
                long = marker.get("side", "LONG").upper() == "LONG"; mc = UP if long else DOWN
                ax.plot(mi, c[mi], marker="^" if long else "v", ms=16, color=mc, mec=BG, mew=1.2, zorder=6)
                ax.annotate(f"{marker.get('label', '')} {'LONG' if long else 'SHORT'} {c[mi]:.2f}", (mi, c[mi]),
                            xytext=(-30, 34 if long else -34), textcoords="offset points", ha="right", va="bottom" if long else "top",
                            fontsize=12, weight="bold", color=BG, bbox=dict(boxstyle="round,pad=0.3", fc=mc, ec="none"), zorder=7)
                for key, lab, colour in (("stop", "stop", DOWN if long else UP), ("secure", "+10", UP if long else DOWN)):
                    if marker.get(key) is not None:
                        ax.plot([mi, n - 1], [marker[key], marker[key]], color=colour, lw=1.2, ls=(0, (5, 3)), alpha=0.85)
                        ax.annotate(f"{lab} {marker[key]:.2f}", (n - 1, marker[key]), xytext=(8, 0), textcoords="offset points",
                                    color=colour, fontsize=10, va="center", bbox=dict(boxstyle="round,pad=0.2", fc=PANEL, ec="none"))
        # axes
        lo = min(l.min(), float(d[[cf, cs]].min().min())); hi = max(h.max(), float(d[[cf, cs]].max().max()))
        if marker:
            for key in ("stop", "secure"):
                if marker.get(key) is not None: lo, hi = min(lo, marker[key]), max(hi, marker[key])
        pad = (hi - lo) * 0.08; ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-1, n + 14)
        ax.yaxis.tick_right()
        ticks = np.linspace(0, n - 1, 6).astype(int); ax.set_xticks(ticks)
        ax.set_xticklabels([datetime.fromtimestamp(int(d["time"].iloc[t]) - off_h * 3600, tz=IST).strftime("%H:%M") for t in ticks])
        order = "▲ fast > slow" if d[cf].iloc[-1] > d[cs].iloc[-1] else "▼ fast < slow"
        ax.set_title(f"{symbol} · M5 · {mode_display} · {order}" + (f" · {title_extra}" if title_extra else ""), color=TEXT,
                     fontsize=14, weight="bold", loc="left", pad=12)
        ax.text(0.995, 1.01, f"{last:.2f} · {datetime.fromtimestamp(int(d['time'].iloc[-1]) - off_h * 3600, tz=IST):%d %b %H:%M} IST",
                transform=ax.transAxes, color=MUTED, fontsize=11, ha="right", va="bottom")
        fig.tight_layout()
        slug = "".join(ch for ch in title_extra.lower() if ch.isalnum())[:16] or "ctx"
        path = os.path.join(out_dir, f"{symbol}_{slug}_{int(d['time'].iloc[-1])}.png")
        fig.savefig(path, facecolor=BG); plt.close(fig)
        # keep the folder small
        files = sorted((f for f in os.listdir(out_dir) if f.endswith(".png")), key=lambda f: os.path.getmtime(os.path.join(out_dir, f)))
        for f in files[:-40]:
            try: os.remove(os.path.join(out_dir, f))
            except Exception: pass
        return path
    except Exception as e:
        print("  (context chart failed:", e, ")")
        return None
