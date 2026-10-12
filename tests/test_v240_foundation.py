"""v2.4.0 — agentic foundation: control_mode, journal actor/rationale/p_win, spec invariants."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5 import bot, broker
from tests.test_v202_alerts import make, ReqMT5


# ================================================================== control_mode persists + defaults
def test_control_mode_default_manual(tmp_path):
    j = Journal(str(tmp_path))
    ctrl = j.load_control("XAUUSD")
    assert ctrl["mode"] == "MANUAL"


def test_control_mode_persists(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 3600)
    # "restart"
    j2 = Journal(str(tmp_path))
    ctrl = j2.load_control("XAUUSD")
    assert ctrl["mode"] == "AGENT" and ctrl["by"] == "tester" and ctrl["until"] is not None


def test_control_mode_per_symbol(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester")
    j.save_control("XAGUSD", "MANUAL", by="tester")
    assert j.load_control("XAUUSD")["mode"] == "AGENT"
    assert j.load_control("XAGUSD")["mode"] == "MANUAL"


def test_control_mode_does_not_break_guardian_state(tmp_path):
    """Saving control_mode doesn't corrupt the guardian state section."""
    j = Journal(str(tmp_path))
    j.save_state("XAUUSD", {1: {"secured": 10.0, "peak": 15.0}})
    j.save_control("XAUUSD", "AGENT", by="tester")
    state = j.load_state("XAUUSD")
    assert state[1]["secured"] == 10.0 and state[1]["peak"] == 15.0


# ================================================================== journal.decision() fills actor/rationale/p_win
def test_journal_decision_fields(tmp_path):
    j = Journal(str(tmp_path))
    j.decision("test_event", actor="human", rationale="button press", p_win=0.65, symbol="XAUUSD")
    recs = j.read(0)
    assert len(recs) == 1
    r = recs[0]
    assert r["event"] == "test_event"
    assert r["actor"] == "human"
    assert r["rationale"] == "button press"
    assert r["p_win"] == 0.65
    assert r["symbol"] == "XAUUSD"


def test_journal_decision_defaults(tmp_path):
    j = Journal(str(tmp_path))
    j.decision("test_event")
    r = j.read(0)[0]
    assert r["actor"] == "human" and r["rationale"] == "" and r["p_win"] is None


# ================================================================== every listed decision event has actor/rationale/p_win
DECISION_EVENTS = {"decision", "alert_decision", "order_placed", "governor_block", "halt", "unhalt"}


def test_decision_event_has_actor(tmp_path, monkeypatch):
    """A button press through ask_decision journals actor='human'."""
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.0},
            "key": "test:key"}
    bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=1.0)
    for event_name in ("decision", "order_placed"):
        recs = [e for e in r.j.read(0) if e["event"] == event_name]
        assert len(recs) >= 1, f"no {event_name} event"
        assert recs[-1].get("actor") == "human", f"{event_name} missing actor=human: {recs[-1]}"
        assert "rationale" in recs[-1], f"{event_name} missing rationale"


def test_governor_block_has_actor(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_trades_day = 0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"},
            "key": "test:key"}
    bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    blocks = [e for e in r.j.read(0) if e["event"] == "governor_block"]
    assert len(blocks) >= 1 and blocks[0]["actor"] == "system"


# ================================================================== old-journal compatibility
def test_old_journal_no_actor_loads(tmp_path):
    """Old journal records without actor/rationale/p_win load fine (None/missing)."""
    j = Journal(str(tmp_path))
    j.log("decision", symbol="XAUUSD", side="LONG", decision="TAKE", by="old_user")
    recs = j.read(0)
    assert len(recs) == 1
    r = recs[0]
    # Old record: no actor key — code that reads it should handle missing
    assert r.get("actor") is None  # not present in old records
    assert r["decision"] == "TAKE"


# ================================================================== spec file lists each invariant
def test_spec_invariants_exist():
    spec_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "AGENTIC_SPEC.md")
    assert os.path.exists(spec_path)
    text = open(spec_path, encoding="utf-8").read()
    invariants = ["SL never loosened", "Governor rules", "23:00", "05:30", "News blackout", "HALT",
                  "Demo-only", "Max lots cap"]
    for inv in invariants:
        assert inv in text, f"Invariant '{inv}' not found in AGENTIC_SPEC.md"
    # Each invariant references a file
    for ref in ["agent.py", "governor.py", "halt.py", "bot.py", "voice.py"]:
        assert ref in text, f"File reference '{ref}' not in spec"
