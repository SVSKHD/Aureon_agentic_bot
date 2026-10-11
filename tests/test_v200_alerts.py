"""v2.0.0 — /alert price alerts: arm/list/cancel, persistence across restart, fires once on a cross from the right side, no fire on a
stale tick, card contains EMA + trend + suggestion fields, suggestion == the mode's own entry verdict for the same bar (property test
against detect.py rows), buttons journal correctly, no order placed when execution is disabled, reports/compare alerts section."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import alerts as A, broker, bot, compare, reports
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from aureon_mt5.common.verdict import entry_verdict
from tests.test_v181 import FakeMT5
from tests.test_v200_ema2050 import _bull, frame, OFF, DAY0

S2050 = get_strategy("ema2050"); S5080 = get_strategy("ema5080")


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []; self.asks = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body.splitlines()[0]); self.bodies.append(str(embed or body)); return True
    def ask(self, key, title, lines, **kw):
        self.asks.append({"key": key, "title": title, "lines": lines, **kw}); return super().ask(key, title, lines, **kw)


class TickMT5(FakeMT5):
    """FakeMT5 + a controllable tick (bid/ask/time), account info and order recording."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.bid = self.price; self.ask = self.price + 0.3; self.tick_time = int(time.time() + 3 * 3600)
        self.trade_mode = 0; self.orders = []; self.closed = True            # no position unless a test opens one
    def symbol_info_tick(self, s): return types.SimpleNamespace(bid=self.bid, ask=self.ask, time=self.tick_time)
    def symbol_select(self, s, enable=True): return True
    def account_info(self): return types.SimpleNamespace(balance=10000.0, equity=10000.0, currency="USD", trade_mode=self.trade_mode, login=12345678)
    def order_send(self, req):
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.orders.append(req); return types.SimpleNamespace(retcode=10009, comment="done", order=5, deal=6)
        return super().order_send(req)


# ================================================================== store
def test_arm_list_cancel_clear(tmp_path):
    st = A.AlertStore(str(tmp_path))
    a = st.arm("xauusd", 4120.0, 4132.4, "cpi level", "me", bar_time=1000)
    assert a["id"] == "A1" and a["symbol"] == "XAUUSD" and a["side_hint"] == "from above" and a["status"] == "armed"
    b = st.arm("XAUUSD", 4150.0, 4132.4, "", "me")
    assert b["side_hint"] == "from below"
    assert A.armed_reply(a, 4132.4) == "ALERT armed · XAUUSD 4120.00 · current 4132.40 · 12.4 pts away · A1 (from above) · cpi level"
    assert [x["id"] for x in st.armed("XAUUSD")] == ["A1", "A2"] and "A1" in A.list_reply(st, "XAUUSD", 4132.4)
    assert st.cancel("A1")["status"] == "cancelled" and st.cancel("A1") is None and st.cancel("A9") is None
    st.arm("XAGUSD", 50.0, 49.0)
    assert st.clear("XAUUSD") == 1 and st.armed("XAUUSD") == [] and len(st.armed("XAGUSD")) == 1


def test_persistence_across_restart(tmp_path):
    st = A.AlertStore(str(tmp_path)); st.arm("XAUUSD", 4120.0, 4132.4, "n", "me"); st.arm("XAUUSD", 4100.0, 4132.4)
    st.check("XAUUSD", bid=4119.9, ask=4120.2)                               # A1 fires
    st2 = A.AlertStore(str(tmp_path))                                        # "restart"
    assert st2.get("A1")["status"] == "fired" and st2.get("A2")["status"] == "armed" and st2.arm("XAUUSD", 4000.0, 4132.4)["id"] == "A3"
    assert os.path.exists(os.path.join(str(tmp_path), "alerts.json"))


def test_fires_once_from_the_right_side(tmp_path):
    st = A.AlertStore(str(tmp_path))
    st.arm("XAUUSD", 4120.0, 4132.4)                                         # from above: fires on the bid
    assert st.check("XAUUSD", bid=4125.0, ask=4125.3) == []
    assert st.check("XAUUSD", bid=4120.5, ask=4119.8) == []                  # ask through, bid not: no fire
    fired = st.check("XAUUSD", bid=4119.9, ask=4120.2)
    assert len(fired) == 1 and fired[0]["hit_price"] == 4119.9 and st.get("A1")["status"] == "fired"
    assert st.check("XAUUSD", bid=4110.0, ask=4110.3) == []                  # never twice
    st.arm("XAUUSD", 4140.0, 4132.4)                                         # from below: fires on the ask
    assert st.check("XAUUSD", bid=4139.9, ask=4140.2)[0]["id"] == "A2"


# ================================================================== agent: poll, stale tick, card
@pytest.fixture
def rig(tmp_path, monkeypatch):
    def make(strategy=S2050, price=4000.5, dry=False):
        fake = TickMT5(price=price)
        monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
        cfg = Config(); cfg.dry = dry; cfg.log_dir = str(tmp_path); cfg.execution_enabled = False
        n = Cap(); j = Journal(str(tmp_path)); store = A.AlertStore(str(tmp_path))
        ag = SymbolAgent("XAUUSD", cfg, strategy, n, j, (), alerts=store); ag.off = OFF
        return types.SimpleNamespace(fake=fake, cfg=cfg, n=n, j=j, store=store, ag=ag)
    return make


def prepare(ag, df):
    res = J.build(df, J.rules_for("XAUUSD"), OFF) if ag.S.name == "ema2050" else ag.S.analyse({"M5": __import__("aureon_mt5.common.source", fromlist=["Bars"]).Bars("XAUUSD", "M5", df)}, "XAUUSD", OFF, ())["M5"]
    res["df"] = df; ag.last_df = df; ag.last_res = res; ag.last_bar = int(df["time"].iloc[-1])
    ag._refresh_snapshot(True)
    return res


def test_no_fire_on_stale_tick_then_fires_when_fresh(rig):
    r = rig(); df = _bull(touch_at=(11,)); prepare(r.ag, df)
    r.store.arm("XAUUSD", 4000.0, 4010.0)                                    # from above
    r.fake.bid, r.fake.ask = 3999.5, 3999.8
    r.fake.tick_time = broker.now_server(OFF) - (r.cfg.heartbeat_stale_min + 1) * 60     # stale
    r.ag._check_alerts(df, df, int(df["time"].iloc[-1]))
    assert r.store.get("A1")["status"] == "armed" and not r.n.asks
    r.fake.tick_time = broker.now_server(OFF) - 5                            # fresh again
    r.ag._check_alerts(df, df, int(df["time"].iloc[-1]))
    assert r.store.get("A1")["status"] == "fired" and len(r.n.asks) == 1
    r.ag._check_alerts(df, df, int(df["time"].iloc[-1]))
    assert len(r.n.asks) == 1                                                # once
    assert any(x["event"] == "alert_fired" and x["alert_id"] == "A1" for x in r.j.read(0))


def test_card_contains_ema_trend_guardian_and_suggestion(rig):
    r = rig(); df = _bull(n=12, touch_at=(11,)); prepare(r.ag, df)          # last bar = the ENTER LONG bar
    r.store.arm("XAUUSD", 4000.0, 4010.0, "cpi")
    r.fake.bid, r.fake.ask = 3999.5, 3999.8; r.fake.tick_time = broker.now_server(OFF)
    r.ag._check_alerts(df, df, int(df["time"].iloc[-1]))
    ask = r.n.asks[0]
    assert ask["key"] == "alert:XAUUSD:A1" and ask["title"].startswith("🔔 ALERT REACHED · XAUUSD 4000.00 · cpi")
    vals = {f["name"]: f["value"] for f in ask["fields"]}
    assert [f["name"] for f in ask["fields"]] == ["PRICE", "EMA", "TREND", "AGENT VERDICT", "CLAUDE VERDICT"]
    assert "hit **3999.50**" in vals["PRICE"] and "approach from above" in vals["PRICE"] and "pts from EMA20" in vals["PRICE"] and "pts from EMA50" in vals["PRICE"]
    assert "EMA 20 / 50:" in vals["EMA"] and "gap" in vals["EMA"] and "slope" in vals["EMA"] and "last cross: LONG" in vals["EMA"] and "confirmed yes" in vals["EMA"]
    assert "crosses today" in vals["EMA"] and "ATR20" in vals["EMA"] and "BULLISH" in vals["TREND"] and "pre-cross shoot" in vals["TREND"]
    line = ask["lines"][0]
    assert line.startswith("**AGENT: LONG — confirmed cross") and "pullback touch now" in line and "pts from EMA20, window open" in line
    assert "SL would be 3987.5 (−12) · early lock +3 → +1 · secure +10" in vals["AGENT VERDICT"] and "position: flat" in vals["AGENT VERDICT"]
    assert vals["CLAUDE VERDICT"] == "— not attached (mode=off)"
    m = ask["meta"]
    assert m["kind"] == "alert" and m["suggested_side"] == "LONG" and abs(m["sl"] - (3999.5 - 12.0)) < 1e-9 and m["stop_pts"] == 12.0
    assert m["agent_verdict"].startswith("AGENT: LONG") and m["claude_state"] == "off"


def test_suggestion_equals_detect_verdict_for_every_bar(rig):
    """Property: for every bar of a day, the SUGGEST decision/reason is the mode's own verdict detect.py prints for that bar."""
    import detect
    r = rig(); df = _bull(n=60, touch_at=(9, 12), close_at={9: 4006.0})
    for i in range(6, len(df)):
        d = df.iloc[: i + 1].reset_index(drop=True); res = prepare(r.ag, d)
        row = detect.bar_row(S2050, res, i, OFF)
        sug = A.suggestion(r.ag, d, res, int(d["time"].iloc[-1]), {"position": None, "window_open": True, "news_block": False}, 4000.0)
        v = entry_verdict(S2050, res, i, OFF)
        assert sug["decision"] == v["decision"] and f"{v['decision']}: {v['reason']}" in row
        if v["decision"] == "WAIT":
            assert sug["reason"] == v["reason"]
        if "too far" in v["reason"]:
            assert sug["reason"].endswith("(chase)") and v["reason"].split("(")[1].split()[0] in sug["reason"]


def test_suggestion_for_ema5080_reads_its_own_events(rig):
    r = rig(strategy=S5080)
    from tests.test_v187_trend_reentry import downtrend_with_pullback
    df = downtrend_with_pullback(); df["time"] = DAY0 + 10 * 3600 + np.arange(len(df)) * 300
    res = prepare(r.ag, df)
    sug = A.suggestion(r.ag, df, res, int(df["time"].iloc[-1]), {"position": None, "window_open": True, "news_block": False}, float(df["close"].iloc[-1]))
    v = entry_verdict(S5080, res, len(df) - 1, OFF)
    assert sug["decision"] == v["decision"] and sug["decision"] in ("LONG", "SHORT", "WAIT", "NO TRADE")
    if sug["side"]:
        assert abs(abs(sug["sl"] - float(df["close"].iloc[-1])) - 6.0) < 1e-9                  # ema5080 guardian stop −6


def test_suggestion_blocked_by_hour_news_or_position(rig):
    r = rig(); df = _bull(n=12, touch_at=(11,)); res = prepare(r.ag, df); t = int(df["time"].iloc[-1])
    assert A.suggestion(r.ag, df, res, t, {"position": None, "window_open": False, "news_block": False}, 4000.0)["decision"] == "NO TRADE"
    assert A.suggestion(r.ag, df, res, t, {"position": None, "window_open": True, "news_block": True}, 4000.0)["reason"] == "news block"
    s = A.suggestion(r.ag, df, res, t, {"position": {"direction": "short", "points": 2.5}, "window_open": True, "news_block": False}, 4000.0)
    assert s["decision"] == "WAIT" and s["side"] is None and "in trade SHORT" in s["line"]


# ================================================================== buttons
def test_buttons_journal_and_no_order_when_execution_disabled(rig):
    r = rig(); r.cfg.execution_enabled = False
    r.store.arm("XAUUSD", 4000.0, 4010.0)
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 3999.9, "bar": 1, "mode": "ema2050", "suggested_side": "LONG"}}
    text = bot.alert_decision(r.cfg, {"XAUUSD": r.ag}, r.j, r.store, item, "LONG", "tester")["text"]
    assert text.startswith("noted — place it in MT5, I will manage it") and "SL 3988.80" in text      # ask 4000.8 − 12
    assert r.fake.orders == [] and r.ag.pending_alert == {"alert_id": "A1", "side": "LONG", "t": pytest.approx(time.time(), abs=5)}
    d = [x for x in r.j.read(0) if x["event"] == "alert_decision"][-1]
    assert (d["alert_id"], d["side"], d["suggested_side"], d["agreed"]) == ("A1", "LONG", "LONG", True)
    text = bot.alert_decision(r.cfg, {"XAUUSD": r.ag}, r.j, r.store, item, "SHORT", "tester")["text"]
    d = [x for x in r.j.read(0) if x["event"] == "alert_decision"][-1]
    assert d["side"] == "SHORT" and d["agreed"] is False and r.fake.orders == []
    assert bot.alert_decision(r.cfg, {"XAUUSD": r.ag}, r.j, r.store, item, "SKIP", "tester")["line"] == "SKIPPED"
    d = [x for x in r.j.read(0) if x["event"] == "alert_decision"][-1]
    assert d["side"] == "skip" and d["agreed"] is False and r.store.get("A1")["decision"] == "skip"


def test_button_places_order_only_on_demo_with_execution_enabled(rig):
    r = rig(); r.cfg.execution_enabled = True; r.cfg.allow_live = False; r.cfg.max_lots = 0.5
    item = {"meta": {"kind": "alert", "alert_id": "A1", "symbol": "XAUUSD", "price": 3999.9, "bar": 1, "mode": "ema2050", "suggested_side": "LONG", "lots": 2.0}}
    r.fake.trade_mode = 2                                                    # real account, allow_live off -> never
    text = bot.alert_decision(r.cfg, {"XAUUSD": r.ag}, r.j, r.store, item, "LONG", "tester")["text"]
    assert r.fake.orders == [] and "live account" in text
    r.fake.trade_mode = 0                                                    # demo -> placed through broker.place_market with the guardian SL
    text = bot.alert_decision(r.cfg, {"XAUUSD": r.ag}, r.j, r.store, item, "LONG", "tester")["text"]
    assert text.startswith("✅ placed LONG 0.5 lots XAUUSD") and len(r.fake.orders) == 1
    o = r.fake.orders[0]
    assert o["volume"] == 0.5 and o["type"] == r.fake.ORDER_TYPE_BUY and abs(o["sl"] - (r.fake.ask - 12.0)) < 1e-9
    assert any(x["event"] == "order_placed" and x["ok"] for x in r.j.read(0))


def test_next_position_carries_the_alert_id(rig):
    r = rig(); r.ag.attach_alert("A7", "LONG")
    r.fake.closed = False; r.fake.price = 4201.0; r.fake.pos.price_open = 4200.0
    n = 200; df = pd.DataFrame({"time": np.arange(n) * 300, "close": 4201.0, "high": 4201.0, "low": 4201.0, "ema20": 4190.0, "ema50": 4180.0})
    r.ag._guard(True, int(df["time"].iloc[-1]), df, 4201.0, 4190.0, 4180.0, 1)
    assert r.ag.state[1]["alert_id"] == "A7" and r.ag.pending_alert is None
    assert any(x["event"] == "position_seen" and x.get("alert_id") == "A7" for x in r.j.read(0))
    r.fake.closed = True
    r.ag._guard(True, int(df["time"].iloc[-1]) + 300, df, 4201.0, 4190.0, 4180.0, 1)
    assert any(x["event"] == "closed" and x.get("alert_id") == "A7" for x in r.j.read(0))


# ================================================================== reports / Saturday card
def test_alert_stats_in_report_and_compare_card():
    recs = [{"t": 10, "event": "alert_fired", "alert_id": "A1"}, {"t": 11, "event": "alert_fired", "alert_id": "A2"},
            {"t": 12, "event": "alert_decision", "alert_id": "A1", "side": "LONG", "suggested_side": "LONG", "agreed": True},
            {"t": 13, "event": "alert_decision", "alert_id": "A2", "side": "skip", "suggested_side": None, "agreed": True},
            {"t": 20, "event": "closed", "alert_id": "A1", "final_points": 8.5}]
    st = A.stats(recs)
    assert st == {"fired": 2, "taken": 1, "skipped": 1, "agreed": 1, "agreed_agent": 1, "agreed_claude": 0, "with_claude": 0, "graded": 1,
                  "wins": 1, "losses": 0, "points": 8.5}
    lines = "\n".join(A.stats_lines(st))
    assert "**ALERTS**" in lines and "fired" in lines and "agent✓" in lines and "claude✓" in lines and "+8.5" in lines and "win 1 / loss 0" in lines
    assert A.stats_lines(A.stats([])) == []
    res = compare.compute(recs, [], 0, 100, OFF)
    c = compare.card(res)
    assert any(f["name"] == "ALERTS" and "+8.5" in f["value"] for f in c["fields"])


def test_every_alert_command_goes_through_run():
    import ast
    tree = ast.parse(open(bot.__file__, encoding="utf-8").read())
    names = {f.name for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef)
             and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "command" for d in f.decorator_list)}
    assert {"alert_cmd", "alerts_cmd", "alert_cancel_cmd", "alert_clear_cmd"} <= names
