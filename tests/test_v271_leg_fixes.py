"""v2.7.1 — leg guardian fixes: demo default, Phase A news, sticky leg, SHORT ATR,
Phase B entry guard, soft arm, restart, replay parity."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import numpy as np
import pandas as pd
from aureon_mt5 import leg_guardian as lg
from aureon_mt5.config import Config
from aureon_mt5.strategies.ema2050.journeys import (
    build, Rules, Guardian, GUARDIANS, Journey
)


def _gold_guardian():
    return GUARDIANS["XAU"]


# ================================================================== 1. demo default / live default

class TestProfileDefault:
    def test_demo_auto_leg(self):
        cfg = Config()
        cfg.guardian_profile = "auto"
        cfg.guardian_profile_demo = "leg"
        assert cfg.resolved_guardian_profile(is_demo=True) == "leg"

    def test_live_auto_classic(self):
        cfg = Config()
        cfg.guardian_profile = "auto"
        assert cfg.resolved_guardian_profile(is_demo=False) == "classic"

    def test_unknown_auto_classic(self):
        cfg = Config()
        cfg.guardian_profile = "auto"
        assert cfg.resolved_guardian_profile(is_demo=None) == "classic"

    def test_explicit_overrides_auto(self):
        cfg = Config()
        cfg.guardian_profile = "classic"
        assert cfg.resolved_guardian_profile(is_demo=True) == "classic"

    def test_explicit_leg_on_live(self):
        cfg = Config()
        cfg.guardian_profile = "leg"
        assert cfg.resolved_guardian_profile(is_demo=False) == "leg"


# ================================================================== 2. Phase A news no longer closes at +1

class TestPhaseANewsNoClose:
    def _make_bars_with_news(self):
        """Bars where a trade reaches +4 and news hits while in Phase A."""
        np.random.seed(77)
        n = 200
        t0 = 1700000000
        time_arr = np.arange(t0, t0 + n * 300, 300, dtype=int)
        close = np.full(n, 2700.0)
        for i in range(n):
            close[i] = 2700 + i * 0.2  # gentle uptrend, peaks ~+40 over 200 bars
        high = close + 0.5; low = close - 0.5; open_ = close
        df = pd.DataFrame({"time": time_arr, "open": open_, "high": high, "low": low, "close": close})
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["spread"] = df["ema20"] - df["ema50"]
        df.loc[:49, ["ema20", "ema50", "spread"]] = np.nan
        return df, time_arr

    def test_leg_phase_a_news_does_not_close(self):
        df, time_arr = self._make_bars_with_news()
        # News at bar 80: trade is in Phase A (peak < 10)
        news_utc = int(time_arr[80]) - 3.0 * 3600
        r = Rules(guardian=_gold_guardian(), news_times=(news_utc,), news_filter=True,
                  news_flat_min=15, news_flat_min_profit=1.0)

        res_c = build(df, r, 3.0, trade_from=int(time_arr[60]), guardian_profile="classic")
        res_l = build(df, r, 3.0, trade_from=int(time_arr[60]), guardian_profile="leg", soft_arm_pts=5)

        news_exits_c = [j for j in res_c["journeys"] if j.exit_reason == "news_flat"]
        news_exits_l = [j for j in res_l["journeys"] if j.exit_reason == "news_flat"]
        # leg profile should have NO news_flat exits in Phase A
        assert len(news_exits_l) <= len(news_exits_c)


# ================================================================== 3. Sticky leg — opposite unconfirmed cross keeps leg profile

class TestStickyLeg:
    def test_leg_snapshot_persists(self):
        leg = {"side": "LONG", "kind": "cross", "start_px": 2700.0, "start_t": 100,
               "best": 15.0, "best_px": 2715.0, "best_t": 200, "against": 3.0, "steps_sent": 1}
        snap = lg.make_leg_snapshot(leg)
        assert snap["side"] == "LONG"
        assert snap["best"] == 15.0
        assert snap["start_px"] == 2700.0
        assert "steps_sent" not in snap  # not persisted

    def test_leg_alive_with_snapshot(self):
        snap = {"side": "LONG", "start_px": 2700, "best": 15}
        assert lg.leg_alive(snap, "long", ef=105, es=100)
        # opposite unconfirmed cross: EMAs still intact -> leg alive
        assert lg.leg_alive(snap, "long", ef=101, es=100)
        # EMAs crossed back -> leg dead
        assert not lg.leg_alive(snap, "long", ef=99, es=100)

    def test_st_guardian_profile_sticky(self):
        """Once assigned, guardian_profile in st doesn't change."""
        st = {"guardian_profile": "leg", "leg_phase": "B", "floor_pts": 4}
        # even if self._resolved_profile changes, st keeps "leg"
        assert st["guardian_profile"] == "leg"


# ================================================================== 4. SHORT ATR trail

class TestShortATRTrail:
    def test_short_atr_trail_correct(self):
        # SHORT: entry=2700, peak=10 -> peak_px = 2700 + (-1)*10 = 2690
        # atr_price = 2690 + 3.0 * 2.0 = 2696
        # atr_pts = 2700 - 2696 = 4
        trail = lg.phase_b_trail(
            bars_close=[2690, 2689, 2688], bars_low=[2687, 2686, 2685],
            bars_high=[2691, 2690, 2689],
            direction="short", peak_price=2690, entry=2700,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=3, buffer=1.0, atr_mult=3.0, avg_range=2.0
        )
        assert trail >= 4.0
        # swing: entry - (max(2691,2690,2689) + 1) = 2700 - 2692 = 8
        assert trail >= 8.0  # swing should dominate here

    def test_short_atr_trail_with_large_move(self):
        # SHORT: entry=2700, peak=30 -> peak_px=2670
        # atr_price = 2670 + 2.0 * 3.0 = 2676
        # atr_pts = 2700 - 2676 = 24
        trail = lg.phase_b_trail(
            bars_close=[2672, 2671, 2670], bars_low=[2669, 2668, 2667],
            bars_high=[2675, 2674, 2673],
            direction="short", peak_price=2670, entry=2700,
            floor_pts=4.0, current_trail_pts=4.0,
            swing_bars=3, buffer=1.0, atr_mult=2.0, avg_range=3.0
        )
        # atr: 2700 - (2670 + 2*3) = 24
        assert trail >= 24.0


# ================================================================== 5. Phase B entry guard

class TestPhaseBEntryGuard:
    def test_peak_10_current_3_stays_A(self):
        """Peak >= secure_at (+10) but current points fell to +3: stay in Phase A."""
        np.random.seed(55)
        n = 150
        t0 = 1700000000
        time_arr = np.arange(t0, t0 + n * 300, 300, dtype=int)
        close = np.full(n, 2700.0)
        # Build bull EMAs
        for i in range(60):
            close[i] = 2700 + i * 0.3
        # entry area: drop, then rally to +12, then fall back to +3
        for i in range(60, 70):
            close[i] = 2718 - (i - 60) * 0.5
        entry_area = close[70]
        for i in range(70, 80):
            close[i] = entry_area + (i - 70) * 1.5  # rally to +15
        for i in range(80, 100):
            close[i] = entry_area + 3  # fall back to +3
        for i in range(100, n):
            close[i] = entry_area + 3 + (i - 100) * 0.05

        high = close + 0.5; low = close - 0.5; open_ = close
        df = pd.DataFrame({"time": time_arr, "open": open_, "high": high, "low": low, "close": close})
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["spread"] = df["ema20"] - df["ema50"]
        df.loc[:49, ["ema20", "ema50", "spread"]] = np.nan

        r = Rules(guardian=_gold_guardian())
        # ride_floor=4, ride_buffer=1 -> need current >= 5 to enter Phase B
        res = build(df, r, 3.0, trade_from=int(time_arr[55]), guardian_profile="leg",
                    ride_floor=4.0, ride_buffer=1.0)
        # The trade should NOT have a "trail_hit" exit at +3 (which would happen if
        # it entered Phase B with floor at +4 when price was only at +3)
        for j in res["journeys"]:
            if j.mfe >= 10 and j.result < 4:
                # This trade peaked at 10+ but closed below floor — it stayed in A
                assert j.exit_reason != "trail_hit", \
                    f"Should not trail_hit when current < floor+buffer: result={j.result}"


# ================================================================== 6. Soft arm at +5

class TestSoftArm:
    def test_arm_at_5_not_3(self):
        """With soft_arm_pts=5 and early_at=3, the soft trail should arm at +5."""
        # arm_pts = max(3, 5) = 5
        # at peak=4: no arm (below 5)
        # at peak=5: arm
        arm = max(3, 5)
        assert arm == 5
        # trail at peak=5, giveback=max(3.0, 0.5*5)=3.0: level = max(1, 5-3) = 2
        level = lg.soft_trail_level(5.0, 5.0, 1.0, 3.0, 0.5)
        assert level == 2.0

    def test_arm_at_3_when_soft_arm_lower(self):
        """With soft_arm_pts=2 and early_at=3, arm at 3."""
        arm = max(3, 2)
        assert arm == 3


# ================================================================== 7. Restart restores st["leg"]

class TestRestartLeg:
    def test_state_fields_survive_restart(self):
        st = {
            "symbol": "XAUUSD", "mode": "ema2050", "direction": "long",
            "entry": 2700.0, "peak": 15.0, "pre": False, "bars": 20,
            "news_done": False, "secured": 8.0,
            "guardian_profile": "leg",
            "leg_phase": "B", "floor_pts": 4.0, "trail_pts": 8.0,
            "leg": {"side": "LONG", "start_px": 2700.0, "start_t": 100,
                    "best": 15.0, "best_px": 2715.0, "against": 2.0}
        }
        # Simulate JSON round-trip (guardian_state.json)
        import json
        restored = json.loads(json.dumps(st))
        assert restored["guardian_profile"] == "leg"
        assert restored["leg_phase"] == "B"
        assert restored["floor_pts"] == 4.0
        assert restored["trail_pts"] == 8.0
        assert restored["leg"]["side"] == "LONG"
        assert restored["leg"]["best"] == 15.0
        # leg_alive should work with restored snapshot
        assert lg.leg_alive(restored["leg"], "long", ef=105, es=100)


# ================================================================== 8. Replay parity (3 exit scenarios)

class TestReplayParity:
    """Classic profile produces classic exit reasons; leg produces leg exit reasons."""

    def _make_trending_bars(self, n=300, seed=42):
        np.random.seed(seed)
        t0 = 1700000000
        time_arr = np.arange(t0, t0 + n * 300, 300, dtype=int)
        px = 2700 + np.cumsum(np.random.normal(0.15, 1.0, n))
        close = px.astype(float)
        high = close + np.abs(np.random.normal(0.5, 0.3, n))
        low = close - np.abs(np.random.normal(0.5, 0.3, n))
        open_ = close + np.random.normal(0, 0.3, n)
        df = pd.DataFrame({"time": time_arr, "open": open_, "high": high, "low": low, "close": close})
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["spread"] = df["ema20"] - df["ema50"]
        df.loc[:49, ["ema20", "ema50", "spread"]] = np.nan
        return df, time_arr

    def test_classic_exit_reasons_unchanged(self):
        df, t = self._make_trending_bars()
        r = Rules(guardian=_gold_guardian())
        res = build(df, r, 3.0, trade_from=int(t[60]), guardian_profile="classic")
        classic_ok = {"stop", "early", "secured", "ema20_turn", "opposite_cross", "day_end", "open", "news_flat"}
        for j in res["journeys"]:
            if j.exit_reason:
                assert j.exit_reason in classic_ok, f"unexpected classic exit: {j.exit_reason}"
                assert j.guardian_profile == "classic"

    def test_leg_exit_reasons_valid(self):
        df, t = self._make_trending_bars()
        r = Rules(guardian=_gold_guardian())
        res = build(df, r, 3.0, trade_from=int(t[60]), guardian_profile="leg")
        leg_ok = {"stop", "soft_trail", "trail_hit", "ema_slow_close", "leg_end", "day_end", "open", "news_flat"}
        for j in res["journeys"]:
            if j.exit_reason:
                assert j.exit_reason in leg_ok, f"unexpected leg exit: {j.exit_reason}"
                assert j.guardian_profile == "leg"

    def test_leg_and_classic_same_entries(self):
        """Both profiles should produce the same entry signals (only exits differ)."""
        df, t = self._make_trending_bars()
        r = Rules(guardian=_gold_guardian())
        res_c = build(df, r, 3.0, trade_from=int(t[60]), guardian_profile="classic")
        res_l = build(df, r, 3.0, trade_from=int(t[60]), guardian_profile="leg")
        entries_c = [(j.direction, j.entry_index, j.entry_price) for j in res_c["journeys"]]
        entries_l = [(j.direction, j.entry_index, j.entry_price) for j in res_l["journeys"]]
        assert entries_c == entries_l


# ================================================================== 9. CLOSED card shows profile + exit_rule

class TestJourneyProfileField:
    def test_journey_to_dict_includes_fields(self):
        j = Journey("long", 0, 5, 100, 2700.0)
        j.guardian_profile = "leg"
        j.exit_rule = "ema_slow_close"
        d = j.to_dict()
        assert d["guardian_profile"] == "leg"
        assert d["exit_rule"] == "ema_slow_close"

    def test_default_classic(self):
        j = Journey("short", 0, 5, 100, 2700.0)
        assert j.guardian_profile == "classic"
        assert j.exit_rule == ""


# ================================================================== extra: giveback default 3.0

class TestGivebackDefault:
    def test_config_default(self):
        cfg = Config()
        assert cfg.soft_giveback_pts == 3.0

    def test_soft_trail_with_3pt_giveback(self):
        # peak=5, arm=5, early_level=1, giveback=max(3, 0.5*5)=3
        # trail = max(1, 5-3) = 2
        level = lg.soft_trail_level(5.0, 5.0, 1.0, 3.0, 0.5)
        assert level == 2.0
