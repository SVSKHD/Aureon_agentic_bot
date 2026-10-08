"""v1.9.7 — fixes from the 8 Oct live session:
1) 'watch for a re-entry LONG' was posted while a bearish P was forming -> now WEAKENING, 'don't buy this dip'; RE suppressed.
2) P timeout closed a winning short at +1.46 just before a +11.5 move -> in profit (or your own TP) = hold + breakeven, never close."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from aureon_mt5 import broker
from aureon_mt5.strategies import get_strategy
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.agent import SymbolAgent
from tests.test_v181 import FakeMT5, guard


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body); self.bodies.append(str(embed)); return True


def bullish_converging(n=120):
    """EMA 50 above 80 but the gap collapsing — like 10:50 IST on 8 Oct."""
    t = np.arange(n) * 300
    fast = np.r_[np.linspace(4120, 4132, n - 6), [4131.8, 4131.6, 4131.4, 4131.3, 4131.2, 4131.17]]
    slow = np.r_[np.linspace(4110, 4126, n - 6), [4126.4, 4126.7, 4127.0, 4127.2, 4127.4, 4127.52]]
    close = np.r_[fast[:-1] + 1.0, [4129.74]]
    return pd.DataFrame({"time": t, "open": close, "high": close + 1.5, "low": close - 1.5, "close": close, "ema_fast": fast, "ema_slow": slow})


def test_pullback_card_becomes_weakening_when_lines_converge(tmp_path):
    n = Cap(); cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    df = bullish_converging()
    ag.trend_state = "BULLISH"
    ag._trend_and_reentry(df, int(df.time.iloc[-1]), 4129.74, 4131.17, 4127.52)
    titles = " ".join(n.titles); bodies = " ".join(n.bodies)
    assert "BULLISH · WEAKENING" in titles and "re-entry LONG" not in bodies and "don't buy this dip" in bodies


def test_healthy_pullback_still_says_pullback(tmp_path):
    n = Cap(); cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    m = 120; t = np.arange(m) * 300
    fast = np.linspace(4100, 4140, m); slow = fast - 8.0                       # lines well apart and NOT converging
    close = fast + 2; close[-1] = fast[-1] - 3
    df = pd.DataFrame({"time": t, "open": close, "high": close + 1, "low": close - 1, "close": close, "ema_fast": fast, "ema_slow": slow})
    ag.trend_state = "BULLISH"
    ag._trend_and_reentry(df, int(t[-1]), float(close[-1]), float(fast[-1]), float(slow[-1]))
    assert any("BULLISH · PULLBACK" in x for x in n.titles) and "healthy pullback" in " ".join(n.bodies)


def test_opposite_p_makes_it_weakening(tmp_path):
    n = Cap(); cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    m = 120; t = np.arange(m) * 300
    fast = np.linspace(4100, 4140, m); slow = fast - 8.0; close = fast + 2; close[-1] = fast[-1] - 3
    df = pd.DataFrame({"time": t, "open": close, "high": close + 1, "low": close - 1, "close": close, "ema_fast": fast, "ema_slow": slow})
    ag.latest_signal = {"bar_time": int(t[-1]), "kind": "P", "direction": "SHORT", "consumed": False}
    assert ag._turning(df, int(t[-1]), "LONG").startswith("opposite P SHORT")


def _harness(tmp_path, monkeypatch, price, tp=0.0):
    fake = FakeMT5(direction="short", entry=4131.39, sl=4143.58, price=price); fake.pos.tp = tp
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); n = Cap()
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    return fake, ag, n


def run_bars(ag, fake, k, price):
    for i in range(k):
        df = pd.DataFrame({"time": np.arange(200) * 300 + i * 300, "close": price, "high": price, "low": price})
        guard(ag, fake, df, sgn=+1)                      # lines still bullish -> the short is in P phase


def test_timeout_holds_winning_short_and_locks_breakeven(tmp_path, monkeypatch):
    fake, ag, n = _harness(tmp_path, monkeypatch, price=4129.63)          # short +1.76 after 12 bars (8 Oct case)
    fake.symbol_info_tick = lambda s: types.SimpleNamespace(bid=4129.33, ask=4129.63, time=int(__import__("time").time() + 3 * 3600))
    run_bars(ag, fake, 13, 4129.6)
    assert not fake.closed and any("P TIMEOUT · HOLDING" in t for t in n.titles)
    assert abs(fake.pos.sl - 4130.89) < 0.01                               # entry − 0.5 for a short


def test_timeout_respects_your_tp_even_when_flat(tmp_path, monkeypatch):
    fake, ag, n = _harness(tmp_path, monkeypatch, price=4131.69, tp=4115.84)   # slightly negative, but you set a TP
    run_bars(ag, fake, 13, 4131.7)
    assert not fake.closed and any("P TIMEOUT · HOLDING" in t for t in n.titles)


def test_timeout_still_closes_losers_without_plan(tmp_path, monkeypatch):
    fake, ag, n = _harness(tmp_path, monkeypatch, price=4133.39)          # short −2, no TP
    run_bars(ag, fake, 13, 4133.4)
    assert fake.closed
