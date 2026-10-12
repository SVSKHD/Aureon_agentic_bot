"""v2.4.2 — AGENT entry executor."""
import os, sys, time, types, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import bot, broker, control
from aureon_mt5.agent_executor import schedule_agent_entry, maybe_schedule
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5 import halt as halt_mod
from tests.test_v200_alerts import TickMT5
from tests.test_v202_alerts import make, ReqMT5


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.edits = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0]); return True


class FakeAgent:
    def __init__(self, symbol="XAUUSD", cfg=None):
        self.symbol = symbol; self.cfg = cfg or Config()
        self.S = types.SimpleNamespace(display_name="EMA 20/50", name="ema2050")
        self.open_asks = []; self.notify = Cap(); self.journal = None
        self.health = {}; self.chop = None; self.off = 3.0
    def _is_chop(self): return False
    def mark_ask_decided(self, card_key, decision, placed=False):
        for a in self.open_asks:
            if a["card_key"] == card_key:
                a["decided"] = decision; a["placed"] = placed


def _ask(card_key="ask:test:1", side="LONG", kind="cross", price=4000.0, lots=1.0, sl=3988.0):
    return {"card_key": card_key, "side": side, "kind": kind, "price": price, "bar": 1000,
            "lots": lots, "sl": sl, "posted_t": time.time(), "decided": None, "placed": False}


# ================================================================== AGENT places after delay
def test_agent_places_after_delay(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0; r.cfg.agent_delay_s = 0.1
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    j = r.j; n = r.n
    # Set AGENT mode
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    ask = _ask(lots=0.8)
    r.ag.open_asks.append(ask)
    t = schedule_agent_entry(r.cfg, r.agents, j, n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    # Should have placed
    orders = [e for e in j.read(0) if e["event"] == "agent_entry"]
    assert len(orders) >= 1 and orders[0]["actor"] == "agent"
    assert ask.get("decided") == "TAKE"


# ================================================================== MANUAL never places
def test_manual_never_places(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.agent_delay_s = 0.1
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    # MANUAL mode (default)
    ask = _ask()
    r.ag.open_asks.append(ask)
    scheduled = maybe_schedule(r.cfg, r.agents, r.j, r.n, r.ag, ask)
    assert not scheduled  # not in AGENT mode


# ================================================================== human SKIP wins
def test_human_skip_wins(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.agent_delay_s = 0.5
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    r.j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    ask = _ask()
    r.ag.open_asks.append(ask)
    # Human skips immediately
    ask["decided"] = "SKIP"
    t = schedule_agent_entry(r.cfg, r.agents, r.j, r.n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    # Agent should not have placed
    agent_entries = [e for e in r.j.read(0) if e["event"] == "agent_entry"]
    assert len(agent_entries) == 0


# ================================================================== human press wins (double-tap guard)
def test_human_press_wins(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.agent_delay_s = 0.5
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    r.j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    ask = _ask()
    r.ag.open_asks.append(ask)
    # Human takes immediately
    ask["decided"] = "TAKE"; ask["placed"] = True
    t = schedule_agent_entry(r.cfg, r.agents, r.j, r.n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    agent_entries = [e for e in r.j.read(0) if e["event"] == "agent_entry"]
    assert len(agent_entries) == 0


# ================================================================== governor blocks agent too
def test_governor_blocks_agent(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.agent_delay_s = 0.1; r.cfg.max_trades_day = 0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    r.j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    ask = _ask()
    r.ag.open_asks.append(ask)
    t = schedule_agent_entry(r.cfg, r.agents, r.j, r.n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    # Governor should block; agent_entry still journaled but placed=False
    agent_entries = [e for e in r.j.read(0) if e["event"] == "agent_entry"]
    if agent_entries:
        assert agent_entries[0].get("placed") is False


# ================================================================== HALT blocks agent
def test_halt_blocks_agent(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.agent_delay_s = 0.1
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    r.j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    halt_mod.set_halt(str(tmp_path), "tester", "test")
    r.cfg.log_dir = str(tmp_path)
    ask = _ask()
    r.ag.open_asks.append(ask)
    t = schedule_agent_entry(r.cfg, r.agents, r.j, r.n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    agent_entries = [e for e in r.j.read(0) if e["event"] == "agent_entry"]
    if agent_entries:
        assert agent_entries[0].get("placed") is False
    halt_mod.clear_halt(str(tmp_path))


# ================================================================== journal actor=agent
def test_journal_actor_agent(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0; r.cfg.agent_delay_s = 0.1
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    r.j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    ask = _ask()
    r.ag.open_asks.append(ask)
    t = schedule_agent_entry(r.cfg, r.agents, r.j, r.n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    # order_placed should have actor=agent
    orders = [e for e in r.j.read(0) if e["event"] == "order_placed"]
    if orders:
        assert orders[-1].get("actor") == "agent"


# ================================================================== no missed card after agent take
def test_no_missed_after_agent_take(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.agent_delay_s = 0.1; r.cfg.missed_grace_min = 0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    r.j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    ask = _ask()
    ask["posted_t"] = time.time() - 60  # past grace
    r.ag.open_asks.append(ask)
    # Agent takes it
    t = schedule_agent_entry(r.cfg, r.agents, r.j, r.n, r.ag, ask, sleep=time.sleep)
    t.join(timeout=5)
    # Now check misses — should find nothing (placed=True)
    r.ag._check_misses()
    missed = [e for e in r.j.read(0) if e["event"] == "setup_missed"]
    assert len(missed) == 0
