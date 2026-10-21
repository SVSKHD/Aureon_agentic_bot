"""v2.16.0 — the ema921 mode: the order gate, the bar-close lock, restart rebuild, the paper tracker, the drawdown guard's
persisted peak, the week card, the mode enumerations."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import broker, ema921_live as L, ops
from aureon_mt5.strategies import available, get_strategy, resolve_mode
from aureon_mt5.strategies.ema921 import journeys as J
from aureon_mt5.strategies.ema921.analysis import add_emas
from tests.test_v181 import harness  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = J.Rules()
T0 = 1_760_918_400                                                          # Mon 2025-10-20 00:00 server


# ----------------------------------------------------------------------------- registry + enumerations
def test_registered_and_enumerated(tmp_path):
    assert "ema921" in available() and resolve_mode("921") == resolve_mode("ema9/21") == "ema921"
    S = get_strategy("ema921")
    assert S.ema_cols == ("ema9", "ema21") and J.guardian_for("XAUUSD").pre_stop == 30.0
    env = tmp_path / ".env"; env.write_text("AUREON_MODE=ema5080\n")
    ops.write_mode(str(env), "ema921")
    assert "AUREON_MODE=ema921" in env.read_text()
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    assert '"ema9200", "ema921"):' in src and 'name="week"' in src
    from aureon_mt5.claude_advisor import EMA_NAMES
    assert EMA_NAMES["ema921"] == ("EMA 9", "EMA 21")


# ----------------------------------------------------------------------------- the bar-close decision
@pytest.mark.parametrize("kw,expect", [
    (dict(peak=25, locked=False, close_pts=18, against=False, run=0, friday_last=False), (True, None, 0)),
    (dict(peak=25, locked=False, close_pts=12, against=False, run=0, friday_last=False), (False, "lock_close", 0)),
    (dict(peak=10, locked=False, close_pts=5, against=True, run=0, friday_last=False), (False, "ema_close", 1)),
    (dict(peak=10, locked=False, close_pts=5, against=False, run=0, friday_last=True), (False, "friday_flat", 0)),
    (dict(peak=30, locked=True, close_pts=12, against=False, run=0, friday_last=False), (False, None, 0)),
])
def test_on_bar_close(kw, expect):
    assert J.on_bar_close(R, **kw) == expect


# ----------------------------------------------------------------------------- the agent
def agent921(harness, monkeypatch, **cfg):
    _, ag0, _ = harness(price=3300.0)
    for k, v in {"auto_mode": True, "execution_enabled": True, "paper": False, "allow_live": False, "max_dd_usd": 0.0, **cfg}.items():
        setattr(ag0.cfg, k, v)
    from aureon_mt5.agent import SymbolAgent
    ag = SymbolAgent("XAUUSD", ag0.cfg, get_strategy("ema921"), ag0.notify, ag0.journal, ())
    ag.dry = False; ag.bar_s = 1800                                         # AUREON_EXEC_TF=M30
    sent, calls = [], {"place": [], "set_sl": [], "close": []}
    ag.notify.send = lambda key, title, lines=None, png=None, **kw: sent.append((key, title, lines, kw)) or True
    ag.notify.card = lambda title, body, **kw: sent.append(("card", title, body, kw)) or True
    ag._spread_pts = lambda: 0.2
    ag._suggested = lambda stop: {"lots": 0.5}
    monkeypatch.setattr(broker, "positions", lambda sym: [])
    monkeypatch.setattr(broker, "is_demo", lambda: True)
    monkeypatch.setattr(broker, "account", lambda: {"equity": 100_000.0, "balance": 100_000.0, "currency": "USD", "trade_mode": "demo",
                                                    "login_masked": "***1"})
    monkeypatch.setattr(broker, "place_market", lambda *a, **k: calls["place"].append((a, k)) or
                        broker.BrokerResult(True, "PLACED", changed=True, price=3300.0, ticket=77, volume=a[2]))
    monkeypatch.setattr(broker, "set_sl", lambda *a, **k: calls["set_sl"].append(a) or broker.BrokerResult(True, "SL_CHANGED", changed=True))
    monkeypatch.setattr(broker, "close", lambda *a, **k: calls["close"].append(a) or broker.BrokerResult(True, "CLOSED", changed=True))
    monkeypatch.setattr("aureon_mt5.agent.context_chart", lambda *a, **k: None)
    monkeypatch.setattr(broker, "connected", lambda: True)
    monkeypatch.setattr(broker, "now_server", lambda off=0: T0 + 260 * 1800 + 60)    # frame()'s last bar just closed
    return ag, sent, calls


def frame(n=260, seed=5):
    rng = np.random.default_rng(seed)
    close = 3300 + np.cumsum(rng.normal(0, 4, n)); op = np.r_[close[0], close[:-1]]
    t = 1_760_918_400 + np.arange(n) * 1800                                  # Mon 2025-10-20 00:00 server
    return add_emas(pd.DataFrame({"time": t, "open": op, "high": np.maximum(op, close) + 2, "low": np.minimum(op, close) - 2,
                                  "close": close}))


def cross_res(df, side="bull"):
    last = len(df) - 1
    info = {"direction": side, "ema9": 3300.0, "ema21": 3299.0, "ema200": 3290.0, "gap": 1.0, "dist9": 0.5, "dist200": 10.0,
            "last3": ["G+1.0", "R-0.5", "G+2.0"], "session": "london", "atr": 5.0}
    return {"events": [J.CrossEvent(last, int(df["time"].iloc[last]), side, "EB" if side == "bull" else "ES", "cross", info)], "rules": R}


@pytest.mark.parametrize("cfg,demo,pos,dd,placed", [
    (dict(), True, False, False, True),                                     # everything on, demo → the order
    (dict(execution_enabled=False), True, False, False, False),            # AUREON_EXECUTION=0
    (dict(auto_mode=False), True, False, False, False),                    # AUREON_AUTO=0
    (dict(), False, False, False, False),                                  # live account, no AUREON_ALLOW_LIVE
    (dict(allow_live=True), False, False, False, True),                    # live + AUREON_ALLOW_LIVE=1
    (dict(), None, False, False, False),                                   # account type unknown → fail closed
    (dict(paper=True), True, False, False, False),                         # AUREON_PAPER=1 → journal only
    (dict(), True, True, False, False),                                    # already in a position
    (dict(max_dd_usd=7000.0), True, False, True, False),                   # drawdown guard tripped
])
def test_order_gate(harness, monkeypatch, cfg, demo, pos, dd, placed):
    ag, sent, calls = agent921(harness, monkeypatch, **cfg)
    monkeypatch.setattr(broker, "is_demo", lambda: demo)
    if pos:
        monkeypatch.setattr(broker, "positions", lambda sym: [{"ticket": 1}])
    if dd:
        L.save_state(ag.cfg.log_dir, "XAUUSD", {"equity_peak": 108_000.0})          # 8k below the peak
        ag._st921 = None
    df = frame(); ag._detect_921(cross_res(df), df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)
    assert bool(calls["place"]) is placed
    title = [t for k, t, *_ in sent if "ENTER_" in str(k)][0].upper()
    assert ("ENTER LONG" if placed or cfg.get("paper") else "SIGNAL LONG") in title
    if placed:
        (sym, side, lots, sl), kw = calls["place"][0]
        assert (sym, side, lots, kw["tp"]) == ("XAUUSD", "LONG", 0.5, None) and sl == pytest.approx(float(df["close"].iloc[-1]) - 30)


def test_lock_arms_only_at_the_bar_close(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch)
    df = frame(); e = float(df["close"].iloc[-1]) - 25.0                   # the last closed bar closed at +25
    p = {"ticket": 9, "price_open": e, "points": 26.0, "direction": "long", "volume": 0.5, "sl": e - 30}
    st = {"peak": 0.0, "secured": 0.0}
    bt = int(df["time"].iloc[-1])
    ag._guard_921(p, st, 1, False, bt, df, 0)                              # +26 intrabar: nothing moves
    assert not calls["set_sl"] and not st.get("locked") and st["peak"] == 26.0
    ag._guard_921(p, st, 1, True, bt, df, 0)                               # the bar closes at +25 ≥ +15 → lock
    assert calls["set_sl"] == [(9, "XAUUSD", pytest.approx(e + 15))] and st["locked"]
    assert [r for r in ag.journal.read(0) if r["event"] == "locked"]


def test_lock_level_seen_but_bar_closed_below_it_exits_at_the_close(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch)
    df = frame(); e = float(df["close"].iloc[-1]) - 10.0                   # closed at +10 after +22 printed
    p = {"ticket": 9, "price_open": e, "points": 10.0, "direction": "long", "volume": 0.5, "sl": e - 30}
    st = {"peak": 22.0, "secured": 0.0}
    ag._guard_921(p, st, 1, True, int(df["time"].iloc[-1]), df, 0)
    assert not calls["set_sl"] and calls["close"] == [(9, "XAUUSD", "aureon 921")]
    row = [r for r in ag.journal.read(0) if r["event"] == "closed_by_rule"][-1]
    assert row["reason"] == "lock_close" and row["lots"] == 0.5


def test_restart_rebuilds_the_lock_from_the_live_sl(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch)
    df = frame(); e = float(df["close"].iloc[-1]) - 25.0
    p = {"ticket": 9, "price_open": e, "points": 25.0, "direction": "long", "volume": 0.5, "sl": e + 15}
    st = {"peak": 25.0, "secured": ag._locked(p)}                          # what the position loop rebuilds: +15 from the SL
    ag._guard_921(p, st, 1, True, int(df["time"].iloc[-1]), df, 0)
    assert st["locked"] and not calls["set_sl"] and not [k for k, *_ in sent if "LOCK" in str(k)]


def test_position_loop_runs_only_the_921_guardian(harness, monkeypatch):
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    i = src.index('if self.S.name == "ema921":                                        # v2.16.0: mechanical guardian')
    block = src[i:i + 300]
    assert 'st["guardian_profile"] = "ema921"' in block and "self._guard_921(" in block and "continue" in block
    assert i < src.index('if st.get("guardian_profile") == "follow":', i)          # before follow / secure / ride / news


def test_broker_sl_exit_is_a_closed_card_not_closed_by_you(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch)
    asks = []
    ag.notify.ask = lambda *a, **k: asks.append(a) or True
    ag.state[9] = {"guardian_profile": "ema921", "direction": "long", "entry": 3300.0, "peak": 25.0, "secured": 15.0, "locked": True,
                   "last_points": 15.0, "lots": 0.5}
    ag._final_points = lambda t, st: (15.0, False)
    df = frame(); ag._guard(True, int(df["time"].iloc[-1]), df, 3315.0, 0.0, 0.0, 1)
    assert not asks and any("BROKER SL" in t.upper() for _, t, *_ in sent), sent
    row = [r for r in ag.journal.read(0) if r["event"] == "closed"][-1]
    assert row["reason"] == "locked" and row["profile"] == "ema921"


# ----------------------------------------------------------------------------- paper
def test_paper_fill_never_sends_an_order(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, paper=True)
    from tests.test_v181 import FakeMT5
    fake = FakeMT5(); sends = []
    fake.order_send = lambda req: sends.append(req)
    monkeypatch.setattr(broker, "_m", fake)
    df = frame(); close = float(df["close"].iloc[-1]); bt = int(df["time"].iloc[-1])
    ag._detect_921(cross_res(df, "bear"), df, bt, close, 0, 0)
    assert not calls["place"] and not sends
    row = [r for r in ag.journal.read(0) if r["event"] == "paper_fill"][-1]
    assert {k: row[k] for k in ("side", "lots", "price", "sl", "tp", "bar")} == {
        "side": "SHORT", "lots": 0.5, "price": close, "sl": pytest.approx(close + 30), "tp": None, "bar": bt}
    st = json.load(open(L.state_path(ag.cfg.log_dir, "XAUUSD")))
    assert st["paper"][0]["side"] == "SHORT" and st["paper"][0]["bar"] == bt
    ag._detect_921(cross_res(df, "bull"), df, bt, close, 0, 0)            # a paper position blocks the next cross
    assert [r for r in ag.journal.read(0) if r["event"] == "paper_fill"].__len__() == 1


def test_paper_tracker_lock_and_close_cards(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, paper=True)
    df = frame(); e = float(df["close"].iloc[-3])
    st = ag._state921(); st["paper"] = [L.new_paper("LONG", e, 0.5, e - 30, None, int(df["time"].iloc[-3]))]
    up = df.copy(); k = len(up) - 1
    up.loc[k, ["high", "close", "low", "ema9", "ema200"]] = [e + 26, e + 24, e + 1, e + 10, e - 50]
    ag._paper_tick(int(up["time"].iloc[k]), up)                             # +26 printed, closed +24 → lock
    assert ag._state921()["paper"][0]["locked"] and any("LOCK" in str(k_) for k_, *_ in sent)
    dn = up.copy(); dn.loc[k, "time"] = int(up["time"].iloc[k]) + 1800
    dn.loc[k, ["high", "close", "low", "ema9"]] = [e + 24, e + 16, e + 15.5, e + 20]
    ag._paper_tick(int(dn["time"].iloc[k]), dn)                             # closes through EMA 9 at +16
    row = [r for r in ag.journal.read(0) if r["event"] == "closed_by_rule"][-1]
    assert row["paper"] is True and row["reason"] == "ema_close" and row["points"] == pytest.approx(16.0) and row["lots"] == 0.5
    assert ag._state921()["paper"] == [] and ag._state921()["paper_realized_usd"] == pytest.approx(800.0)
    assert any("CLOSED LONG +16.0" in t for _, t, *_ in sent)
    assert not calls["place"] and not calls["set_sl"] and not calls["close"]


def week_frame(weeks=12, seed=8):
    """Mon–Fri M30 bars, each Friday ending with the 22:00 bar (the live Friday flat bar = the replay's last Friday bar)."""
    rng = np.random.default_rng(seed); t0 = 1_760_918_400; ts = []
    for w in range(weeks):
        for d in range(5):
            day = t0 + (w * 7 + d) * 86400
            last = 22 * 3600 if d == 4 else 23 * 3600 + 1800
            ts += list(range(day, day + last + 1, 1800))
    n = len(ts); close = 3300 + np.cumsum(np.repeat(rng.normal(0, 1.2, n // 16 + 1), 16)[:n] + rng.normal(0, 4.5, n))
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"time": ts, "open": op, "high": np.maximum(op, close) + rng.gamma(2, 1.5, n),
                         "low": np.minimum(op, close) - rng.gamma(2, 1.5, n), "close": close})


def test_paper_tracker_matches_the_replay_trade_for_trade():
    """Bar by bar, paper_step reproduces build()'s journeys (entry bar, exit bar, reason, points; the replay books 0.2 spread)."""
    df = add_emas(week_frame()); res = J.build(df, R, 3.0)
    want = [(j.entry_index, j.exit_index, j.exit_reason, round(j.result + 0.2, 2)) for j in res["journeys"] if j.exit_reason != "open"]
    got = []; pos = None; idx = None
    for k in range(len(df)):
        row = {c: float(df[c].iloc[k]) for c in ("high", "low", "close", "ema9", "ema200")}; row["time"] = int(df["time"].iloc[k])
        if pos is not None:
            out = L.paper_step(pos, row, R)
            if out and out["event"] == "close":
                reason = {"lock_close": "locked", "ema_close": "ema_close"}.get(out["reason"], out["reason"])
                got.append((idx, k, reason, round(out["points"], 2))); pos = None
                continue                                                    # replay: a cross on the exit bar is "in position"
        ev = next((e for e in res["events"] if e.index == k and e.label in ("EB", "ES")), None)
        if ev is not None and pos is None:
            pos = L.new_paper("LONG" if ev.label == "EB" else "SHORT", float(df["close"].iloc[k]), 1.0, 0, None, row["time"]); idx = k
    assert len(want) > 20 and got[:len(want)] == want


# ----------------------------------------------------------------------------- drawdown guard + the week
def test_equity_peak_persists_across_restarts(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, max_dd_usd=7000.0)
    eq = {"v": 120_000.0}
    monkeypatch.setattr(broker, "account", lambda: {"equity": eq["v"]})
    assert ag._equity_dd_block() is None
    eq["v"] = 112_000.0
    from aureon_mt5.agent import SymbolAgent
    ag2 = SymbolAgent("XAUUSD", ag.cfg, get_strategy("ema921"), ag.notify, ag.journal, ()); ag2.dry = False   # "restart"
    why = ag2._equity_dd_block()
    assert why and "8,000 below peak 120,000" in why
    assert json.load(open(L.state_path(ag.cfg.log_dir, "XAUUSD")))["equity_peak"] == 120_000.0


def test_week_card(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, max_dd_usd=7000.0)
    for pts, lots, ev in ((24.0, 0.5, "closed_by_rule"), (-30.0, 0.5, "closed"), (8.0, 1.0, "closed_by_rule")):
        kw = {"points": pts} if ev == "closed_by_rule" else {"final_points": pts, "reason": "stop"}
        ag.journal.log(ev, symbol="XAUUSD", mode="ema921", lots=lots, **kw)
    ag.journal.log("closed_by_rule", symbol="XAUUSD", mode="ema921", points=99.0, lots=1.0, paper=True)     # paper: not in a live week
    L.save_state(ag.cfg.log_dir, "XAUUSD", {"equity_peak": 103_000.0}); ag._st921 = None
    lines = ag.week_921()
    assert lines[0] == "trades 3 · won 2 · net +2.0 pts · $+500"
    assert lines[1] == "best +24.0 · worst -30.0" and "drawdown used $3,000 of $7,000" in lines[2] and "58% of weeks positive" in lines[3]
    fri = 1_760_918_400 + 4 * 86400 + 22 * 3600
    ag._week_card_921(fri); ag._week_card_921(fri)
    assert sum(1 for k, *_ in sent if k == "card") == 1                    # once per week


def test_alert_context_works_in_ema921(harness, monkeypatch):
    from aureon_mt5.alerts import guardian_context
    ag, *_ = agent921(harness, monkeypatch)
    ag.snapshot = {"positions": [], "state": {}}; ag.news = ()
    fri = 1_760_918_400 + 4 * 86400
    assert guardian_context(ag, fri + 12 * 3600)[1]["window_open"] is True
    assert guardian_context(ag, fri + 17 * 3600)[1]["window_open"] is False


def test_replay_tool_runs(tmp_path):
    df = week_frame(6); csv = tmp_path / "m30.csv"; df.to_csv(csv, index=False)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "replay_921.py"), "--csv", str(csv)], capture_output=True,
                         text=True, timeout=120, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "crosses" in out.stdout and "max DD" in out.stdout and (tmp_path / "m30_921.png").exists()


# ----------------------------------------------------------------------------- network loss + stale crosses
def test_no_automatic_entry_while_the_network_or_feed_is_down(harness, monkeypatch):
    from aureon_mt5 import netguard
    ag, sent, calls = agent921(harness, monkeypatch)
    monkeypatch.setattr(netguard, "degraded", lambda: True)
    df = frame(); ag._detect_921(cross_res(df), df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)
    assert not calls["place"] and any("network degraded" in str(kw.get("fields")) for *_, kw in sent)
    monkeypatch.setattr(netguard, "degraded", lambda: False); monkeypatch.setattr(broker, "connected", lambda: False)
    ag.notify.sent = set()
    ag._detect_921(cross_res(df), df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)
    assert not calls["place"] and any("MT5 feed down" in str(kw.get("fields")) for *_, kw in sent)


def test_cross_on_a_bar_older_than_the_current_closed_bar_is_not_taken(harness, monkeypatch):
    """The feed returns two bars after the cross bar closed: the cross is journaled cross_skipped, never ordered."""
    ag, sent, calls = agent921(harness, monkeypatch)
    df = frame(); bt = int(df["time"].iloc[-1])
    monkeypatch.setattr(broker, "now_server", lambda off=0: bt + 3 * 1800 + 60)     # the current closed bar is bt + 2 bars
    ag._detect_921(cross_res(df), df, bt, float(df["close"].iloc[-1]), 0, 0)
    assert not calls["place"] and not [k for k, *_ in sent if "ENTER_" in str(k)]
    row = [r for r in ag.journal.read(0) if r["event"] == "cross_skipped"][-1]
    assert row["why"] == "stale after reconnect" and row["bar"] == bt
    assert not [r for r in ag.journal.read(0) if r["event"] == "signal"]
    monkeypatch.setattr(broker, "now_server", lambda off=0: bt + 1800 + 60)         # on time → taken
    ag._detect_921(cross_res(df), df, bt, float(df["close"].iloc[-1]), 0, 0)
    assert calls["place"]


# ----------------------------------------------------------------------------- Friday flat: replay == live
def friday_frame():
    """Mon–Fri, Friday running to 23:30; an up-move all Friday so a long opened Friday morning is still open at 22:00."""
    ts = []
    for d in range(5):
        day = T0 + d * 86400
        ts += list(range(day, day + 23 * 3600 + 1801, 1800))
    n = len(ts); close = 3300 + np.arange(n) * 0.0
    i_fri = ts.index(T0 + 4 * 86400 + 8 * 3600)
    close[:i_fri] = 3300 + np.sin(np.arange(i_fri) / 3.0) * 6
    close[i_fri:] = close[i_fri - 1] + np.arange(1, n - i_fri + 1) * 0.6
    op = np.r_[close[0], close[:-1]]
    return add_emas(pd.DataFrame({"time": ts, "open": op, "high": np.maximum(op, close) + 0.5, "low": np.minimum(op, close) - 0.5,
                                  "close": close}))


def test_friday_flat_hour_is_the_same_bar_in_replay_and_live():
    assert R.friday_flat_hour == 22.0
    df = friday_frame(); res = J.build(df, R, 3.0)
    fri = [j for j in res["journeys"] if j.exit_reason == "friday_flat"]
    assert fri, [(j.entry_time, j.exit_reason) for j in res["journeys"]]
    t = fri[-1].exit_time
    assert J.server_weekday(t) == 4 and J.server_hour(t) == 22.0                 # not the 23:30 bar at the end of the data
    assert J.friday_last_bar(t, R) and not J.friday_last_bar(t - 1800, R)
    import dataclasses
    late = J.build(df, dataclasses.replace(R, friday_flat_hour=23.0), 3.0)
    assert [j for j in late["journeys"] if j.exit_reason == "friday_flat"][-1].exit_time == T0 + 4 * 86400 + 23 * 3600
