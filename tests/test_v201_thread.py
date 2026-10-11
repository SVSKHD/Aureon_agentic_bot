"""v2.0.1 — SymbolAgent must not shadow threading.Thread._stop(): start(), stop(), join(timeout=2) return cleanly in dry mode."""
import os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy


def test_agent_start_stop_join_cleanly(tmp_path):
    cfg = Config(); cfg.dry = True; cfg.source = "synthetic"; cfg.poll_seconds = 1; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema2050"), Notifier(None), Journal(str(tmp_path)), ())
    assert not isinstance(getattr(ag, "_stop", None), threading.Event)                # Thread._stop (where it exists) is not shadowed
    ag.start(); time.sleep(0.3)
    assert ag.is_alive()
    ag.stop(); ag.join(timeout=2)                                                   # no TypeError: 'Event' object is not callable
    assert not ag.is_alive() and ag._stop_evt.is_set()
