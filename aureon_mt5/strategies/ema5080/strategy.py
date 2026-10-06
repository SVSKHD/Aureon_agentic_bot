"""EMA 50/80 — the ACTIVE Aureon system. Detection thresholds live in journeys.RULES and are NOT changed here."""
from __future__ import annotations

from .. import Guardian, Strategy
from . import analysis, render_html, render_png
from .journeys import rules_for

GUARDIANS = {
    "XAU": Guardian(secure_at=10.0, secure_level=10.0, pre_stop=6.0, ride_step=5.0, ema_slow_sl_buffer=1.0, enabled=True,
                    note="Gold profile"),
    "XAG": Guardian(secure_at=0.15, secure_level=0.15, pre_stop=0.08, ride_step=0.08, ema_slow_sl_buffer=0.02, enabled=False,
                    note="Silver profile — EXPERIMENTAL, disabled until validated (enable with --enable-silver)"),
}


class EMA5080(Strategy):
    name = "ema5080"; display_name = "EMA 50/80"; fast = 50; slow = 80

    def guardian_for(self, symbol):
        for k, g in GUARDIANS.items():
            if symbol.upper().startswith(k):
                return g
        return None

    def rules_for(self, symbol): return rules_for(symbol)
    def add_emas(self, df): return analysis.add_emas(df)
    def analyse(self, bars, symbol, server_offset_h, news, display_from=None, trade_from=None, overrides=None):
        return analysis.analyse(bars, display_from, symbol, server_offset_h, news, trade_from, overrides)
    def render_png(self, *a, **k): return render_png.render_png(*a, **k)
    def render_html(self, *a, **k): return render_html.render_html(*a, **k)
    def describe(self):
        return ("EMA 50/80 on M5 · P (pre-cross) or confirmed cross · whipsaw zone · no extension cap · "
                "guardian: −6 pre-stop, 12-bar timeout, cross → SL EMA 80, secure +10, ride +5 steps, close on EMA 50 turn")


STRATEGY = EMA5080()
