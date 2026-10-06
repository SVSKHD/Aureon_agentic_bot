from __future__ import annotations

from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
import numpy as np

BG = "#141922"
PANEL = "#1b2230"
GRID = "#263042"
TEXT = "#c9d1dc"
MUTED = "#7b8798"
UP = "#2fbf9a"
DOWN = "#e0635a"
EMA20 = "#ff3b3b"
EMA50 = "#ffffff"
SESSION_COL = {"asia": "#f2c14e", "london": "#5aa9ff", "ny": "#ff5a5a"}
SESSION_ALPHA = 0.13

from .journeys import session_of

IST = timezone(timedelta(hours=5, minutes=30))


def _ist(ts: int, server_offset_h: float) -> datetime:
    utc = datetime.fromtimestamp(ts - server_offset_h * 3600, tz=timezone.utc)
    return utc.astimezone(IST)


def _panel(ax, df, crosses, tf, n, server_offset_h, highlight, rng=None, jd=None):
    df = df.tail(n).reset_index(drop=True)
    base = len(df)
    ax.set_facecolor("none")
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.grid(True, color=GRID, lw=0.6, alpha=0.8)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)

    x = np.arange(base)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    # session background bands
    sess = [session_of(int(t), server_offset_h) for t in df["time"]]
    k0 = 0
    for k in range(1, base + 1):
        if k == base or sess[k] != sess[k0]:
            col = SESSION_COL.get(sess[k0])
            if col:
                ax.axvspan(k0 - 0.5, k - 0.5, color=col, alpha=SESSION_ALPHA, lw=0, zorder=0)
            k0 = k
    col = np.where(c >= o, UP, DOWN)
    ax.vlines(x, l, h, color=col, lw=0.8)
    w = 0.62 if base <= 400 else 0.9
    for xi, oi, ci, ci_col in zip(x, o, c, col):
        ax.add_patch(Rectangle((xi - w / 2, min(oi, ci)), w, max(abs(ci - oi), 1e-6), color=ci_col, lw=0))

    ax.plot(x, df["ema20"], color=EMA20, lw=1.6, label="EMA 20")
    ax.plot(x, df["ema50"], color=EMA50, lw=1.6, label="EMA 50")
    fp, sp = df.attrs.get("ema_periods", (20, 50))
    for key, col, per in (("ema20", EMA20, fp), ("ema50", EMA50, sp)):
        yv = df[key].iloc[-1]
        if not np.isnan(yv):
            ax.annotate(f"EMA {per}", (base - 1, yv), xytext=(6, 0), textcoords="offset points",
                        color=col, fontsize=8, weight="bold", va="center", ha="left", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.2", fc=PANEL, ec="none", alpha=0.9))

    full_len = df.attrs.get("full_len", base)
    shift = full_len - base
    def tag(xi, y, text, col, above):
        ax.annotate(text, (xi, y), xytext=(0, 14 if above else -14), textcoords="offset points", ha="center",
                    va="bottom" if above else "top", fontsize=7.5, weight="bold", color=BG, zorder=7,
                    bbox=dict(boxstyle="round,pad=0.25", fc=col, ec="none"))
    if jd is None or "events" not in jd:
        for cr in crosses:
            xi = cr.index - shift
            if 0 <= xi < base:
                y = df["ema20"].iloc[xi]
                ax.plot(xi, y, marker="^" if cr.direction == "bull" else "v", ms=8,
                        color=UP if cr.direction == "bull" else DOWN, mec=BG, mew=0.8, zorder=5)
    else:
        for pcx in jd["pre"]:
            xi = pcx.index - shift
            if 0 <= xi < base:
                tag(xi, df["ema20"].iloc[xi], pcx.label, MUTED, pcx.label == "PB")
        for ev in jd["events"]:
            xi = ev.index - shift
            if 0 <= xi < base:
                y = df["ema20"].iloc[xi]
                if ev.label in ("WP", "F"):
                    ax.plot(xi, y, marker="x", ms=7, color=MUTED, mew=1.4, zorder=5)
                    txt = (f"WP·{ev.reason}" if ev.label == "WP" else
                           (f"ext {ev.reason.split()[1]} · wait pb" if "waiting" in ev.reason else f"F·{ev.reason}"))
                    tag(xi, y, txt, "#4a5568", ev.direction == "bear")
                else:
                    bull = ev.label.startswith("EB")
                    col = UP if bull else DOWN
                    ax.plot(xi, y, marker="^" if bull else "v", ms=9, color=col, mec=BG, mew=0.8, zorder=6)
        for num, jn in enumerate(jd["journeys"], 1):
            xe, xx = jn.entry_index - shift, jn.exit_index - shift
            col = UP if jn.direction == "long" else DOWN
            long = jn.direction == "long"
            ok = jn.result > 0 or jn.exit_reason == "open"
            # entry / exit columns
            for xv, c_ in ((xe, col), (xx, col if ok else "#8a5a5a")):
                if 0 <= xv < base:
                    ax.axvline(xv, color=c_, lw=0.8, ls=(0, (2, 3)), alpha=0.55, zorder=3)
            if 0 <= xe < base:
                ax.plot(xe, jn.entry_price, marker="o", ms=5, color=col, mec=BG, mew=0.8, zorder=7)
                ax.annotate(f"#{num} {'EB' if long else 'ES'}{'·late' if jn.late else ''}{'·re' if jn.reentry else ''}{'·pb' if jn.pullback else ''}{'·pre' if jn.pre else ''} {jn.grade} · TAKEN {jn.entry_price:.2f}", (xe, jn.entry_price),
                            xytext=(-8, 14 if long else -14), textcoords="offset points", ha="right",
                            va="bottom" if long else "top", fontsize=7.5, weight="bold", color=BG, zorder=7,
                            bbox=dict(boxstyle="round,pad=0.25", fc=col, ec="none"))
            if 0 <= xx < base and jn.exit_reason != "open":
                ax.plot(xx, jn.exit_price, marker="s", ms=5, color=col if ok else "#8a5a5a", mec=BG, mew=0.8, zorder=7)
                lab = f"#{num} CLOSED {jn.exit_price:.2f} · {jn.result:+.1f}" + (f" (leg, {len(jn.adds)} adds)" if jn.adds else "") + f" · {jn.exit_reason.replace('_', ' ')}"
                near_edge = xx > base * 0.78
                ax.annotate(lab, (xx, jn.exit_price), xytext=(-8 if near_edge else 8, -14 if long else 14), textcoords="offset points",
                            ha="right" if near_edge else "left", va="top" if long else "bottom", fontsize=7.5, weight="bold", color=BG, zorder=7,
                            bbox=dict(boxstyle="round,pad=0.25", fc=col if ok else "#8a5a5a", ec="none"))
            for ad in jn.adds:
                xa = ad["index"] - shift
                if 0 <= xa < base:
                    ax.plot(xa, ad["price"], marker="D", ms=5, color=col, mec=BG, mew=0.8, zorder=7)
                    ax.annotate(f"+add {ad['price']:.2f} ({ad['result']:+.1f})", (xa, ad["price"]), xytext=(0, -14 if long else 14),
                                textcoords="offset points", ha="center", va="top" if long else "bottom", fontsize=7, color=col, zorder=7)
            if jn.lock_index is not None and 0 <= jn.lock_index - shift < base:
                xl = jn.lock_index - shift
                ax.annotate("⚡+2", (xl, df["high" if long else "low"].iloc[xl]),
                            xytext=(0, 8 if long else -8), textcoords="offset points", ha="center",
                            va="bottom" if long else "top", fontsize=7, weight="bold", color="#f2c14e", zorder=7)
            if jn.target_index is not None and 0 <= jn.target_index - shift < base:
                xt = jn.target_index - shift
                ax.annotate("15", (xt, df["high" if long else "low"].iloc[xt]),
                            xytext=(0, 6 if long else -6), textcoords="offset points", ha="center",
                            va="bottom" if long else "top", fontsize=7, weight="bold", color=col, zorder=7)
            a, b = max(0, xe), min(base - 1, xx)
            if b > a:
                ax.plot([a, b], [jn.entry_price, jn.entry_price], color=col, lw=1, ls=(0, (3, 3)), alpha=0.8, zorder=4)
                if jn.exit_reason != "open":
                    ax.annotate("", xy=(b, jn.exit_price), xytext=(a, jn.entry_price),
                                arrowprops=dict(arrowstyle="->", color=col if ok else "#8a5a5a", lw=1.2, alpha=0.9), zorder=5)
    if rng:
        t = df["time"].to_numpy()
        idx = int(np.searchsorted(t, rng["day_start"]))
        if 0 < idx < base:
            ax.axvline(idx - 0.5, color=MUTED, lw=1, ls=(0, (4, 3)), alpha=0.9)
            ax.axvspan(-1, idx - 0.5, color=BG, alpha=0.35, lw=0, zorder=0.5)   # dim the context day
            ax.text(idx - 1.2, 0.03, rng["prev"].strftime("%a %d %b") + " · context", transform=ax.get_xaxis_transform(),
                    color=MUTED, fontsize=8, ha="right", va="bottom")
            ax.text(idx + 0.2, 0.03, rng["day"].strftime("%a %d %b"), transform=ax.get_xaxis_transform(),
                    color=TEXT, fontsize=8, ha="left", va="bottom")
    ticks = np.linspace(0, base - 1, 6).astype(int)
    ax.set_xticks(ticks)
    ax.set_xticklabels([_ist(int(df["time"].iloc[t]), server_offset_h).strftime("%H:%M") for t in ticks])
    ax.set_xlim(-1, base + 1)
    pad = (h.max() - l.min()) * 0.08
    ax.set_ylim(l.min() - pad, h.max() + pad * 2.6)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")

    last = df.iloc[-1]
    state = "20 > 50  ↑" if last["ema20"] > last["ema50"] else "20 < 50  ↓"
    if jd is not None and "trend_now" in jd:
        state += f"   trend: {jd['trend_now'].upper()}"
    ax.text(0.012, 0.93, f"{tf}", transform=ax.transAxes, color=TEXT, fontsize=13, weight="bold", va="top")
    ax.text(0.012, 0.80, state, transform=ax.transAxes, color=UP if "↑" in state else DOWN, fontsize=9.5, va="top")
    ax.text(0.99, 0.93, f"{last['close']:.2f}", transform=ax.transAxes, color=TEXT, fontsize=11, ha="right", va="top")


def render_png(symbol: str, analysed: dict, path: str, server_offset_h: float = 3.0,
               bars_per_panel: dict | None = None, rng: dict | None = None) -> str:
    if rng and not bars_per_panel:       # two full days: show everything loaded
        bars_per_panel = {tf: len(analysed[tf]["df"]) for tf in analysed}
        if "M1" in analysed:
            m1t = analysed["M1"]["df"]["time"].to_numpy()
            bars_per_panel["M1"] = int((m1t >= rng["day_start"]).sum()) + 60   # M1: today only
    bars_per_panel = bars_per_panel or ({"M15": 96, "M5": 144, "M1": 180} if len(analysed) > 1 else {"M5": 288})
    journeys = analysed["M5"]["journeys"]
    n_rows = max(1, min(len(journeys), 10))
    figH = (11 if len(analysed) > 1 else 7.5) + 0.33 * n_rows + 0.9
    fig = plt.figure(figsize=(14, figH), facecolor=BG)
    yin = lambda inches: 1 - inches / figH          # header rows measured in inches from the top
    fig.subplots_adjust(left=0.02, right=0.94, top=yin(1.85), bottom=0.03, hspace=0.30)
    order = [tf for tf in ("M15", "M5", "M1") if tf in analysed]
    heights = {"M15": 1, "M5": 1.45 if len(order) > 1 else 2.6, "M1": 1}
    gs = fig.add_gridspec(len(order) + 1, 1, height_ratios=[heights[tf] for tf in order] + [0.22 + 0.095 * n_rows])
    for row, tf in enumerate(order):
        d = analysed[tf]
        df = d["df"]; df.attrs["full_len"] = len(df); df.attrs["ema_periods"] = analysed["M5"].get("ema_periods", (20, 50))
        ax = fig.add_subplot(gs[row])
        ax.set_zorder(3)
        ax.patch.set_alpha(0)
        # rounded panel background drawn in figure coordinates behind the axes
        bb = ax.get_position()
        fig.patches.append(FancyBboxPatch((bb.x0 - 0.008, bb.y0 - 0.035), bb.width + 0.062, bb.height + 0.06,
                                          boxstyle="round,pad=0.004,rounding_size=0.012",
                                          fc=PANEL, ec="none", transform=fig.transFigure, zorder=0))
        if tf == "M5":
            fig.patches.append(FancyBboxPatch((bb.x0 - 0.008, bb.y0 - 0.035), bb.width + 0.062, bb.height + 0.06,
                                              boxstyle="round,pad=0.004,rounding_size=0.012",
                                              fc="none", ec=EMA20, lw=1.2, alpha=0.55, transform=fig.transFigure, zorder=0.5))
        _panel(ax, df, d["crosses"], tf, bars_per_panel[tf], server_offset_h, highlight=(tf == "M5"), rng=rng,
               jd=(dict(d, trend_now=d["trend"][-1]) if tf == "M5" else
                   {"trend_now": analysed["M5"]["m15_trend_now"]} if tf == "M15" else None))

    # ---- positions table
    axt = fig.add_subplot(gs[len(order)]); axt.axis("off"); axt.set_zorder(3)
    bb = axt.get_position()
    fig.patches.append(FancyBboxPatch((bb.x0 - 0.008, bb.y0 - 0.01), bb.width + 0.062, bb.height + 0.02,
                                      boxstyle="round,pad=0.004,rounding_size=0.012", fc=PANEL, ec="none",
                                      transform=fig.transFigure, zorder=0))
    m5 = analysed["M5"]["df"]; last_close = float(m5["close"].iloc[-1])
    open_j = [j for j in journeys if j.exit_reason == "open"]
    if open_j:
        oj = open_j[0]; sgn = 1 if oj.direction == "long" else -1
        unreal = sgn * (last_close - oj.entry_price)
        pos = f"POSITION OPEN — {oj.direction.upper()} {oj.grade} from {oj.entry_price:.2f} ({_ist(oj.entry_time, server_offset_h):%H:%M} IST) · unrealised {unreal:+.2f} · best {oj.mfe:+.2f} · dd {oj.mae:+.2f}"
        pos_col = UP if unreal >= 0 else DOWN
    else:
        pos, pos_col = "POSITION — flat", MUTED
    axt.text(0.0, 1.0, f"Close {last_close:.2f}", transform=axt.transAxes, color=TEXT, fontsize=12, weight="bold", va="bottom")
    axt.text(0.11, 1.0, pos, transform=axt.transAxes, color=pos_col, fontsize=9.5, va="bottom")
    cols = ["#", "Side", "Grade", "Entry IST", "Entry", "Exit IST", "Exit", "Points", "Reason", "Session", "M15" if len(analysed) > 1 else "Trend"]
    rows = []
    for k, j in enumerate(journeys[-10:], 1):
        rows.append([str(k), "LONG" if j.direction == "long" else "SHORT", j.grade,
                     _ist(j.entry_time, server_offset_h).strftime("%d %b %H:%M"), f"{j.entry_price:.2f}",
                     _ist(j.exit_time, server_offset_h).strftime("%H:%M") if j.exit_reason != "open" else "—",
                     f"{j.exit_price:.2f}" if j.exit_reason != "open" else "open",
                     f"{j.result:+.2f}" if j.exit_reason != "open" else f"({(1 if j.direction=='long' else -1)*(last_close-j.entry_price):+.2f})",
                     j.exit_reason.replace("_", " "), j.session, j.m15_trend])
    if not rows:
        rows = [["—", "no clean entries", "", "", "", "", "", "", "", "", ""]]
    tbl = axt.table(cellText=rows, colLabels=cols, loc="upper left", cellLoc="center",
                    colWidths=[0.03, 0.06, 0.05, 0.11, 0.08, 0.08, 0.08, 0.07, 0.11, 0.07, 0.06],
                    bbox=[0, 0, 0.98, 0.86])
    tbl.auto_set_font_size(False); tbl.set_fontsize(8.5)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID); cell.set_linewidth(0.6)
        cell.set_facecolor(PANEL if r else "#232c3b")
        cell.get_text().set_color(MUTED if r == 0 else TEXT)
        if r and rows[r - 1][0] != "—":
            j = journeys[-10:][r - 1]
            if c == 1: cell.get_text().set_color(UP if j.direction == "long" else DOWN)
            if c == 7: cell.get_text().set_color(UP if j.result > 0 or j.exit_reason == "open" and rows[r-1][7].startswith("(+") else DOWN)
            if c == 2: cell.get_text().set_color(UP if j.grade == "A" else TEXT if j.grade == "B" else MUTED)

    last_t = int(analysed["M5"]["df"]["time"].iloc[-1])
    fp_, sp_ = analysed["M5"].get("ema_periods", (20, 50))
    sm = analysed["M5"]["summary"]; un = analysed["M5"]["unfiltered_summary"]
    ist = _ist(last_t, server_offset_h)
    srv = datetime.fromtimestamp(last_t, tz=timezone.utc)
    fig.text(0.02, yin(0.42), f"{symbol}   EMA {fp_} / {sp_} cross map" + ("   ·   LEG MODE" if sm.get("leg_mode") else ""), color=TEXT, fontsize=17, weight="bold", va="center")
    sub = ("" if not rng else f"{rng['prev']:%a %d %b} context only · trading {rng['day']:%a %d %b}  ·  ") + \
          ("M5 only — every M5 cross, trend from EMA50 slope. " if len(analysed) == 1 else "M5 execution, M15 permission. ") + "PB/PS pre-cross · EB/ES entry + grade · CB/ESC exit · WP whipsaw · F filtered (reason)"
    fig.text(0.02, yin(0.72), sub,
             color=MUTED, fontsize=9, va="center")
    fr = ", ".join(f"{k} {v}" for k, v in sm["filtered_reasons"].items()) or "none"
    fig.text(0.02, yin(0.98), f"FILTERED  {sm['trades']} trades · net {sm['net']:+.1f} · win {sm['win_rate']}% · worst dd {sm['worst_drawdown']:+.1f} · "
                          f"hit 15: {sm['hit_target']} · best run {sm['best_run']:+.1f} · late {sm['late_entries']} · pre {sm['pre_entries']} · pullback entries {sm['pullback_entries']}/{sm['waited_for_pullback']} waited · re-entries {sm['reentries']} ({sm['reentry_net']:+.1f}) · WP {sm['whipsaws']} · spike locks {sm['spike_locks']} · news flats {sm['news_flats']} · removed {sm['filtered']} ({fr})",
             color=TEXT, fontsize=8.5, va="center")
    fig.text(0.02, yin(1.20), f"unfiltered  {un['trades']} trades · net {un['net']:+.1f} · win {un['win_rate']}% · worst dd {un['worst_drawdown']:+.1f}"
                          f"   |   sessions (IST): Asia 05:30–12:30  London 12:30–17:30  NY 17:30–02:30   |   {'M15' if len(analysed) > 1 else 'M5'} trend now: {analysed['M5']['m15_trend_now'].upper()}",
             color=MUTED, fontsize=8.5, va="center")
    fig.text(0.94, yin(0.42), f"IST {ist:%d %b %H:%M}", color=TEXT, fontsize=10, ha="right", va="center")
    fig.text(0.94, yin(0.72), f"server {srv:%d %b %H:%M} (UTC{server_offset_h:+.0f})", color=MUTED, fontsize=9, ha="right", va="center")
    # legend
    fp_, sp_ = analysed["M5"].get("ema_periods", (20, 50))
    sm = analysed["M5"]["summary"]; un = analysed["M5"]["unfiltered_summary"]
    fig.text(0.52, yin(0.42), f"— EMA {fp_}", color=EMA20, fontsize=10, ha="right", va="center", weight="bold")
    fig.text(0.60, yin(0.42), f"— EMA {sp_}", color=EMA50, fontsize=10, ha="right", va="center", weight="bold")
    fig.savefig(path, dpi=130, facecolor=BG)
    plt.close(fig)
    return path
