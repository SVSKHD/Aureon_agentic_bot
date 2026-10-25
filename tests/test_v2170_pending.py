"""v2.17.0 commit 4 — the ema921 pending-order entry: level / side math both ways, the broker request, TTL expiry, adopt on fill,
cancel on an opposite cross, boot adoption, paper fills, /auto-trade off, /place limit=, the replay's fill rule."""
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import auto_trade as AT, broker, ema921_live as L, manual as M, pending921 as P
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema921 import journeys as J
from aureon_mt5.strategies.ema921.analysis import add_emas
from tests.test_v181 import harness  # noqa: F401
from tests.test_v200_alerts import TickMT5
from tests.test_v2160_ema921 import agent921, cross_res, frame

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------------- math
def test_level_and_side_both_directions():
    assert P.level("SHORT", 4000.0, 1.5) == 4001.5 and P.order_type("SHORT") == "sell limit"     # sell ABOVE the close
    assert P.level("LONG", 4000.0, 1.5) == 3998.5 and P.order_type("LONG") == "buy limit"        # buy BELOW the close
    assert P.stops("SHORT", 4001.5, 30, None) == (4031.5, None) and P.stops("LONG", 3998.5, 30, 60) == (3968.5, 4058.5)
    assert P.expiry(1_000_000 - 1_000_000 % 1800, 1800, 2) == 1_000_000 - 1_000_000 % 1800 + 3 * 1800
    assert P.marketable("SHORT", 4001.5, bid=4001.6, ask=4001.9) and not P.marketable("SHORT", 4001.5, bid=4001.0, ask=4001.3)
    assert P.marketable("LONG", 3998.5, bid=3998.0, ask=3998.4) and not P.marketable("LONG", 3998.5, bid=3999, ask=3999.3)
    assert P.bar_fills("SHORT", 4001.5, high=4001.5, low=3990) and not P.bar_fills("SHORT", 4001.5, high=4001.4, low=3990)
    assert P.bar_fills("LONG", 3998.5, high=4010, low=3998.5) and not P.bar_fills("LONG", 3998.5, high=4010, low=3998.6)
    assert P.saved("SHORT", 4000.0, 4001.5) == 1.5 and P.saved("LONG", 4000.0, 3998.5) == 1.5


def test_parse_limit():
    assert P.parse_limit("1.5", "LONG", 4000.0, 4000.3) == 3998.8 and P.parse_limit("-1.5", "LONG", 4000.0, 4000.3) == 3998.8
    assert P.parse_limit("+2", "SHORT", 4000.0, 4000.3) == 4002.0
    assert P.parse_limit("3990", "LONG", 4000.0, 4000.3) == 3990.0
    with pytest.raises(ValueError, match="buy limit must be below the ask"):
        P.parse_limit("4005", "LONG", 4000.0, 4000.3)
    with pytest.raises(ValueError, match="sell limit must be above the bid"):
        P.parse_limit("3995", "SHORT", 4000.0, 4000.3)
    assert M.parse_place("short 0.5 limit=4010")["limit"] == "4010"


# ----------------------------------------------------------------------------- the broker request
class PendMT5(TickMT5):
    TRADE_ACTION_PENDING = 5; TRADE_ACTION_REMOVE = 8; ORDER_TYPE_BUY_LIMIT = 2; ORDER_TYPE_SELL_LIMIT = 3; ORDER_TIME_SPECIFIED = 2
    ORDER_FILLING_RETURN = 2

    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.pending = []

    def orders_get(self, symbol=None, ticket=None):
        return [o for o in self.pending if ticket is None or o.ticket == ticket]

    def order_send(self, req):
        if req["action"] == self.TRADE_ACTION_PENDING:
            o = types.SimpleNamespace(ticket=700 + len(self.pending), type=req["type"], price_open=req["price"], sl=req.get("sl", 0.0),
                                      tp=req.get("tp", 0.0), volume_current=req["volume"], time_expiration=req.get("expiration", 0),
                                      comment=req["comment"], magic=0, time_setup=0)
            self.pending.append(o); self.orders.append(req)
            return types.SimpleNamespace(retcode=10008, comment="placed", order=o.ticket)
        if req["action"] == self.TRADE_ACTION_REMOVE:
            self.pending = [o for o in self.pending if o.ticket != req["order"]]
            return types.SimpleNamespace(retcode=10009, comment="done")
        return super().order_send(req)


def test_place_pending_request_and_cap(monkeypatch):
    fake = PendMT5(price=4000.0); monkeypatch.setattr(broker, "_m", fake)
    r = broker.place_pending("XAUUSD", "SHORT", 0.5, 4001.5, 4031.5, None, 1_800_000, comment="aureon 921")
    req = fake.orders[-1]
    assert r.ok and r.status == "PENDING_PLACED" and r.ticket == 700 and r.price == 4001.5
    assert (req["action"], req["type"], req["price"], req["sl"], req["type_time"], req["expiration"]) == (5, 3, 4001.5, 4031.5, 2, 1_800_000)
    assert "tp" not in req and req["type_filling"] == 2                                    # RETURN first for a resting order
    assert broker.pending_orders("XAUUSD")[0]["side"] == "SHORT"
    again = broker.place_pending("XAUUSD", "LONG", 0.5, 3990.0, 3960.0, None, 1_800_000)
    assert again.status == "POSITION_CAP" and len(fake.pending) == 1                       # a pending order counts as in position
    assert broker.place_market("XAUUSD", "LONG", 0.5, 3970.0, sleep=lambda s: None).status == "POSITION_CAP"
    assert broker.cancel_order(700, "XAUUSD").ok and broker.pending_orders("XAUUSD") == []
    assert broker.cancel_order(700, "XAUUSD").status == "ORDER_NOT_FOUND"
    r = broker.place_pending("XAUUSD", "LONG", 0.5, 3998.5, 3968.5, 4058.5, 1_800_000)
    assert r.ok and fake.orders[-1]["type"] == 2 and fake.orders[-1]["tp"] == 4058.5
    broker.cancel_order(r.ticket, "XAUUSD")
    send = fake.order_send
    fake.order_send = lambda req: (types.SimpleNamespace(retcode=10022, comment="invalid expiration")
                                   if req.get("type_time") == 2 else send(req))
    r = broker.place_pending("XAUUSD", "LONG", 0.5, 3998.5, 3968.5, None, 1_800_000)
    assert r.ok and fake.orders[-1]["type_time"] == 0 and "expiration" not in fake.orders[-1]   # GTC; Aureon cancels at expiry


def test_paper_pending_counts_for_the_cap(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch, paper=True)
    df = frame(); cross(ag, df, "bull")
    first = dict(ag._state921()["pending"])
    cross(ag, df, "bull")                                                       # the same cross again: capped, not replaced
    assert ag._state921()["pending"] == first
    assert [r for r in ag.journal.read(0) if r["event"] == "entry_blocked_position_cap"]


# ----------------------------------------------------------------------------- the agent
def limit_agent(harness, monkeypatch, *, paper=False, orders=None, positions=None, now=None, **cfg):
    ag, sent, calls = agent921(harness, monkeypatch, entry_921="limit", limit_offset_921=1.5, limit_ttl_bars_921=2, paper=paper, **cfg)
    box = {"orders": orders if orders is not None else [], "positions": positions if positions is not None else [], "cancel": [],
           "pend": []}

    def place_pending(sym, side, lots, level, sl, tp, exp, comment=""):
        box["pend"].append((sym, side, lots, level, sl, tp, exp, comment))
        box["orders"].append({"ticket": 701, "side": side, "price": level, "sl": sl, "tp": tp or 0.0, "volume": lots, "expiration": exp,
                              "comment": comment, "type": P.order_type(side).replace(" ", "_")})
        return broker.BrokerResult(True, "PENDING_PLACED", changed=True, retcode=10008, ticket=701, price=level, volume=lots)

    def cancel(ticket, sym=""):
        box["cancel"].append(ticket); box["orders"] = [o for o in box["orders"] if o["ticket"] != ticket]
        return broker.BrokerResult(True, "CANCELLED", changed=True)

    monkeypatch.setattr(broker, "place_pending", place_pending)
    monkeypatch.setattr(broker, "cancel_order", cancel)
    monkeypatch.setattr(broker, "pending_orders", lambda sym: list(box["orders"]))
    monkeypatch.setattr(broker, "positions", lambda sym: list(box["positions"]))
    monkeypatch.setattr(broker, "tick", lambda sym: None)                     # no tick: the marketable check is skipped
    return ag, sent, calls, box


def cross(ag, df, side="bull"):
    ag._detect_921(cross_res(df, side), df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)


def titles(sent):
    return [t.upper() for _, t, *_ in sent]                                     # agent cards are upper-case (notify.title)


def test_limit_cross_places_a_pending_order_not_a_market_one(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch)
    df = frame(); c = float(df["close"].iloc[-1]); bt = int(df["time"].iloc[-1])
    monkeypatch.setattr(broker, "tick", lambda sym: {"bid": c, "ask": c + 0.3, "time": 0})
    cross(ag, df, "bear")
    assert calls["place"] == [] and len(box["pend"]) == 1
    sym, side, lots, level, sl, tp, exp, comment = box["pend"][0]
    assert (side, level, sl, tp, comment) == ("SHORT", round(c + 1.5, 2), round(c + 1.5 + 30, 2), None, "aureon 921")
    assert exp == bt + 3 * 1800
    row = [r for r in ag.journal.read(0) if r["event"] == "pending_placed"][-1]
    assert (row["ticket"], row["level"], row["expires"]) == (701, level, exp)
    t = [x for x in titles(sent) if "PENDING SHORT" in x][0]
    assert f"XAUUSD · MT5 · PENDING SHORT · SELL LIMIT {level:.2f} · SL {sl:.2f} · TP — · EXPIRES " in t
    assert ag._state921()["pending"]["ticket"] == 701


def test_ttl_expiry_cancels_and_cards(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch)
    df = frame(); cross(ag, df, "bull")
    exp = ag._state921()["pending"]["expires"]
    monkeypatch.setattr(broker, "now_server", lambda off=0: exp - 10)
    ag._pending921_tick(int(df["time"].iloc[-1]), df, False)
    assert box["cancel"] == [] and ag._state921()["pending"]                    # still inside its TTL
    monkeypatch.setattr(broker, "now_server", lambda off=0: exp + 1)
    ag._pending921_tick(int(df["time"].iloc[-1]), df, False)
    assert box["cancel"] == [701] and ag._state921()["pending"] is None
    row = [r for r in ag.journal.read(0) if r["event"] == "pending_expired"][-1]
    assert row["removed_by"] == "aureon" and calls["place"] == []
    assert "XAUUSD · MT5 · PENDING EXPIRED · NO FILL IN 2 BARS" in titles(sent)


def test_broker_expired_it_first(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch)
    df = frame(); cross(ag, df, "bull"); exp = ag._state921()["pending"]["expires"]
    box["orders"] = []; monkeypatch.setattr(broker, "now_server", lambda off=0: exp + 5)
    ag._pending921_tick(int(df["time"].iloc[-1]), df, False)
    assert [r for r in ag.journal.read(0) if r["event"] == "pending_expired"][-1]["removed_by"] == "broker" and box["cancel"] == []


def test_fill_is_adopted_like_a_market_entry(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch)
    df = frame(); c = float(df["close"].iloc[-1]); cross(ag, df, "bull")
    lvl = ag._state921()["pending"]["level"]
    box["orders"] = []
    box["positions"] = [{"ticket": 701, "symbol": "XAUUSD", "direction": "long", "price_open": lvl - 0.05, "time": 0, "sl": lvl - 30,
                         "tp": 0.0, "volume": 0.5, "current": lvl, "comment": "aureon 921", "magic": 0, "points": 0.05}]
    ag.closed_by_aureon.add(701)
    ag._pending921_tick(int(df["time"].iloc[-1]), df, False)
    row = [r for r in ag.journal.read(0) if r["event"] == "pending_filled"][-1]
    assert (row["ticket"], row["fill"], row["saved"]) == (701, lvl - 0.05, round(c - (lvl - 0.05), 2))
    assert ag._state921()["pending"] is None and 701 not in ag.closed_by_aureon
    assert any(f"FILLED LONG · BUY LIMIT {lvl:.2f} · FILL {lvl - 0.05:.2f} · +1.55 VS THE CROSS CLOSE" in t for t in titles(sent))
    p = dict(box["positions"][0]); st = {"peak": 0.0, "secured": 0.0}                # the same guardian takes it from here
    ag._guard_921(p, st, 1, True, int(df["time"].iloc[-1]), df, float(df["close"].iloc[-1]))
    assert st["entry"] == p["price_open"]


def test_opposite_cross_cancels_the_pending_order_first(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch)
    df = frame(); cross(ag, df, "bull")
    assert ag._state921()["pending"]["side"] == "LONG"
    df2 = frame(n=261); monkeypatch.setattr(broker, "now_server", lambda off=0: int(df2["time"].iloc[-1]) + 1800 + 60)
    cross(ag, df2, "bear")
    assert box["cancel"] == [701]
    row = [r for r in ag.journal.read(0) if r["event"] == "pending_cancelled"][-1]
    assert row["reason"] == "opposite cross" and row["side"] == "LONG"
    assert ag._state921()["pending"]["side"] == "SHORT" and [p[1] for p in box["pend"]] == ["LONG", "SHORT"]


def test_boot_adopts_pending_orders_and_never_replaces_them(harness, monkeypatch):
    o = {"ticket": 655, "side": "SHORT", "price": 3010.0, "sl": 3040.0, "tp": 0.0, "volume": 0.5, "expiration": 1_900_000_000,
         "comment": "aureon 921", "type": "sell_limit"}
    ag, sent, calls, box = limit_agent(harness, monkeypatch, orders=[o])
    df = frame()
    ag._pending921_tick(int(df["time"].iloc[-1]), df, True)
    pend = ag._state921()["pending"]
    assert (pend["ticket"], pend["side"], pend["level"], pend["expires"]) == (655, "SHORT", 3010.0, 1_900_000_000)
    assert box["pend"] == [] and calls["place"] == [] and [r for r in ag.journal.read(0) if r["event"] == "pending_adopted"]
    ag._pending921_tick(int(df["time"].iloc[-1]), df, True)
    assert len([r for r in ag.journal.read(0) if r["event"] == "pending_adopted"]) == 1
    cross(ag, df, "bear")                                                       # same side: the cap holds, nothing new placed
    assert box["pend"] == []


def test_paper_pending_fills_on_a_later_bar_and_expires(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch, paper=True)
    df = frame(); bt = int(df["time"].iloc[-1]); c = float(df["close"].iloc[-1])
    cross(ag, df, "bull")
    pend = ag._state921()["pending"]; lvl = pend["level"]
    assert pend["paper"] and box["pend"] == [] and pend["id"] == f"L{bt}"
    ag._pending921_tick(bt, df, True)                                           # the signal bar itself never fills
    assert ag._state921()["pending"]
    nxt = df.iloc[[-1]].copy(); nxt["time"] = bt + 1800; nxt["high"] = c + 3; nxt["low"] = lvl + 0.1; nxt["close"] = c + 1
    d2 = pd.concat([df, nxt], ignore_index=True)
    ag._pending921_tick(bt + 1800, d2, True)
    assert ag._state921()["pending"]                                            # low stayed above the level
    nxt2 = nxt.copy(); nxt2["time"] = bt + 3600; nxt2["low"] = lvl - 0.5
    d3 = pd.concat([d2, nxt2], ignore_index=True)
    ag._pending921_tick(bt + 3600, d3, True)
    paper = ag._state921()["paper"]
    assert ag._state921()["pending"] is None and paper[-1]["entry"] == lvl and paper[-1]["id"] == f"L{bt}"
    assert [r for r in ag.journal.read(0) if r["event"] == "pending_filled"][-1]["paper"] is True
    # a second one that never fills: expires after the TTL
    ag._state921()["paper"] = []
    ag._state921()["pending"] = dict(pend, id="L9", bar=bt + 3600, expires=bt + 3600 + 3 * 1800)
    for k in (2, 3):
        b = nxt2.copy(); b["time"] = bt + k * 1800 + 1800; b["low"] = lvl + 5; b["high"] = lvl + 9
        ag._pending921_tick(int(b["time"].iloc[0]), pd.concat([d3, b], ignore_index=True), True)
    assert ag._state921()["pending"] is None
    assert [r for r in ag.journal.read(0) if r["event"] == "pending_expired"][-1]["paper"] is True


def test_auto_trade_off_cancels_an_automatic_pending_order(harness, monkeypatch, tmp_path):
    ag, sent, calls, box = limit_agent(harness, monkeypatch)
    df = frame(); cross(ag, df, "bull")
    from tests.test_v2170_auto_trade import ADMIN, Note, act
    out, n, _ = act(ag.cfg, "off", agents={"XAUUSD": ag})
    assert box["cancel"] == [701] and "automatic pending order cancelled: XAUUSD" in n.cards[0][1]
    assert [r for r in ag.journal.read(0) if r["event"] == "pending_cancelled"][-1]["reason"] == "auto-trade off"


def test_auto_trade_off_never_places_a_pending_order(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch, auto_trade=False)
    cross(ag, frame(), "bull")
    assert box["pend"] == [] and any("SIGNAL LONG" in t.upper() for t in titles(sent))


def test_place_limit_by_hand_with_auto_trade_off(harness, monkeypatch):
    ag, sent, calls, box = limit_agent(harness, monkeypatch, auto_trade=False)
    monkeypatch.setattr(broker, "tick", lambda sym: {"bid": 4000.0, "ask": 4000.3, "time": 0})
    monkeypatch.setattr(broker, "now_server", lambda off=0: 1_800_000_000 + 600)
    r = M.place(ag.cfg, {"XAUUSD": ag}, ag.journal, ag, "SHORT", lots=0.5, limit_raw="2", governor_ctx={"open_positions": []})
    assert r["ok"] and box["pend"][0][1:6] == ("SHORT", 0.5, 4002.0, 4012.0, 3992.0) and box["pend"][0][7] == "aureon /place"
    assert r["text"].startswith("✅ PENDING SHORT · sell limit 4002.00 · SL 4012.00 · TP 3992.00 · expires")
    assert ag._state921()["pending"]["by"] == "/place"
    r2 = M.place(ag.cfg, {"XAUUSD": ag}, ag.journal, ag, "LONG", lots=0.5, limit_raw="3990", governor_ctx={"open_positions": []})
    assert not r2["ok"] and "position cap" in r2["text"]                       # the pending order counts as in position


# ----------------------------------------------------------------------------- the replay's fill rule
def bars(n=1500, seed=11):
    rng = np.random.default_rng(seed)
    close = 3300 + np.cumsum(np.repeat(rng.normal(0, 1.5, n // 8 + 1), 8)[:n] + rng.normal(0, 2.5, n)); op = np.r_[close[0], close[:-1]]
    t = 1_760_918_400 + np.arange(n) * 1800
    return add_emas(pd.DataFrame({"time": t, "open": op, "high": np.maximum(op, close) + rng.uniform(0.2, 3, n),
                                  "low": np.minimum(op, close) - rng.uniform(0.2, 3, n), "close": close}))


def test_replay_fills_on_the_first_later_bar_that_reaches_the_level():
    df = bars(); r = J.Rules(entry="limit", limit_offset_pts=150, limit_ttl_bars=2, exit_mode="e9", sl_pts=3000, tp_pts=0,
                             lock_at_pts=2000, lock_to_pts=1500)
    res = J.build(df, r)
    js = [j for j in res["journeys"]]
    assert js and res["summary"]["entry"] == "limit"
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    for j in js:
        s = 1 if j.direction == "long" else -1; k, kf = j.cross_index, j.entry_index
        lvl = round(cl[k] - s * 1.5, 6)
        assert 1 <= kf - k <= 2 and j.entry_price == pytest.approx(lvl)            # never the signal bar, within the TTL
        reach = (lambda i: lo[i] <= lvl) if s > 0 else (lambda i: hi[i] >= lvl)
        assert reach(kf) and not any(reach(i) for i in range(k + 1, kf))       # the FIRST later bar that reaches it
    m = J.build(df, J.Rules(entry="market", exit_mode="e9"))
    assert all(j.entry_index == j.cross_index for j in m["journeys"]) and m["summary"]["unfilled"] == 0
    assert res["summary"]["unfilled"] > 0


def test_replay_tool_prints_the_comparison(tmp_path, capsys):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import replay_921 as R
    csv = tmp_path / "xau_m30.csv"; bars()[["time", "open", "high", "low", "close"]].to_csv(csv, index=False)
    out = R.main(["--csv", str(csv), "--no-render", "--set", "entry=limit", "--set", "limit_offset=1.5", "--set", "limit_ttl_bars=2"])
    line = out["compare"]
    assert line.startswith("entry=limit offset 1.5 ttl 2: trades ") and " vs  entry=market: trades " in line and "$/lot" in line
    assert line in capsys.readouterr().out
    assert out["market"]["trades"] == R.stats(R.run(pd.read_csv(csv), {"entry": "market"}))["trades"]
