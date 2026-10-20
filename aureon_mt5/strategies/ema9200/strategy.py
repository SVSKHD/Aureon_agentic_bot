"""EMA 9/200 on M30 — v2.15.0. Candidates are OFFERED (TAKE / SKIP), never auto-taken. Detection in journeys.Rules; the
guardian profile in journeys.GUARDIANS (replay and live guardian read the same object). Nothing here is derived from the
other modes. Run with AUREON_EXEC_TF=M30 (the research timeframe)."""
from __future__ import annotations

from .. import Strategy
from . import analysis, render_html, render_png
from .journeys import guardian_for, rules_for


class EMA9200(Strategy):
    name = "ema9200"; display_name = "EMA 9/200"; fast = 9; slow = 200
    ema_cols = ("ema9", "ema200")
    day_end_exit = True

    def guardian_for(self, symbol):
        return guardian_for(symbol)

    def rules_for(self, symbol): return rules_for(symbol)
    def add_emas(self, df): return analysis.add_emas(df, 9, 200)
    def analyse(self, bars, symbol, server_offset_h, news, display_from=None, trade_from=None, overrides=None, day_end=None, **guardian_kw):
        return analysis.analyse(bars, display_from, symbol, server_offset_h, news, trade_from, overrides, day_end, **guardian_kw)
    def render_png(self, *a, **k): return render_png.render_png(*a, **k)
    def render_html(self, *a, **k): return render_html.render_html(*a, **k)
    def describe(self):
        return ("EMA 9/200 on M30 · bias = EMA9 vs EMA200 · candidate = pullback to EMA9 (±8) with the bias, or a momentum bar (body ≥ 30) · "
                "one ask per cluster (4 bars / $10) · the ask expires at the next bar close · no asks 21:00–24:00 server or Friday after 17:00 · "
                "news 60/30 · YOU decide TAKE / SKIP · guardian: −30 stop · +40 → SL +30 · ride +10 · close on EMA9 turn once secured · Friday flat")


STRATEGY = EMA9200()
