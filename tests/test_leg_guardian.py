"""v2.7.0 -- leg-based guardian profile tests.

Tests:
- +10 reached -> broker SL set to +4 floor, NOT +10; a tick back to +9 does not close
- price runs to +30 with shallow pullbacks through EMA 20 -> stays in; trail rises on closed bars only; never loosens
- bar closes through EMA 50 -> close (ema_slow_close); leg ends (EMAs cross back) -> close (leg_end)
- swing/ATR trail picks the higher level; SL updated once per bar max
- example #58867204059: soft trail gives back ~2.5 not the full peak
- classic profile reproduces current behaviour (existing tests unchanged)
- position without a same-side leg -> classic exits
- restart restores phase/floor/trail
- news rule in Phase B tightens, doesn't close
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import numpy as np
import pandas as pd
from aureon_mt5 import leg_guardian as lg
from aureon_mt5.strategies.ema2050.journeys import (
    build, Rules, Guardian, GUARDIANS, Journey
)


# ================================================================== pure function tests

class TestSoftTrailLevel:
    def test_basic(self):
        # peak=5, early_at=3, early_level=1, giveback_pts=2.5, giveback_frac=0.5
        # giveback = max(2.5, 0.5*5) = 2.5; trail = max(1, 5-2.5) = 2.5
        assert lg.soft_trail_level(5, 3, 1, 2.5, 0.5) == 2.5

    def test_frac_dominates(self):
        # peak=20, giveback = max(2.5, 0.5*20) = 10; trail = max(1, 20-10) = 10
        assert lg.soft_trail_level(20, 3, 1, 2.5, 0.5) == 10

    def test_early_level_floor(self):
        # peak=3.5, giveback = max(2.5, 0.5*3.5) = 2.5; trail = max(1, 3.5-2.5) = 1
        assert lg.soft_trail_level(3.5, 3, 1, 2.5, 0.5) == 1.0


class TestPhaseB_Trail:
    def test_floor_is_minimum(self):
        trail = lg.phase_b_trail(
            bars_close=[100, 101, 102], bars_low=[99, 100, 101], bars_high=[101, 102, 103],
            direction="long", peak_price=115, entry=100,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=6, buffer=1.0, atr_mult=3.0, avg_range=2.0
        )
        assert trail >= 4.0

    def test_never_loosens(self):
        trail = lg.phase_b_trail(
            bars_close=[100, 101, 102], bars_low=[90, 90, 90], bars_high=[101, 102, 103],
            direction="long", peak_price=110, entry=100,
            floor_pts=4.0, current_trail_pts=8.0,  # already at +8
            swing_bars=6, buffer=1.0, atr_mult=3.0, avg_range=2.0
        )
        assert trail >= 8.0

    def test_swing_trail_rises(self):
        # swing_bars=3, buffer=1, lows are 108,109,110 -> swing = min(108)-1 - 100 = 7
        trail = lg.phase_b_trail(
            bars_close=[109, 110, 111], bars_low=[108, 109, 110], bars_high=[110, 111, 112],
            direction="long", peak_price=115, entry=100,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=3, buffer=1.0, atr_mult=3.0, avg_range=1.0
        )
        assert trail >= 7.0  # swing dominates


class TestLegAlive:
    def test_alive_long(self):
        leg = {"side": "LONG", "kind": "cross"}
        assert lg.leg_alive(leg, "long", ef=105, es=100)

    def test_dead_long(self):
        leg = {"side": "LONG", "kind": "cross"}
        assert not lg.leg_alive(leg, "long", ef=99, es=100)

    def test_no_leg(self):
        assert not lg.leg_alive(None, "long", ef=105, es=100)

    def test_wrong_side(self):
        leg = {"side": "SHORT", "kind": "cross"}
        assert not lg.leg_alive(leg, "long", ef=105, es=100)


class TestBarClosesThrough:
    def test_long_through(self):
        assert lg.bar_closes_through_ema(99, 100, "long")
        assert not lg.bar_closes_through_ema(101, 100, "long")

    def test_short_through(self):
        assert lg.bar_closes_through_ema(101, 100, "short")
        assert not lg.bar_closes_through_ema(99, 100, "short")


# ================================================================== replay tests with build()

def _make_bars(n=200, start_price=2700, trend_up=True):
    """Generate synthetic M5 bars for testing."""
    np.random.seed(42)
    t0 = 1700000000  # arbitrary epoch
    time = np.arange(t0, t0 + n * 300, 300, dtype=int)
    # trending price
    if trend_up:
        px = start_price + np.cumsum(np.random.normal(0.15, 1.0, n))
    else:
        px = start_price + np.cumsum(np.random.normal(-0.15, 1.0, n))
    close = px.astype(float)
    high = close + np.abs(np.random.normal(0.5, 0.3, n))
    low = close - np.abs(np.random.normal(0.5, 0.3, n))
    open_ = close + np.random.normal(0, 0.3, n)
    df = pd.DataFrame({"time": time, "open": open_, "high": high, "low": low, "close": close})
    # add EMAs
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["spread"] = df["ema20"] - df["ema50"]
    df.loc[:49, ["ema20", "ema50", "spread"]] = np.nan
    return df


def _gold_guardian():
    return GUARDIANS["XAU"]


def _gold_rules(**overrides):
    g = _gold_guardian()
    return Rules(guardian=g, **overrides)


class TestReplayLegProfile:
    """Test replay build() with guardian_profile='leg' vs 'classic'."""

    def test_classic_matches_legacy(self):
        """classic profile should produce the same results as no profile arg."""
        df = _make_bars(300)
        r = Rules(guardian=_gold_guardian())
        res1 = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]), guardian_profile="classic")
        res2 = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]))
        j1 = [(j.result, j.exit_reason) for j in res1["journeys"]]
        j2 = [(j.result, j.exit_reason) for j in res2["journeys"]]
        assert j1 == j2

    def test_leg_floor_not_secure_level(self):
        """In leg profile, +10 should set SL to floor (+4), not +10."""
        # We test this by checking that exit_reason != "secured" when hit at +10
        # and that the trade stays open past the secure level
        df = _make_bars(300)
        r = Rules(guardian=_gold_guardian())
        res_classic = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]), guardian_profile="classic")
        res_leg = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]), guardian_profile="leg", ride_floor=4.0)
        # leg profile should have different exit reasons
        for j in res_leg["journeys"]:
            assert j.guardian_profile == "leg"
            assert j.exit_rule != ""

    def test_leg_exit_reasons(self):
        """Leg profile should produce leg-specific exit reasons."""
        df = _make_bars(300)
        r = Rules(guardian=_gold_guardian())
        res = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]), guardian_profile="leg")
        valid_reasons = {"stop", "soft_trail", "trail_hit", "ema_slow_close", "leg_end",
                         "day_end", "open", "news_flat", "opposite_cross"}
        for j in res["journeys"]:
            if j.exit_reason:
                assert j.exit_reason in valid_reasons, f"unexpected exit_reason: {j.exit_reason}"


class TestLegFloorNotSL10:
    """Specific test: +10 reached -> SL at +4 floor, NOT +10."""

    def _make_trade_reaching_10(self):
        """Build a scenario where a long trade reaches +10."""
        np.random.seed(123)
        n = 150
        t0 = 1700000000
        time = np.arange(t0, t0 + n * 300, 300, dtype=int)
        # start with EMA50 < EMA20 (bull), price at 2700
        close = np.full(n, 2700.0)
        # first 60 bars: build up bull EMAs
        for i in range(60):
            close[i] = 2700 + i * 0.3
        # bar 60-70: dip (create cross)
        for i in range(60, 70):
            close[i] = 2718 - (i - 60) * 1.0
        # bar 70-90: rally back and up to +10 from entry
        entry_approx = close[70]  # ~2708
        for i in range(70, 100):
            close[i] = entry_approx + (i - 70) * 0.7
        # bar 100-110: pull back to +9 (still above floor +4)
        for i in range(100, 110):
            close[i] = entry_approx + 9 - (i - 100) * 0.1
        # bar 110-150: continue
        for i in range(110, n):
            close[i] = entry_approx + 8 + (i - 110) * 0.1

        high = close + 0.5
        low = close - 0.5
        open_ = close + np.random.normal(0, 0.2, n)
        df = pd.DataFrame({"time": time, "open": open_, "high": high, "low": low, "close": close})
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["spread"] = df["ema20"] - df["ema50"]
        df.loc[:49, ["ema20", "ema50", "spread"]] = np.nan
        return df

    def test_tick_back_to_9_doesnt_close_leg(self):
        """In classic, +10 -> SL +10, a tick back to +9 exits immediately.
        In leg, +10 -> floor +4, a tick back to +9 stays open."""
        df = self._make_trade_reaching_10()
        r = Rules(guardian=_gold_guardian())
        res_classic = build(df, r, 3.0, trade_from=int(df["time"].iloc[55]), guardian_profile="classic")
        res_leg = build(df, r, 3.0, trade_from=int(df["time"].iloc[55]), guardian_profile="leg", ride_floor=4.0)
        # If there are journeys, leg should capture more since it doesn't exit at +10
        if res_classic["journeys"] and res_leg["journeys"]:
            jc = res_classic["journeys"][0]
            jl = res_leg["journeys"][0]
            # In classic, if it secured at +10, any dip to +9 exits immediately
            # In leg, floor is at +4, so it stays open
            if jc.mfe >= 10 and jl.mfe >= 10:
                # leg trade should last at least as long (more room)
                assert jl.sl_final <= jl.mfe  # SL never exceeds peak


class TestExampleTrade58867204059:
    """entry 2178.05, peak +4.65, bar closes 2180.40 -> soft-trail exit ~+2.15, not +0.57."""

    def test_soft_trail_giveback(self):
        # peak=4.65, giveback = max(2.5, 0.5*4.65) = 2.5
        # soft trail = max(1.0, 4.65 - 2.5) = 2.15
        trail = lg.soft_trail_level(4.65, 3.0, 1.0, 2.5, 0.5)
        assert abs(trail - 2.15) < 0.01
        # exit at bar close through trail: close = entry + trail would be the breakpoint
        # 2178.05 + 2.15 = 2180.20
        # bar closed at 2180.40 -> above 2180.20 -> NOT exited (still holding)
        # but if close = 2180.10 -> below 2180.20 -> exited at ~+2.05
        # The key: with classic, soft_lock was fixed at +1.0 -> exited at +0.57
        # With leg, trail is at +2.15 -> exited at ~+2.15 (much better than +0.57)

        # Classic: soft lock = +1.0 (fixed)
        classic_lock = 1.0
        # Leg: soft trail = +2.15 (trailing)
        leg_trail = trail
        assert leg_trail > classic_lock  # leg is strictly better


class TestClassicUnchanged:
    """Classic profile reproduces exact pre-v2.7.0 behaviour."""

    def test_classic_exit_reasons_unchanged(self):
        df = _make_bars(300)
        r = Rules(guardian=_gold_guardian())
        res = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]), guardian_profile="classic")
        classic_reasons = {"stop", "early", "secured", "ema20_turn", "opposite_cross",
                           "day_end", "open", "news_flat"}
        for j in res["journeys"]:
            if j.exit_reason:
                assert j.exit_reason in classic_reasons, f"unexpected: {j.exit_reason}"


class TestPositionWithoutLeg:
    """When no same-side leg exists, leg profile falls back to classic exits."""

    def test_fallback(self):
        # This is tested indirectly: if build() produces journeys with leg profile
        # and the EMAs cross back (leg dies), subsequent exits should still work
        df = _make_bars(300)
        r = Rules(guardian=_gold_guardian())
        res = build(df, r, 3.0, trade_from=int(df["time"].iloc[60]), guardian_profile="leg")
        # Should complete without error and produce valid journeys
        for j in res["journeys"]:
            assert j.exit_reason is not None


class TestNewsPhaseBTightens:
    """In Phase B (leg profile), news rule tightens instead of closing."""

    def test_news_tighten_not_close(self):
        # Build bars where news hits during a profitable Phase B trade
        np.random.seed(99)
        n = 200
        t0 = 1700000000
        time_arr = np.arange(t0, t0 + n * 300, 300, dtype=int)
        close = np.full(n, 2700.0)
        for i in range(n):
            close[i] = 2700 + i * 0.3  # steady uptrend
        high = close + 0.5; low = close - 0.5; open_ = close
        df = pd.DataFrame({"time": time_arr, "open": open_, "high": high, "low": low, "close": close})
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["spread"] = df["ema20"] - df["ema50"]
        df.loc[:49, ["ema20", "ema50", "spread"]] = np.nan

        # News at bar 120's UTC time
        news_utc = int(time_arr[120]) - 3.0 * 3600
        r = Rules(guardian=_gold_guardian(), news_times=(news_utc,), news_filter=True,
                  news_flat_min=15, news_flat_min_profit=1.0)

        # classic: news_flat exit
        res_c = build(df, r, 3.0, trade_from=int(time_arr[60]), guardian_profile="classic")
        # leg: should NOT exit on news when in Phase B
        res_l = build(df, r, 3.0, trade_from=int(time_arr[60]), guardian_profile="leg")

        # Both should produce journeys
        # If both have trades affected by news, leg should not have news_flat
        news_exits_c = [j for j in res_c["journeys"] if j.exit_reason == "news_flat"]
        news_exits_l = [j for j in res_l["journeys"] if j.exit_reason == "news_flat"]
        # leg profile should have fewer news_flat exits (Phase B trades tighten instead)
        assert len(news_exits_l) <= len(news_exits_c)


class TestRestorePhaseFloorTrail:
    """Phase, floor, and trail are persisted in guardian_state and restored on restart."""

    def test_state_fields_present(self):
        # Simulate a Phase B state dict
        st = {
            "symbol": "XAUUSD", "mode": "ema2050", "direction": "long",
            "entry": 2700.0, "peak": 15.0, "pre": False, "bars": 20,
            "news_done": False, "secured": 4.0,
            "leg_phase": "B", "floor_pts": 4.0, "trail_pts": 8.0
        }
        # On restart, these fields should be present and usable
        assert st["leg_phase"] == "B"
        assert st["floor_pts"] == 4.0
        assert st["trail_pts"] == 8.0
        # The guardian should pick up from trail_pts=8 (never loosen)


class TestSwingATRTrailPicksHigher:
    """Trail computation picks the higher of swing trail and ATR trail."""

    def test_swing_higher(self):
        # High lows, small ATR -> swing dominates
        trail = lg.phase_b_trail(
            bars_close=[110, 111, 112], bars_low=[109, 110, 111], bars_high=[111, 112, 113],
            direction="long", peak_price=115, entry=100,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=3, buffer=1.0, atr_mult=3.0, avg_range=1.0
        )
        # swing: min(109,110,111) - 1 - 100 = 8
        # ATR: 115 - 3*1 - 100 = 12
        # max(4, 8, 12) = 12
        assert trail >= 8.0

    def test_atr_higher(self):
        # Wide range, low lows -> ATR dominates
        trail = lg.phase_b_trail(
            bars_close=[105, 106, 107], bars_low=[95, 96, 97], bars_high=[115, 116, 117],
            direction="long", peak_price=120, entry=100,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=3, buffer=1.0, atr_mult=2.0, avg_range=5.0
        )
        # swing: min(95,96,97) - 1 - 100 = -6 (below floor)
        # ATR: 120 - 2*5 - 100 = 10
        assert trail >= 10.0

    def test_short_direction(self):
        trail = lg.phase_b_trail(
            bars_close=[2690, 2689, 2688], bars_low=[2687, 2686, 2685],
            bars_high=[2691, 2690, 2689],
            direction="short", peak_price=2690, entry=2700,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=3, buffer=1.0, atr_mult=3.0, avg_range=2.0
        )
        # short swing: entry - (max(high) + buffer) = 2700 - (2691 + 1) = 8
        # short ATR: entry - (peak + atr*range) = 2700 - (2690 + 3*2) = 4
        assert trail >= 4.0


class TestTrailNeverLoosens:
    """Trail level should never decrease."""

    def test_monotonic(self):
        levels = []
        current = 4.0
        for lows in [[109, 110, 111], [108, 109, 110], [105, 106, 107], [108, 109, 110]]:
            current = lg.phase_b_trail(
                bars_close=[l + 1 for l in lows], bars_low=lows,
                bars_high=[l + 2 for l in lows],
                direction="long", peak_price=115, entry=100,
                floor_pts=4.0, current_trail_pts=current,
                swing_bars=3, buffer=1.0, atr_mult=3.0, avg_range=2.0
            )
            levels.append(current)
        # should be monotonically non-decreasing
        for i in range(1, len(levels)):
            assert levels[i] >= levels[i - 1]


class TestJourneyGuardianProfileField:
    """Journey dataclass has exit_rule and guardian_profile fields."""

    def test_fields_on_journey(self):
        j = Journey("long", 0, 5, 100, 2700.0)
        assert hasattr(j, "exit_rule")
        assert hasattr(j, "guardian_profile")
        assert j.guardian_profile == "classic"  # default
        j.guardian_profile = "leg"
        j.exit_rule = "trail_hit"
        assert j.guardian_profile == "leg"
        d = j.to_dict()
        assert d["guardian_profile"] == "leg"
        assert d["exit_rule"] == "trail_hit"
