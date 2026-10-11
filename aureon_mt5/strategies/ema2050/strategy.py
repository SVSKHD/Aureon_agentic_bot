"""EMA 20/50 — ACTIVE since v2.0.0. Detection thresholds live in journeys.Rules; the guardian profile in journeys.GUARDIANS
(the replay and the live guardian read the same object). Nothing here is derived from ema5080."""
from __future__ import annotations

from .. import Strategy
from . import analysis, render_html, render_png
from .journeys import guardian_for, rules_for


class EMA2050(Strategy):
    name = "ema2050"; display_name = "EMA 20/50"; fast = 20; slow = 50
    ema_cols = ("ema20", "ema50")
    day_end_exit = True

    def guardian_for(self, symbol):
        return guardian_for(symbol)

    def rules_for(self, symbol): return rules_for(symbol)
    def add_emas(self, df): return analysis.add_emas(df, 20, 50)
    def analyse(self, bars, symbol, server_offset_h, news, display_from=None, trade_from=None, overrides=None, day_end=None):
        return analysis.analyse(bars, display_from, symbol, server_offset_h, news, trade_from, overrides, day_end)
    def render_png(self, *a, **k): return render_png.render_png(*a, **k)
    def render_html(self, *a, **k): return render_html.render_html(*a, **k)
    def describe(self):
        return ("EMA 20/50 on M5 · cross confirmed by 3 bars + 1.5 gap (within 18 bars, flip = multi cross) · pullback entry to EMA20 "
                "(±1.5, window 12, fallback ≤5 from EMA20) · no entries 21:00–23:59 server / outside 05:30–23:00 IST · news 60/30 · "
                "guardian: −12 stop, +3 → SL +1, secure +10, ride +5, close on EMA20 turn once secured, exit on the next confirmed cross")


STRATEGY = EMA2050()
