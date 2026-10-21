"""EMA 9/21 on M30 — v2.16.0. Mechanical: the cross is the order. Rules in journeys.Rules, exits in journeys.GUARDIANS + Rules.
Run with AUREON_MODE=ema921 AUREON_EXEC_TF=M30 AUREON_AUTO=1 (and AUREON_PAPER=1 for the first week)."""
from __future__ import annotations

from .. import Strategy
from . import analysis, render_html, render_png
from .journeys import guardian_for, rules_for


class EMA921(Strategy):
    name = "ema921"; display_name = "EMA 9/21"; fast = 9; slow = 21
    ema_cols = ("ema9", "ema21")
    day_end_exit = True

    def guardian_for(self, symbol): return guardian_for(symbol)
    def rules_for(self, symbol): return rules_for(symbol)
    def add_emas(self, df): return analysis.add_emas(df, 9, 21)
    def analyse(self, bars, symbol, server_offset_h, news, display_from=None, trade_from=None, overrides=None, day_end=None, **guardian_kw):
        return analysis.analyse(bars, display_from, symbol, server_offset_h, news, trade_from, overrides, day_end, **guardian_kw)
    def render_png(self, *a, **k): return render_png.render_png(*a, **k)
    def render_html(self, *a, **k): return render_html.render_html(*a, **k)
    def describe(self):
        r = rules_for("XAUUSD"); tp = f"TP +{r.tp:g}" if r.tp else "no TP"
        return (f"EMA 9/21 on M30 · every cross taken at the bar close · broker SL −{r.stop:g} · {tp} · +{r.lock_at:g} seen → SL +{r.lock_to:g} · "
                f"exit {r.exit_mode}: first close through EMA 9{' (EMA 200 after +' + format(r.lock_at, 'g') + ')' if r.exit_mode == 'e9>e200' else ''} · "
                f"no entries Fri ≥ {int(r.friday_no_entry_from):02d}:00 · news 60/30 · Friday flat · PRE cards informational")


STRATEGY = EMA921()
