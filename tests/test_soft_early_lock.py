"""v2.4.5 — soft early lock: bar-close based instead of broker SL."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from tests.test_v181 import FakeMT5

S2050 = get_strategy("ema2050")
OFF = 3.0


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0])
        self.bodies.append(str(embed or body))
        return True


def _df(n=200, price=4200.0):
    d = {"time": np.arange(n) * 300, "close": price, "high": price + 1, "low": price - 1}
    for c in ("ema20", "ema50", "ema_fast", "ema_slow"):
        d[c] = price - 5 if "fast" in c or "20" in c else price - 10
    return pd.DataFrame(d)


def _agent(tmp_path, monkeypatch, mode="soft", price=4202.0, sl=0.0):
    fake = FakeMT5(direction="long", entry=4200.0, sl=sl, price=price)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.early_lock_mode = mode
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, j, ()); ag.off = OFF
    return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, ag=ag)


def guard(ag, fake, df, sgn, new_bar=True, close=None):
    ef, es = (4190.0, 4180.0) if sgn > 0 else (4180.0, 4190.0)
    df = df.copy()
    for c in ("ema20", "ema50", "ema_fast", "ema_slow"):
        df[c] = ef if ("fast" in c or "20" in c) else es
    ag._guard(new_bar, int(df["time"].iloc[-1]), df, close if close is not None else fake.price, ef, es, sgn)


# ================================================================== peak +3 → no SL change, soft_lock set
def test_soft_peak_no_sl_change(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="soft", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)  # protect first
    # Price reaches +3.5 → peak >= early_at (3)
    assert r.ag.state[1].get("soft_lock") is not None
    # Broker SL NOT moved to entry+1 — it stays at protect level
    assert r.fake.pos.sl < 4200.0 + 1  # SL still at entry - pre_stop (4188)
    assert any("EARLY LOCK" in t and "SOFT" in t.upper() for t in r.n.titles)
    # State saved
    j2 = Journal(str(tmp_path))
    st = j2.load_state("XAUUSD")
    assert st.get(1, {}).get("soft_lock") is not None


# ================================================================== wick through entry inside bar → no close
def test_soft_wick_no_close(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="soft", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)  # protect + soft lock
    assert r.ag.state[1].get("soft_lock")
    # Wick: price drops but bar doesn't close below lock level
    r.fake.price = 4199.0  # below entry+1, but this is a tick, not a bar close
    guard(r.ag, r.fake, df, 1, new_bar=False, close=4203.0)  # bar close is still +3
    assert 1 in r.ag.state  # position still open


# ================================================================== bar CLOSING below entry+1 → close (LONG)
def test_soft_bar_close_triggers_exit_long(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="soft", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)  # protect + soft lock
    # Bar closes at entry+0.5 (below entry+1)
    r.fake.price = 4200.5
    guard(r.ag, r.fake, df, 1, new_bar=True, close=4200.5)
    # Position should be closed
    assert r.fake.closed
    exits = [e for e in r.j.read(0) if e["event"] == "soft_lock_exit"]
    assert len(exits) == 1


# ================================================================== bar CLOSING below entry-level → close (SHORT)
def test_soft_bar_close_triggers_exit_short(tmp_path, monkeypatch):
    fake = FakeMT5(direction="short", entry=4200.0, sl=0.0, price=4196.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.early_lock_mode = "soft"
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, j, ()); ag.off = OFF
    df = _df(price=4196.5)
    ef, es = 4180.0, 4190.0
    for c in ("ema20", "ema50", "ema_fast", "ema_slow"):
        df[c] = ef if ("fast" in c or "20" in c) else es
    ag._guard(True, int(df["time"].iloc[-1]), df, 4196.5, ef, es, -1)
    # Peak = 4200-4196.5 = 3.5 >= early_at (3) → soft lock set
    assert ag.state[1].get("soft_lock")
    # Bar closes at entry-0.5 = 4199.5 (above entry-1 = 4199 for SHORT → lock_px = 4200-1=4199; close >= 4199 → triggers)
    fake.price = 4199.5
    ag._guard(True, int(df["time"].iloc[-1]) + 300, df, 4199.5, ef, es, -1)
    assert fake.closed


# ================================================================== price runs to +10 → broker SL moves, soft_lock cleared
def test_secure_clears_soft_lock(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="soft", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)
    assert r.ag.state[1].get("soft_lock")
    # Price runs to +12 → secure at +10
    r.fake.price = 4212.0
    guard(r.ag, r.fake, df, 1, close=4212.0)
    assert r.ag.state[1]["secured"] >= 10.0
    assert r.ag.state[1].get("soft_lock") is None  # cleared
    cleared = [e for e in r.j.read(0) if e["event"] == "soft_lock_cleared"]
    assert len(cleared) == 1


# ================================================================== hard mode reproduces today's behaviour
def test_hard_mode_moves_sl(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="hard", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)
    # In hard mode, SL should be moved to entry+1
    assert r.fake.pos.sl >= 4200.0 + 1 - 0.1  # entry + early_level
    assert r.ag.state[1]["secured"] >= 1.0
    assert r.ag.state[1].get("soft_lock") is None


# ================================================================== off mode → no lock at all
def test_off_mode_no_lock(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="off", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)
    assert r.ag.state[1].get("soft_lock") is None
    assert r.ag.state[1]["secured"] == 0.0
    assert not any("EARLY LOCK" in t for t in r.n.titles)


# ================================================================== restart restores soft_lock
def test_restart_restores_soft_lock(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, mode="soft", price=4203.5)
    df = _df(price=4203.5)
    guard(r.ag, r.fake, df, 1)
    assert r.ag.state[1].get("soft_lock")
    # "restart"
    j2 = Journal(str(tmp_path))
    st = j2.load_state("XAUUSD")
    assert st[1].get("soft_lock") is not None
