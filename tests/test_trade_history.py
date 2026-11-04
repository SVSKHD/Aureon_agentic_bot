"""Trade origins (docs/TRADE_ORIGINS.md): the bot's magic on every order, deal/position classification, the read-only
history collector, and the push bridge's `trades` section."""
from __future__ import annotations

import ast
import json
import os
import threading
from collections import namedtuple
from types import SimpleNamespace

import pytest

from aureon_mt5 import broker
from aureon_mt5 import trade_history as th
from tests.test_push_bridge import _bridge, _setup, ingest  # noqa: F401
from tests.test_v181 import FakeMT5

BOT = 261103
Deal = namedtuple("TradeDeal", "ticket order time time_msc type entry magic position_id reason volume price commission "
                               "swap profit fee symbol comment")
Order = namedtuple("TradeOrder", "ticket sl tp")
Pos = namedtuple("TradePosition", "ticket identifier time type magic reason volume price_open price_current sl tp swap "
                                  "profit symbol comment")
Acct = namedtuple("AccountInfo", "login server trade_mode currency")


# ----------------------------------------------------------------------------- classification
@pytest.mark.parametrize("magic,reason,want", [
    (BOT, 3, "bot"),             # the bot's own order (the API counts as an expert)
    (BOT, 0, "bot"),             # magic wins
    (0, 3, "bot"),               # the bot before AUREON_MAGIC existed: an API order with magic 0
    (0, 0, "manual"),            # desktop terminal
    (0, 1, "manual"),            # mobile
    (0, 2, "manual"),            # web
    (0, 4, "broker"),            # SL hit
    (BOT, 5, "bot"),             # TP of a bot position (magic carries over)
    (0, 5, "broker"),            # TP of a manual position
    (0, 6, "broker"),            # stop-out
    (777, 0, "unknown"),         # another EA's magic placed from a terminal
    (0, 7, "unknown"),           # rollover
    (0, 9, "unknown"),           # split
    (0, None, "unknown"),
    ("x", 0, "unknown"),
])
def test_actor(magic, reason, want):
    assert th.actor(magic, reason, BOT) == want


def _deal(ticket, pid, entry, magic, reason, profit=0.0, t=1_000):
    return {"ticket": ticket, "position_id": pid, "entry": entry, "magic": magic, "reason": reason, "profit": profit,
            "time": t}


def test_position_origin_comes_from_the_opening_deal():
    deals = [_deal(1, 10, "in", 0, 0), _deal(2, 10, "out", 0, 4),            # manual, closed by its SL
             _deal(3, 20, "in", BOT, 3), _deal(4, 20, "out", BOT, 5),        # bot, closed by TP
             _deal(5, 30, "in", 0, 1), _deal(6, 30, "out", BOT, 3),          # manual (mobile), closed by the guardian
             _deal(7, 40, "in", 555, 0)]                                     # foreign EA magic
    th.classify(deals, [], bot_magic=BOT)
    by = {d["ticket"]: d for d in deals}
    assert [by[t]["origin"] for t in (1, 2, 3, 4, 5, 6, 7)] == ["manual", "manual", "bot", "bot", "manual", "manual",
                                                                  "unknown"]
    assert [by[t]["actor"] for t in (2, 4, 6)] == ["broker", "bot", "bot"]
    assert not by[1]["guardian_touched"] and by[5]["guardian_touched"] and by[6]["guardian_touched"]
    assert not by[3]["guardian_touched"]                                     # only manual positions are "managed"


def test_open_positions_use_their_own_magic_and_reason_and_guardian_tickets():
    pos = [{"ticket": 50, "position_id": 50, "magic": 0, "reason": 0},       # manual, the guardian holds state for it
           {"ticket": 51, "position_id": 51, "magic": BOT, "reason": 3},
           {"ticket": 52, "position_id": 52, "magic": 0, "reason": 2}]
    th.classify([], pos, bot_magic=BOT, touched={50, "51", "junk"})
    assert [(p["origin"], p["guardian_touched"]) for p in pos] == [("manual", True), ("bot", False), ("manual", False)]


# ----------------------------------------------------------------------------- the collector
class FakeHist:
    def __init__(self, off_h=2.0):
        o = int(off_h * 3600)
        self.now = 1_800_000_000
        self.deals = [
            Deal(1, 11, self.now - 9 * 86400 + o, 0, 0, 0, 0, 100, 0, 0.1, 2600.0, 0, 0, 0, 0, "XAUUSD", ""),  # too old
            Deal(2, 0, self.now - 3600 + o, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 1000.0, 0, "", "deposit"),          # balance
            Deal(3, 13, self.now - 1800 + o, (self.now - 1800 + o) * 1000 + 7, 0, 0, 0, 300, 1, 0.1, 2650.0, -0.5, 0, 0, 0,
                 "XAUUSD", ""),
            Deal(4, 14, self.now - 600 + o, 0, 1, 1, BOT, 300, 3, 0.1, 2655.0, -0.5, 0, 50.0, 0, "XAUUSD", "aureon"),
            Deal(5, 15, self.now - 300 + o, 0, 1, 0, BOT, 400, 3, 0.2, 2654.0, -1.0, 0, 0, 0, "XAUUSD", "aureon-alert"),
        ]
        self.orders = [Order(13, 2640.0, 0.0), Order(15, 2664.0, 2630.0)]
        self.positions = [Pos(400, 400, self.now - 300 + o, 1, BOT, 3, 0.2, 2654.0, 2653.0, 2664.0, 2630.0, 0, 20.0,
                              "XAUUSD", "aureon-alert")]
        self.calls = []

    def account_info(self):
        self.calls.append("account_info")
        return Acct(12345678, "Broker-Demo", 0, "USD")

    def history_deals_get(self, a, b):
        self.calls.append(("deals", a, b))
        return tuple(self.deals)

    def history_orders_get(self, a, b):
        self.calls.append("orders")
        return tuple(self.orders)

    def positions_get(self, **kw):
        self.calls.append("positions")
        return tuple(self.positions)


def test_collector_window_conversion_and_shape(monkeypatch):
    monkeypatch.delenv("AUREON_MAGIC", raising=False)
    fake = FakeHist(off_h=2.0)
    c = th.Collector(clock=lambda: fake.now, mt5=fake, lock=threading.RLock())
    out = c.collect(off_h=2.0)
    assert out["window"]["kind"] == "backfill" and out["window"]["from"] == fake.now - 7 * 86400
    assert [d["ticket"] for d in out["deals"]] == [3, 4, 5]                  # old + balance deals dropped
    d3, d4, d5 = out["deals"]
    assert d3["time"] == fake.now - 1800 and d3["time_msc"] == (fake.now - 1800) * 1000 + 7   # server time → UTC
    assert d3["sl"] == 2640.0 and d3["origin"] == "manual" and d3["side"] == "buy" and d3["entry"] == "in"
    assert d4["actor"] == "bot" and d4["origin"] == "manual" and d4["guardian_touched"]   # the bot closed my trade
    assert d5["origin"] == "bot" and d5["reason_name"] == "expert"
    (p,) = out["positions"]
    assert p["origin"] == "bot" and p["side"] == "sell" and p["initial_sl"] == 2664.0 and p["time"] == fake.now - 300
    assert out["acct"] == th.acct_key("Broker-Demo", 12345678) and len(out["acct"]) == 16
    assert "12345678" not in json.dumps(out)                                 # never the login itself
    assert out["trade_mode"] == "demo" and out["magic"] == BOT and out["window"]["deal_count"] == 3


def test_incremental_after_commit_and_backfill_every_day():
    fake = FakeHist(off_h=0.0)
    now = [fake.now]
    c = th.Collector(clock=lambda: now[0], mt5=fake, lock=threading.RLock())
    c.collect(off_h=0.0)
    assert c.deal_t is None                                                  # nothing moves before the server acks
    c.commit()
    assert c.deal_t == fake.now - 300 and c.backfill_at == fake.now
    now[0] += 60
    out = c.collect(off_h=0.0)
    assert out["window"]["kind"] == "incremental" and out["window"]["from"] == fake.now - 300 - 3600
    assert [d["ticket"] for d in out["deals"]] == [3, 4, 5]                  # the overlap re-sends; the server dedupes
    c.commit()
    now[0] = fake.now + 86400
    assert c.collect(off_h=0.0)["window"]["kind"] == "backfill"
    st = c.state()
    c2 = th.Collector(clock=lambda: now[0], mt5=fake, lock=threading.RLock())
    c2.restore(st)
    assert c2.deal_t == c.deal_t and c2.backfill_at == c.backfill_at


def test_no_mt5_no_section():
    assert th.Collector(mt5=False, lock=threading.RLock()).collect(off_h=0) is None


def test_collector_only_reads():
    tree = ast.parse(open("aureon_mt5/trade_history.py", encoding="utf-8").read())
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    mt5_calls = {c for c in called if c.endswith("_get") or c in ("account_info", "order_send", "order_check")}
    assert mt5_calls == {"history_deals_get", "history_orders_get", "positions_get", "account_info"}, mt5_calls
    src = open("aureon_mt5/trade_history.py", encoding="utf-8").read()
    for word in ("order_send", "order_check", "TRADE_ACTION", "place_", "set_sl", "modify", "close("):
        assert word not in src, word


# ----------------------------------------------------------------------------- the bot's magic on its orders
def test_bot_magic_env(monkeypatch):
    monkeypatch.delenv("AUREON_MAGIC", raising=False)
    assert broker.bot_magic() == 261103
    monkeypatch.setenv("AUREON_MAGIC", "424242")
    assert broker.bot_magic() == 424242
    for bad in ("0", "-5", "abc"):
        monkeypatch.setenv("AUREON_MAGIC", bad)
        assert broker.bot_magic() == 261103


def test_every_order_the_bot_sends_carries_the_magic(monkeypatch):
    monkeypatch.delenv("AUREON_MAGIC", raising=False)
    fake = FakeMT5("long", 3300.0, 3270.0, 3320.0)
    sent = []
    orig = fake.order_send
    fake.order_send = lambda req: sent.append(dict(req)) or orig(req)
    monkeypatch.setattr(broker, "_m", fake)
    broker.close(1, "XAUUSD")
    broker.close_partial(1, "XAUUSD", 0.5)
    deals = [r for r in sent if r.get("action") == FakeMT5.TRADE_ACTION_DEAL]
    assert deals and all(r["magic"] == BOT for r in deals), sent
    src = open("aureon_mt5/broker.py", encoding="utf-8").read()
    assert src.count('"magic": bot_magic()') == 4                            # market, pending, close, partial close


# ----------------------------------------------------------------------------- the push bridge
class StubTrades:
    def __init__(self):
        self.seen, self.commits, self.restored = [], 0, None

    def restore(self, cur):
        self.restored = cur

    def state(self):
        return {"deal_t": 123, "backfill_at": 4.0}

    def commit(self):
        self.commits += 1

    def collect(self, *, off_h, touched):
        self.seen.append((off_h, set(touched)))
        return {"acct": "abc", "window": {"kind": "backfill"}, "deals": [], "positions": []}


def test_bridge_sends_trades_and_commits_only_on_2xx(tmp_path, ingest):  # noqa: F811
    cfg, env, logs = _setup(tmp_path, ingest.url)
    b = _bridge(cfg, env, logs)
    b.trades = StubTrades()
    ingest.code = 500
    assert b.tick() is False and b.trades.commits == 0
    assert ingest.bodies[0]["trades"]["acct"] == "abc"
    off_h, touched = b.trades.seen[0]
    assert off_h == 2.0 and 101 in touched                                    # the guardian's ticket (journal + state)
    ingest.code = 202
    assert b.tick() is True and b.trades.commits == 1
    assert json.load(open(logs / "push_cursor.json"))["trades"] == {"deal_t": 123, "backfill_at": 4.0}


def test_bridge_restores_the_trades_cursor(tmp_path, ingest):  # noqa: F811
    cfg, env, logs = _setup(tmp_path, ingest.url)
    with open(logs / "push_cursor.json", "w") as f:
        json.dump({"journal_offset": 0, "tb_t": 0, "trades": {"deal_t": 9, "backfill_at": 1.0}}, f)
    from aureon_mt5 import push_bridge as pb
    st = pb.Settings(env, log_dir=str(logs))
    stub = StubTrades()
    pb.PushBridge(st, lambda: {}, trades=stub)
    assert stub.restored == {"deal_t": 9, "backfill_at": 1.0}


def test_bridge_without_trades_is_unchanged(tmp_path, ingest):  # noqa: F811
    cfg, env, logs = _setup(tmp_path, ingest.url)
    b = _bridge(cfg, env, logs)
    assert b.tick() is True and "trades" not in ingest.bodies[0]
    assert "trades" not in json.load(open(logs / "push_cursor.json"))


def test_main_wires_the_collector():
    src = open("main.py", encoding="utf-8").read()
    assert "trades=_th.Collector()" in src
    assert os.path.exists("docs/TRADE_ORIGINS.md")
