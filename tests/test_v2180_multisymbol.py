"""v2.18.0 — ema921 on six symbols, M15, the pullback-to-EMA9 entry: per-symbol rules in points, a guardian for every symbol
(and none for a symbol without rules), the ema9touch state machine (replay = live), the TRADE card, six dry agents for 200
bars, the gateway-only restart gate, AUREON_CLAUDE_SYMBOLS, the boot symbol checks, webhook masking."""
import importlib
import threading
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import broker, selfheal as H
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.common import tf_synth
from aureon_mt5.common.source import synthetic_m1
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier, mask
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema921 import journeys as J
from tests.test_v2162_selfrestart import guard

SIX = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "AUDUSD", "USDJPY"]
TABLE = {  # symbol: (point, tp, sl, lock_at, lock_to, allowed hours)
    "XAUUSD": (0.01, 1000, 1000, 300, 150, None),
    "XAGUSD": (0.001, 200, 100, 60, 30, (9, 15)),
    "EURUSD": (0.00001, 100, 200, 50, 20, (9, 22)),
    "GBPUSD": (0.00001, 100, 200, 50, 20, (9, 22)),
    "AUDUSD": (0.00001, 100, 200, 50, 20, (9, 15)),
    "USDJPY": (0.001, 350, 350, 150, 60, None),
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("AUREON_921_"):
            monkeypatch.delenv(k)
    J._POINT.clear()
    yield
    J._POINT.clear()


# ----------------------------------------------------------------------------- 1. per-symbol rules in points
@pytest.mark.parametrize("sym", SIX)
def test_rules_table_per_symbol(sym):
    point, tp, sl, la, lt, hours = TABLE[sym]
    r = J.rules_for(sym)
    assert (r.point, r.tp_pts, r.sl_pts, r.lock_at_pts, r.lock_to_pts) == (point, tp, sl, la, lt)
    assert r.no_entry_server_hours == J.hours_complement(hours) and r.entry == "ema9touch" and r.exit_mode == "bracket"
    assert (r.friday_no_entry_from, r.friday_flat) == (16.0, True)
    assert r.stop == pytest.approx(sl * point) and r.tp == pytest.approx(tp * point) and r.lock_to == pytest.approx(lt * point)
    g = J.guardian_for(sym)
    assert g is not None and g.enabled and g.pre_stop == pytest.approx(J.to_price(sl, point))
    assert g.secure_at == pytest.approx(J.to_price(la, point)) and g.secure_level == pytest.approx(J.to_price(lt, point))
    assert J.guardian_for(sym + ".m") is g                                          # broker suffixes


def test_rules_are_distinct_objects_and_to_price_round_trips():
    rs = [J.rules_for(s) for s in SIX]
    assert len({id(r) for r in rs}) == 6 and len({(r.point, r.sl_pts, r.tp_pts) for r in rs}) == 4
    assert J.to_price(200, 0.00001) == pytest.approx(0.002)                         # EURUSD SL 200 pts = 0.00200
    assert J.to_price(1000, 0.01) == 10.0 and J.to_price(350, 0.001) == pytest.approx(0.35)
    assert J.rules_for("AUDUSD").no_entry_server_hours == ((0.0, 9.0), (15.0, 24.0))     # London 9–15 only


def test_unknown_symbols_raise():
    for sym in ("XPTUSD", "EURJPY", "BTCUSD"):
        with pytest.raises(J.UnknownSymbol):
            J.guardian_for(sym)
        with pytest.raises(J.UnknownSymbol):
            J.rules_for(sym)
    with pytest.raises(J.UnknownSymbol):
        get_strategy("ema921").guardian_for("XPTUSD")


def test_env_overrides_and_banner():
    env = {"AUREON_921_TP_EURUSD": "150", "AUREON_921_SL_EURUSD": "250", "AUREON_921_LOCK_AT_EURUSD": "60",
           "AUREON_921_LOCK_TO_EURUSD": "25", "AUREON_921_HOURS_EURUSD": "8-17", "AUREON_921_HOURS_XAUUSD": "9-15"}
    r = J.rules_for("EURUSD", env)
    assert (r.tp_pts, r.sl_pts, r.lock_at_pts, r.lock_to_pts) == (150, 250, 60, 25)
    assert r.no_entry_server_hours == ((0.0, 8.0), (17.0, 24.0))
    assert J.rules_for("GBPUSD", env).tp_pts == 100                                  # per symbol, never shared
    lines = J.banner_lines(SIX + ["XPTUSD"], env)
    assert lines[2] == "EURUSD: ema9touch · TP +150 · SL −250 · +60→+25 pts (point 1e-05) · hours 8–17 server · exit bracket"
    assert lines[0].endswith("hours 9–15 server · exit bracket") and lines[-1].startswith("⛔ XPTUSD")


def test_broker_point_keeps_the_price_distances():
    J.set_point("XAUUSD", 0.001)                                                     # a 3-digit gold feed
    r = J.rules_for("XAUUSD")
    assert (r.point, r.sl_pts, r.stop, r.tp) == (0.001, 10000, 10.0, 10.0)
    J.set_point("XAUUSD", None)
    assert J.rules_for("XAUUSD").sl_pts == 1000


# ----------------------------------------------------------------------------- 2. the ema9touch state machine
T0 = 4 * 86400                                                                       # a Monday 00:00 server


def frame(cross_at=230, touch_at=None, opposite_at=None, n=260, d=+1, point=0.00001):
    """Hand-built EMAs: bear until `cross_at`, then `d` (until `opposite_at`). Bars sit ABOVE EMA 9 (long) so nothing touches,
    except the `touch_at` bar whose low (high for a short) reaches it."""
    t = T0 + np.arange(n) * 900
    e9 = np.full(n, 1.10000); e21 = np.where(np.arange(n) < cross_at, e9 + 0.0005 * d, e9 - 0.0005 * d)
    if opposite_at is not None:
        e21[opposite_at:] = e9[opposite_at:] + 0.0005 * d
    close = e9 + 0.0008 * d; high = close + 0.0002; low = close - 0.0002
    if touch_at is not None:
        if d > 0:
            low[touch_at] = e9[touch_at] - 0.00005
        else:
            high[touch_at] = e9[touch_at] + 0.00005
    return pd.DataFrame({"time": t, "open": close, "high": high, "low": low, "close": close, "ema9": e9, "ema21": e21,
                         "ema200": e9 - 0.01})


def run(df, sym="EURUSD", **kw):
    import dataclasses
    return J.build(df, dataclasses.replace(J.rules_for(sym), **kw))


def labels(res):
    return [(e.index, e.label) for e in res["events"] if e.label in ("ARM", "CANCEL", "EXPIRE", "EB", "ES", "F")]


def test_cross_then_touch_is_the_entry():
    res = run(frame(touch_at=233))
    assert labels(res) == [(230, "ARM"), (233, "EB")]
    jn = res["journeys"][0]
    assert (jn.cross_index, jn.entry_index, jn.entry_price, jn.direction) == (230, 233, 1.1, "long")
    ev = [e for e in res["events"] if e.label == "EB"][0]
    assert ev.info["level"] == 1.1 and ev.info["bars_after_cross"] == 3


def test_short_side_touches_with_the_high():
    res = run(frame(touch_at=240, d=-1))
    assert labels(res) == [(230, "ARM"), (240, "ES")] and res["journeys"][0].direction == "short"


def test_cross_then_opposite_cross_cancels_without_entry():
    res = run(frame(opposite_at=236))
    assert labels(res)[:3] == [(230, "ARM"), (236, "CANCEL"), (236, "ARM")]
    assert not [j for j in res["journeys"] if j.direction == "long"]               # the long never entered
    assert res["journeys"][0].cross_index == 236                                   # (the new short arms on the opposite cross)


def test_no_touch_for_16_bars_expires():
    res = run(frame(touch_at=247))                                                  # bar 247 is 17 bars after the cross
    assert labels(res) == [(230, "ARM"), (247, "EXPIRE")] and not res["journeys"]
    assert labels(run(frame(touch_at=246)))[-1] == (246, "EB")                      # 16 bars: still armed


def test_the_cross_bar_itself_never_enters():
    df = frame(touch_at=230)
    assert labels(run(df)) == [(230, "ARM"), (247, "EXPIRE")]


def test_a_blocked_hour_keeps_waiting():
    df = frame(touch_at=233)
    df["time"] = T0 + 6 * 3600 + (np.arange(len(df)) - 233) * 900                    # the touch bar opens 06:00 server
    df.loc[234, "low"] = 1.09995                                                    # 06:15: still outside 9–22
    res = run(df)
    assert [l for _, l in labels(res)] == ["ARM", "EXPIRE"] and not res["journeys"]


def test_one_function_for_replay_and_live(monkeypatch):
    monkeypatch.setattr(J, "touches_ema9", lambda *a: False)
    assert not run(frame(touch_at=233))["journeys"]
    import inspect
    from aureon_mt5 import agent
    assert "touches_ema9" not in inspect.getsource(agent)                           # live reads build()'s events: no copy


def test_bracket_exit_has_no_close_through_ema_exit():
    from tests.test_v2170_pending import bars
    res = J.build(bars(), J.rules_for("XAUUSD"))
    assert res["summary"]["trades"] > 0 and res["summary"]["ema_close"] == 0 and res["summary"]["entry"] == "ema9touch"
    assert all(j.pullback and j.entry_index > j.cross_index for j in res["journeys"])


# ----------------------------------------------------------------------------- the cards
class Sent(list):
    def send(self, key, title, lines=None, png=None, **kw):
        self.append((key, title, lines, kw)); return True

    def card(self, title, body="", **kw):
        self.append(("card", title, body, kw)); return True


def agent_for(sym, tmp_path, monkeypatch, **cfg):
    c = Config(); c.dry = True; c.log_dir = str(tmp_path / sym); c.mode = "ema921"; c.symbols = [sym]; c.lot = 1.0
    c.auto_trade = False; c.exec_tf = "m15"
    for k, v in cfg.items():
        setattr(c, k, v)
    tf_synth.set_exec_tf("M15", c)
    ag = SymbolAgent(sym, c, get_strategy("ema921"), Notifier(None), Journal(c.log_dir), ())
    sent = Sent(); ag.notify = sent
    monkeypatch.setattr("aureon_mt5.agent.context_chart", lambda *a, **k: None)
    return ag, sent


def test_arm_and_trade_cards_eurusd(tmp_path, monkeypatch):
    ag, sent = agent_for("EURUSD", tmp_path, monkeypatch)
    df = frame(touch_at=233)
    res = J.build(df.iloc[:231].reset_index(drop=True), J.rules_for("EURUSD")); res["rules"] = J.rules_for("EURUSD")
    d1 = df.iloc[:231].reset_index(drop=True)
    ag._detect_921(res, d1, int(d1["time"].iloc[-1]), float(d1["close"].iloc[-1]), 0, 0)
    key, title, lines, kw = sent[-1]
    assert title == "EURUSD · MT5 · CROSS LONG — WAITING FOR PULLBACK · LONG" and "within 16 bars (240 min)" in lines[0]
    d2 = df.iloc[:234].reset_index(drop=True)
    res = J.build(d2, J.rules_for("EURUSD")); res["rules"] = J.rules_for("EURUSD")
    ag._detect_921(res, d2, int(d2["time"].iloc[-1]), float(d2["close"].iloc[-1]), 0, 0)
    key, title, lines, kw = sent[-1]
    f = {x["name"]: x["value"] for x in kw["fields"]}
    assert title == "EURUSD · MT5 · TRADE LONG · EMA 9 PULLBACK · ACT BY HAND · LONG"
    assert f["Entry"].startswith("**1.10000** (EMA 9 touch")
    assert f["TP"] == "1.10100 · +100 pts · $100" and f["SL"] == "1.09800 · −200 pts · $200"
    assert f["EMA gap"] == "+50 pts" and f["Spread"] == "—" and "auto-trade off" in f["Not placed"]
    assert f["Lot"].startswith("1 (AUREON_LOT)") and f["After the cross"] == "3 bar(s)"
    assert [r["event"] for r in ag.journal.read(0) if r["event"].startswith("cross_")][:1] == ["cross_armed"]


def test_cancel_card(tmp_path, monkeypatch):
    ag, sent = agent_for("GBPUSD", tmp_path, monkeypatch)
    df = frame(opposite_at=236).iloc[:237].reset_index(drop=True)
    res = J.build(df, J.rules_for("GBPUSD")); res["rules"] = J.rules_for("GBPUSD")
    ag._detect_921(res, df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)
    titles = [t for _, t, *_ in sent]
    assert "GBPUSD · MT5 · CROSS CANCELLED · LONG PULLBACK NEVER CAME · LONG" in titles


def test_usdjpy_money_is_in_the_account_currency(tmp_path, monkeypatch):
    ag, _ = agent_for("USDJPY", tmp_path, monkeypatch)
    ag.health["close"] = 150.0
    assert ag._money921(J.rules_for("USDJPY"), 350, 1.0) == pytest.approx(350 * 0.001 * 100000 / 150.0)
    monkeypatch.setattr(broker, "point_value", lambda s: 0.6667)
    ag.dry = False
    assert ag._money921(J.rules_for("USDJPY"), 350, 2.0) == pytest.approx(350 * 0.6667 * 2)


# ----------------------------------------------------------------------------- six dry agents, 200 bars, no thread dies
def test_six_symbols_200_bars_dry_no_thread_dies(tmp_path, monkeypatch):
    monkeypatch.setattr("aureon_mt5.agent.context_chart", lambda *a, **k: None)
    errors, done = {}, []

    def drive(sym):
        try:
            ag, sent = agent_for(sym, tmp_path, monkeypatch, paper=True)
            m15 = tf_synth.resample_tf(synthetic_m1(sym, 15 * 700).df, "M15")
            S = ag.S
            for i in range(len(m15) - 200, len(m15)):
                closed = m15.iloc[:i].reset_index(drop=True)
                df = S.add_emas(closed); bar_t = int(df["time"].iloc[-1]); c = float(df["close"].iloc[-1])
                ag._detect(closed, df, bar_t, c, float(df["ema9"].iloc[-1]), float(df["ema21"].iloc[-1]))
                ag._pending921_tick(bar_t, df, True)
                ag._paper_tick(bar_t, df)
            done.append((sym, len([x for x in sent if "TRADE" in str(x[1])]), len(ag.journal.read(0))))
        except Exception as e:                                                       # pragma: no cover - the failure we test for
            errors[sym] = repr(e)

    ts = [threading.Thread(target=drive, args=(s,), name=f"dry-{s}") for s in SIX]
    [t.start() for t in ts]; [t.join(120) for t in ts]
    assert errors == {} and sorted(s for s, *_ in done) == sorted(SIX) and not any(t.is_alive() for t in ts)


# ----------------------------------------------------------------------------- 3. process-level
def test_gateway_down_400s_with_mt5_up_does_not_restart(tmp_path):
    rebuilds, exits = [], []
    hg, m, c, n, j = guard(tmp_path, True, rebuilds, exits)
    hg.mt5_up = lambda: True
    m.disconnected_since = c.t; c.t += 400; hg.tick(c.t)
    assert exits == []
    c.t += 1401; hg.tick(c.t)                                                          # past AUREON_GATEWAY_ONLY_RESTART_S
    assert exits == [75]


def test_gateway_down_400s_with_mt5_down_restarts(tmp_path):
    rebuilds, exits = [], []
    hg, m, c, n, j = guard(tmp_path, True, rebuilds, exits)
    hg.mt5_up = lambda: False
    m.disconnected_since = c.t; c.t += 400; hg.tick(c.t)
    assert exits == [75] and any("MT5 disconnected" in r[1]["reason"] for r in j.rows if r[0] == "self_restart")


def test_claude_only_on_the_listed_symbols(tmp_path, monkeypatch):
    adv = SimpleNamespace(advises=lambda S: True, SOURCE="claude")
    for sym, expect in (("XAUUSD", 1), ("EURUSD", 0)):
        ag, _ = agent_for(sym, tmp_path, monkeypatch, claude_symbols=("XAUUSD", "XAGUSD"))
        ag.claude = adv
        assert len(ag.advisors()) == expect


def test_startup_symbols(monkeypatch):
    import main
    assert main.startup_symbols(SIX, "ema921") == (SIX, [])
    keep, bad = main.startup_symbols(["XAUUSD", "EURJPY", "EURUSD"], "ema921")
    assert keep == ["XAUUSD", "EURUSD"] and "EURJPY" in bad[0]
    with pytest.raises(SystemExit, match="must start with a metal"):
        main.startup_symbols(["EURUSD", "XAUUSD"], "ema921")
    assert main.startup_symbols(["XAUUSD", "EURJPY"], "ema5080") == (["XAUUSD", "EURJPY"], [])


def test_webhook_tokens_are_masked(capsys):
    from aureon_mt5 import notify
    url = "https://discord.com/api/webhooks/1234567890/AbCdEf-gh_ij?wait=true"
    assert mask(url) == "https://discord.com/api/webhooks/1234567890/***?wait=true"
    notify.print("  (notify failed:", ConnectionError(f"Max retries exceeded with url: {url}"), ")")
    out = capsys.readouterr().out
    assert "AbCdEf" not in out and "webhooks/1234567890/***" in out


def test_aureon_tf_alias_and_new_keys(monkeypatch):
    from aureon_mt5 import config as C
    monkeypatch.delenv("AUREON_EXEC_TF", raising=False)
    monkeypatch.setenv("AUREON_TF", "M15"); monkeypatch.setenv("AUREON_LOT", "0.5"); monkeypatch.setenv("AUREON_CLAUDE_SYMBOLS", "xauusd")
    importlib.reload(C)
    try:
        c = C.Config()
        assert (c.exec_tf, c.lot, c.claude_symbols, c.entry_921) == ("m15", 0.5, ("XAUUSD",), "ema9touch")
    finally:
        monkeypatch.delenv("AUREON_TF"); monkeypatch.delenv("AUREON_LOT"); monkeypatch.delenv("AUREON_CLAUDE_SYMBOLS")
        importlib.reload(C)
