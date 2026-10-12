"""v2.3.14 — spread + slippage in replay and reports."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import bot, broker, compare
from aureon_mt5.config import Config
from tests.test_v202_alerts import make, ReqMT5
from tests.test_v200_ema2050 import OFF
from run import research_summary


# ================================================================== spread recorded in signal journal
def test_spread_in_signal_meta(tmp_path, monkeypatch):
    """Signal meta and ask card include spread_pts from the agent's _spread_pts()."""
    from tests.test_v200_alerts import TickMT5
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.journal import Journal
    from aureon_mt5.notify import Notifier
    from aureon_mt5.strategies import get_strategy
    S = get_strategy("ema2050")
    fake = TickMT5(price=4000.5)
    fake.bid = 4000.2; fake.ask = 4000.5  # spread = 0.3
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S, Notifier(None), j, ()); ag.off = OFF
    assert ag._spread_pts() == pytest.approx(0.3, abs=0.01)


# ================================================================== slippage sign for LONG and SHORT
def test_slippage_long(tmp_path, monkeypatch):
    """LONG: slippage = fill − card_price (positive = worse)."""
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.0, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.0},
            "key": "test:long"}
    bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=1.0)
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o.get("slippage_pts") is not None
    # fill is price + 0.1 (from ReqMT5), card_price is 4000.0
    # slippage_LONG = fill - card_price = 4000.9 - 4000.0 = 0.9 (positive = worse for LONG)
    assert o["slippage_pts"] > 0


def test_slippage_short(tmp_path, monkeypatch):
    """SHORT: slippage = card_price − fill (positive = worse)."""
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 4001.0, "bar": 1,
                     "mode": "ema2050", "suggested_side": "SHORT", "lots": 1.0}}
    r.store.arm("XAUUSD", 4001.0, 4000.0)
    bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "SHORT", "me", notify=r.n, lots=1.0)
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o.get("slippage_pts") is not None and o.get("card_price") is not None


# ================================================================== replay net = gross − spread
def test_replay_net():
    """research_summary with spread > 0 shows gross/cost/net."""
    from collections import namedtuple
    J = namedtuple("J", "result mfe mae exit_reason entry_price exit_price entry_time exit_time direction confirm_index pullback")
    journeys = [J(result=10.0, mfe=12.0, mae=-2.0, exit_reason="ema_fast_turn", entry_price=4000, exit_price=4010,
                  entry_time=1000, exit_time=2000, direction="bull", confirm_index=None, pullback=False),
                J(result=-5.0, mfe=1.0, mae=-5.0, exit_reason="stop", entry_price=4010, exit_price=4005,
                  entry_time=3000, exit_time=4000, direction="bear", confirm_index=None, pullback=False)]
    from datetime import date
    days = [{"day": date(2026, 10, 8), "journeys": journeys, "summary": {"trades": 2, "net": 5.0, "offered": 15.0}}]
    from aureon_mt5.strategies import get_strategy
    S = get_strategy("ema2050")
    text = research_summary(days, S, "XAUUSD", 1.0, 100.0, 3.0, spread=0.3)
    assert "gross +5.0" in text and "cost 0.6" in text and "net +4.4" in text
    assert "(assumed 0.3)" in text


# ================================================================== cards show gross/cost/net
def test_compare_card_costs():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD", "spread_pts": 0.3},
        {"t": 11, "event": "decision", "decision": "TAKE", "symbol": "XAUUSD", "bar": 100},
        {"t": 12, "event": "order_placed", "symbol": "XAUUSD", "bar": 100, "ok": True, "slippage_pts": 0.1, "side": "LONG"},
        {"t": 13, "event": "position_seen", "symbol": "XAUUSD", "bar": 100, "side": "LONG", "open_time": 101},
    ]
    from aureon_mt5.reports import GradedSignal
    graded = [GradedSignal(symbol="XAUUSD", kind="ENTER", side="LONG", bar=100, grade="WIN10", points=8.0, money=None, session="london")]
    rows = compare.build_rows(records, graded, 0, 1000, OFF)
    assert len(rows) == 1 and rows[0].spread_pts == 0.3 and rows[0].slippage_pts == 0.1
    res = compare.compute(records, graded, 0, 1000, OFF)
    c = compare.card(res)
    field_names = [f["name"] for f in c["fields"]]
    assert "Costs (taken trades)" in field_names
    costs_field = next(f for f in c["fields"] if f["name"] == "Costs (taken trades)")
    assert "gross" in costs_field["value"] and "net" in costs_field["value"]


# ================================================================== old journals without cost fields still load
def test_old_journal_no_cost_fields():
    records = [
        {"t": 10, "event": "signal", "mode": "ema5080", "signal_kind": "P", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
    ]
    rows = compare.build_rows(records, [], 0, 1000, OFF)
    assert len(rows) == 1
    assert rows[0].spread_pts is None and rows[0].slippage_pts is None
