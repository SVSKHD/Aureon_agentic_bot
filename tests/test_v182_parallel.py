"""v1.8.2 — parallel safety: MT5 lock, symbol-aware dedupe, Discord isolation, state concurrency, agent isolation, follow-SL path."""
import os, sys, time, threading, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import broker, telemetry
from aureon_mt5.strategies import get_strategy
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.agent import SymbolAgent
from tests.test_v181 import FakeMT5, guard


# ------------------------------------------------------------------ 1) one shared MT5 lock around every API call
class CountingLock:
    def __init__(self): self.inner = threading.RLock(); self.enters = 0
    def __enter__(self): self.enters += 1; return self.inner.__enter__()
    def __exit__(self, *a): return self.inner.__exit__(*a)

def test_mt5_calls_take_shared_lock(monkeypatch):
    fake = FakeMT5(); lock = CountingLock()
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_lock", lock); monkeypatch.setattr(broker, "_offset_h", 3.0)
    broker.positions("XAUUSD"); broker.set_sl(1, "XAUUSD", 4190.0); broker.tick_age("XAUUSD", 3.0); broker.market_open("XAUUSD", 3.0)
    broker.connected(); broker.closed_deals("XAUUSD", __import__("datetime").datetime(2026, 1, 1), __import__("datetime").datetime(2026, 1, 2))
    assert lock.enters >= 6


# ------------------------------------------------------------------ 2) symbol-aware dedupe
def test_dedupe_keys_are_symbol_aware(tmp_path, monkeypatch):
    cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path)
    n = Notifier(None); j = Journal(str(tmp_path))
    au = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, j, ()); ag = SymbolAgent("XAGUSD", cfg, get_strategy("ema5080"), n, j, ())
    assert au.key("P_LONG", "2026-10-06T14:20") != ag.key("P_LONG", "2026-10-06T14:20")
    assert n.send(au.key("P_LONG", "x"), "a", []) and n.send(ag.key("P_LONG", "x"), "b", [])      # gold never suppresses silver
    assert not n.send(au.key("P_LONG", "x"), "a", [])                                            # but the same event is sent once


# ------------------------------------------------------------------ 3) Discord failure never interrupts guardian work
def test_discord_failure_isolated(tmp_path, monkeypatch):
    fake = FakeMT5(price=4212.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    class BoomNotifier(Notifier):
        def raw(self, body, png=None): raise RuntimeError("discord down")
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    n = BoomNotifier("https://x"); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, j, ())
    import pandas as pd, numpy as np
    df = pd.DataFrame({"time": np.arange(200) * 300, "close": 4212.0, "high": 4212.0, "low": 4212.0})
    guard(ag, fake, df, sgn=1)                                   # no exception propagates
    assert fake.pos.sl == 4210.0 and ag.state[1]["secured"] == 10.0            # MT5 action + state done
    assert any(r["event"] == "secured" for r in j.read(0))                     # journaled before Discord was attempted


# ------------------------------------------------------------------ 4) XAU/XAG state concurrency
def test_state_concurrency(tmp_path):
    j = Journal(str(tmp_path)); errors = []
    def worker(sym, ticket):
        try:
            for i in range(200):
                j.save_state(sym, {ticket: {"symbol": sym, "mode": "ema5080", "i": i}})
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=worker, args=("XAUUSD", 1)), threading.Thread(target=worker, args=("XAGUSD", 2))]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    au, ag = j.load_state("XAUUSD"), j.load_state("XAGUSD")
    assert au[1]["symbol"] == "XAUUSD" and au[1]["mode"] == "ema5080" and au[1]["i"] == 199
    assert ag[2]["symbol"] == "XAGUSD" and ag[2]["mode"] == "ema5080" and ag[2]["i"] == 199
    assert not os.path.exists(j.state_path + ".tmp")                          # atomic replace left no temp file


# ------------------------------------------------------------------ 6) EMA80 follow-SL goes through the safe path
def test_follow_sl_uses_safe_path(tmp_path, monkeypatch):
    fake = FakeMT5(sl=4150.0, price=4201.0); fake.reject = True
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    j = Journal(str(tmp_path)); ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Notifier(None), j, ())
    import pandas as pd, numpy as np
    df = pd.DataFrame({"time": np.arange(200) * 300, "close": 4201.0, "high": 4201.0, "low": 4201.0})
    guard(ag, fake, df, sgn=1)                                   # post-cross, not secured -> follow EMA80 attempted and rejected
    errs = [r for r in j.read(0) if r["event"] == "error"]
    assert errs and any("CROSS SL" in r.get("action", "") or "FOLLOW" in r.get("action", "") for r in errs)
    assert fake.pos.sl == 4150.0                                 # rejected -> unchanged, never loosened, no false success


# ------------------------------------------------------------------ 9) parallel agent isolation + supervisor restarts only the dead one
def test_agent_failure_isolation(tmp_path, monkeypatch):
    cfg = Config(); cfg.dry = True; cfg.source = "synthetic"; cfg.poll_seconds = 1; cfg.log_dir = str(tmp_path)
    n = Notifier(None); j = Journal(str(tmp_path)); S = get_strategy("ema5080")
    real_bars = broker.bars
    def bars(symbol, n_, source, off):
        if symbol == "XAUUSD": raise RuntimeError("MT5 failed for gold")
        return real_bars(symbol, n_, source, off)
    monkeypatch.setattr(broker, "bars", bars)
    au = SymbolAgent("XAUUSD", cfg, S, n, j, ()); ag = SymbolAgent("XAGUSD", cfg, S, n, j, ())
    au.start(); ag.start(); time.sleep(4)
    assert au.is_alive() and au.health["errors"] >= 1 and "error" in au.health["status"]
    assert ag.is_alive() and ag.health["errors"] == 0 and ag.health["status"] == "running"
    au.stop(); ag.stop()

def test_supervisor_restarts_only_dead_agent(tmp_path):
    from main import supervise
    class Dead:
        def is_alive(self): return False
    class Alive:
        def is_alive(self): return True
    agents = {"XAUUSD": Dead(), "XAGUSD": Alive()}; made = []
    class NewAgent:
        def __init__(self, sym): self.sym = sym; made.append(sym)
        def start(self): pass
        def is_alive(self): return True
    restarted = supervise(agents, lambda sym: NewAgent(sym), Notifier(None), "ema5080")
    assert restarted == ["XAUUSD"] and made == ["XAUUSD"] and isinstance(agents["XAGUSD"], Alive)


# ------------------------------------------------------------------ v1.8.3) failed delivery is retried, not suppressed
def test_failed_discord_delivery_retries_then_commits():
    class Flaky(Notifier):
        def __init__(self): super().__init__("https://x"); self.calls = 0; self.fail_first = 2
        def raw(self, body, png=None):
            self.calls += 1
            return self.calls > self.fail_first
    n = Flaky()
    assert n.send("ema5080:XAUUSD:P_LONG:t1", "sig", []) is False      # attempt 1 fails -> not committed
    assert n.send("ema5080:XAUUSD:P_LONG:t1", "sig", []) is False      # attempt 2 fails -> still retryable
    assert n.send("ema5080:XAUUSD:P_LONG:t1", "sig", []) is True       # attempt 3 delivered -> committed
    assert n.send("ema5080:XAUUSD:P_LONG:t1", "sig", []) is False      # now deduped
    assert n.calls == 3

def test_console_mode_counts_as_delivered():
    n = Notifier(None)
    assert n.send("k", "t", []) is True and n.send("k", "t", []) is False

def test_dead_webhook_gives_up_eventually():
    from aureon_mt5 import notify as nm
    class Dead(Notifier):
        def raw(self, body, png=None): return False
    n = Dead("https://x")
    for _ in range(nm.MAX_ATTEMPTS):
        n.send("k", "t", [])
    assert "k" in n.sent                                                 # committed after MAX_ATTEMPTS, so it won't loop forever
