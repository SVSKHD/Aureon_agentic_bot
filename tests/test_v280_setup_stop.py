"""v2.8.0 Part 3 — the stop is the setup (AUREON_SETUP_STOP). Pure functions + the guardian with a fake MT5."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import setup as su
from tests.test_v181 import harness  # noqa: F401  (fixture)
from tests.fixtures.replay_20261015 import replay


def bars(closes, ef, es, lows=None, rng=1.0, t0=0, n_pad=150):
    closes = [closes[0]] * n_pad + list(closes)
    n = len(closes); c = np.array(closes, float)
    ef = np.full(n, ef, float) if np.isscalar(ef) else np.concatenate([np.full(n_pad, ef[0]), ef])
    es = np.full(n, es, float) if np.isscalar(es) else np.concatenate([np.full(n_pad, es[0]), es])
    low = c - rng
    if lows is not None:
        low[-len(lows):] = lows
    return pd.DataFrame({"time": t0 + np.arange(n) * 300, "open": c, "high": c + rng, "low": low, "close": c,
                         "ema_fast": ef, "ema_slow": es})


def step(ag, fake, df, m15="BULLISH"):
    ag._m15_state = lambda: m15
    ef, es = float(df["ema_fast"].iloc[-1]), float(df["ema_slow"].iloc[-1])
    fake.price = float(df["close"].iloc[-1]); ag.last_bar = int(df["time"].iloc[-1])
    ag._guard(True, int(df["time"].iloc[-1]), df, fake.price, ef, es, 1 if ef > es else -1)


def jr(ag, ev):
    return [r for r in ag.journal.read(0) if r["event"] == ev]


# ------------------------------------------------------------------ pure functions
def test_checks_and_alive():
    c = su.setup_checks("long", 4102.3, 4099.8, 4098.7, 1.0, [4101.0, 4100.0, 4099.5], [4104.0] * 3, 6, "BULLISH")
    assert c == {"ema_order": True, "ema80_close": True, "m15": True, "swing": True} and su.setup_alive(c)
    c = su.setup_checks("long", 4097.0, 4099.8, 4098.7, 1.0, [4099.5], [4104.0], 6, None)
    assert c["ema80_close"] is False and c["m15"] is None and c["swing"] is False and not su.setup_alive(c)
    assert su.failed_checks(c) == ["ema80_close", "swing"]
    assert su.setup_alive({"m15": None, "swing": None})


def test_structure_stop_pts_swing_and_cap():
    assert su.structure_stop_pts("long", 4103.5, [4101, 4100, 4099.0, 4100.5], [4105] * 4, 6, 1.0, 10.0, -10.0) == pytest.approx(-5.5)
    assert su.structure_stop_pts("long", 4103.5, [4090.0], [4105], 6, 1.0, 10.0, -10.0) == pytest.approx(-10.0)
    assert su.structure_stop_pts("short", 4100.0, [4095], [4102.0], 6, 1.0, 10.0, -10.0) == pytest.approx(-3.0)
    assert su.structure_stop_pts("long", 4103.5, [4090.0], [4105], 6, 1.0, 10.0, -4.0) == pytest.approx(-4.0)   # never looser
    assert su.cap_points(10, 0, 3.0) == 10 and su.cap_points(10, 5, 3.0) == 15 and su.cap_points(10, 2, 3.0) == 10


# ------------------------------------------------------------------ 1. default is v2.7.2
def test_default_initial_stop_is_pre_stop(harness):
    fake, ag, _ = harness(entry=4103.5, price=4103.5)
    assert ag.cfg.setup_stop is False
    step(ag, fake, bars([4103.5] * 10, 4101.0, 4100.0, lows=[4101, 4100, 4099.0, 4100.5, 4101, 4102]))
    assert jr(ag, "protected")[0]["sl"] == pytest.approx(4097.5) and "setup" not in ag.state[1]   # (then FOLLOW EMA80 tightens)


# ------------------------------------------------------------------ 2. initial structure stop
@pytest.mark.parametrize("low,want", [(4099.0, 4098.0), (4090.0, 4093.5)])
def test_initial_structure_stop(harness, low, want):
    fake, ag, _ = harness(entry=4103.5, price=4103.5); ag.cfg.setup_stop = True
    step(ag, fake, bars([4103.5] * 10, 4101.0, 4100.0, lows=[4101, 4100, low, 4100.5, 4101, 4102]))
    assert jr(ag, "protected")[0]["sl"] == pytest.approx(want)                 # PROTECT level (FOLLOW EMA80 may tighten after)
    assert fake.pos.sl >= want - 1e-9


# ------------------------------------------------------------------ 3. slow pullback in the P phase: held, timeout paused
def test_slow_pullback_holds_and_pauses_timeout(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    closes = [4200.0]; ef = [4185.0]; es = 4190.0
    step(ag, fake, bars(closes, ef, es, rng=3.0))              # P long: EMA fast still below slow
    assert ag.state[1]["pre"]
    for i in range(1, 14):
        closes.append(max(4196.0, 4200.0 - 0.5 * i)); ef.append(4185.0 + 0.3 * i)    # drift to −4, lines converging
        step(ag, fake, bars(closes, np.array(ef), es, rng=3.0, t0=0))
        assert not fake.closed, f"closed at bar {i}"
    st = ag.state[1]
    assert st["bars"] >= 12 and st["setup_alive"] and st["setup"]["ema_order"] is None
    assert len(jr(ag, "pre_timeout_paused")) == 1 and not [r for r in jr(ag, "exit") if r["reason"] == "pre_timeout"]


def test_timeout_fires_when_lines_stop_converging(harness):
    fake, ag, _ = harness(entry=4200.0, price=4199.0); ag.cfg.setup_stop = True
    closes = [4199.0]; ef = [4185.0]
    for i in range(14):
        closes.append(4199.0); ef.append(4185.0 - 0.01 * i)        # gap widening slowly (no 3-bar separation abort: flat)
        ef[-1] = 4185.0 if i % 2 else 4184.99
        step(ag, fake, bars(closes, np.array(ef), 4190.0, rng=3.0))
        if fake.closed:
            break
    assert fake.closed and jr(ag, "exit")[-1]["reason"] in ("pre_timeout", "pre_abort")


# ------------------------------------------------------------------ 4. invalidation on the close
def test_close_under_ema80_buffer_invalidates(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    step(ag, fake, bars([4200.0], 4190.0, 4180.0, rng=15.0))                 # post-cross long
    step(ag, fake, bars([4200.0, 4178.5], 4190.0, 4180.0, rng=15.0))         # close 4178.5 < 4180 − 1
    ex = jr(ag, "exit")[-1]
    assert fake.closed and ex["reason"] == "setup_invalidated" and "ema80_close" in jr(ag, "setup_invalidated")[-1]["failed"]


def test_close_under_ema80_but_inside_buffer_is_a_wick(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    step(ag, fake, bars([4200.0], 4190.0, 4180.0, rng=25.0))
    step(ag, fake, bars([4200.0, 4179.5], 4190.0, 4180.0, rng=25.0))          # 4179.5 > 4180 − 1, swing intact: alive
    assert not jr(ag, "setup_invalidated")      # (the classic ema_slow_stop rule — no buffer — still applies after the cross)


def test_ema_order_flip_invalidates_and_names_it(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    step(ag, fake, bars([4200.0], 4190.0, 4180.0, rng=5.0))
    step(ag, fake, bars([4200.0, 4200.0], [4190.0, 4179.0], 4180.0, rng=5.0))
    assert fake.closed and jr(ag, "exit")[-1]["reason"] == "setup_invalidated"
    assert jr(ag, "setup_invalidated")[-1]["failed"] == ["ema_order"]


def test_m15_none_never_fails(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    step(ag, fake, bars([4200.0], 4190.0, 4180.0, rng=5.0), m15=None)
    assert ag.state[1]["setup"]["m15"] is None and ag.state[1]["setup_alive"] and not fake.closed
    step(ag, fake, bars([4200.0, 4200.0], 4190.0, 4180.0, rng=5.0, t0=300), m15="BEARISH")
    assert fake.closed and jr(ag, "setup_invalidated")[-1]["failed"] == ["m15"]


# ------------------------------------------------------------------ 5. structure stop only tightens
def test_structure_stop_only_tightens(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    closes = [4200.0]; lows = [4196.0] * 6
    step(ag, fake, bars(closes, 4185.0, 4190.0, lows=lows))       # P phase (no FOLLOW EMA80), SL at 4195
    assert fake.pos.sl == pytest.approx(4195.0)
    step(ag, fake, bars(closes + [4200.0], 4185.0, 4190.0, lows=[4198.0] * 6))   # swing low rises → SL rises
    assert fake.pos.sl == pytest.approx(4197.0) and jr(ag, "structure_sl")[-1]["level"] == pytest.approx(4197.0)
    step(ag, fake, bars(closes + [4200.0] * 2, 4185.0, 4190.0, lows=[4192.0] * 6))   # swing low falls → unchanged
    assert fake.pos.sl == pytest.approx(4197.0)


# ------------------------------------------------------------------ 6. hand-over to the staircase
def test_handover_after_secure(harness):
    fake, ag, _ = harness(entry=4200.0, price=4200.0); ag.cfg.setup_stop = True
    step(ag, fake, bars([4200.0], 4190.0, 4180.0, lows=[4197.0] * 6))
    step(ag, fake, bars([4200.0, 4212.0], 4190.0, 4180.0, lows=[4197.0] * 6))       # +12 → SECURE +10
    assert ag.state[1]["secured"] == 10.0 and fake.pos.sl == pytest.approx(4210.0)
    n = len(jr(ag, "structure_sl"))
    step(ag, fake, bars([4200.0, 4212.0, 4213.0], 4190.0, 4180.0, lows=[4212.5] * 6))  # swing above +10: not recomputed
    assert fake.pos.sl == pytest.approx(4210.0) and len(jr(ag, "structure_sl")) == n


# ------------------------------------------------------------------ 7. the 15-Oct trade (SYNTHETIC bars, see fixtures/)
def test_15_oct_replay_documented():
    base = replay()
    assert base["exit"]["time"] == "02:05" and base["exit"]["reason"] == "p_flip" and base["exit"]["points"] == pytest.approx(-1.2)
    held = replay(flip_require_structure=True)
    assert held["exit"] is None or held["exit"]["reason"] != "p_flip"
    assert held["worst_close"] == pytest.approx(-3.0)
    # AUREON_SETUP_STOP=1 as specified: the structure stop is recomputed every bar while under water and follows the
    # 6-bar swing low UP during the grind into the entry; the 01:55 pullback low touches it. The brief expected "held, worst
    # close ≈ −3"; on these bars it is stopped at −3.8. Kept as built, flagged in the CHANGELOG — see the v2.8.0 notes.
    ss = replay(setup_stop=True)
    assert ss["initial_sl"] == pytest.approx(4098.85)
    assert ss["exit"]["reason"] == "sl_hit" and ss["exit"]["time"] == "01:55" and ss["exit"]["points"] == pytest.approx(-3.83, abs=0.05)
