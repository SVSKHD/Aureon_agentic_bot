"""v1.8.6 — move progress after a signal, leg end, position advisor ('should we close?'), snapshots. Notifications only."""
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
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None): self.titles.append(embed["title"] if embed else body); return True


def agent(tmp_path, notifier):
    cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    return SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), notifier, Journal(str(tmp_path)), ())


def frame(prices):
    n = len(prices); p = np.array(prices, float)
    return pd.DataFrame({"time": np.arange(n) * 300, "open": p, "close": p, "high": p + 0.2, "low": p - 0.2,
                         "ema_fast": p, "ema_slow": p + 1})


def test_progress_milestones_once_each(tmp_path):
    n = Cap(); ag = agent(tmp_path, n)
    ag.leg = {"side": "SHORT", "kind": "cross", "start_t": 0, "start_px": 4158.0, "best": 0.0, "best_px": 4158.0, "best_t": 0, "against": 0.0, "steps_sent": 0}
    df = frame([4158, 4150, 4140, 4135, 4126, 4120])          # best ≈ +38
    ag._track_leg(df, int(df.time.iloc[-1]), False, -1)
    ag._track_leg(df, int(df.time.iloc[-1]), False, -1)       # repeated poll: no duplicates
    moved = [t for t in n.titles if "MOVED" in t]
    assert moved == ["XAUUSD · MT5 · MOVED +30 · SHORT"]       # one card for the highest step reached, then +40 later
    df2 = frame([4158, 4150, 4140, 4135, 4126, 4117])
    ag._track_leg(df2, int(df2.time.iloc[-1]), False, -1)
    assert [t for t in n.titles if "MOVED" in t][-1] == "XAUUSD · MT5 · MOVED +40 · SHORT"

def test_leg_end_on_cross_back(tmp_path):
    n = Cap(); ag = agent(tmp_path, n)
    ag.leg = {"side": "SHORT", "kind": "cross", "start_t": 0, "start_px": 4158.0, "best": 0.0, "best_px": 4158.0, "best_t": 0, "against": 0.0, "steps_sent": 0}
    df = frame([4158, 4140, 4120, 4125, 4130])
    ag._track_leg(df, int(df.time.iloc[-1]), True, +1)         # 50 back above 80 -> leg over
    assert ag.leg is None and any("LEG ENDED · SHORT" in t for t in n.titles)
    assert any(r["event"] == "leg" for r in ag.journal.read(0))

def test_advisor_asks_on_giveback_without_closing(tmp_path, monkeypatch):
    fake = FakeMT5(price=4205.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); n = Cap()
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    df = pd.DataFrame({"time": np.arange(200) * 300, "close": 4205.0, "high": 4205.0, "low": 4205.0})
    guard(ag, fake, df, sgn=1)
    ag.state[1]["peak"] = 20.0                                  # had +20, now +5
    df2 = df.copy(); df2["time"] = df2["time"] + 300
    guard(ag, fake, df2, sgn=1)
    assert any("SHOULD WE CLOSE?" in t for t in n.titles) and not fake.closed      # asks, does not close by itself
    assert any("TRACKING" in t for t in n.titles)

def test_snapshot_every_6_bars(tmp_path, monkeypatch):
    fake = FakeMT5(price=4203.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); n = Cap()
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, Journal(str(tmp_path)), ())
    for i in range(7):
        df = pd.DataFrame({"time": np.arange(200) * 300 + i * 300, "close": 4203.0, "high": 4203.0, "low": 4203.0})
        guard(ag, fake, df, sgn=1)
    assert sum("POSITION UPDATE" in t for t in n.titles) == 1
