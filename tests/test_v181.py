"""Targeted tests for the high-risk behaviour in v1.8.1. Run: python -m pytest -q"""
import os, sys, time, json, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import broker
from aureon_mt5.strategies import resolve_mode, get_strategy, UnknownMode, DEFAULT_MODE
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.agent import SymbolAgent


# ------------------------------------------------------------------ mode
def test_default_mode(monkeypatch):
    monkeypatch.delenv("AUREON_MODE", raising=False)
    assert resolve_mode(None) == "ema5080" == DEFAULT_MODE

def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("AUREON_MODE", "ema2050")
    assert resolve_mode(None) == "ema2050" and resolve_mode("5080") == "ema5080"

def test_invalid_mode_rejected():
    with pytest.raises(UnknownMode):
        resolve_mode("ema1234")

def test_strategies_isolated():
    a, b = get_strategy("ema5080"), get_strategy("ema2050")
    assert a.guardian_for("XAUUSD") is not None and b.guardian_for("XAUUSD") is None     # 20/50 gets no 50/80 guardian rules
    assert a.ema_cols != b.ema_cols and (a.fast, a.slow) == (50, 80) and (b.fast, b.slow) == (20, 50)


# ------------------------------------------------------------------ guardian harness
class FakeMT5:
    """Minimal MT5 stand-in: one position, SL modifications honoured or rejected on demand."""
    POSITION_TYPE_BUY = 0; TRADE_ACTION_SLTP = 6; TRADE_ACTION_DEAL = 1; TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1; ORDER_TYPE_BUY = 0; ORDER_FILLING_IOC = 1; ORDER_FILLING_FOK = 0; DEAL_ENTRY_OUT = 1; DEAL_TYPE_SELL = 1
    SYMBOL_TRADE_MODE_DISABLED = 0
    def __init__(self, direction="long", entry=4200.0, sl=0.0, price=4200.0):
        self.pos = types.SimpleNamespace(ticket=1, type=0 if direction == "long" else 1, price_open=entry, sl=sl, tp=0.0, volume=0.1, time=0, comment="", magic=0)
        self.price = price; self.reject = False; self.sends = []; self.closed = False
    def initialize(self): return True
    def last_error(self): return (0, "")
    def terminal_info(self): return object()
    def symbol_info(self, s): return types.SimpleNamespace(digits=2, trade_mode=4)
    def symbol_info_tick(self, s): return types.SimpleNamespace(bid=self.price, ask=self.price + 0.3, time=int(time.time() + 3 * 3600))
    def positions_get(self, symbol=None, ticket=None): return [] if self.closed else [self.pos]
    def history_deals_get(self, a, b): return []
    def order_send(self, req):
        self.sends.append(req)
        if req["action"] == self.TRADE_ACTION_SLTP:
            if self.reject: return types.SimpleNamespace(retcode=10016, comment="Invalid stops")
            self.pos.sl = req["sl"]; return types.SimpleNamespace(retcode=10009, comment="done")
        self.closed = True; return types.SimpleNamespace(retcode=10009, comment="done")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    def make(direction="long", entry=4200.0, sl=0.0, price=4200.0):
        fake = FakeMT5(direction, entry, sl, price)
        monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
        cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
        ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Notifier(None), Journal(str(tmp_path)), ())
        import pandas as pd, numpy as np
        n = 200; df = pd.DataFrame({"time": np.arange(n) * 300, "close": np.full(n, price), "high": price, "low": price})
        return fake, ag, df
    return make


def guard(ag, fake, df, sgn, new_bar=True, close=None):
    ef, es = (4190.0, 4180.0) if sgn > 0 else (4180.0, 4190.0)
    df = df.copy(); df["ema_fast"] = ef; df["ema_slow"] = es
    ag._guard(new_bar, int(df["time"].iloc[-1]), df, close if close is not None else fake.price, ef, es, sgn)


def test_never_loosen_existing_sl(harness):
    fake, ag, df = harness(sl=4220.0, price=4230.0)          # manual SL already locks +20
    guard(ag, fake, df, sgn=1)
    assert fake.pos.sl >= 4220.0                              # never lowered to entry+10
    assert ag.state[1]["secured"] >= 20.0                     # reconstructed from the live SL

def test_failed_sl_does_not_mark_secured(harness):
    fake, ag, df = harness(price=4212.0); fake.reject = True
    guard(ag, fake, df, sgn=1)
    assert ag.state[1]["secured"] == 0.0 and fake.pos.sl == 0.0

def test_secure_then_ride_immediate_state(harness):
    fake, ag, df = harness(price=4212.0)
    guard(ag, fake, df, sgn=1); assert ag.state[1]["secured"] == 10.0 and fake.pos.sl == 4210.0
    fake.price = 4218.0; guard(ag, fake, df, sgn=1)           # +18 >= 15 + 2 air
    assert ag.state[1]["secured"] == 15.0 and fake.pos.sl == 4215.0

def test_restart_reconstructs_protection(harness, tmp_path):
    fake, ag, df = harness(sl=4215.0, price=4216.0)
    guard(ag, fake, df, sgn=1)
    assert ag.state[1]["secured"] == 15.0

def test_wider_manual_sl_not_replaced(harness):
    fake, ag, df = harness(sl=4150.0, price=4201.0)           # manual SL −50: wider than −6, but it exists
    guard(ag, fake, df, sgn=-1)                               # P phase: no strategy rule tightens it
    assert fake.pos.sl == 4150.0

def test_p_timeout_closes(harness):
    fake, ag, df = harness(price=4201.0)
    for i in range(13):                                        # lines not crossed for the long, 13 bars
        df2 = df.copy(); df2["time"] = df2["time"] + i * 300
        guard(ag, fake, df2, sgn=-1)
    assert fake.closed

def test_p_separation_abort(harness):
    fake, ag, df = harness(price=4201.0)
    df2 = df.copy(); df2["ema_fast"] = 4180.0; df2["ema_slow"] = 4190.0
    df2.loc[df2.index[-3:], "ema_fast"] = [4185.0, 4183.0, 4180.0]    # gap widening 3 bars
    ag._guard(True, 0, df2, 4201.0, 4180.0, 4190.0, -1); ag._guard(True, 300, df2, 4201.0, 4180.0, 4190.0, -1)
    assert fake.closed

def test_opposite_p_flips_without_auto_open(harness):
    fake, ag, df = harness(price=4201.0)
    ag.latest_signal = {"bar_time": int(df["time"].iloc[-1]), "symbol": "XAUUSD", "strategy": "ema5080", "kind": "P", "direction": "SHORT", "consumed": False}
    guard(ag, fake, df, sgn=1)
    assert fake.closed and all(r["action"] != FakeMT5.TRADE_ACTION_DEAL or r.get("position") == 1 for r in fake.sends)   # only a close, never an open

def test_cross_moves_pre_to_post(harness):
    fake, ag, df = harness(price=4201.0)
    guard(ag, fake, df, sgn=-1); assert ag.state[1]["pre"]
    guard(ag, fake, df, sgn=1); assert not ag.state[1]["pre"] and fake.pos.sl > 0

def test_mode_saved_in_state(harness):
    fake, ag, df = harness(price=4201.0); guard(ag, fake, df, sgn=1)
    assert ag.state[1]["mode"] == "ema5080"


# ------------------------------------------------------------------ multi-symbol persistence
def test_state_per_symbol_isolated(tmp_path):
    j = Journal(str(tmp_path))
    j.save_state("XAUUSD", {1: {"symbol": "XAUUSD"}}); j.save_state("XAGUSD", {2: {"symbol": "XAGUSD"}})
    j.save_state("XAUUSD", {1: {"symbol": "XAUUSD", "x": 1}})
    assert j.load_state("XAGUSD") == {2: {"symbol": "XAGUSD"}} and j.load_state("XAUUSD")[1]["x"] == 1
