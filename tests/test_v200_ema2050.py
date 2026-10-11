"""v2.0.0 — EMA 20/50 un-frozen: confirm rule (3 bars + 1.5 gap, multi cross), pullback entry (pre-confirm touch -> confirm-bar
entry, post-confirm touch, chase cap, fallback), no-entry hour, guardian early lock (+3 -> entry+1, never loosened), pre_stop 12,
secure/ride unchanged, EMA20-turn exit only after secured, exit on the confirmed opposite cross, P-timeout path skipped,
no EMA50 follow-SL, replay exit reasons, detect.py rows == alert suggestion, and ema5080 Guardian defaults byte-identical."""
import dataclasses, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import Guardian, get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from aureon_mt5.strategies.ema5080 import strategy as S5080
from aureon_mt5.common.verdict import entry_verdict
from tests.test_v181 import FakeMT5

OFF = 3.0
DAY0 = 1_791_331_200          # 2026-10-07 00:00 server (a Wednesday)
S = get_strategy("ema2050")
RULES = J.rules_for("XAUUSD")


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body.splitlines()[0]); self.bodies.append(str(embed or body)); return True


# ------------------------------------------------------------------ frame builder: explicit EMA columns, no warm-up games
def frame(gaps, closes=None, highs=None, lows=None, e20=4000.0, t0=DAY0 + 10 * 3600):
    """gaps[i] = EMA20 − EMA50 at bar i. EMA20 fixed at e20 unless a list is given. Bars start at 10:00 server (12:30 IST)."""
    n = len(gaps); gaps = np.array(gaps, float)
    e20v = np.full(n, e20, float) if np.isscalar(e20) else np.array(e20, float)
    e50v = e20v - gaps
    c = np.array(closes, float) if closes is not None else e20v + 0.5
    h = np.array(highs, float) if highs is not None else np.maximum(c, e20v) + 0.5
    l = np.array(lows, float) if lows is not None else np.minimum(c, e20v) - 0.5
    return pd.DataFrame({"time": t0 + np.arange(n) * 300, "open": c, "high": h, "low": l, "close": c,
                         "ema20": e20v, "ema50": e50v, "spread": gaps})


def build(df, **over):
    return J.build(df, dataclasses.replace(RULES, **over), OFF)


def labels(res):
    return [(e.index, e.label) for e in res["events"]]


# ================================================================== confirm rule
def test_confirm_three_bars_and_gap():
    # bear for 5 bars, cross to bull at bar 5; gap grows 0.5, 1.0, 2.0 -> 3 bars holding AND gap >= 1.5 first at bar 7
    g = [-2] * 5 + [0.5, 1.0, 2.0, 2.5, 3.0, 3.0, 3.0]
    res = build(frame(g, closes=[4000.5] * 12, lows=[4002.0] * 12))        # no touch (lows above EMA20 + 1.5)
    assert (5, "CROSS") in labels(res) and (7, "CONFIRMED") in labels(res)
    assert res["cross_infos"][0].confirm_index == 7


def test_confirm_waits_for_the_gap():
    g = [-2] * 5 + [0.5, 1.0, 1.2, 1.4, 1.6, 3.0, 3.0]                   # 3 bars hold at bar 7 but gap < 1.5 until bar 9
    res = build(frame(g, closes=[4000.5] * 12, lows=[4002.0] * 12))
    assert res["cross_infos"][0].confirm_index == 9
    st = res["bar_states"][8]
    assert st["verdict"] == "WAIT" and "bar 4 of 3" in st["confirm"] and "gap 1.40 < 1.5" in st["confirm"]


def test_multi_cross_no_trade():
    g = [-2] * 5 + [0.5, 1.0, -0.5, -1.0, -2.0, -2.5, -3.0, -3.0]         # flips back at bar 7 before any confirmation
    res = build(frame(g, closes=[4000.5] * 13, lows=[4002.0] * 13, highs=[4000.6] * 13))
    x = res["cross_infos"][0]
    assert x.multi and x.confirm_index is None and x.entry_index is None
    assert (7, "MULTI") in labels(res) and (7, "CROSS") in labels(res)          # the flip is itself a new (bear) cross
    assert res["summary"]["trades"] == 0 and res["summary"]["multi"] == 1
    assert entry_verdict(S, {"df": res and frame(g), "bar_states": res["bar_states"], "events": res["events"]}, 6, OFF)["decision"] == "WAIT"


def test_no_confirmation_inside_window_is_not_a_trade():
    g = [-2] * 3 + [1.0] * 30                                              # holds forever but the gap never reaches 1.5
    res = build(frame(g, closes=[4000.5] * 33, lows=[4002.0] * 33))
    x = res["cross_infos"][0]
    assert x.timeout and x.confirm_index is None and res["summary"]["trades"] == 0
    assert res["bar_states"][3 + 19]["verdict"] == "NO TRADE"


# ================================================================== pullback entry
def _bull(n=40, touch_at=(), close_at=None):
    """Bull cross at bar 5, confirmed at bar 7 (gap 2.0). Lows touch EMA20 (+1.0 above it) at the given bars."""
    g = [-2] * 5 + [2.0] * (n - 5)
    lows = [4002.0] * n; closes = [4000.5] * n
    for k in touch_at: lows[k] = 4001.0
    for k, v in (close_at or {}).items(): closes[k] = v
    return frame(g, closes=closes, lows=lows, highs=[max(c, 4000.0) + 0.5 for c in closes])


def test_pre_confirm_touch_enters_at_confirm_bar():
    res = build(_bull(touch_at=(6,)))
    x = res["cross_infos"][0]
    assert x.confirm_index == 7 and x.touched_before_confirm and x.entry_index == 7 and x.entry_kind == "pullback"
    assert (7, "EB") in labels(res) and res["journeys"][0].pullback


def test_post_confirm_first_touch_enters():
    res = build(_bull(touch_at=(11,)))
    x = res["cross_infos"][0]
    assert x.entry_index == 11 and x.entry_kind == "pullback"
    assert res["bar_states"][9]["verdict"] == "WAIT" and "waiting for the pullback" in res["bar_states"][9]["reason"]
    assert res["bar_states"][11]["verdict"] == "LONG"


def test_chase_cap_rejects_far_close_then_takes_next_touch():
    res = build(_bull(touch_at=(9, 12), close_at={9: 4006.0}))             # bar 9 touches but closes 6 above EMA20 -> too far
    x = res["cross_infos"][0]
    assert 9 in x.fails and x.fails[9].startswith("too far from EMA20") and x.entry_index == 12
    assert (9, "F") in labels(res) and res["bar_states"][9]["verdict"] == "NO TRADE"


def test_fallback_at_confirm_plus_12_within_5_of_ema20():
    res = build(_bull())                                                     # no touch at all: fallback at bar 19 (close 0.5 from EMA20)
    x = res["cross_infos"][0]
    assert x.entry_index == 19 and x.entry_kind == "no-pullback" and not res["journeys"][0].pullback
    assert res["bar_states"][19]["verdict"] == "LONG" and "no pullback" in res["bar_states"][19]["reason"]
    res2 = build(_bull(close_at={19: 4006.0}))                               # fallback bar closes 6 away -> never chase
    assert res2["cross_infos"][0].entry_index is None and 19 in res2["cross_infos"][0].fails
    assert res2["bar_states"][25]["verdict"] == "NO TRADE" and "window passed" in res2["bar_states"][25]["reason"]


# ================================================================== hours and news
def test_no_entry_hour_server_21_to_24():
    df = _bull(touch_at=(11,)); df["time"] = DAY0 + 21 * 3600 + np.arange(len(df)) * 300        # bars from 21:00 server
    res = build(df)
    x = res["cross_infos"][0]
    assert x.entry_index is None and x.fails[11].startswith("no-entry hour (server 21:")
    assert any(e.label == "F" and "no-entry hour" in e.reason for e in res["events"])


def test_ist_window_is_the_stricter_one():
    df = _bull(touch_at=(11,)); df["time"] = DAY0 + 1 * 3600 + np.arange(len(df)) * 300          # 01:55 server = 04:25 IST (< 05:30)
    res = build(df)
    assert res["cross_infos"][0].fails[11].startswith("no-entry hour (outside 5.5-23 IST)")
    df2 = _bull(touch_at=(11,)); df2["time"] = DAY0 + 4 * 3600 + np.arange(len(df2)) * 300       # 04:55 server = 07:25 IST -> allowed
    assert build(df2)["cross_infos"][0].entry_index == 11


def test_news_block_60_30():
    df = _bull(touch_at=(11,))
    release = int(df["time"].iloc[11]) - OFF * 3600 + 30 * 60                 # release 30 min after the entry bar (UTC)
    res = J.build(df, dataclasses.replace(RULES, news_times=(release,)), OFF)
    assert res["cross_infos"][0].fails[11] == "news"


# ================================================================== replay exits (the guardian profile, replayed)
def _trade(path_h, path_l, n_extra=0):
    """Bull cross at 5, touch at 6, entry at bar 7 close 4000.5. path_h/path_l: highs/lows from bar 8 on."""
    n = 8 + len(path_h) + n_extra
    g = [-2] * 5 + [2.0] * (n - 5)
    lows = [4002.0] * n; lows[6] = 4001.0; highs = [4001.0] * n; closes = [4000.5] * n
    for k, (h, l) in enumerate(zip(path_h, path_l), start=8):
        highs[k], lows[k] = h, l; closes[k] = (h + l) / 2
    return frame(g, closes=closes, highs=highs, lows=lows)


def test_stop_at_minus_12():
    res = build(_trade([4001, 4001], [3995, 3988.0]))
    j = res["journeys"][0]
    assert j.entry_index == 7 and j.exit_reason == "stop" and j.result == -12.0 and j.exit_index == 9


def test_early_lock_plus_3_then_plus_1():
    res = build(_trade([4003.6, 4002.0], [4000.0, 4001.4]))                 # +3.1 seen on bar 8 -> SL entry+1; bar 9 low 4001.4 = +0.9
    j = res["journeys"][0]
    assert j.lock_index == 8 and j.exit_reason == "early" and j.result == 1.0 and j.exit_index == 9


def test_secure_10_and_ride_unchanged():
    # +10 printed -> SL +10 ; +17 printed -> SL +15 ; then a drop to +14.9 -> secured exit at +15
    res = build(_trade([4010.6, 4017.6, 4018.0], [4004.0, 4012.0, 4015.4]))
    j = res["journeys"][0]
    assert j.target_index == 8 and j.exit_reason == "secured" and j.result == 15.0


def test_ema20_turn_only_after_secured():
    df = _trade([4005.0, 4005.0], [4003.0, 4003.0])                         # +4.5 peak, never +10
    df.loc[9, "close"] = 3999.0                                             # bar 9 closes through EMA20 against the long (SL +1 not hit: low 4003)
    res = build(df)
    j = res["journeys"][0]
    assert j.exit_reason != "ema20_turn"                                     # not secured -> the EMA20 turn does not close it
    df2 = _trade([4011.0, 4016.0, 4016.0], [4005.0, 4012.0, 4012.0])          # +10 on bar 8 -> SL +10 from bar 9
    df2.loc[9:, "ema20"] = 4015.0; df2.loc[9:, "ema50"] = 4013.0
    df2.loc[9, "close"] = 4015.5; df2.loc[10, "close"] = 4013.0                 # bar 10 closes through EMA20, still above the SL (+10)
    j2 = build(df2)["journeys"][0]
    assert j2.exit_reason == "ema20_turn" and j2.exit_index == 10 and j2.result == 12.5


def test_exit_on_next_confirmed_opposite_cross():
    n = 30
    g = [-2] * 5 + [2.0] * 10 + [-0.5, -1.0, -2.0] + [-3.0] * (n - 18)      # bear cross at 15, confirmed at 17 (3 bars + gap 2.0)
    lows = [4002.0] * n; lows[6] = 4001.0
    df = frame(g, closes=[4000.5] * n, lows=lows, highs=[4001.0] * n)
    res = build(df)
    j = res["journeys"][0]
    assert j.direction == "long" and j.exit_reason == "opposite_cross" and j.exit_index == 17
    assert res["cross_infos"][1].confirm_index == 17


def test_multi_opposite_cross_does_not_exit():
    n = 30
    g = [-2] * 5 + [2.0] * 10 + [-0.5, 2.0, 2.0] + [2.0] * (n - 18)          # bear cross at 15 flips back at 16 -> multi, position kept
    lows = [4002.0] * n; lows[6] = 4001.0
    res = build(frame(g, closes=[4000.5] * n, lows=lows, highs=[4001.0] * n))
    assert res["journeys"][0].exit_reason == "open" and res["cross_infos"][1].multi


def test_day_end_closes_open_trade():
    df = _trade([4001.0] * 3, [4000.0] * 3)
    day_end = int(df["time"].iloc[-1]) + 300
    res = J.build(df, RULES, OFF, day_end=day_end)
    assert res["journeys"][0].exit_reason == "day_end" and res["journeys"][0].exit_index == len(df) - 1
    res2 = J.build(df, RULES, OFF, day_end=day_end + 3600)                   # data stops before the day end -> still open
    assert res2["journeys"][0].exit_reason == "open"


def test_one_trade_per_cross():
    res = build(_trade([4003.6, 4002.0, 4001.0, 4001.0], [4000.0, 4001.4, 4001.0, 4001.0]))   # early exit at bar 9, touches after
    assert len(res["journeys"]) == 1 and res["bar_states"][11]["verdict"] == "NO TRADE"


# ================================================================== detect.py rows == the per-bar verdict used by the alert card
def test_detect_rows_agree_with_verdict():
    import detect
    df = _bull(touch_at=(11,))
    res = J.build(df, RULES, OFF); res["df"] = df
    rows = detect.bar_rows(S, res, OFF)
    for i, line in enumerate(rows):
        v = entry_verdict(S, res, i, OFF)
        assert f"{v['decision']}: {v['reason']}" in line
    assert "entry yes · LONG" in rows[11] and "touch yes" in rows[11]


# ================================================================== live guardian (agent + FakeMT5)
@pytest.fixture
def rig(tmp_path, monkeypatch):
    def make(direction="long", entry=4200.0, sl=0.0, price=4200.0):
        fake = FakeMT5(direction, entry, sl, price)
        monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
        cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); n = Cap()
        ag = SymbolAgent("XAUUSD", cfg, S, n, Journal(str(tmp_path)), ())
        return fake, ag, n
    return make


def guard(ag, fake, sgn, close=None, t=0, ef=None, es=None):
    ef = ef if ef is not None else (4190.0 if sgn > 0 else 4180.0); es = es if es is not None else (4180.0 if sgn > 0 else 4190.0)
    n = 200; df = pd.DataFrame({"time": np.arange(n) * 300 + t, "close": np.full(n, fake.price), "high": fake.price, "low": fake.price})
    df["ema20"] = ef; df["ema50"] = es
    ag._guard(True, int(df["time"].iloc[-1]), df, close if close is not None else fake.price, ef, es, sgn)


def test_protect_at_minus_12(rig):
    fake, ag, n = rig(price=4200.5)
    guard(ag, fake, sgn=1)
    assert fake.pos.sl == 4188.0 and any("PROTECTED" in t for t in n.titles)


def test_early_lock_live_never_loosened(rig):
    fake, ag, n = rig(price=4203.2)                                 # +3.2 seen
    guard(ag, fake, sgn=1)
    assert fake.pos.sl == 4201.0 and ag.state[1]["secured"] == 1.0 and any("EARLY LOCK +1" in t for t in n.titles)
    assert any(r["event"] == "early_lock" for r in ag.journal.read(0))
    fake.price = 4201.8; guard(ag, fake, sgn=1, t=300)              # back to +1.8: SL stays at entry+1
    assert fake.pos.sl == 4201.0
    fake.pos.sl = 4203.0; fake.price = 4204.0; guard(ag, fake, sgn=1, t=600)     # a manual tighter SL is never loosened to +1
    assert fake.pos.sl == 4203.0 and ag.state[1]["secured"] == 3.0


def test_secure_and_ride_live_unchanged(rig):
    fake, ag, n = rig(price=4212.0)
    guard(ag, fake, sgn=1); assert fake.pos.sl == 4210.0 and ag.state[1]["secured"] == 10.0
    fake.price = 4218.0; guard(ag, fake, sgn=1, t=300)
    assert fake.pos.sl == 4215.0 and ag.state[1]["secured"] == 15.0


def test_ema20_turn_closes_only_once_secured(rig):
    fake, ag, n = rig(price=4204.0)
    guard(ag, fake, sgn=1, close=4185.0)                             # close below EMA20 (4190) but not secured -> open
    assert not fake.closed
    fake.price = 4212.0; guard(ag, fake, sgn=1, t=300)               # secured +10
    guard(ag, fake, sgn=1, close=4189.0, t=600)                      # bar through EMA20 -> ride over
    assert fake.closed and any("CLOSED" in t for t in n.titles)


def test_no_p_phase_no_timeout_no_slow_follow(rig):
    fake, ag, n = rig(price=4198.0)                                  # lines AGAINST the long (50/80 would call this a P phase)
    for i in range(14):
        guard(ag, fake, sgn=-1, t=i * 300)
    assert not fake.closed and ag.state[1]["pre"] is False           # no P timeout, no pre-stop
    assert fake.pos.sl == 4188.0                                     # protect only: no follow-SL on EMA50, no close through EMA50
    fake.price = 4196.0; guard(ag, fake, sgn=1, t=5000, close=4170.0)   # bar through EMA50 (4180) with lines bullish: no ema_slow_stop
    assert not fake.closed


def test_exit_on_confirmed_opposite_cross_live(rig):
    fake, ag, n = rig(price=4203.0)
    guard(ag, fake, sgn=1)
    ag.last_confirmed_cross = {"bar_time": 199 * 300 + 300, "direction": "SHORT"}
    guard(ag, fake, sgn=-1, t=300)
    assert fake.closed and any("OPPOSITE CROSS" in t for t in n.titles)
    assert any(r["event"] == "exit" and r.get("reason") == "opposite_cross" for r in ag.journal.read(0))


def test_same_side_confirmed_cross_does_not_close(rig):
    fake, ag, n = rig(price=4203.0)
    ag.last_confirmed_cross = {"bar_time": 199 * 300, "direction": "LONG"}
    guard(ag, fake, sgn=1)
    assert not fake.closed


# ================================================================== cards from the live detector
def test_detect_2050_cards(tmp_path):
    cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path); n = Cap()
    ag = SymbolAgent("XAUUSD", cfg, S, n, Journal(str(tmp_path)), ())
    df = _bull(touch_at=(11,))
    for last in (5, 7, 11):
        d = df.iloc[: last + 1].reset_index(drop=True)
        res = J.build(d, RULES, OFF); res["df"] = d
        ag._detect_2050(res, d, int(d["time"].iloc[-1]), float(d["close"].iloc[-1]), float(d["ema20"].iloc[-1]), float(d["ema50"].iloc[-1]))
    assert any("CROSS LONG (UNCONFIRMED)" in t for t in n.titles)
    assert any("CROSS CONFIRMED · LONG" in t for t in n.titles) and ag.last_confirmed_cross["direction"] == "LONG"
    assert any("ENTER LONG (PULLBACK)" in t for t in n.titles)
    body = " ".join(n.bodies)
    for key in ("Cross", "Confirm", "Pre-cross shoot", "Session", "EMA 20", "EMA 50", "Bars since cross"):
        assert key in body
    assert ag.latest_signal["kind"] == "pullback" and any(r["event"] == "signal" and r["signal_kind"] == "pullback" for r in ag.journal.read(0))
    d = _bull(touch_at=(9,), close_at={9: 4006.0}).iloc[:10].reset_index(drop=True)
    res = J.build(d, RULES, OFF); res["df"] = d
    ag._detect_2050(res, d, int(d["time"].iloc[-1]), 4006.0, 4000.0, 3998.0)
    assert any("NO ENTRY (TOO FAR FROM EMA20)" in t for t in n.titles)


# ================================================================== ema5080 untouched
def test_ema5080_guardian_defaults_byte_identical():
    want_xau = {"secure_at": 10.0, "secure_level": 10.0, "pre_stop": 6.0, "ride_step": 5.0, "pre_timeout_bars": 12, "news_flat_min": 15,
                "ema_slow_sl_buffer": 1.0, "enabled": True, "note": "Gold profile",
                "early_at": None, "early_level": 0.0, "p_phase": True, "slow_ema_sl": True, "exit_on_confirmed_cross": False}
    want_xag = {"secure_at": 0.15, "secure_level": 0.15, "pre_stop": 0.08, "ride_step": 0.08, "pre_timeout_bars": 12, "news_flat_min": 15,
                "ema_slow_sl_buffer": 0.02, "enabled": False,
                "note": "Silver profile — EXPERIMENTAL, disabled until validated (enable with --enable-silver)",
                "early_at": None, "early_level": 0.0, "p_phase": True, "slow_ema_sl": True, "exit_on_confirmed_cross": False}
    assert dataclasses.asdict(S5080.GUARDIANS["XAU"]) == want_xau and dataclasses.asdict(S5080.GUARDIANS["XAG"]) == want_xag
    assert dataclasses.asdict(Guardian(1, 1, 1, 1)) == {"secure_at": 1, "secure_level": 1, "pre_stop": 1, "ride_step": 1, "pre_timeout_bars": 12,
                                                        "news_flat_min": 15, "ema_slow_sl_buffer": 0.0, "enabled": True, "note": "",
                                                        "early_at": None, "early_level": 0.0, "p_phase": True, "slow_ema_sl": True,
                                                        "exit_on_confirmed_cross": False}


def test_ema2050_profile_values():
    g = S.guardian_for("XAUUSD")
    assert (g.pre_stop, g.early_at, g.early_level, g.secure_at, g.secure_level, g.ride_step, g.ema_slow_sl_buffer) == (12.0, 3.0, 1.0, 10.0, 10.0, 5.0, 0.0)
    assert g.p_phase is False and g.slow_ema_sl is False and g.exit_on_confirmed_cross is True and g.enabled
    assert S.guardian_for("XAGUSD").enabled is False
    assert RULES.reentry_max == 0 and RULES.skip_sessions == () and RULES.m15_align is False and RULES.min_slope50 == 0.0
    assert RULES.max_extension == 0 and RULES.max_extension_atr == 0 and RULES.min_shoot == 0 and RULES.guardian is g
