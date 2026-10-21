"""v2.16.2 fix 3 — one open position per symbol, every placement path; the 15 Oct 15:23 burst (five tickets in seconds)."""
import json
import os
import sys
import time
import types

import pytest

from aureon_mt5 import broker, position_cap
from aureon_mt5.actions import Actions
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from tests.test_v200_alerts import TickMT5

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import order_burst as OB  # noqa: E402


class OpenMT5(TickMT5):
    """Every market order OPENS a position (the way the live account behaves)."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.open = []

    def positions_get(self, symbol=None, ticket=None):
        if ticket is not None:
            return [p for p in self.open if p.ticket == ticket]
        return list(self.open)

    def order_send(self, req):
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            t = 9000 + len(self.open)
            self.open.append(types.SimpleNamespace(ticket=t, type=0 if req["type"] == self.ORDER_TYPE_BUY else 1, price_open=req["price"],
                                                   sl=req.get("sl", 0.0), tp=req.get("tp", 0.0), volume=req["volume"], time=0, comment="",
                                                   magic=0))
            self.orders.append(req)
            return types.SimpleNamespace(retcode=10009, comment="done", order=t, deal=t + 1, price=req["price"], volume=req["volume"])
        return super().order_send(req)


def five_asks(tmp_path, monkeypatch):
    """A fast move: five ema5080 asks open at once (P · CROSS · ENTER · RE …), execution on, a demo account."""
    fake = OpenMT5(price=4000.5); monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.execution_enabled = True; cfg.max_lots = 1.0
    n = Notifier(None); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, j, ())
    for i, kind in enumerate(("P", "CROSS", "ENTER", "RE", "ENTER2")):
        n.ask(f"ema5080:XAUUSD:{kind}_LONG:{i}", f"XAUUSD · {kind} LONG", ["x"],
              meta={"symbol": "XAUUSD", "side": "LONG", "kind": kind, "price": 4000.5, "bar": i, "mode": "ema5080", "lots": 0.1})
    from aureon_mt5 import bot as B
    monkeypatch.setattr(B, "_governor_context", lambda cfg, agents, sym, ist_now=None: {
        "trades_today": 0, "losses_today": 0, "consecutive_losses": 0, "realised_pts_today": 0.0, "realised_money_today": 0.0,
        "equity": 10000.0, "open_positions": [], "news_block": False,
        "ist_now": __import__("datetime").datetime.now(__import__("aureon_mt5.common.timeutil", fromlist=["IST"]).IST).replace(hour=14)})
    return fake, cfg, n, j, ag


def test_root_cause_take_without_a_key_walks_back_through_open_asks(tmp_path, monkeypatch):
    """The mechanism behind 15 Oct: each keyless TAKE takes the newest UNDECIDED ask — a different one every time."""
    fake, cfg, n, j, ag = five_asks(tmp_path, monkeypatch)
    taken = []
    acts = Actions(cfg, {"XAUUSD": ag}, j, n, executor=lambda item, d, user, lots: taken.append(item["key"]) or
                   {"ok": True, "line": "TAKEN", "text": "placed", "placed": True, "ticket": 1})
    for _ in range(5):
        acts.take(user="me")
    assert len(taken) == 5 and len(set(taken)) == 5                    # five different asks → five orders


def test_the_cap_stops_the_burst_on_the_real_path(tmp_path, monkeypatch):
    fake, cfg, n, j, ag = five_asks(tmp_path, monkeypatch)
    acts = Actions(cfg, {"XAUUSD": ag}, j, n)
    res = [acts.take(user="me") for _ in range(5)]
    assert len(fake.orders) == 1 and len(fake.open) == 1                 # ONE position
    assert res[0]["ok"] and all("position cap" in r["text"] for r in res[1:])
    rows = [r for r in j.read(0) if r["event"] == "entry_blocked_position_cap"]
    assert len(rows) == 4 and rows[0]["open"] == [9000] and rows[0]["source"].startswith("execute_entry")


def test_broker_cap_under_the_lock(monkeypatch):
    fake = OpenMT5(price=4000.5); monkeypatch.setattr(broker, "_m", fake)
    logged = []
    broker.SINK["log"] = lambda ev, **kw: logged.append((ev, kw))
    assert broker.place_market("XAUUSD", "LONG", 0.1, 3990.0, sleep=lambda s: None).ok
    r = broker.place_market("XAUUSD", "SHORT", 0.1, 4010.0, sleep=lambda s: None)                 # opposite side too
    assert r.status == "POSITION_CAP" and len(fake.orders) == 1
    assert logged[-1][0] == "entry_blocked_position_cap" and logged[-1][1]["open"] == [9000]


def test_racing_taps_cannot_both_fill(monkeypatch):
    import threading
    fake = OpenMT5(price=4000.5); monkeypatch.setattr(broker, "_m", fake)
    out = []
    ts = [threading.Thread(target=lambda: out.append(broker.place_market("XAUUSD", "LONG", 0.1, 3990.0, sleep=lambda s: None)))
          for _ in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(r.ok for r in out) == 1 and len(fake.open) == 1


def test_place_command_and_auto_are_capped(tmp_path, monkeypatch):
    from aureon_mt5 import manual as M
    fake, cfg, n, j, ag = five_asks(tmp_path, monkeypatch)
    fake.open.append(types.SimpleNamespace(ticket=77, type=0, price_open=4000.0, sl=3990.0, tp=0.0, volume=0.1, time=0, comment="", magic=0))
    r = M.place(cfg, {"XAUUSD": ag}, j, ag, "SHORT", lots=0.1, governor_ctx={"open_positions": []})
    assert not r["ok"] and "position cap" in r["text"] and not fake.orders
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    assert 'position_cap.block(self.symbol, side, journal=self.journal, source="ema921 auto"' in src
    assert [x for x in j.read(0) if x["event"] == "entry_blocked_position_cap" and x["source"] == "/place"]


def test_paper_positions_count_for_the_cap():
    why = position_cap.block("ZZZ", "LONG", paper_positions=[{"id": "P1"}])
    assert why and "#P1" in why


def test_order_burst_tool_names_the_walk_back(tmp_path):
    t0 = float(__import__("datetime").datetime(2026, 10, 15, 15, 23, tzinfo=OB.IST).timestamp())   # 15:23 IST
    rows = [{"t": t0 + i, "event": "order_placed", "by": "koushik", "alert_id": f"ema5080:XAUUSD:{k}_LONG:{i}", "ok": True,
             "ticket": 9000 + i, "side": "LONG"} for i, k in enumerate(("P", "CROSS", "ENTER", "RE", "ENTER2"))]
    rows += [{"t": t0 + i + 0.5, "event": "position_seen", "ticket": 9000 + i} for i in range(5)]
    p = tmp_path / "journal.jsonl"; p.write_text("\n".join(json.dumps(r) for r in rows))
    verdict = OB.main(["--journal", str(p), "--from", "2026-10-15 15:20", "--to", "2026-10-15 15:26"])
    assert verdict[0].startswith("walk-back: 5 orders on 5 different asks by koushik")
