"""v2.13.0 — the `follow` guardian profile: secure early, lock every +10, exit only on the stop. FakeMT5, no network."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from tests.test_v181 import harness  # noqa: F401  (fixture)
from tests.test_v280_flip_giveback import mkdf, step, p_short, exits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def follow(harness, price=4200.0, sl=0.0, **cfg):
    fake, ag, _ = harness(price=price, sl=sl)
    ag.cfg.guardian_profile = "follow"
    for k, v in cfg.items():
        setattr(ag.cfg, k, v)
    return fake, ag


def sl_sends(fake):
    return [r for r in fake.sends if r["action"] == fake.TRADE_ACTION_SLTP]


def go(ag, fake, price, t0, ef=4190.0, es=4180.0, new_bar=True):
    fake.price = price
    step(ag, fake, mkdf(price, ef, es, t0=t0), new_bar=new_bar)


def rows(ag, ev):
    return [r for r in ag.journal.read(0) if r["event"] == ev]


# ----------------------------------------------------------------------------- dispatch + adopt
def test_classic_stays_the_default(harness):
    fake, ag, _ = harness(price=4201.0)
    step(ag, fake, mkdf(4201.0, 4190.0, 4180.0))
    assert ag.state[1]["guardian_profile"] == "classic"


def test_dispatch_follow_and_sticky_per_ticket(harness):
    fake, ag = follow(harness, price=4201.0)
    go(ag, fake, 4201.0, 0)
    assert ag.state[1]["guardian_profile"] == "follow" and ag.state[1]["pre"] is False
    ag.cfg.guardian_profile = "classic"                         # changed mid-trade: the open ticket keeps follow
    go(ag, fake, 4201.0, 300)
    assert ag.state[1]["guardian_profile"] == "follow"


def test_adopt_places_the_stop_and_never_loosens(harness):
    fake, ag = follow(harness, price=4201.0, sl=0.0)
    go(ag, fake, 4201.0, 0)
    assert fake.pos.sl == 4175.0                                # entry − 25
    p = rows(ag, "protected")[-1]
    assert p["profile"] == "follow" and p["stop_pts"] == 25.0
    fake2, ag2 = follow(harness, price=4201.0, sl=4190.0)       # a tighter stop is already there: kept
    go(ag2, fake2, 4201.0, 0)
    assert fake2.pos.sl == 4190.0 and sl_sends(fake2) == []
    fake3, ag3 = follow(harness, price=4201.0, sl=4170.0)       # a looser one (−30) is pulled in to −25
    go(ag3, fake3, 4201.0, 0)
    assert fake3.pos.sl == 4175.0


def test_range_scaled_stop(harness):
    fake, ag = follow(harness, price=4201.0, follow_stop=5.0, follow_stop_x_range=1.5)
    go(ag, fake, 4201.0, 0)                                     # mkdf range 4.0 → 1.5 × 4 = 6 > 5
    assert fake.pos.sl == 4194.0 and ag.state[1]["follow_stop_pts"] == 6.0


# ----------------------------------------------------------------------------- secure + step ladder
def test_secure_at_plus_3_exactly_once(harness):
    fake, ag = follow(harness, price=4201.0)
    go(ag, fake, 4201.0, 0)
    n0 = len(sl_sends(fake))
    go(ag, fake, 4203.0, 300)
    assert fake.pos.sl == 4203.0 and ag.state[1]["secured"] == 3.0
    assert rows(ag, "secured")[-1]["level"] == 3.0 and any("FOLLOW_LOCK" in k for k in ag.notify.sent)
    go(ag, fake, 4203.5, 600); go(ag, fake, 4204.0, 900)
    assert len(sl_sends(fake)) == n0 + 1


def test_step_locks_with_clearance_and_floor(harness):
    fake, ag = follow(harness, price=4201.0)
    for i, px in enumerate([4201.0, 4204.0, 4211.9]):
        go(ag, fake, px, i * 300)
    assert fake.pos.sl == 4203.0                                # +11.9: nothing moves
    sends = len(sl_sends(fake))
    go(ag, fake, 4212.0, 900)                                   # +12: k=1 → +0, floored to +3: no SL move, ladder row
    assert fake.pos.sl == 4203.0 and len(sl_sends(fake)) == sends
    assert [r["level"] for r in rows(ag, "follow_lock")] == [3.0]
    go(ag, fake, 4222.0, 1200); assert fake.pos.sl == 4210.0
    go(ag, fake, 4232.0, 1500); assert fake.pos.sl == 4220.0
    go(ag, fake, 4242.0, 1800); assert fake.pos.sl == 4230.0
    assert [r["level"] for r in rows(ag, "follow_lock")] == [3.0, 10.0, 20.0, 30.0]
    assert any(k.endswith("-10") and "FOLLOW_LOCK" in k for k in ag.notify.sent)
    sec = rows(ag, "secured")[-1]
    assert sec["profile"] == "follow" and sec["lock_level"] == 30.0 and sec["peak"] == 42.0


def test_continuous_mode_ratchets_to_peak_minus_step(harness):
    fake, ag = follow(harness, price=4201.0, follow_trail_mode="continuous")
    go(ag, fake, 4201.0, 0); go(ag, fake, 4215.0, 300)
    assert fake.pos.sl == 4205.0
    go(ag, fake, 4218.0, 600); assert fake.pos.sl == 4208.0
    go(ag, fake, 4212.0, 900); assert fake.pos.sl == 4208.0    # never back
    go(ag, fake, 4206.0, 1200); assert fake.pos.sl == 4208.0


def test_wick_to_the_lock_hits_the_broker_stop_and_journals_stop(harness):
    fake, ag = follow(harness, price=4201.0)
    for i, px in enumerate([4201.0, 4212.0, 4222.0]):
        go(ag, fake, px, i * 300)
    assert fake.pos.sl == 4210.0
    go(ag, fake, 4210.0, 900, new_bar=False)                    # the wick reaches the lock …
    fake.closed = True                                          # … the broker stop fills
    go(ag, fake, 4214.0, 1200)
    c = rows(ag, "closed")[-1]
    assert c["reason"] == "stop" and c["profile"] == "follow" and c["lock_level"] == 10.0 and c["final_points"] == 10.0
    assert not any("CLOSED_BY_YOU" in k for k in ag.notify.sent)


# ----------------------------------------------------------------------------- nothing else closes it
@pytest.mark.parametrize("bars", [12, 36, 100])
def test_no_timeout_while_unsecured(harness, bars):
    fake, ag = follow(harness, price=4199.0)
    go(ag, fake, 4199.0, 0)
    sl0, n0 = fake.pos.sl, len(sl_sends(fake))
    for i in range(1, bars + 1):
        go(ag, fake, 4199.0, i * 300, ef=4180.0, es=4190.0)     # lines not crossed: classic would time out at 12 bars
    assert not fake.closed and exits(ag) == [] and fake.pos.sl == sl0 and len(sl_sends(fake)) == n0


def test_no_close_on_ema_turn_plain_p_giveback_or_slow_ema(harness):
    fake, ag = follow(harness, price=4201.0, giveback_pct=50.0)
    for i, px in enumerate([4201.0, 4222.0]):                   # secured, locked +10
        go(ag, fake, px, i * 300)
    sl0, n0 = fake.pos.sl, len(sl_sends(fake))
    go(ag, fake, 4214.0, 600, ef=4216.0, es=4205.0)            # close through the fast EMA after securing
    df = mkdf(4213.0, 4216.0, 4205.0, t0=900); fake.price = 4213.0; p_short(ag, df); step(ag, fake, df)   # plain opposite P
    go(ag, fake, 4211.0, 1200, ef=4216.0, es=4214.0)           # 50 % giveback from +22 and a close through the slow EMA
    assert not fake.closed and exits(ag) == []
    assert fake.pos.sl == sl0 and len(sl_sends(fake)) == n0


# ----------------------------------------------------------------------------- the structure flip
def cross(ag, df, direction="SHORT"):
    ag.last_confirmed_cross = {"bar_time": int(df["time"].iloc[-1]), "direction": direction, "cross_time": None}


def test_opposite_confirmed_cross_needs_structure(harness, monkeypatch):
    fake, ag = follow(harness, price=4205.0)
    monkeypatch.setattr(ag, "_m15_state", lambda: "BULLISH")
    go(ag, fake, 4205.0, 0)
    df = mkdf(4205.0, 4191.0, 4190.0, t0=300); fake.price = 4205.0; cross(ag, df); step(ag, fake, df)
    assert not fake.closed and rows(ag, "opposite_cross_ignored")[-1]["evidence"] == []
    monkeypatch.setattr(ag, "_m15_state", lambda: "BEARISH")
    df2 = mkdf(4188.0, 4191.0, 4190.0, t0=600); fake.price = 4188.0; cross(ag, df2); step(ag, fake, df2)
    assert fake.closed
    ex = exits(ag)[-1]
    assert ex["reason"] == "flip" and ex["profile"] == "follow" and "M15 against" in ag.state[1]["flip_evidence"]


def test_flip_goes_through_the_pre_exit_read_when_on(harness, monkeypatch):
    fake, ag = follow(harness, price=4205.0, pre_exit_read="defer")
    adv = MagicMock(); adv.SOURCE = "claude"; adv.advises.return_value = True; adv.request_pre_exit_read.return_value = True
    ag.claude = adv
    monkeypatch.setattr(ag, "_m15_state", lambda: "BEARISH")
    go(ag, fake, 4205.0, 0)
    df = mkdf(4188.0, 4191.0, 4190.0, t0=300); fake.price = 4188.0; cross(ag, df); step(ag, fake, df)
    assert not fake.closed and ag.state[1]["pending_exit"]["rule"] == "flip"
    assert adv.request_pre_exit_read.call_args.kwargs["rule"] == "flip"
    ag.finish_pending_exit(broker.positions("XAUUSD")[0], ag.state[1], "exit")
    assert fake.closed and exits(ag)[-1]["claude_pre_exit"] == "exit" and exits(ag)[-1]["profile"] == "follow"


def test_flip_off(harness, monkeypatch):
    fake, ag = follow(harness, price=4205.0, follow_exit_on_flip="off")
    monkeypatch.setattr(ag, "_m15_state", lambda: "BEARISH")
    go(ag, fake, 4205.0, 0)
    df = mkdf(4188.0, 4195.0, 4190.0, t0=300); fake.price = 4188.0; cross(ag, df); step(ag, fake, df)
    assert not fake.closed


# ----------------------------------------------------------------------------- max hold + news
def test_max_hold_only_when_unsecured_and_setup_dead(harness, monkeypatch):
    fake, ag = follow(harness, price=4199.0, follow_max_hold_bars=36)
    alive = {"v": True}

    def upd(p, st, *a, **k):
        st["setup_alive"] = alive["v"]
    monkeypatch.setattr(ag, "_update_setup", upd)
    for i in range(0, 37):
        go(ag, fake, 4199.0, i * 300)
    assert not fake.closed                                      # 36 bars, setup alive
    alive["v"] = False
    go(ag, fake, 4199.0, 37 * 300)
    assert fake.closed and exits(ag)[-1]["reason"] == "max_hold"
    fake2, ag2 = follow(harness, price=4204.0, follow_max_hold_bars=36)     # secured: never
    monkeypatch.setattr(ag2, "_update_setup", lambda p, st, *a, **k: st.__setitem__("setup_alive", False))
    for i in range(0, 40):
        go(ag2, fake2, 4204.0, i * 300)
    assert not fake2.closed


def test_news_flat_only_while_unsecured(harness):
    fake, ag = follow(harness, price=4201.0)
    ag.news = [int(time.time()) + 5 * 60]
    go(ag, fake, 4201.0, 0)
    assert fake.closed and exits(ag)[-1]["reason"] == "news_flat"
    fake2, ag2 = follow(harness, price=4201.0)
    go(ag2, fake2, 4205.0, 0)                                   # secured +3
    ag2.news = [int(time.time()) + 5 * 60]
    go(ag2, fake2, 4205.0, 300)
    assert not fake2.closed
    fake3, ag3 = follow(harness, price=4201.0, follow_news_flat=False)
    ag3.news = [int(time.time()) + 5 * 60]
    go(ag3, fake3, 4201.0, 0)
    assert not fake3.closed


# ----------------------------------------------------------------------------- restart + guardian block
def test_restart_readopts_peak_and_secured_and_keeps_the_broker_sl(harness, tmp_path):
    fake, ag = follow(harness, price=4201.0)
    for i, px in enumerate([4201.0, 4224.0, 4220.0]):
        go(ag, fake, px, i * 300)
    assert fake.pos.sl == 4210.0
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.strategies import get_strategy
    os.remove(ag.journal.state_path)                            # force the journal rebuild path
    ag2 = SymbolAgent("XAUUSD", ag.cfg, get_strategy("ema5080"), ag.notify, ag.journal, ())
    n0 = len(sl_sends(fake))
    fake.price = 4220.0; step(ag2, fake, mkdf(4220.0, 4190.0, 4180.0, t0=900))
    st = ag2.state[1]
    assert st["guardian_profile"] == "follow" and st["peak"] == 24.0 and st["secured"] == 10.0
    assert fake.pos.sl == 4210.0 and len(sl_sends(fake)) == n0


def test_guardian_block_for_follow(harness):
    fake, ag = follow(harness, price=4201.0)
    go(ag, fake, 4201.0, 0)
    p = broker.positions("XAUUSD")[0]
    g = ag.guardian_intent(p, ag.state[1], mkdf(4201.0, 4190.0, 4180.0), 4201.0, 4190.0, 4180.0)
    assert g["profile"] == "follow" and g["phase"] == "protect"
    assert g["next_exit"] == {"rule": "stop", "level": 4175.0, "distance_pts": 26.0, "bars_left": None, "fires_on": "tick"}
    assert g["next_lock"]["lock_pts"] == 3.0 and g["next_lock"]["at_peak"] == 3.0
    go(ag, fake, 4214.0, 300)
    p = broker.positions("XAUUSD")[0]
    g = ag.guardian_intent(p, ag.state[1], mkdf(4214.0, 4190.0, 4180.0), 4214.0, 4190.0, 4180.0)
    assert g["phase"] == "secured" and g["next_exit"]["rule"] == "ride_step" and g["next_exit"]["level"] == 4203.0
    assert g["next_lock"] == {"level": 4210.0, "lock_pts": 10.0, "at_peak": 22.0, "distance_pts": 8.0}


def test_registry_group_and_governor_text(harness):
    from aureon_mt5 import features as F
    from aureon_mt5.bot import guardian_profile_text, profile_warning
    names = {f.name for f in F.registry() if f.group == "follow"}
    assert {"guardian_profile", "follow_stop", "follow_secure_at", "follow_step", "follow_clearance", "follow_trail_mode",
            "follow_exit_on_flip", "follow_news_flat", "follow_max_hold_bars", "follow_pre_exit_read"} <= names
    assert not F.get("follow_stop").hot and F.get("follow_pre_exit_read").hot and not F.get("guardian_profile").hot
    fake, ag = follow(harness, price=4201.0)
    go(ag, fake, 4201.0, 0)
    t = guardian_profile_text(ag.cfg, {"XAUUSD": ag})
    assert "Guardian profile:** follow" in t and "stop −25" in t and "#1 XAUUSD: follow" in t
    assert "#1 XAUUSD keeps follow" in profile_warning(ag.cfg, {"XAUUSD": ag})


def test_per_symbol_profile_env(monkeypatch):
    from aureon_mt5.config import Config
    monkeypatch.setenv("AUREON_GUARDIAN_PROFILE_XAUUSD", "follow")
    c = Config()
    assert c.resolved_guardian_profile(None, "XAUUSD") == "follow" and c.resolved_guardian_profile(None, "XAGUSD") == "classic"


# ----------------------------------------------------------------------------- the replay harness
def test_replay_follow_on_the_15_oct_fixture(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import replay_guardian as RG
    out = RG.main(["--bars", os.path.join(ROOT, "tests", "fixtures", "xauusd_20261015_m5.json"), "--trade", "10-15T01:30,long,4103.5",
                   "--set", "guardian_profile=follow", "--out", str(tmp_path)])
    t = out["combo"]["trades_detail"][0]
    assert t["reason"] in ("stop", "open_at_end") and t["sequence"].startswith("secured +3")


def test_replay_follow_prints_the_ladder_for_a_long_run():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import replay_guardian as RG
    n = 400
    t0 = 1_792_000_000
    up = np.concatenate([np.full(120, 4100.0), 4100 + np.arange(60) * 0.8, np.full(220, 4148.0)])
    up[180:] = np.linspace(4148, 4120, 220)                     # the run, then a slide back through the locks
    c = up
    bars = pd.DataFrame({"time": t0 + np.arange(n) * 300, "open": c, "high": c + 0.6, "low": c - 0.6, "close": c})
    bars = RG.prepare(bars)
    ents = [{"i": 125, "direction": "long", "entry": float(bars["close"][125]), "source": "test"}]
    res = RG.run_combo(bars, ents, RG.parse_sets(["guardian_profile=follow"]), signals={})
    t = res["trades_detail"][0]
    assert t["sequence"] == "secured +3 → lock +3 → lock +10 → lock +20 → lock +30 → stop", t
    assert t["reason"] == "stop" and t["points"] == 30.0
    assert "secured +3 → lock +3 → lock +10" in RG.table("follow", res)


def test_secure_needs_live_points_and_locks_stay_below_price(harness):
    fake, ag = follow(harness, price=4201.0)
    go(ag, fake, 4201.0, 0)
    ag.state[1]["peak"] = 25.0                                  # e.g. a rebuilt peak after a restart, price now +1
    n0 = len(sl_sends(fake))
    go(ag, fake, 4201.0, 300)
    assert fake.pos.sl == 4175.0 and len(sl_sends(fake)) == n0
    go(ag, fake, 4204.0, 600)                                   # live +4: secure +3; the +10 lock (peak 25) is above price: waits
    assert fake.pos.sl == 4203.0
    go(ag, fake, 4212.0, 900)
    assert fake.pos.sl == 4210.0
