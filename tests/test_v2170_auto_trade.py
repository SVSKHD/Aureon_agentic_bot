"""v2.17.0 commit 1 — /auto-trade: env default, persistence across a restart, each refusal, a cross while off (journaled, signal
card, no order), on-after-off never backfills, the weekly line, /feature-set goes through the same refusals."""
from types import SimpleNamespace

import pytest

from aureon_mt5 import auto_trade as A, broker, features as F
from aureon_mt5.bot import auto_trade_action, feature_set_action
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from tests.test_v181 import harness  # noqa: F401
from tests.test_v2160_ema921 import agent921, cross_res, frame

class User:
    def __init__(self, name, roles):
        self.name, self.roles = name, roles

    def __str__(self):
        return self.name


ADMIN = User("koushik", [SimpleNamespace(id=42)])
NOBODY = User("guest", [])
ENV = {"AUREON_MAX_LOTS": "1.0"}


class Note:
    def __init__(self):
        self.cards = []

    def card(self, title, body="", **kw):
        self.cards.append((title, body))


def cfg_(tmp_path, **kw):
    c = Config(); c.log_dir = str(tmp_path); c.mode = "ema921"; c.execution_enabled = True; c.paper = False; c.dry = False
    c.allow_live = False; c.max_lots = 1.0; c.auto_trade = False
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def act(c, action, user=ADMIN, agents=None, demo=True, env=ENV, **kw):
    n = Note(); j = Journal(c.log_dir)
    out = auto_trade_action(c, agents or {}, j, n, user, action, admin_role="42", is_demo=lambda: demo, environ=env, **kw)
    return out, n, j


# ----------------------------------------------------------------------------- boot default + persistence
@pytest.mark.parametrize("val,on", [("1", True), ("0", False)])
def test_env_is_the_boot_default(tmp_path, monkeypatch, val, on):
    monkeypatch.setenv("AUREON_AUTO", val)
    import importlib
    from aureon_mt5 import config as C
    importlib.reload(C)
    try:
        c = C.Config(); c.log_dir = str(tmp_path)
        assert c.auto_trade is on and c.auto_mode is on
        assert A.source(c, {"AUREON_AUTO": val}) == "env" and A.is_on(c) is on
    finally:
        monkeypatch.delenv("AUREON_AUTO"); importlib.reload(C)


def test_persists_across_a_restart(tmp_path):
    c = cfg_(tmp_path)
    out, n, j = act(c, "on")
    assert A.is_on(c) and n.cards[0][0] == "AUTO-TRADE ON · ema921 · 1 · live"
    row = [r for r in j.read(0) if r["event"] == "auto_trade_changed"][-1]
    assert (row["by"], row["from"], row["to"]) == ("koushik", "off", "on")
    boot = cfg_(tmp_path)                                                    # a new process: AUREON_AUTO=0 in the env
    assert boot.auto_trade is False
    F.apply(boot, F.load_overrides(boot.log_dir))                            # what main() does at boot
    assert boot.auto_trade is True and A.source(boot, {"AUREON_AUTO": "0"}) == "discord"
    assert "✅ on · ema921 · 1 lots · live (discord)" in A.status_line(boot, {"AUREON_AUTO": "0"})
    out, n, j = act(boot, "off")
    assert not A.is_on(boot) and n.cards[0][0] == "AUTO-TRADE OFF · signals only"
    assert F.load_overrides(boot.log_dir)["auto_trade"] is False


def test_paper_card(tmp_path):
    c = cfg_(tmp_path, paper=True, execution_enabled=False)
    out, n, _ = act(c, "on")
    assert n.cards[0][0] == "AUTO-TRADE ON · ema921 · 1 · paper"            # paper sends no order: execution not needed


# ----------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("kw,demo,env,force,why", [
    (dict(execution_enabled=False), True, ENV, False, "AUREON_EXECUTION=0"),
    (dict(), False, ENV, False, "live account and AUREON_ALLOW_LIVE=0"),
    (dict(), None, ENV, False, "account type unknown"),
    (dict(), True, {}, False, "AUREON_MAX_LOTS is not set"),
    (dict(max_lots=2.0), True, {"AUREON_MAX_LOTS": "2"}, False, "AUREON_MAX_LOTS=2 > 1.0"),
])
def test_each_refusal(tmp_path, kw, demo, env, force, why):
    c = cfg_(tmp_path, **kw)
    out, n, j = act(c, "on", demo=demo, env=env, force=force)
    assert out.startswith("⛔ auto-trade stays off") and why in out
    assert not A.is_on(c) and not n.cards and "auto_trade" not in F.load_overrides(c.log_dir)
    assert [r for r in j.read(0) if r["event"] == "auto_trade_refused"]


def test_force_covers_the_lots_check_only(tmp_path):
    c = cfg_(tmp_path, max_lots=2.0)
    out, n, _ = act(c, "on", env={"AUREON_MAX_LOTS": "2"}, force=True)
    assert A.is_on(c) and n.cards[0][0] == "AUTO-TRADE ON · ema921 · 2 · live"
    c2 = cfg_(tmp_path / "b", execution_enabled=False)
    assert "AUREON_EXECUTION=0" in act(c2, "on", force=True)[0]
    c3 = cfg_(tmp_path / "c", allow_live=True)
    assert A.is_on(c3) is False and act(c3, "on", demo=False)[0].startswith("**AUTO-TRADE ON")   # live allowed explicitly


def test_drawdown_guard_refuses(tmp_path):
    c = cfg_(tmp_path)
    agents = {"XAUUSD": SimpleNamespace(_equity_dd_block=lambda: "equity 8,000 below the peak (limit 7,000)")}
    out, *_ = act(c, "on", agents=agents)
    assert "drawdown guard tripped — XAUUSD: equity 8,000" in out and not A.is_on(c)


def test_admin_only_and_status_is_read_only(tmp_path):
    c = cfg_(tmp_path)
    assert act(c, "on", user=NOBODY)[0] == "Denied: configured administrator role required"
    out, n, _ = act(c, "status", user=NOBODY)
    assert out.startswith("auto-trade ⬜ off · signals only") and not n.cards


def test_feature_set_goes_through_the_same_refusals(tmp_path, monkeypatch):
    c = cfg_(tmp_path, execution_enabled=False)
    monkeypatch.setattr(broker, "is_demo", lambda: True)
    out = feature_set_action(c, {}, Journal(c.log_dir), Note(), None, ADMIN, "auto_trade", "on", admin_role="42")
    assert "AUREON_EXECUTION=0" in out and not A.is_on(c)


def test_switching_never_touches_positions(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(broker, "close", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(broker, "set_sl", lambda *a, **k: calls.append(a))
    c = cfg_(tmp_path); agents = {"XAUUSD": SimpleNamespace(snapshot={"positions": [{"ticket": 5}]})}
    act(c, "on", agents=agents); act(c, "off", agents=agents)
    assert calls == []


# ----------------------------------------------------------------------------- the order gate
def test_cross_while_off_is_journaled_signal_card_no_order(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, auto_trade=False)
    df = frame(); ag._detect_921(cross_res(df), df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)
    assert calls["place"] == []
    row = [r for r in ag.journal.read(0) if r["event"] == "cross_skipped"][-1]
    assert row["why"] == "auto-trade off" and row["side"] == "LONG"
    key, title, _, kw = [x for x in sent if "ENTER_" in str(x[0])][0]
    names = {f["name"]: f["value"] for f in kw["fields"]}
    assert "SIGNAL LONG" in title.upper() and "Order" not in names and "/place" in names["Not placed"]


def test_on_after_off_does_not_backfill(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, auto_trade=False)
    df = frame(); bt = int(df["time"].iloc[-1])
    closed_at = bt + ag.bar_s - ag.off * 3600                                # the bar's close, real epoch
    monkeypatch.setattr(F._time, "time", lambda: closed_at + 120)            # /auto-trade on two minutes after that close
    c = ag.cfg; c.max_lots = 1.0; c.execution_enabled = True
    assert act(c, "on")[0].startswith("**AUTO-TRADE ON")
    ag._detect_921(cross_res(df), df, bt, float(df["close"].iloc[-1]), 0, 0)  # the cross that fired while off
    assert calls["place"] == []
    assert [r for r in ag.journal.read(0) if r["event"] == "cross_skipped"][-1]["why"] == "auto-trade off"
    df2 = frame(n=261)                                                       # the next bar closes after the switch
    monkeypatch.setattr(broker, "now_server", lambda off=0: int(df2["time"].iloc[-1]) + 1800 + 60)
    ag._detect_921(cross_res(df2), df2, int(df2["time"].iloc[-1]), float(df2["close"].iloc[-1]), 0, 0)
    assert len(calls["place"]) == 1


def test_auto_mode_is_only_the_boot_default(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, auto_trade=False, auto_mode=True)
    df = frame(); ag._detect_921(cross_res(df), df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]), 0, 0)
    assert calls["place"] == []


# ----------------------------------------------------------------------------- the weekly line
def test_week_line():
    ws = 1_792_368_000.0 - 5.5 * 3600                                       # a Monday 00:00 IST
    fri = ws + 4 * 86400 + 20 * 3600
    assert A.week_line([], ws, fri, True) == "auto-trade: on 5/5 days"
    assert A.week_line([], ws, fri, False) == "auto-trade: off all week (signals only)"
    rows = [{"t": ws + 86400 + 14 * 3600, "event": "auto_trade_changed", "from": "on", "to": "off"},
            {"t": ws + 2 * 86400 + 9 * 3600 + 1800, "event": "auto_trade_changed", "from": "off", "to": "on"}]
    assert A.week_line(rows, ws, fri, True) == "auto-trade: off Tue 14:00 → Wed 09:30 IST"
    assert A.week_line(rows[:1], ws, fri, False) == "auto-trade: off Tue 14:00 → now IST"


def test_week_card_carries_the_line(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch, auto_trade=True)
    assert any(x.startswith("auto-trade: on ") for x in ag.week_921())
