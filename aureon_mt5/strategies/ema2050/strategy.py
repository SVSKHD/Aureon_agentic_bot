"""EMA 20/50 — FROZEN system, included as-is from the original aureon_ema project (M5-only mode, GOLD/SILVER rules).
Detection only in Aureon MT5: it has no live guardian profile, so positions are NOT managed under this mode.
Nothing here is derived from ema5080."""
from __future__ import annotations

from .. import Strategy
from . import analysis, render_html, render_png


class EMA2050(Strategy):
    name = "ema2050"; display_name = "EMA 20/50"; fast = 20; slow = 50
    ema_cols = ("ema20", "ema50")

    def guardian_for(self, symbol):
        return None          # frozen system: signals only, no live position management

    def add_emas(self, df): return analysis.add_emas(df, 20, 50)
    def analyse(self, bars, symbol, server_offset_h, news, display_from=None, trade_from=None, overrides=None):
        return analysis.analyse(bars, horizon=24, display_from=display_from, symbol=symbol, server_offset_h=server_offset_h,
                                m5_only=True, news_blackout=news, trade_from=trade_from, fast=20, slow=50)
    def render_png(self, *a, **k): return render_png.render_png(*a, **k)
    def render_html(self, *a, **k): return render_html.render_html(*a, **k)
    def describe(self):
        return "EMA 20/50 on M5 (frozen) · confirmed cross, late/pullback entries, 15→12 floor · signals only, no live guardian"


STRATEGY = EMA2050()
