"""v2.16.1 — AUREON_FILLING, /place, /sl, /tp (every mode)."""
import os
import types
from types import SimpleNamespace

import pytest

from aureon_mt5 import broker, manual as M
from aureon_mt5.strategies import get_strategy
from tests.test_v181 import FakeMT5, harness  # noqa: F401
from tests.test_v2160_ema921 import agent921, frame  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------------- parsing + defaults
@pytest.mark.parametrize("raw,out", [("30", ("pts", 30.0)), ("+15", ("pts", 15.0)), ("-20", ("pts", -20.0)), ("200", ("pts", 200.0)),
                                     ("2450.5", ("price", 2450.5)), ("sl=25", ("pts", 25.0)), ("off", ("off", None)), ("0", ("off", None))])
def test_parse_value(raw, out):
    assert M.parse_value(raw) == out


def test_parse_value_rejects_junk():
    with pytest.raises(ValueError):
        M.parse_value("abc")


def test_parse_place():
    assert M.parse_place("short 0.24") == {"side": "SHORT", "lots": 0.24, "sl": None, "tp": None}
    assert M.parse_place("long sl=25 tp=2500") == {"side": "LONG", "lots": None, "sl": "25", "tp": "2500"}
    with pytest.raises(ValueError):
        M.parse_place("sideways 1")


def test_defaults_per_mode():
    assert M.mode_defaults(get_strategy("ema921"), "XAUUSD") == (30.0, None)
    assert M.mode_defaults(get_strategy("ema9200"), "XAUUSD") == (30.0, None)
    for mode in ("ema2050", "ema5080"):
        S = get_strategy(mode)
        assert M.mode_defaults(S, "XAUUSD") == (float(S.guardian_for("XAUUSD").pre_stop), None)


def test_place_levels_points_vs_price():
    assert M.place_levels("SHORT", 3300.0, None, None, (30.0, None)) == (3330.0, None)          # defaults
    assert M.place_levels("LONG", 3300.0, "25", "60", (30.0, None)) == (3275.0, 3360.0)         # distances
    assert M.place_levels("LONG", 3300.0, "3280.5", "3400", (30.0, None)) == (3280.5, 3400.0)   # prices
    assert M.place_levels("LONG", 3300.0, None, "off", (30.0, 50.0)) == (3270.0, None)
    with pytest.raises(ValueError):
        M.place_levels("LONG", 3300.0, "3310", None, (30.0, None))                            # SL above a long's entry


# ----------------------------------------------------------------------------- /place: the policy gate
OK_GOV = {"trades_today": 0, "losses_today": 0, "consecutive_losses": 0, "realised_pts_today": 0.0, "realised_money_today": 0.0,
          "equity": 100_000.0, "open_positions": [], "news_block": False}


def placer(harness, monkeypatch, **cfg):
    ag, sent, calls = agent921(harness, monkeypatch, **cfg)
    monkeypatch.setattr(broker, "tick", lambda sym: {"bid": 3300.0, "ask": 3300.3, "time": 1})
    ag.last_df = frame(); ag.last_bar = int(ag.last_df["time"].iloc[-1])
    return ag, sent, calls


def do_place(ag, side="SHORT", **kw):
    from aureon_mt5.governor import Governor
    ctx = dict(OK_GOV)
    try:
        from datetime import datetime
        from aureon_mt5.common.timeutil import IST
        ctx["ist_now"] = datetime.now(IST).replace(hour=12, minute=0)
    except Exception:
        pass
    return M.place(ag.cfg, {"XAUUSD": ag}, ag.journal, ag, side, governor_ctx=ctx, user="me", **kw)


def test_place_short_with_mode_defaults(harness, monkeypatch):
    ag, sent, calls = placer(harness, monkeypatch, max_lots=1.0)
    res = do_place(ag, "SHORT", lots=0.24)
    assert res["ok"] and res["placed"] and calls["place"]
    (sym, side, lots, sl), kw = calls["place"][0]
    assert (sym, side, lots, sl, kw["tp"], kw["comment"]) == ("XAUUSD", "SHORT", 0.24, 3330.0, None, "aureon /place")
    row = [r for r in ag.journal.read(0) if r["event"] == "order_placed"][-1]
    assert row["actor"] == "human" and row["rationale"] == "/place" and row["sl"] == 3330.0 and row["tp"] is None
    assert res["card"]["title"] == "XAUUSD · MT5 · PLACED SHORT"
    names = [f["name"] for f in res["card"]["fields"]]
    assert {"Side", "Entry", "Lot", "Stop / TP", "Lock", "Exit", "Order"} <= set(names)


@pytest.mark.parametrize("cfg,demo,why", [
    (dict(execution_enabled=False), True, "execution off"),
    (dict(), False, "AUREON_ALLOW_LIVE=1 needed"),
    (dict(), None, "account type unknown"),
])
def test_place_policy_refusals(harness, monkeypatch, cfg, demo, why):
    ag, sent, calls = placer(harness, monkeypatch, **cfg)
    monkeypatch.setattr(broker, "is_demo", lambda: demo)
    res = do_place(ag, "LONG", lots=0.1)
    assert not res["ok"] and why in res["text"] and not calls["place"]


def test_place_live_with_allow_live(harness, monkeypatch):
    ag, sent, calls = placer(harness, monkeypatch, allow_live=True)
    monkeypatch.setattr(broker, "is_demo", lambda: False)
    assert do_place(ag, "LONG", lots=0.1)["ok"] and calls["place"]


def test_place_lots_capped_and_dd_guard(harness, monkeypatch):
    ag, sent, calls = placer(harness, monkeypatch, max_lots=0.5)
    res = do_place(ag, "LONG", lots=2.0)
    assert res["ok"] and calls["place"][0][0][2] == 0.5 and "capped at max 0.5" in res["text"]
    ag2, _, calls2 = placer(harness, monkeypatch, max_dd_usd=7000.0)
    from aureon_mt5 import ema921_live as L
    L.save_state(ag2.cfg.log_dir, "XAUUSD", {"equity_peak": 108_000.0}); ag2._st921 = None
    res2 = do_place(ag2, "LONG", lots=0.1)
    assert not res2["ok"] and "drawdown guard" in res2["text"] and not calls2["place"]


def test_place_governor_block(harness, monkeypatch):
    ag, sent, calls = placer(harness, monkeypatch)
    from aureon_mt5 import governor as G
    monkeypatch.setattr(G.Governor, "check", lambda self, *a: SimpleNamespace(allowed=False, rule="max_trades", detail="3 trades today"))
    res = do_place(ag, "LONG", lots=0.1)
    assert not res["ok"] and "governor · max_trades" in res["text"] and not calls["place"]


def test_place_in_paper_mode_goes_to_the_tracker(harness, monkeypatch):
    ag, sent, calls = placer(harness, monkeypatch, paper=True)
    res = do_place(ag, "SHORT", lots=0.24, sl_raw="25", tp_raw="60")
    assert res["ok"] and res["paper"] and not calls["place"]
    pos = ag._state921()["paper"][-1]
    assert pos["side"] == "SHORT" and pos["sl_pts"] == pytest.approx(-25.0) and pos["tp_pts"] == pytest.approx(60.0)
    row = [r for r in ag.journal.read(0) if r["event"] == "paper_fill"][-1]
    assert row["actor"] == "human" and row["rationale"] == "/place"


def test_place_no_cross_needed_and_adopted_by_the_mode(harness, monkeypatch):
    """No event, no ask: the human override. The position is then managed by the mode's guardian (ema921 dispatch by S.name)."""
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    assert 'if self.S.name == "ema921":                                        # v2.16.0: mechanical guardian' in src
    ag, sent, calls = placer(harness, monkeypatch)
    assert do_place(ag, "LONG", lots=0.1)["ok"]


# ----------------------------------------------------------------------------- /sl /tp
def pos(sl=0.0, tp=0.0, direction="long", entry=3300.0, current=3325.0, ticket=7):
    return {"ticket": ticket, "direction": direction, "price_open": entry, "sl": sl, "tp": tp, "current": current, "points": current - entry}


def modifier(harness, monkeypatch, positions):
    ag, sent, calls = agent921(harness, monkeypatch)
    monkeypatch.setattr(broker, "positions", lambda sym: positions)
    sends = []
    monkeypatch.setattr(broker, "modify_sltp", lambda t, s, sl, tp: sends.append((t, sl, tp)) or broker.BrokerResult(True, "SLTP_CHANGED", changed=True))
    return ag, sends


def test_sl_plus_15_keeps_the_tp(harness, monkeypatch):
    ag, sends = modifier(harness, monkeypatch, [pos(sl=3270.0, tp=3360.0)])
    res = M.modify(ag.cfg, ag.journal, ag, "sl", "+15")
    assert res["ok"] and sends == [(7, 3315.0, 3360.0)]                      # BOTH values in the one request
    row = [r for r in ag.journal.read(0) if r["event"] == "sltp_modified"][-1]
    assert (row["sl_before"], row["sl_after"], row["tp_before"], row["tp_after"]) == (3270.0, 3315.0, 3360.0, 3360.0)


def test_sl_loosening_a_lock_needs_force(harness, monkeypatch):
    ag, sends = modifier(harness, monkeypatch, [pos(sl=3315.0)])
    res = M.modify(ag.cfg, ag.journal, ag, "sl", "-20")
    assert not res["ok"] and "loosens the locked level" in res["text"] and not sends
    assert M.modify(ag.cfg, ag.journal, ag, "sl", "-20", force=True)["ok"] and sends == [(7, 3280.0, None)]


def test_sl_through_the_market_refused(harness, monkeypatch):
    ag, sends = modifier(harness, monkeypatch, [pos(sl=3270.0, current=3310.0)])
    assert "through the market" in M.modify(ag.cfg, ag.journal, ag, "sl", "+15")["text"] and not sends


def test_tp_plus_60_and_off(harness, monkeypatch):
    ag, sends = modifier(harness, monkeypatch, [pos(sl=3315.0)])
    assert M.modify(ag.cfg, ag.journal, ag, "tp", "+60")["ok"] and sends[-1] == (7, 3315.0, 3360.0)
    ag2, sends2 = modifier(harness, monkeypatch, [pos(sl=3315.0, tp=3360.0)])
    assert M.modify(ag2.cfg, ag2.journal, ag2, "tp", "off")["ok"] and sends2[-1] == (7, 3315.0, None)


def test_sl_short_side_and_ticket_choice(harness, monkeypatch):
    two = [pos(direction="short", entry=3300.0, current=3280.0, sl=3330.0, ticket=7), pos(ticket=8)]
    ag, sends = modifier(harness, monkeypatch, two)
    assert "more than one position" in M.modify(ag.cfg, ag.journal, ag, "sl", "+15")["text"]
    assert M.modify(ag.cfg, ag.journal, ag, "sl", "+15", ticket=7)["ok"] and sends[-1] == (7, 3285.0, None)


def test_modify_sltp_sends_both_values_in_one_request(monkeypatch):
    fake = FakeMT5("long", 3300.0, 3270.0, 3320.0); fake.pos.tp = 3360.0
    sent = []
    orig = fake.order_send
    fake.order_send = lambda req: sent.append(dict(req)) or orig(req)
    monkeypatch.setattr(broker, "_m", fake)
    r = broker.modify_sltp(1, "XAUUSD", 3315.0, 3360.0)
    assert r.ok and len(sent) == 1 and sent[0]["action"] == FakeMT5.TRADE_ACTION_SLTP
    assert sent[0]["sl"] == 3315.0 and sent[0]["tp"] == 3360.0
    broker.modify_sltp(1, "XAUUSD", 3315.0, None)
    assert sent[-1]["tp"] == 0.0 and sent[-1]["sl"] == 3315.0


def test_guardian_keeps_a_tighter_manual_sl(harness, monkeypatch):
    ag, sent, calls = agent921(harness, monkeypatch)
    monkeypatch.setattr(broker, "set_sl", lambda *a, **k: calls["set_sl"].append((a, k)) or
                        broker.BrokerResult(True, "ALREADY_BETTER", changed=False, existing_sl=3325.0))
    df = frame(); e = float(df["close"].iloc[-1]) - 25.0
    p = {"ticket": 9, "price_open": e, "points": 25.0, "direction": "long", "volume": 0.5, "sl": e + 25}
    st = {"peak": 25.0, "secured": 0.0}                                     # (the loop would rebuild +25; force the lock path)
    ag._guard_921(p, st, 1, True, int(df["time"].iloc[-1]), df, 0)
    (args, kw), = calls["set_sl"]
    assert kw.get("allow_loosen", False) is False                           # never asks the broker to loosen
    assert any("already tighter — kept" in str(lines) for _, _, lines, _ in sent)


# ----------------------------------------------------------------------------- filling
class FillMT5(FakeMT5):
    ORDER_FILLING_RETURN = 2; TRADE_RETCODE_INVALID_FILL = 10030

    def __init__(self, accept, **k):
        super().__init__(**k); self.accept = accept; self.tried = []
        self.closed = True                                   # flat: v2.16.2's position cap would refuse the order otherwise

    def symbol_select(self, *a):
        return True

    def order_send(self, req):
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            name = {0: "fok", 1: "ioc", 2: "return"}[req["type_filling"]]; self.tried.append(name)
            if name != self.accept:
                return types.SimpleNamespace(retcode=10030, comment="Unsupported filling mode")
            return types.SimpleNamespace(retcode=10009, comment="done", order=55, deal=56, price=req["price"], volume=req["volume"])
        return super().order_send(req)


@pytest.mark.parametrize("configured,accept,tried", [("ioc", "ioc", ["ioc"]), ("ioc", "return", ["ioc", "fok", "return"]),
                                                     ("fok", "ioc", ["fok", "ioc"]), ("return", "fok", ["return", "ioc", "fok"])])
def test_filling_fallback_order(monkeypatch, configured, accept, tried):
    fake = FillMT5(accept); monkeypatch.setattr(broker, "_m", fake)
    broker.configure_filling(configured)
    try:
        r = broker.place_market("XAUUSD", "LONG", 0.1, 3270.0, sleep=lambda s: None)
    finally:
        broker.configure_filling("ioc")
    assert r.ok and fake.tried == tried and r.fill_mode == accept


def test_no_fallback_on_other_rejections(monkeypatch):
    class NoMoney(FillMT5):
        def order_send(self, req):
            if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
                self.tried.append(req["type_filling"]); return types.SimpleNamespace(retcode=10019, comment="No money")
            return super().order_send(req)
    fake = NoMoney("ioc"); monkeypatch.setattr(broker, "_m", fake)
    r = broker.place_market("XAUUSD", "LONG", 0.1, 3270.0, sleep=lambda s: None)
    assert not r.ok and len(fake.tried) == 1


def test_fill_mode_journaled_and_boot_warning():
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    assert "fill_mode=placed.fill_mode" in src
    assert "fill_mode=r.fill_mode" in open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    import main
    cfg = SimpleNamespace(dry=False, symbols=["XAUUSD"])
    line = main.filling_line(cfg, supported_fn=lambda s: {"ioc": False, "fok": True, "return": False})
    assert line.startswith("ioc") and "⚠️ XAUUSD does not accept ioc (accepts fok)" in line
    assert main.filling_line(cfg, supported_fn=lambda s: {"ioc": True, "fok": True, "return": False}) == "ioc"


def test_filling_supported_flags(monkeypatch):
    class Info(FakeMT5):
        SYMBOL_TRADE_EXECUTION_MARKET = 2
        def symbol_info(self, s): return types.SimpleNamespace(digits=2, trade_mode=4, filling_mode=1, trade_exemode=2)
    monkeypatch.setattr(broker, "_m", Info())
    assert broker.filling_supported("XAUUSD") == {"fok": True, "ioc": False, "return": False}


def test_commands_registered():
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    for name in ("place", "sl", "tp"):
        assert f'name="{name}"' in src
    from aureon_mt5 import bot
    assert {"place", "sl", "tp", "close"} <= set(bot.HELP["Trading"])
