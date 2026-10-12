"""v2.4.4 — scripted rehearsal for the manual/override/agent flow.

Drives the real code against a fake MT5 + console notifier through scenarios.
Prints a pass/fail table.

Usage:
  python scripts/rehearse.py           # fake MT5, all scenarios
  python scripts/rehearse.py --demo    # real demo account, interactive (requires MT5 terminal)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aureon_mt5 import bot, broker, control, halt as halt_mod
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy


class FakeMT5:
    POSITION_TYPE_BUY = 0; TRADE_ACTION_SLTP = 6; TRADE_ACTION_DEAL = 1; TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1; ORDER_TYPE_BUY = 0; ORDER_FILLING_IOC = 1; ORDER_FILLING_FOK = 0
    DEAL_ENTRY_OUT = 1; DEAL_TYPE_SELL = 1; SYMBOL_TRADE_MODE_DISABLED = 0

    def __init__(self, price=4200.0):
        self.price = price; self.bid = price; self.ask = price + 0.3
        self.pos = types.SimpleNamespace(ticket=1, type=0, price_open=4200.0, sl=0, tp=0, volume=0.1, time=0, comment="", magic=0)
        self.closed = True; self.reject = False; self.orders = []; self.sends = []
        self.tick_time = int(time.time() + 3 * 3600); self.trade_mode = 0
    def initialize(self): return True
    def last_error(self): return (0, "")
    def terminal_info(self): return object()
    def symbol_info(self, s): return types.SimpleNamespace(digits=2, trade_mode=4)
    def symbol_info_tick(self, s): return types.SimpleNamespace(bid=self.bid, ask=self.ask, time=self.tick_time)
    def symbol_select(self, s, enable=True): return True
    def positions_get(self, symbol=None, ticket=None): return [] if self.closed else [self.pos]
    def history_deals_get(self, a, b): return []
    def account_info(self): return types.SimpleNamespace(balance=10000.0, equity=10000.0, currency="USD", trade_mode=self.trade_mode, login=12345678)
    def order_send(self, req):
        self.sends.append(req)
        if req["action"] == self.TRADE_ACTION_SLTP:
            if self.reject: return types.SimpleNamespace(retcode=10016, comment="Invalid stops")
            self.pos.sl = req["sl"]; return types.SimpleNamespace(retcode=10009, comment="done")
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.orders.append(req)
            return types.SimpleNamespace(retcode=10009, comment="done", order=99, deal=100, price=req["price"] + 0.1, volume=req["volume"])
        self.closed = True; return types.SimpleNamespace(retcode=10009, comment="done")


class ConsoleCap(Notifier):
    def __init__(self): super().__init__(None); self.log = []
    def raw(self, body, png=None, *, embed=None):
        title = embed["title"] if embed else str(body).splitlines()[0]
        self.log.append(title); print(f"  [CARD] {title}"); return True


def scenario(name, fn, fake, cfg, j, n, agents):
    """Run a scenario, return pass/fail."""
    try:
        result = fn(fake, cfg, j, n, agents)
        status = "PASS" if result else "FAIL"
    except Exception as e:
        status = f"FAIL ({e})"
    print(f"  {'✅' if status == 'PASS' else '❌'} {name}: {status}")
    return status == "PASS"


# ================================================================== scenarios
def sc_missed_override(fake, cfg, j, n, agents):
    """Missed setup → /override places the order."""
    ag = agents["XAUUSD"]
    ag.track_ask("ask:rehearse:1", "LONG", "cross", 4200.0, 1000, 1.0, 4188.0)
    ag.open_asks[0]["posted_t"] = time.time() - 600  # past grace
    ag._check_misses()
    if not ag.open_misses:
        return False
    miss = ag.check_override()
    if miss is None:
        return False
    item = {"meta": {"symbol": "XAUUSD", "side": "LONG", "kind": "cross", "price": 4200.0, "bar": 1000, "mode": "ema2050", "sl": 4188.0, "lots": 1.0}, "key": "ask:rehearse:1"}
    r = bot.execute_entry(cfg, agents, j, item, "LONG", "tester", notify=n, lots=0.5)
    ag.consume_miss("ask:rehearse:1")
    return r.get("placed", False) or "waiting" in r.get("line", "")


def sc_override_refused_drift(fake, cfg, j, n, agents):
    """Override refused for drift > max."""
    ag = agents["XAUUSD"]
    ag.open_misses.append({"card_key": "ask:rehearse:2", "side": "LONG", "kind": "cross", "price": 4210.0,
                            "bar": 1000, "lots": 1.0, "sl": 4198.0, "missed_t": time.time(), "now_price": 4200.0, "drift": -10.0})
    miss = ag.check_override("ask:rehearse:2")
    ag.consume_miss("ask:rehearse:2")
    return miss is None  # should be refused


def sc_agent_window(fake, cfg, j, n, agents):
    """AGENT window: agent takes an ENTER."""
    from aureon_mt5.agent_executor import schedule_agent_entry
    j.save_control("XAUUSD", "AGENT", by="test", until=time.time() + 3600)
    ask = {"card_key": "ask:rehearse:3", "side": "LONG", "kind": "cross", "price": 4200.0, "bar": 1000,
           "lots": 1.0, "sl": 4188.0, "posted_t": time.time(), "decided": None, "placed": False}
    agents["XAUUSD"].open_asks.append(ask)
    cfg.agent_delay_s = 0.1
    t = schedule_agent_entry(cfg, agents, j, n, agents["XAUUSD"], ask, sleep=time.sleep)
    t.join(timeout=5)
    j.save_control("XAUUSD", "MANUAL", by="test")
    return ask.get("decided") == "TAKE"


def sc_halt_mid_trade(fake, cfg, j, n, agents):
    """HALT closes positions."""
    fake.closed = False
    results = halt_mod.close_all_positions(["XAUUSD"])
    halt_mod.clear_halt(cfg.log_dir)
    fake.closed = True
    return len(results) == 1 and results[0]["ok"]


def sc_governor_blocks(fake, cfg, j, n, agents):
    """Governor blocks when max_trades_day=0."""
    old = cfg.max_trades_day; cfg.max_trades_day = 0
    item = {"meta": {"symbol": "XAUUSD", "side": "LONG", "kind": "cross", "price": 4200.0, "bar": 1000, "mode": "ema2050"}, "key": "test"}
    r = bot.execute_entry(cfg, agents, j, item, "LONG", "tester", notify=n)
    cfg.max_trades_day = old
    return "BLOCKED" in r.get("line", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="run against real demo MT5 (interactive)")
    a = ap.parse_args()

    if a.demo:
        print("Demo mode not implemented in this version — use fake mode")
        return

    tmp = tempfile.mkdtemp()
    fake = FakeMT5(); broker._m = fake; broker._offset_h = 3.0
    cfg = Config(); cfg.dry = False; cfg.log_dir = tmp; cfg.execution_enabled = True
    cfg.missed_grace_min = 5; cfg.override_window_min = 10; cfg.override_max_drift = 3.0
    cfg.agent_default_min = 60; cfg._test_skip_session = True
    j = Journal(tmp); n = ConsoleCap()
    S = get_strategy("ema2050")
    ag = SymbolAgent("XAUUSD", cfg, S, n, j, ()); ag.off = 3.0
    agents = {"XAUUSD": ag}

    print("AUREON REHEARSAL · fake MT5\n")
    results = []
    scenarios = [
        ("missed → /override placed", sc_missed_override),
        ("override refused (drift)", sc_override_refused_drift),
        ("AGENT window → agent takes", sc_agent_window),
        ("HALT closes position", sc_halt_mid_trade),
        ("governor blocks", sc_governor_blocks),
    ]
    for name, fn in scenarios:
        # Fresh state
        fake.orders = []; fake.sends = []; fake.closed = True; ag.open_asks = []; ag.open_misses = []
        ok = scenario(name, fn, fake, cfg, j, n, agents)
        results.append((name, ok))

    print(f"\n{'='*50}")
    passed = sum(1 for _, ok in results if ok)
    print(f"  {passed}/{len(results)} scenarios passed")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")


if __name__ == "__main__":
    main()
