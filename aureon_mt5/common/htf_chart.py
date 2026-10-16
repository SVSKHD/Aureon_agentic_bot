"""v2.14.0 — the HTF panel under the exec chart (AUREON_HTF_ALIGN show|block): the higher confirm TF's candles + EMAs, the
lower confirm TF's EMAs as thin lines on the same time axis (e.g. exec M30: H1 candles, M45 EMAs). Never raises a trade."""
from __future__ import annotations

import os

import numpy as np

BG, PANEL, MUTED = "#0f1115", "#151922", "#8a93a6"
UP, DOWN, FAST, SLOW = "#26a69a", "#ef5350", "#f5c542", "#4aa3ff"


def render_htf_panel(frames: dict, ema_cols: tuple, out_path: str, bars: int = 60) -> str | None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    tfs = [tf for tf, d in frames.items() if d is not None and len(d)]
    if not tfs:
        return None
    from .tf_synth import tf_minutes
    tfs.sort(key=tf_minutes)
    top = tfs[-1]
    cf, cs = ema_cols
    d = frames[top].tail(bars).reset_index(drop=True)
    w = tf_minutes(top) * 60 * 0.6
    fig, ax = plt.subplots(figsize=(12, 3.2), dpi=100, facecolor=BG)
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    t = d["time"].to_numpy(dtype=float)
    for k in range(len(d)):
        o, h, l, c = (float(d[x].iloc[k]) for x in ("open", "high", "low", "close"))
        col = UP if c >= o else DOWN
        ax.vlines(t[k], l, h, color=col, lw=1)
        ax.add_patch(plt.Rectangle((t[k] - w / 2, min(o, c)), w, max(abs(c - o), 1e-6), color=col))
    ax.plot(t, d[cf], color=FAST, lw=1.6, label=f"{top} fast")
    ax.plot(t, d[cs], color=SLOW, lw=1.6, label=f"{top} slow")
    for tf in tfs[:-1]:                                    # the lower confirm TF: thin EMA lines on the same time axis
        e = frames[tf]
        e = e[e["time"] >= t[0]] if len(t) else e
        ax.plot(e["time"].to_numpy(dtype=float), e[cf], color=FAST, lw=0.7, ls="--", label=f"{tf} fast")
        ax.plot(e["time"].to_numpy(dtype=float), e[cs], color=SLOW, lw=0.7, ls="--", label=f"{tf} slow")
    ax.set_xticks([])
    ax.legend(loc="upper left", fontsize=8, facecolor=PANEL, labelcolor=MUTED, framealpha=0.8)
    ax.set_title(f"HTF · {' / '.join(tfs)}", color=MUTED, fontsize=10, loc="left")
    fig.subplots_adjust(left=0.05, right=0.99, top=0.88, bottom=0.05)
    fig.savefig(out_path, facecolor=BG)
    plt.close(fig)
    return out_path


def add_htf_panel(png: str, frames: dict, ema_cols: tuple, symbol: str = "") -> str | None:
    """Stack the HTF panel under `png`. Returns the new PNG path (the exec chart stays as it was)."""
    if not png or not os.path.exists(png):
        return None
    from PIL import Image
    base, ext = os.path.splitext(png)
    panel = render_htf_panel(frames, ema_cols, base + "_htfpanel.png")
    if panel is None:
        return None
    a, b = Image.open(png).convert("RGB"), Image.open(panel).convert("RGB")
    if b.width != a.width:
        b = b.resize((a.width, int(b.height * a.width / b.width)))
    out = Image.new("RGB", (a.width, a.height + b.height), (15, 17, 21))
    out.paste(a, (0, 0)); out.paste(b, (0, a.height))
    path = base + "_htf.png"
    out.save(path)
    return path
