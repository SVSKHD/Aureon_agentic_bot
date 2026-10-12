"""v2.4.1 — control-mode state machine."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5 import control, halt as halt_mod


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0]); return True


def _cfg(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True
    cfg.agent_default_min = 60; cfg.max_trades_day = 99; cfg.daily_loss_limit_pct = 99
    return cfg


# ================================================================== transitions
def test_manual_to_agent(tmp_path):
    j = Journal(str(tmp_path)); n = Cap()
    ctrl = control.to_agent(j, n, "XAUUSD", "tester", 30)
    assert ctrl["mode"] == "AGENT"
    assert any("AGENT ON" in t for t in n.titles)
    recs = [r for r in j.read(0) if r["event"] == "control_mode"]
    assert len(recs) == 1 and recs[0]["to"] == "AGENT" and recs[0]["by"] == "tester"


def test_agent_to_manual(tmp_path):
    j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 30)
    n.titles.clear()
    ctrl = control.to_manual(j, n, "XAUUSD", "tester", "/manual command")
    assert ctrl["mode"] == "MANUAL"
    assert any("MANUAL" in t for t in n.titles)


# ================================================================== timeout
def test_timeout_forces_manual(tmp_path):
    j = Journal(str(tmp_path)); n = Cap()
    # Set AGENT with 0 minutes (already expired)
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() - 1)
    ctrl = control.current_mode(j, "XAUUSD")
    assert ctrl["mode"] == "MANUAL" and ctrl.get("expired")


def test_auto_transition_timeout(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() - 1)
    reason = control.check_auto_transitions(j, n, "XAUUSD", {}, cfg)
    assert reason == "timeout"
    assert j.load_control("XAUUSD")["mode"] == "MANUAL"


# ================================================================== HALT forces MANUAL
def test_halt_forces_manual(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 60)
    halt_mod.set_halt(str(tmp_path), "tester", "test halt")
    reason = control.check_auto_transitions(j, n, "XAUUSD", {}, cfg)
    assert reason == "HALT"
    assert j.load_control("XAUUSD")["mode"] == "MANUAL"
    halt_mod.clear_halt(str(tmp_path))


# ================================================================== MT5 disconnect forces MANUAL
def test_disconnect_forces_manual(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 60)
    # Simulate watchdog mt5 incident open > 5 min
    wd_path = os.path.join(str(tmp_path), "watchdog.json")
    with open(wd_path, "w") as f:
        json.dump({"incidents": {"mt5": {"open": True, "open_t": time.time() - 600}}}, f)
    reason = control.check_auto_transitions(j, n, "XAUUSD", {}, cfg)
    assert reason == "MT5 disconnect > 5 min"


# ================================================================== restart restores AGENT with remaining time
def test_restart_restores_agent(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 1800)
    # "restart"
    j2 = Journal(str(tmp_path))
    ctrl = control.current_mode(j2, "XAUUSD")
    assert ctrl["mode"] == "AGENT"


def test_restart_drops_expired(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() - 100)
    j2 = Journal(str(tmp_path))
    ctrl = control.current_mode(j2, "XAUUSD")
    assert ctrl["mode"] == "MANUAL"


# ================================================================== cards and journal once per transition
def test_one_card_per_transition(tmp_path):
    j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 30)
    assert len([t for t in n.titles if "AGENT ON" in t]) == 1
    control.to_manual(j, n, "XAUUSD", "tester", "manual")
    assert len([t for t in n.titles if "MANUAL" in t]) == 1
    recs = [r for r in j.read(0) if r["event"] == "control_mode"]
    assert len(recs) == 2


# ================================================================== mode_label
def test_mode_label_manual(tmp_path):
    j = Journal(str(tmp_path))
    assert "MANUAL" in control.mode_label(j, "XAUUSD")


def test_mode_label_agent(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    label = control.mode_label(j, "XAUUSD")
    assert "AGENT" in label and "left" in label
