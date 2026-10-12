"""v2.3.4 — Confirm card [TAKE] and /take place orders through execute_entry (same path as alert buttons);
attach_alert stores card_key and the ATTACHED edit lands on it; streak half size."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import alerts as A, bot, broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.strategies import get_strategy
from tests.test_v200_alerts import TickMT5
from tests.test_v202_alerts import make, ReqMT5

S2050 = get_strategy("ema2050")
OFF = 3.0


def _item(side="LONG", price=4000.6, lots=None, key="ask:ema2050:XAUUSD:ENTER_LONG:20260711_1200"):
    m = {"kind": "cross", "symbol": "XAUUSD", "price": price, "bar": 1, "mode": "ema2050", "side": side}
    if lots is not None:
        m["lots"] = lots
    return {"meta": m, "key": key}


# ------------------------------------------------------------------ execution off → text contains why, pending_alert has card_key
def test_execution_off_pending_alert_has_card_key(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=False)
    item = _item()
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert "execution off (AUREON_EXECUTION=0)" in res["text"]
    assert res["placed"] is False
    pa = r.ag.pending_alert
    assert pa is not None and pa["card_key"] == item["key"]
    # decision journaled
    d = [x for x in r.j.read(0) if x["event"] == "decision"][-1]
    assert d["decision"] == "TAKE" and d["side"] == "LONG"


# ------------------------------------------------------------------ execution on → order placed with the card's lots, capped by max_lots
def test_execution_on_order_placed_capped_lots(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 0.3
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = _item(lots=2.0)
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert res["ok"] and res["placed"] is True and res["ticket"] == 1234567
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o["lots"] == 0.3  # capped by max_lots
    assert o["ok"] is True


# ------------------------------------------------------------------ broker reject → line starts with "TAKEN LONG · ❌ order rejected"
def test_broker_reject_line(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True)
    fake = ReqMT5(price=4000.5, requotes=0, final_ok=False); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = _item()
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, retry_sleep=0.0)
    assert "TAKEN LONG" in res["line"] and "order rejected" in res["line"]
    assert res["placed"] is False
    assert "retcode" in " ".join(str(v) for v in res.values()) or res["ok"] is False


# ------------------------------------------------------------------ SKIP journals and places nothing
def test_skip_journals_nothing_placed(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True)
    item = _item()
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "SKIP", "me")
    assert res["line"] == "SKIPPED" and res["placed"] is False
    d = [x for x in r.j.read(0) if x["event"] == "decision"][-1]
    assert d["decision"] == "SKIP"
    assert r.fake.orders == []


# ------------------------------------------------------------------ ATTACHED edit lands on Confirm card key after _guard sees position
def test_attached_edit_on_confirm_card_key(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=False)
    item = _item()
    bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    # simulate the position appearing
    edits = []
    r.n.card_edit_hook = lambda key, upd: edits.append((key, upd)) or True
    r.fake.closed = False; r.fake.pos.price_open = 4000.8; r.fake.pos.sl = 3988.8; r.fake.price = 4001.0
    n = 200; df = pd.DataFrame({"time": np.arange(n) * 300, "close": 4001.0, "high": 4001.0, "low": 4001.0, "ema20": 3995.0, "ema50": 3990.0})
    r.ag._guard(True, int(df["time"].iloc[-1]), df, 4001.0, 3995.0, 3990.0, 1)
    assert edits and edits[0][0] == item["key"]  # the Confirm card key, not alert:XAUUSD:...
    assert edits[0][1]["Decision"].startswith("ATTACHED · ticket 1 · LONG @ 4000.80")


# ------------------------------------------------------------------ streak half size
class StreakMT5(TickMT5):
    """TickMT5 with controllable closed deals for streak testing."""
    def __init__(self, deals, *a, **k):
        super().__init__(*a, **k); self._deals = deals
    def history_deals_get(self, a, b): return self._deals


def _losing_deal(ticket, t=None):
    t = t or int(time.time())
    return types.SimpleNamespace(ticket=ticket + 1000, position_id=ticket, symbol="XAUUSD", time=t,
                                 price=4000.0, volume=0.1, profit=-50.0, type=1, entry=1, comment="")


def _winning_deal(ticket, t=None):
    t = t or int(time.time())
    return types.SimpleNamespace(ticket=ticket + 1000, position_id=ticket, symbol="XAUUSD", time=t,
                                 price=4000.0, volume=0.1, profit=100.0, type=1, entry=1, comment="")


def test_streak_half_size_off(tmp_path, monkeypatch):
    """streak_half_after=0 → half size never active."""
    now = int(time.time())
    deals = [_losing_deal(i, now - (6 - i) * 100) for i in range(6)]
    fake = StreakMT5(deals, price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.streak_half_after = 0
    ag = SymbolAgent("XAUUSD", cfg, S2050, __import__("aureon_mt5.notify", fromlist=["Notifier"]).Notifier(None), Journal(str(tmp_path)), ())
    ag.off = OFF; ag.last_bar = 1
    assert ag._streak_n() == 6
    assert ag._half_size() is False


def test_streak_half_size_on(tmp_path, monkeypatch):
    """streak_half_after=3 and 6 losses → half size active; lot field says 'half size'."""
    now = int(time.time())
    deals = [_losing_deal(i, now - (6 - i) * 100) for i in range(6)]
    fake = StreakMT5(deals, price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.streak_half_after = 3
    from aureon_mt5.notify import Notifier
    n = Notifier(None)
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, Journal(str(tmp_path)), ())
    ag.off = OFF; ag.last_bar = 1
    assert ag._streak_n() == 6
    assert ag._half_size() is True
    rl = ag._risk_lots(12.0)
    assert rl["half"] is True
    stk = ag._streak()
    assert stk is not None and "half size" in stk and "applied" in stk


def test_streak_threshold_above_streak(tmp_path, monkeypatch):
    """streak_half_after=10 but only 6 losses → half size NOT active."""
    now = int(time.time())
    deals = [_losing_deal(i, now - (6 - i) * 100) for i in range(6)]
    fake = StreakMT5(deals, price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.streak_half_after = 10
    from aureon_mt5.notify import Notifier
    ag = SymbolAgent("XAUUSD", cfg, S2050, Notifier(None), Journal(str(tmp_path)), ())
    ag.off = OFF; ag.last_bar = 1
    assert ag._streak_n() == 6
    assert ag._half_size() is False


def test_streak_with_winning_deal_breaks(tmp_path, monkeypatch):
    """A winning deal resets the streak count."""
    now = int(time.time())
    deals = [_winning_deal(1, now - 600), _losing_deal(2, now - 500), _losing_deal(3, now - 400)]
    fake = StreakMT5(deals, price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.streak_half_after = 3
    from aureon_mt5.notify import Notifier
    ag = SymbolAgent("XAUUSD", cfg, S2050, Notifier(None), Journal(str(tmp_path)), ())
    ag.off = OFF; ag.last_bar = 1
    assert ag._streak_n() == 2
    assert ag._half_size() is False


# ------------------------------------------------------------------ alert_decision still returns placed key
def test_alert_decision_returns_placed_key(tmp_path, monkeypatch):
    """alert_decision result includes 'placed' key."""
    r = make(tmp_path, monkeypatch, execution=False)
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 3999.9, "bar": 1, "mode": "ema2050", "suggested_side": "LONG"}}
    res = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "LONG", "me")
    assert "placed" in res and res["placed"] is False
    res2 = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "SKIP", "me")
    assert res2["placed"] is False
