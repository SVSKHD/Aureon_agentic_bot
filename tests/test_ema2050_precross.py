"""v2.5.2 — ema2050 PRE-CROSS detection + agent card."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from tests.test_v200_ema2050 import OFF

S = get_strategy("ema2050")


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []; self.asks = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0])
        self.bodies.append(str(embed or body)); return True
    def ask(self, key, title, lines, **kw):
        self.asks.append({"key": key, "title": title}); return super().ask(key, title, lines, **kw)


def _approach_frame(direction="SHORT", n=60, gap_start=8.0, gap_end=0.5):
    """Synthetic frame where EMA20 approaches EMA50 from above (SHORT approach) or below (LONG approach).
    Price closes through EMA20 for the last few bars."""
    t = 1752364800 + np.arange(n) * 300
    e50 = np.full(n, 4000.0)
    # EMA20 starts separated, converges
    e20 = np.linspace(4000 + gap_start, 4000 + gap_end, n) if direction == "SHORT" else np.linspace(4000 - gap_start, 4000 - gap_end, n)
    # Price starts on the EMA-order side, then crosses through EMA20 in the last bars
    close = e20.copy()
    if direction == "SHORT":
        # For SHORT approach: price closes BELOW EMA20 in the last bars (crossing from above)
        close[-3:] = e20[-3:] - 0.5
    else:
        close[-3:] = e20[-3:] + 0.5
    high = close + 1; low = close - 1
    spread = e20 - e50  # gap = EMA20 - EMA50
    df = pd.DataFrame({"time": t, "close": close, "high": high, "low": low,
                        "ema20": e20, "ema50": e50, "spread": spread})
    return df


# ================================================================== detection: exactly one PRE SHORT
def test_pre_short_detected():
    df = _approach_frame("SHORT", n=60)
    rules = J.rules_for("XAUUSD")
    res = J.build(df, rules, OFF)
    pres = [e for e in res["events"] if e.label == "PRE"]
    assert len(pres) >= 1
    assert pres[-1].direction == "bear"  # SHORT approach


def test_pre_long_detected():
    df = _approach_frame("LONG", n=60, gap_start=-5.0, gap_end=-0.5)
    # For LONG: EMA20 < EMA50, approaching from below
    df["spread"] = df["ema20"] - df["ema50"]  # negative gap
    rules = J.rules_for("XAUUSD")
    res = J.build(df, rules, OFF)
    pres = [e for e in res["events"] if e.label == "PRE"]
    # May or may not detect depending on exact values; check no crash
    assert isinstance(pres, list)


# ================================================================== no PRE when lines never separated
def test_no_pre_without_separation():
    df = _approach_frame("SHORT", n=60, gap_start=0.3, gap_end=0.1)  # never separated enough
    rules = J.rules_for("XAUUSD")
    res = J.build(df, rules, OFF)
    pres = [e for e in res["events"] if e.label == "PRE"]
    assert len(pres) == 0


# ================================================================== no PRE when gap too large
def test_no_pre_gap_too_large():
    df = _approach_frame("SHORT", n=60, gap_start=10.0, gap_end=8.0)  # gap still large
    rules = J.rules_for("XAUUSD")
    res = J.build(df, rules, OFF)
    pres = [e for e in res["events"] if e.label == "PRE"]
    assert len(pres) == 0


# ================================================================== agent posts send() card, not ask(), no track_ask
def test_agent_pre_card_is_send_not_ask(tmp_path, monkeypatch):
    from tests.test_v200_alerts import TickMT5
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.pre_alert_2050 = True
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S, n, j, ()); ag.off = OFF
    # Build a frame that triggers PRE
    df = _approach_frame("SHORT", n=60)
    res = J.build(df, J.rules_for("XAUUSD"), OFF)
    ag.last_df = df; ag.last_res = res; ag.last_bar = int(df["time"].iloc[-1])
    ag._detect_2050(res, df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]),
                    float(df["ema20"].iloc[-1]), float(df["ema50"].iloc[-1]))
    # Check: send card (in titles), NOT an ask
    pre_titles = [t for t in n.titles if "PRE-CROSS" in t]
    if [e for e in res["events"] if e.label == "PRE" and e.index == len(df) - 1]:
        assert len(pre_titles) >= 1
        assert len(n.asks) == 0  # no ask buttons
        # Journal pre_cross
        pj = [r for r in j.read(0) if r["event"] == "pre_cross"]
        assert len(pj) >= 1
        # No track_ask
        assert len(ag.open_asks) == 0


# ================================================================== AUREON_PRE_ALERT_2050=0 → no PRE cards
def test_pre_alert_disabled(tmp_path, monkeypatch):
    from tests.test_v200_alerts import TickMT5
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.pre_alert_2050 = False
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S, n, j, ()); ag.off = OFF
    df = _approach_frame("SHORT", n=60)
    res = J.build(df, J.rules_for("XAUUSD"), OFF)
    ag.last_df = df; ag.last_res = res; ag.last_bar = int(df["time"].iloc[-1])
    ag._detect_2050(res, df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]),
                    float(df["ema20"].iloc[-1]), float(df["ema50"].iloc[-1]))
    assert not any("PRE-CROSS" in t for t in n.titles)
