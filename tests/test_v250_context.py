"""v2.5.0 — context packer: deterministic output, size budget, field coverage."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5.context_pack import pack, to_prompt
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal


class FakeAgent:
    def __init__(self):
        self.symbol = "XAUUSD"; self.off = 3.0; self.news = (); self.trend_state = "BULLISH"
        self.S = types.SimpleNamespace(name="ema2050", ema_cols=("ema20", "ema50"), fast=20, slow=50, display_name="EMA 20/50")
        self.cfg = Config(); self.cfg.ctx_bars = 48; self.cfg.ctx_max_chars = 6000
        self.last_res = None; self.chop = None
        self._htf_cache = None; self._streak_cache = None; self._limit_cache = None
    def _is_chop(self): return False
    def _warnings(self, df): return []


def _df(n=60, price=4200.0):
    t = 1752364800 + np.arange(n) * 300
    return pd.DataFrame({"time": t, "open": price, "close": price, "high": price + 1, "low": price - 1,
                          "ema20": price - 2, "ema50": price - 5})


# ================================================================== deterministic output for a fixture
def test_deterministic_output():
    ag = FakeAgent(); df = _df()
    sig = {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])}
    ctx1 = pack(ag, df, sig)
    ctx2 = pack(ag, df, sig)
    assert ctx1 == ctx2  # identical for same inputs


# ================================================================== all expected keys present
def test_key_coverage():
    ag = FakeAgent(); df = _df()
    sig = {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])}
    ctx = pack(ag, df, sig)
    expected_keys = {"symbol", "mode", "side", "event", "bar_time_ist", "close", "ema_fast", "ema_slow", "gap",
                     "crosses_today", "morning_crosses", "bars_since_cross", "chop", "session", "confirm_state",
                     "dist_to_fast_ema_pts", "swing_against_last6_pts", "atr20", "day_pnl_pts", "trades_today",
                     "minutes_to_news", "minutes_since_news", "governor", "control_mode", "trend_state",
                     "htf_context", "spread_warning", "streak", "whipsaw", "bars_ohlc", "last_trades", "track_record"}
    assert expected_keys <= set(ctx.keys())


# ================================================================== size budget respected
def test_size_budget():
    ag = FakeAgent(); ag.cfg.ctx_bars = 200; ag.cfg.ctx_max_chars = 2000
    df = _df(n=200)
    sig = {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])}
    ctx = pack(ag, df, sig)
    text = to_prompt(ctx, max_chars=2000)
    assert len(text) <= 2000


def test_large_budget_includes_bars():
    ag = FakeAgent(); ag.cfg.ctx_bars = 48; ag.cfg.ctx_max_chars = 6000
    df = _df(n=60)
    sig = {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])}
    ctx = pack(ag, df, sig)
    text = to_prompt(ctx, max_chars=6000)
    assert "BARS (M5)" in text


# ================================================================== whipsaw/chop/news fields
def test_chop_field():
    ag = FakeAgent()
    ag.chop = {"chop": True, "crosses": 6, "day": "2026-10-13", "threshold": 5}
    ag._is_chop = lambda: True
    df = _df()
    ctx = pack(ag, df, {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])})
    assert ctx["chop"] is True


def test_news_field():
    import time
    ag = FakeAgent(); ag.news = (int(time.time()) + 600,)  # 10 min from now
    df = _df()
    ctx = pack(ag, df, {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])})
    assert ctx["minutes_to_news"] is not None and ctx["minutes_to_news"] <= 11


# ================================================================== position data included
def test_position_context():
    ag = FakeAgent(); df = _df()
    pos = {"price_open": 4200.0, "sl": 4188.0, "direction": "long", "current": 4205.0, "points": 5.0}
    st = {"peak": 7.0, "secured": 0.0, "bars": 10, "mae": -2.0, "soft_lock": 1.0}
    ctx = pack(ag, df, {"event": "pullback", "side": "LONG", "bar_t": int(df["time"].iloc[-1])}, position=pos, st=st)
    assert "position" in ctx
    assert ctx["position"]["entry"] == 4200.0 and ctx["position"]["soft_lock"] == 1.0


# ================================================================== to_prompt contains key lines
def test_to_prompt_content():
    ag = FakeAgent(); df = _df()
    ctx = pack(ag, df, {"event": "enter", "side": "LONG", "bar_t": int(df["time"].iloc[-1])})
    text = to_prompt(ctx)
    assert "CONTEXT" in text and "XAUUSD" in text and "ema2050" in text
    assert "crosses today" in text and "chop" in text and "session" in text
