"""v2.4.4 — pytest coverage for every control-mode transition and governor rule rejection."""
import os, sys, time, types, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from datetime import datetime, timedelta, timezone
from aureon_mt5 import bot, broker, control, halt as halt_mod
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.governor import Governor
from tests.test_v202_alerts import make, ReqMT5

IST = timezone(timedelta(hours=5, minutes=30))


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0]); return True


# ================================================================== control-mode transitions
def test_manual_to_agent_and_back(tmp_path):
    j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 30)
    assert j.load_control("XAUUSD")["mode"] == "AGENT"
    control.to_manual(j, n, "XAUUSD", "tester", "manual")
    assert j.load_control("XAUUSD")["mode"] == "MANUAL"
    recs = [r for r in j.read(0) if r["event"] == "control_mode"]
    assert len(recs) == 2


def test_timeout_transition(tmp_path):
    j = Journal(str(tmp_path)); n = Cap()
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() - 1)
    reason = control.check_auto_transitions(j, n, "XAUUSD", {}, Config())
    assert reason == "timeout"


def test_halt_forces_manual(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True
    j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 60)
    halt_mod.set_halt(str(tmp_path), "tester", "test")
    reason = control.check_auto_transitions(j, n, "XAUUSD", {}, cfg)
    assert reason == "HALT"
    halt_mod.clear_halt(str(tmp_path))


def test_disconnect_forces_manual(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True
    j = Journal(str(tmp_path)); n = Cap()
    control.to_agent(j, n, "XAUUSD", "tester", 60)
    with open(os.path.join(str(tmp_path), "watchdog.json"), "w") as f:
        json.dump({"incidents": {"mt5": {"open": True, "open_t": time.time() - 600}}}, f)
    reason = control.check_auto_transitions(j, n, "XAUUSD", {}, cfg)
    assert reason == "MT5 disconnect > 5 min"


def test_restart_preserves_agent(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() + 1800)
    j2 = Journal(str(tmp_path))
    assert control.current_mode(j2, "XAUUSD")["mode"] == "AGENT"


def test_restart_drops_expired(tmp_path):
    j = Journal(str(tmp_path))
    j.save_control("XAUUSD", "AGENT", by="tester", until=time.time() - 100)
    j2 = Journal(str(tmp_path))
    assert control.current_mode(j2, "XAUUSD")["mode"] == "MANUAL"


# ================================================================== governor rule rejections (parametrised)
@pytest.mark.parametrize("rule,ctx_override", [
    ("session_window", {"ist_now": datetime(2026, 10, 13, 23, 30, tzinfo=IST)}),
    ("news_block", {"news_block": True}),
    ("max_trades_day", {"trades_today": 99}),
    ("daily_loss_cap", {"realised_money_today": -9999, "equity": 10000}),
    ("one_position_per_direction", {"open_positions": [{"symbol": "XAUUSD", "direction": "long"}]}),
    ("halted", {"halted": True}),
])
def test_governor_rule_blocks(rule, ctx_override):
    cfg = Config(); cfg.max_trades_day = 4; cfg.daily_loss_limit_pct = 2.0
    gov = Governor(cfg)
    ctx = {"trades_today": 0, "realised_money_today": 0, "equity": 10000,
           "ist_now": datetime(2026, 10, 13, 10, 0, tzinfo=IST),
           "news_block": False, "open_positions": [], "halted": False}
    ctx.update(ctx_override)
    d = gov.check("XAUUSD", "LONG", 1.0, ctx)
    assert not d.allowed and d.rule == rule


@pytest.mark.parametrize("rule,ctx_override", [
    ("session_window", {"ist_now": datetime(2026, 10, 13, 10, 0, tzinfo=IST)}),
    ("max_trades_day", {"trades_today": 3}),
    ("daily_loss_cap", {"realised_money_today": -100, "equity": 10000}),
])
def test_governor_rule_allows_at_edge(rule, ctx_override):
    cfg = Config(); cfg.max_trades_day = 4; cfg.daily_loss_limit_pct = 2.0
    gov = Governor(cfg)
    ctx = {"trades_today": 0, "realised_money_today": 0, "equity": 10000,
           "ist_now": datetime(2026, 10, 13, 10, 0, tzinfo=IST),
           "news_block": False, "open_positions": [], "halted": False}
    ctx.update(ctx_override)
    d = gov.check("XAUUSD", "LONG", 1.0, ctx)
    assert d.allowed


# ================================================================== blocked press journals + no order
def test_governor_block_journals(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_trades_day = 0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake)
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"}, "key": "test"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert "BLOCKED" in res["line"] and not res["placed"]
    blocks = [e for e in r.j.read(0) if e["event"] == "governor_block"]
    assert len(blocks) >= 1


# ================================================================== rehearse.py runs clean
def test_rehearse_script():
    """The rehearse script runs without errors in fake mode."""
    import subprocess
    result = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "rehearse.py")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "scenarios passed" in result.stdout
