"""v2.16.2 fix 2 — stops validation: clamp SL / TP to the broker's stops / freeze level; 10016 → 5-min cooldown + red card."""
import types

import pytest

from aureon_mt5 import broker
from tests.test_v181 import FakeMT5


class StopsMT5(FakeMT5):
    """stops_level / freeze_level in points (point 0.01); bid = price, ask = price + 0.3."""
    def __init__(self, direction="long", entry=4200.0, sl=0.0, price=4210.0, stops=50, freeze=0, tp=0.0, reject_stops=False):
        super().__init__(direction, entry, sl, price); self.pos.tp = tp
        self.stops, self.freeze, self.reject_stops = stops, freeze, reject_stops

    def symbol_info(self, s):
        return types.SimpleNamespace(digits=2, point=0.01, trade_mode=4, trade_stops_level=self.stops, freeze_level=self.freeze)

    def symbol_select(self, *a):
        return True

    def order_send(self, req):
        if self.reject_stops and req["action"] == self.TRADE_ACTION_SLTP:
            self.sends.append(req); return types.SimpleNamespace(retcode=10016, comment="Invalid stops")
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.sends.append(req)
            return types.SimpleNamespace(retcode=10009, comment="done", order=5, deal=6, price=req["price"], volume=req["volume"])
        return super().order_send(req)


@pytest.fixture
def sink():
    rows, cards = [], []
    broker.SINK.update(log=lambda ev, **kw: rows.append((ev, kw)), alert=lambda t, b: cards.append((t, b)))
    return rows, cards


@pytest.mark.parametrize("is_long,sl,tp,expect", [
    (True, 4209.9, 4210.1, (4209.5, 4210.5)),     # long: SL ≤ bid − 0.5, TP ≥ bid + 0.5
    (True, 4205.0, 4230.0, (4205.0, 4230.0)),     # already far enough: untouched
    (False, 4210.4, 4210.0, (4210.8, 4209.8)),    # short: SL ≥ ask + 0.5, TP ≤ ask − 0.5 (ask 4210.3)
    (False, 4220.0, 4190.0, (4220.0, 4190.0)),
    (True, None, None, (None, None)),
])
def test_clamp_stops(is_long, sl, tp, expect):
    assert broker.clamp_stops(is_long, 4210.0, 4210.3, sl, tp, 0.5) == expect


def test_zero_levels_inclusive_rule_clamps_nothing_at_the_bid():
    assert broker.clamp_stops(True, 4210.0, 4210.3, 4210.0, None, 0.0) == (4210.0, None)


def test_set_sl_long_clamped_and_journaled(monkeypatch, sink):
    rows, _ = sink
    fake = StopsMT5("long", 4200.0, 4190.0, price=4210.0, stops=50); monkeypatch.setattr(broker, "_m", fake)
    r = broker.set_sl(1, "XAUUSD", 4209.8)                                   # lock right under the price
    assert r.ok and fake.pos.sl == 4209.5
    ev, kw = rows[-1]
    assert ev == "sl_clamped" and (kw["requested"], kw["sent"], kw["side"], kw["stops_level"]) == (4209.8, 4209.5, "LONG", 50)


def test_set_sl_short_clamped(monkeypatch, sink):
    rows, _ = sink
    fake = StopsMT5("short", 4220.0, 4230.0, price=4210.0, stops=30, freeze=50); monkeypatch.setattr(broker, "_m", fake)
    r = broker.set_sl(1, "XAUUSD", 4210.5)                                   # freeze 50 > stops 30 → 0.50 above the ask
    assert r.ok and fake.pos.sl == 4210.8 and rows[-1][1]["sent"] == 4210.8


def test_clamped_sl_still_never_loosens(monkeypatch, sink):
    fake = StopsMT5("long", 4200.0, 4209.6, price=4210.0, stops=50); monkeypatch.setattr(broker, "_m", fake)
    r = broker.set_sl(1, "XAUUSD", 4209.9)                                   # clamps to 4209.50 < the live 4209.60
    assert r.status == "ALREADY_BETTER" and fake.pos.sl == 4209.6


def test_tp_sent_with_the_sl_is_clamped_too(monkeypatch, sink):
    fake = StopsMT5("long", 4200.0, 4190.0, price=4210.0, stops=50, tp=4210.2); monkeypatch.setattr(broker, "_m", fake)
    broker.set_sl(1, "XAUUSD", 4205.0)
    assert fake.sends[-1]["tp"] == 4210.5 and fake.sends[-1]["sl"] == 4205.0


def test_10016_cooldown_and_red_card(monkeypatch, sink):
    rows, cards = sink
    fake = StopsMT5("long", 4200.0, 4190.0, price=4210.0, stops=50, reject_stops=True); monkeypatch.setattr(broker, "_m", fake)
    clock = {"t": 1000.0}
    monkeypatch.setattr(broker._time, "monotonic", lambda: clock["t"])
    r = broker.set_sl(1, "XAUUSD", 4205.0)
    assert not r.ok and r.retcode == 10016 and len(fake.sends) == 1
    title, body = cards[-1]
    assert "GUARDIAN FAILURE" in title and "LONG" in body and "bid 4210.0" in body and "ask 4210.3" in body and "stops_level 50" in body
    assert [kw for ev, kw in rows if ev == "guardian_failure"][0]["cooldown_s"] == 300.0
    clock["t"] += 120
    r2 = broker.set_sl(1, "XAUUSD", 4206.0)                                  # within 5 min: no request at all
    assert r2.status == "COOLDOWN" and len(fake.sends) == 1 and len(cards) == 1
    clock["t"] += 181                                                        # 5 min passed: tried again
    fake.reject_stops = False
    assert broker.set_sl(1, "XAUUSD", 4206.0).ok and len(fake.sends) == 2


def test_move_sl_is_quiet_during_the_cooldown(monkeypatch):
    from types import SimpleNamespace
    from aureon_mt5.agent import SymbolAgent
    calls = []
    ag = SimpleNamespace(dry=False, symbol="XAUUSD", health={}, notify=None, journal=None, S=SimpleNamespace(name="ema5080"),
                         state={}, key=lambda *a: "k")
    monkeypatch.setattr(broker, "set_sl", lambda *a, **k: broker.BrokerResult(False, "COOLDOWN"))
    import aureon_mt5.agent as A
    monkeypatch.setattr(A.telemetry, "failure", lambda *a, **k: calls.append(a))
    assert SymbolAgent._move_sl(ag, {"ticket": 1}, 4205.0, "LOCK") is False and not calls


def test_place_market_clamps_sl_and_tp(monkeypatch, sink):
    rows, _ = sink
    fake = StopsMT5("long", price=4210.0, stops=50); fake.closed = True; monkeypatch.setattr(broker, "_m", fake)
    r = broker.place_market("XAUUSD", "SHORT", 0.1, 4210.4, tp=4210.1, sleep=lambda s: None)
    req = fake.sends[-1]
    assert r.ok and req["sl"] == 4210.8 and req["tp"] == 4209.8
    assert rows[-1][0] == "sl_clamped" and rows[-1][1]["ticket"] is None
