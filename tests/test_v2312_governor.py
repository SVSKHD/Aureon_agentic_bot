"""v2.3.12 — enforced risk governor: each rule allows at edge, blocks one past it."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from datetime import datetime, timedelta, timezone
from aureon_mt5 import bot, broker
from aureon_mt5.governor import Governor, Decision
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from tests.test_v202_alerts import make, ReqMT5

IST = timezone(timedelta(hours=5, minutes=30))


def _cfg(**kw):
    cfg = Config()
    cfg.max_trades_day = kw.get("max_trades_day", 4)
    cfg.daily_loss_limit_pct = kw.get("daily_loss_limit_pct", 2.0)
    cfg.loss_multiple = kw.get("loss_multiple", 2.0)
    cfg.risk_pct = kw.get("risk_pct", 1.0)
    return cfg


def _ctx(**kw):
    return {"trades_today": kw.get("trades_today", 0), "losses_today": kw.get("losses_today", 0),
            "realised_money_today": kw.get("realised_money_today", 0.0), "equity": kw.get("equity", 10000.0),
            "ist_now": kw.get("ist_now", datetime(2026, 10, 13, 10, 0, tzinfo=IST)),
            "news_block": kw.get("news_block", False), "open_positions": kw.get("open_positions", [])}


# ================================================================== session_window
def test_session_window_allows_at_edge():
    gov = Governor(_cfg())
    ctx = _ctx(ist_now=datetime(2026, 10, 13, 5, 30, tzinfo=IST))  # 05:30 = allowed
    assert gov.check("XAUUSD", "LONG", 1.0, ctx).allowed

def test_session_window_blocks_one_past():
    gov = Governor(_cfg())
    ctx = _ctx(ist_now=datetime(2026, 10, 13, 23, 0, tzinfo=IST))  # 23:00 = blocked
    d = gov.check("XAUUSD", "LONG", 1.0, ctx)
    assert not d.allowed and d.rule == "session_window"

def test_session_window_blocks_early_morning():
    gov = Governor(_cfg())
    ctx = _ctx(ist_now=datetime(2026, 10, 13, 3, 0, tzinfo=IST))  # 03:00 = blocked
    assert not gov.check("XAUUSD", "LONG", 1.0, ctx).allowed


# ================================================================== news_block
def test_news_block():
    gov = Governor(_cfg())
    assert gov.check("XAUUSD", "LONG", 1.0, _ctx(news_block=True)).rule == "news_block"
    assert gov.check("XAUUSD", "LONG", 1.0, _ctx(news_block=False)).allowed


# ================================================================== max_trades_day
def test_max_trades_allows_at_edge():
    gov = Governor(_cfg(max_trades_day=4))
    assert gov.check("XAUUSD", "LONG", 1.0, _ctx(trades_today=3)).allowed

def test_max_trades_blocks_one_past():
    gov = Governor(_cfg(max_trades_day=4))
    d = gov.check("XAUUSD", "LONG", 1.0, _ctx(trades_today=4))
    assert not d.allowed and d.rule == "max_trades_day"


# ================================================================== daily_loss_cap
def test_daily_loss_cap_allows_at_edge():
    gov = Governor(_cfg(daily_loss_limit_pct=2.0))
    # equity 10000, cap = 200; realised = -199 → allowed
    assert gov.check("XAUUSD", "LONG", 1.0, _ctx(realised_money_today=-199.0, equity=10000.0)).allowed

def test_daily_loss_cap_blocks_one_past():
    gov = Governor(_cfg(daily_loss_limit_pct=2.0))
    d = gov.check("XAUUSD", "LONG", 1.0, _ctx(realised_money_today=-200.0, equity=10000.0))
    assert not d.allowed and d.rule == "daily_loss_cap"


# ================================================================== loss_multiple (2x rule)
def test_loss_multiple_allows_at_edge():
    gov = Governor(_cfg(loss_multiple=2.0, risk_pct=1.0))
    # per_trade_risk = 10000 * 1% = 100, threshold = 200; realised = -199 → allowed
    assert gov.check("XAUUSD", "LONG", 1.0, _ctx(realised_money_today=-199.0, equity=10000.0)).allowed

def test_loss_multiple_blocks_one_past():
    gov = Governor(_cfg(loss_multiple=2.0, risk_pct=1.0))
    d = gov.check("XAUUSD", "LONG", 1.0, _ctx(realised_money_today=-200.0, equity=10000.0))
    assert not d.allowed and d.rule in ("daily_loss_cap", "loss_multiple")  # both hit at 200


# ================================================================== one_position_per_direction
def test_one_position_allows_opposite():
    gov = Governor(_cfg())
    ctx = _ctx(open_positions=[{"symbol": "XAUUSD", "direction": "long"}])
    assert gov.check("XAUUSD", "SHORT", 1.0, ctx).allowed  # opposite direction OK

def test_one_position_blocks_same():
    gov = Governor(_cfg())
    ctx = _ctx(open_positions=[{"symbol": "XAUUSD", "direction": "long"}])
    d = gov.check("XAUUSD", "LONG", 1.0, ctx)
    assert not d.allowed and d.rule == "one_position_per_direction"


# ================================================================== server-day rollover resets counts
def test_server_day_rollover():
    gov = Governor(_cfg(max_trades_day=4))
    # 4 trades "yesterday" → but today's count resets to 0
    assert gov.check("XAUUSD", "LONG", 1.0, _ctx(trades_today=0)).allowed


# ================================================================== blocked press places nothing + journals governor_block
def test_blocked_press_no_order(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_trades_day = 0  # block everything
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert not res["placed"] and "BLOCKED" in res["line"]
    assert fake.orders == []  # no order sent
    blocks = [e for e in r.j.read(0) if e["event"] == "governor_block"]
    assert len(blocks) == 1 and blocks[0]["rule"] == "max_trades_day"


# ================================================================== /override also blocked
def test_override_blocked(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_trades_day = 0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"symbol": "XAUUSD", "side": "LONG", "kind": "cross", "price": 4000.5, "bar": 1, "mode": "ema2050"},
            "key": "test:override"}
    res = bot.execute_entry(r.cfg, r.agents, r.j, item, "LONG", "me", notify=r.n, lots=0.5)
    assert not res["placed"] and "BLOCKED" in res["line"]


# ================================================================== /governor rules table
def test_governor_rules_table():
    gov = Governor(_cfg())
    ctx = _ctx(trades_today=2, realised_money_today=-50.0)
    rules = gov.rules_table(ctx)
    assert len(rules) == 6
    names = [r["rule"] for r in rules]
    assert "session_window" in names and "max_trades_day" in names and "one_position_per_direction" in names
    # All should be ok in this context
    assert all(r["ok"] for r in rules)


# ================================================================== /status text
def test_governor_status_text():
    gov = Governor(_cfg())
    ctx = _ctx(trades_today=1, realised_money_today=-30.0, equity=10000.0)
    s = gov.status(ctx)
    assert "1/4 trades" in s and "OK" in s
