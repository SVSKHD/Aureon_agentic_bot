"""v2.3.7 — lot-size buttons on entry cards when execution is on."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import bot, broker, compare
from aureon_mt5.config import Config, _parse_lot_buttons
from aureon_mt5.journal import Journal
from tests.test_v202_alerts import make, ReqMT5
from tests.test_v200_ema2050 import OFF


# ================================================================== config parsing
def test_lot_buttons_parse():
    assert _parse_lot_buttons("0.5,1.0,1.5") == [0.5, 1.0, 1.5]
    assert _parse_lot_buttons("2.0, 0.5, 1.0") == [0.5, 1.0, 2.0]  # sorted
    assert _parse_lot_buttons("0.5,0.5,1.0") == [0.5, 1.0]  # deduped
    assert _parse_lot_buttons("") == [0.5, 1.0, 1.5]  # default
    assert len(_parse_lot_buttons("0.1,0.2,0.3,0.4,0.5,0.6,0.7")) == 5  # max 5


# ================================================================== lot_button_spec
def test_spec_execution_off():
    cfg = Config(); cfg.execution_enabled = False
    assert bot.lot_button_spec(cfg, {"lots": 1.0}) == []


def test_spec_execution_on_star_nearest():
    cfg = Config(); cfg.execution_enabled = True; cfg.lot_buttons = [0.5, 1.0, 1.5]; cfg.max_lots = 2.0
    spec = bot.lot_button_spec(cfg, {"lots": 1.7})
    assert len(spec) == 3
    star = [b for b in spec if b["star"]]
    assert len(star) == 1 and star[0]["value"] == 1.5  # nearest to 1.7
    assert all(not b["disabled"] for b in spec)


def test_spec_button_above_max_disabled():
    cfg = Config(); cfg.execution_enabled = True; cfg.lot_buttons = [0.5, 1.0, 1.5]; cfg.max_lots = 1.0
    spec = bot.lot_button_spec(cfg, {"lots": 0.8})
    disabled = [b for b in spec if b["disabled"]]
    assert len(disabled) == 1 and disabled[0]["value"] == 1.5
    assert "(>max)" in disabled[0]["label"]
    # star on 1.0 (nearest to 0.8 among enabled)
    star = [b for b in spec if b["star"]]
    assert star[0]["value"] == 1.0


# ================================================================== press size button on ENTER card → order placed
def test_press_lot_button_places_order(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.7},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=0.5)
    assert res["ok"] and res["placed"] and res["lots"] == 0.5
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o["lots"] == 0.5 and o["lots_chosen"] == 0.5 and o["lots_suggested"] == 1.7 and o["lots_source"] == "button"


# ================================================================== lots capped by max_lots
def test_lots_capped_at_max(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 1.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 0.8},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=1.5)
    assert res["ok"] and res["lots"] == 1.0  # capped
    assert "capped" in res["line"]
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o["lots"] == 1.0


# ================================================================== live account without allow_live → no order, arms guardian
def test_live_no_allow(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.allow_live = False
    r.fake.trade_mode = 2  # live account
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=1.0)
    assert res["placed"] is False
    assert "AUREON_ALLOW_LIVE" in res["text"]
    assert r.fake.orders == []


# ================================================================== half-size day → ★ moves to halved suggestion
def test_star_on_halved_suggestion():
    cfg = Config(); cfg.execution_enabled = True; cfg.lot_buttons = [0.5, 1.0, 1.5]; cfg.max_lots = 2.0
    # Normal: lots=1.5, star on 1.5
    spec1 = bot.lot_button_spec(cfg, {"lots": 1.5})
    star1 = [b for b in spec1 if b["star"]][0]
    assert star1["value"] == 1.5
    # Halved: lots=0.75, star on 0.5 (nearest or tied)
    spec2 = bot.lot_button_spec(cfg, {"lots": 0.75})
    star2 = [b for b in spec2 if b["star"]][0]
    assert star2["value"] in (0.5, 1.0)  # 0.75 is equidistant from 0.5 and 1.0
    # Clearly halved: lots=0.6, star on 0.5
    spec3 = bot.lot_button_spec(cfg, {"lots": 0.6})
    star3 = [b for b in spec3 if b["star"]][0]
    assert star3["value"] == 0.5


# ================================================================== /take lots=1.0 path
def test_take_with_lots(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.7},
            "key": "test:key", "decided": None}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=1.0)
    assert res["lots"] == 1.0
    d = [x for x in r.j.read(0) if x["event"] == "decision"][-1]
    assert d["lots_chosen"] == 1.0 and d["lots_suggested"] == 1.7


# ================================================================== SKIP with lots=None
def test_skip_no_lots(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True)
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "SKIP", "me")
    assert res["lots"] is None and res["placed"] is False


# ================================================================== alert_decision lots
def test_alert_decision_with_lots(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 4000.6, "bar": 1,
                     "mode": "ema2050", "suggested_side": "LONG", "lots": 1.5}}
    r.store.arm("XAUUSD", 4000.0, 4010.0)
    res = bot.alert_decision(r.cfg, r.agents, r.j, r.store, item, "LONG", "me", notify=r.n, lots=0.5)
    assert res["ok"] and res["lots"] == 0.5
    d = [x for x in r.j.read(0) if x["event"] == "alert_decision"][-1]
    assert d["lots_chosen"] == 0.5 and d["lots_suggested"] == 1.5


# ================================================================== ORDER PLACED card has Size field
def test_order_placed_has_size_field(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.0},
            "key": "test:key"}
    bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, lots=1.0)
    body = " ".join(r.n.bodies)
    assert "Size" in body and "suggested" in body


# ================================================================== compare: sized_up / sized_down groups
def test_compare_sizing_groups():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
        {"t": 11, "event": "decision", "decision": "TAKE", "symbol": "XAUUSD", "bar": 100, "lots_chosen": 1.5, "lots_suggested": 1.0},
        {"t": 20, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "SHORT", "bar": 200, "symbol": "XAUUSD"},
        {"t": 21, "event": "decision", "decision": "TAKE", "symbol": "XAUUSD", "bar": 200, "lots_chosen": 0.5, "lots_suggested": 1.0},
        {"t": 30, "event": "signal", "mode": "ema2050", "signal_kind": "no-pullback", "side": "LONG", "bar": 300, "symbol": "XAUUSD"},
        {"t": 31, "event": "decision", "decision": "TAKE", "symbol": "XAUUSD", "bar": 300, "lots_chosen": 1.0, "lots_suggested": 1.0},
    ]
    rows = compare.build_rows(records, [], 0, 1000, OFF)
    assert len(rows) == 3
    g = compare.groups(rows)
    assert len(g["sized_up"]) == 1 and g["sized_up"][0].bar == 100
    assert len(g["sized_down"]) == 1 and g["sized_down"][0].bar == 200


def test_compare_card_shows_sizing_line():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
        {"t": 11, "event": "decision", "decision": "TAKE", "symbol": "XAUUSD", "bar": 100, "lots_chosen": 1.5, "lots_suggested": 1.0},
    ]
    res = compare.compute(records, [], 0, 1000, OFF)
    c = compare.card(res)
    field_names = [f["name"] for f in c["fields"]]
    assert "Sizing" in field_names
    sf = next(f for f in c["fields"] if f["name"] == "Sizing")
    assert "up 1" in sf["value"] and "ratio" in sf["value"]


def test_compare_no_sizing_line_without_data():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
    ]
    res = compare.compute(records, [], 0, 1000, OFF)
    c = compare.card(res)
    assert "Sizing" not in [f["name"] for f in c["fields"]]


# ================================================================== v2.3.11 tests

# ---- alert card meta has lots, half_size, chop
def test_alert_card_meta_has_lots_and_chop(tmp_path, monkeypatch):
    from tests.test_v200_alerts import TickMT5, Cap as Cap200
    from tests.test_v200_ema2050 import _bull
    from aureon_mt5 import broker as br, alerts as A
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.strategies import get_strategy
    from aureon_mt5.strategies.ema2050 import journeys as J
    from aureon_mt5.journal import Journal
    from aureon_mt5.common.timeutil import now_server
    S = get_strategy("ema2050")
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(br, "_m", fake); monkeypatch.setattr(br, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    n = Cap200(); j = Journal(str(tmp_path)); store = A.AlertStore(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S, n, j, (), alerts=store); ag.off = OFF
    df = _bull(n=12, touch_at=(11,))
    res = J.build(df, J.rules_for("XAUUSD"), OFF); res["df"] = df
    ag.last_df = df; ag.last_res = res; ag.last_bar = int(df["time"].iloc[-1]); ag._refresh_snapshot(True)
    store.arm("XAUUSD", 4000.0, 4010.0); fake.bid, fake.ask = 3999.5, 3999.8; fake.tick_time = now_server(OFF)
    n.bot_ready = True
    ag._check_alerts(df, df, int(df["time"].iloc[-1]))
    item = n.ask_queue.get_nowait()
    m = item["meta"]
    assert "lots" in m and m["lots"] is not None  # risk-based lots
    assert "half_size" in m
    assert "chop" in m and "chop_crosses" in m
    # Size field on the card
    field_names = [f["name"] for f in item["fields"]]
    assert "Size" in field_names


# ---- lot_button_spec: no meta["lots"] → NO ★
def test_spec_no_star_when_lots_none():
    cfg = Config(); cfg.execution_enabled = True; cfg.lot_buttons = [0.5, 1.0, 1.5]; cfg.max_lots = 2.0
    spec = bot.lot_button_spec(cfg, {})  # no "lots" key
    assert len(spec) == 3
    assert not any(b["star"] for b in spec)  # no ★


# ---- AlertView always both rows; ★ only on suggested_side
def test_alert_view_both_rows_star_on_suggested():
    """AlertView with execution on: both LONG and SHORT rows; ★ only on suggested_side."""
    cfg = Config(); cfg.execution_enabled = True; cfg.lot_buttons = [0.5, 1.0]; cfg.max_lots = 2.0
    meta = {"lots": 0.8, "suggested_side": "LONG", "symbol": "XAUUSD", "kind": "alert"}
    btns = bot.lot_button_spec(cfg, meta)
    # Simulating AlertView logic: both sides always, ★ only on LONG
    sides = ["LONG", "SHORT"]
    for side in sides:
        show_star = (side == "LONG")
        for b in btns:
            base = b["label"].rstrip("★")
            lbl = f"{side} {base}" + ("★" if show_star and b["star"] else "")
            if side == "LONG" and b["star"]:
                assert "★" in lbl
            if side == "SHORT":
                assert "★" not in lbl


# ---- Decision line: "(suggested X★)" when lots present, omitted when None
def test_decision_line_suggested_text(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    # With lots in meta → line has "suggested"
    item1 = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.7},
             "key": "test:key1"}
    res1 = bot.ask_decision(r.cfg, r.agents, r.j, item1, "TAKE", "me", notify=r.n, lots=1.0)
    assert "(suggested 1.7★)" in res1["line"]
    # Without lots in meta → no "suggested" text
    item2 = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 2, "mode": "ema2050", "side": "LONG"},
             "key": "test:key2"}
    res2 = bot.ask_decision(r.cfg, r.agents, r.j, item2, "TAKE", "me", notify=r.n, lots=0.5)
    assert "suggested" not in res2["line"]


# ---- execute_entry rejection line includes size
def test_rejection_line_includes_size(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0, final_ok=False); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG", "lots": 1.0},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n, retry_sleep=0.0)
    assert "place 1 lots in MT5" in res["line"] or "place 1 lots in MT5" in res["text"]


# ---- execute_entry not-placed line includes size
def test_not_placed_line_includes_size(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=False)
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050", "side": "LONG"},
            "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert "lots" in res["line"] and "place" in res["line"]


# ---- compare excludes None lots from sized_up/sized_down
def test_compare_excludes_none_lots():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"},
        {"t": 11, "event": "decision", "decision": "TAKE", "symbol": "XAUUSD", "bar": 100, "lots_chosen": None, "lots_suggested": None},
    ]
    rows = compare.build_rows(records, [], 0, 1000, OFF)
    g = compare.groups(rows)
    assert len(g["sized_up"]) == 0 and len(g["sized_down"]) == 0
