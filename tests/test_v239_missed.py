"""v2.3.9 — missed setup detection + /override."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import bot, broker, compare
from aureon_mt5.agent import SymbolAgent, _save_open_asks, _load_open_asks
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from tests.test_v200_alerts import TickMT5, Cap
from tests.test_v202_alerts import make, ReqMT5
from tests.test_v200_ema2050 import OFF

S2050 = get_strategy("ema2050")


def _agent(tmp_path, monkeypatch, price=4000.5, execution=False):
    fake = TickMT5(price=price)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.execution_enabled = execution
    cfg.missed_grace_min = 5; cfg.override_window_min = 10; cfg.override_max_drift = 3.0
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, j, ()); ag.off = OFF
    return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, ag=ag, agents={"XAUUSD": ag})


# ================================================================== miss fires once after grace
def test_miss_fires_after_grace(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    # Track an ask
    r.ag.track_ask("ask:test:1", "LONG", "cross", 4000.0, 1000, 1.0, 3988.0)
    assert len(r.ag.open_asks) == 1
    # Not yet missed (within grace)
    r.ag._check_misses()
    assert len(r.ag.open_misses) == 0
    # Simulate grace expired
    r.ag.open_asks[0]["posted_t"] = time.time() - 6 * 60  # 6 min ago, grace is 5
    r.ag._check_misses()
    assert len(r.ag.open_misses) == 1
    assert any("SETUP MISSED" in t for t in r.n.titles)
    missed_events = [e for e in r.j.read(0) if e["event"] == "setup_missed"]
    assert len(missed_events) == 1 and missed_events[0]["reason"] == "no position"
    # Open asks list is now empty
    assert len(r.ag.open_asks) == 0


# ================================================================== not missed on SKIP
def test_no_miss_on_skip(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    r.ag.track_ask("ask:test:2", "LONG", "cross", 4000.0, 1000, 1.0, 3988.0)
    r.ag.mark_ask_decided("ask:test:2", "SKIP")
    r.ag.open_asks[0]["posted_t"] = time.time() - 6 * 60
    r.ag._check_misses()
    assert len(r.ag.open_misses) == 0


# ================================================================== not missed when order placed
def test_no_miss_when_placed(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    r.ag.track_ask("ask:test:3", "LONG", "cross", 4000.0, 1000, 1.0, 3988.0)
    r.ag.mark_ask_decided("ask:test:3", "TAKE", placed=True)
    r.ag.open_asks[0]["posted_t"] = time.time() - 6 * 60
    r.ag._check_misses()
    assert len(r.ag.open_misses) == 0


# ================================================================== not missed when position already exists
def test_no_miss_with_existing_position(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    r.fake.closed = False  # position exists
    r.ag.track_ask("ask:test:4", "LONG", "cross", 4000.0, 1000, 1.0, 3988.0)
    r.ag.open_asks[0]["posted_t"] = time.time() - 6 * 60
    r.ag._check_misses()
    assert len(r.ag.open_misses) == 0


# ================================================================== TAKE pressed but no ticket → missed with reason
def test_take_pressed_no_ticket_reason(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    r.ag.track_ask("ask:test:5", "LONG", "cross", 4000.0, 1000, 1.0, 3988.0)
    r.ag.mark_ask_decided("ask:test:5", "TAKE", placed=False)  # TAKE pressed, no order placed
    r.ag.open_asks[0]["posted_t"] = time.time() - 6 * 60
    r.ag._check_misses()
    assert len(r.ag.open_misses) == 1
    missed = [e for e in r.j.read(0) if e["event"] == "setup_missed"]
    assert missed[0]["reason"] == "TAKE pressed, no ticket"


# ================================================================== survives restart
def test_open_asks_survive_restart(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    r.ag.track_ask("ask:test:6", "LONG", "cross", 4000.0, 1000, 1.0, 3988.0)
    # "restart": load in a new agent
    loaded = _load_open_asks(str(tmp_path), "XAUUSD")
    assert len(loaded) == 1 and loaded[0]["card_key"] == "ask:test:6"


# ================================================================== override places via execute_entry
def test_override_places_order(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    # Create a miss
    miss = {"card_key": "ask:test:7", "side": "LONG", "kind": "cross", "price": 4000.0, "bar": 1000,
            "lots": 1.0, "sl": 3988.0, "posted_t": time.time() - 600, "decided": None, "placed": False,
            "missed_t": time.time(), "now_price": 4000.5, "drift": 0.5, "reason": "no position"}
    r.ag.open_misses.append(miss)
    # Override: check + execute
    checked = r.ag.check_override()
    assert checked is not None
    item = {"meta": {"symbol": "XAUUSD", "side": "LONG", "kind": "cross", "price": 4000.5,
                     "bar": 1000, "mode": "ema2050", "sl": 3988.0, "lots": 1.0},
            "key": "ask:test:7"}
    res = bot.execute_entry(r.cfg, r.agents, r.j, item, "LONG", "me", notify=r.n, lots=0.5)
    assert res["placed"] is True
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o["lots_source"] == "button"  # from execute_entry with lots


# ================================================================== override refused: drift too large
def test_override_refused_drift(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    # Miss where price drifted against the trade
    miss = {"card_key": "ask:test:8", "side": "LONG", "kind": "cross", "price": 4005.0, "bar": 1000,
            "lots": 1.0, "sl": 3993.0, "posted_t": time.time() - 600, "decided": None, "placed": False,
            "missed_t": time.time(), "now_price": 4000.0, "drift": -5.0, "reason": "no position"}
    r.ag.open_misses.append(miss)
    # Price drifted 5 pts against LONG (bid=4000.5, entry was 4005) → drift_against = 4.5
    checked = r.ag.check_override()
    assert checked is None  # refused: drift > 3.0


# ================================================================== override refused: window expired
def test_override_refused_window(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    miss = {"card_key": "ask:test:9", "side": "LONG", "kind": "cross", "price": 4000.0, "bar": 1000,
            "lots": 1.0, "sl": 3988.0, "posted_t": time.time() - 600, "decided": None, "placed": False,
            "missed_t": time.time() - 11 * 60, "now_price": 4000.5, "drift": 0.5, "reason": "no position"}
    r.ag.open_misses.append(miss)
    checked = r.ag.check_override()
    assert checked is None


# ================================================================== override refused: SL crossed
def test_override_refused_sl_crossed(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch, price=3985.0)  # price below SL for LONG
    miss = {"card_key": "ask:test:10", "side": "LONG", "kind": "cross", "price": 4000.0, "bar": 1000,
            "lots": 1.0, "sl": 3988.0, "posted_t": time.time() - 600, "decided": None, "placed": False,
            "missed_t": time.time(), "now_price": 3985.0, "drift": -15.0, "reason": "no position"}
    r.ag.open_misses.append(miss)
    checked = r.ag.check_override()
    assert checked is None


# ================================================================== IGNORE journals
def test_ignore_journals(tmp_path, monkeypatch):
    r = _agent(tmp_path, monkeypatch)
    miss = {"card_key": "ask:test:11", "side": "LONG", "kind": "cross", "price": 4000.0, "bar": 1000}
    r.ag.open_misses.append(miss)
    r.j.log("override_ignored", symbol="XAUUSD", card_key="ask:test:11", side="LONG", by="me")
    r.ag.consume_miss("ask:test:11")
    assert len(r.ag.open_misses) == 0
    assert any(e["event"] == "override_ignored" for e in r.j.read(0))


# ================================================================== compare groups
def test_compare_missed_groups():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
        {"t": 15, "event": "setup_missed", "symbol": "XAUUSD", "bar": 100, "card_key": "k1"},
        {"t": 20, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "SHORT", "bar": 200, "symbol": "XAUUSD"},
        {"t": 25, "event": "setup_missed", "symbol": "XAUUSD", "bar": 200, "card_key": "k2"},
        {"t": 26, "event": "override", "symbol": "XAUUSD", "bar": 200, "card_key": "k2"},
        {"t": 30, "event": "signal", "mode": "ema2050", "signal_kind": "no-pullback", "side": "LONG", "bar": 300, "symbol": "XAUUSD"},
    ]
    rows = compare.build_rows(records, [], 0, 1000, OFF)
    assert len(rows) == 3
    g = compare.groups(rows)
    assert len(g["missed"]) == 2
    assert len(g["overridden"]) == 1
    assert len(g["missed_not_overridden"]) == 1
    assert g["missed_not_overridden"][0].bar == 100


def test_compare_card_shows_missed_line():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
        {"t": 15, "event": "setup_missed", "symbol": "XAUUSD", "bar": 100, "card_key": "k1"},
    ]
    res = compare.compute(records, [], 0, 1000, OFF)
    c = compare.card(res)
    assert "Missed setups" in [f["name"] for f in c["fields"]]


def test_compare_no_missed_line_without_data():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
    ]
    res = compare.compute(records, [], 0, 1000, OFF)
    c = compare.card(res)
    assert "Missed setups" not in [f["name"] for f in c["fields"]]
