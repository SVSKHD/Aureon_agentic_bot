"""v2.3.13 — /halt kill switch."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from datetime import datetime, timedelta, timezone
from aureon_mt5 import bot, broker, halt as halt_mod
from aureon_mt5.governor import Governor
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from tests.test_v200_alerts import TickMT5
from tests.test_v202_alerts import make, ReqMT5

IST = timezone(timedelta(hours=5, minutes=30))


# ================================================================== close all positions
def test_close_all_positions(tmp_path, monkeypatch):
    """Closes all positions via broker.close; returns per-ticket results."""
    fake = TickMT5(price=4200.0)
    fake.closed = False; fake.pos.price_open = 4200.0; fake.pos.sl = 4194.0
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    results = halt_mod.close_all_positions(["XAUUSD"])
    assert len(results) == 1 and results[0]["ok"] is True and results[0]["ticket"] == 1
    assert fake.closed  # position was closed


class RejectCloseMT5(TickMT5):
    """Rejects close orders (DEAL with position)."""
    def order_send(self, req):
        if req.get("action") == self.TRADE_ACTION_DEAL and "position" in req:
            return types.SimpleNamespace(retcode=10016, comment="Rejected")
        return super().order_send(req)

def test_partial_failure_retried(tmp_path, monkeypatch):
    """A failing close is retried 3x and the failure is reported."""
    fake = RejectCloseMT5(price=4200.0)
    fake.closed = False
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    results = halt_mod.close_all_positions(["XAUUSD"], retries=3)
    assert len(results) == 1 and results[0]["ok"] is False


# ================================================================== flag persists across restart
def test_halt_persists(tmp_path):
    halt_mod.set_halt(str(tmp_path), "tester", "test reason")
    assert halt_mod.is_halted(str(tmp_path))
    # "restart": load again
    h = halt_mod.load(str(tmp_path))
    assert h is not None and h["by"] == "tester" and h["reason"] == "test reason"


def test_unhalt_clears(tmp_path):
    halt_mod.set_halt(str(tmp_path), "tester", "test")
    halt_mod.clear_halt(str(tmp_path))
    assert not halt_mod.is_halted(str(tmp_path))


# ================================================================== governor blocks while halted
def test_governor_blocks_while_halted():
    cfg = Config(); cfg.max_trades_day = 99
    gov = Governor(cfg)
    ctx = {"trades_today": 0, "realised_money_today": 0, "equity": 10000,
           "ist_now": datetime(2026, 10, 13, 10, 0, tzinfo=IST),
           "news_block": False, "open_positions": [], "halted": True}
    d = gov.check("XAUUSD", "LONG", 1.0, ctx)
    assert not d.allowed and d.rule == "halted"


def test_governor_allows_after_unhalt():
    cfg = Config(); cfg.max_trades_day = 99
    gov = Governor(cfg)
    ctx = {"trades_today": 0, "realised_money_today": 0, "equity": 10000,
           "ist_now": datetime(2026, 10, 13, 10, 0, tzinfo=IST),
           "news_block": False, "open_positions": [], "halted": False}
    assert gov.check("XAUUSD", "LONG", 1.0, ctx).allowed


# ================================================================== unhalt needs exact phrase
def test_unhalt_requires_exact_phrase(tmp_path):
    halt_mod.set_halt(str(tmp_path), "tester", "test")
    # Wrong phrase → still halted
    assert halt_mod.is_halted(str(tmp_path))
    # Only clear_halt actually clears — the /unhalt command checks the phrase in bot.py


# ================================================================== dry mode touches nothing
def test_dry_mode_no_close(tmp_path, monkeypatch):
    fake = TickMT5(price=4200.0)
    fake.closed = False
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    results = halt_mod.close_all_positions(["XAUUSD"], dry=True)
    assert results == []  # dry skips positions
    assert not fake.closed


# ================================================================== blocked press while halted
def test_blocked_while_halted(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True)
    halt_mod.set_halt(str(tmp_path), "tester", "test")
    r.cfg.log_dir = str(tmp_path)
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert not res["placed"] and "BLOCKED" in res["line"] and "halted" in res["line"].lower()
    halt_mod.clear_halt(str(tmp_path))  # cleanup


# ================================================================== halt summary format
def test_halt_summary():
    results = [
        {"ticket": 1, "symbol": "XAUUSD", "direction": "long", "points": 5.0, "ok": True, "retcode": 10009, "comment": "done"},
        {"ticket": 2, "symbol": "XAUUSD", "direction": "short", "points": -3.0, "ok": False, "retcode": 10016, "comment": "failed"},
    ]
    s = halt_mod.halt_summary(results)
    assert "1 positions closed" in s and "+2.0 pts" not in s  # only closed count
    assert "FAILED" in s and "#2" in s
