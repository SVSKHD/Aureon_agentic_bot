"""v2.14.0 Part A/B — the execution timeframe (AUREON_EXEC_TF) and the synthetic M45."""
import dataclasses
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import broker, features as F, regime as R
from aureon_mt5.common import tf_synth as T
from aureon_mt5.config import Config
from aureon_mt5.strategies.ema2050 import journeys as J20
from aureon_mt5.strategies.ema5080 import journeys as J58
from tests.test_v181 import harness  # noqa: F401


# ----------------------------------------------------------------------------- M5 = v2.13.0 exactly
def test_m5_is_the_v2130_numbers():
    T.set_exec_tf("M5")
    assert T.exec_tf() == "M5" and T.bar_s() == 300
    assert T.strategy_overrides("ema2050") == {} and T.strategy_overrides("ema5080") == {}
    assert J20.rules_for("XAUUSD") is J20.GOLD and J20.rules_for("XAGUSD") is J20.SILVER
    assert J58.rules_for("XAUUSD") == dataclasses.replace(J58.rules_for("XAUUSD"))
    assert R._window() == R.WINDOW == 24 and R._flat_window() == R.FLAT_WINDOW == 12
    assert T.bars_for(60) == 12 and T.bars_for(120) == 24 and T.bars_for(600) == 120


def test_config_default_and_registry():
    c = Config()
    assert c.exec_tf.upper() == "M5" and c.htf_align == "off" and c.htf_align_min == "all"
    assert c.htf_pre_max_gap == 0.35 and c.htf_with_max_bars == 8 and c.htf_require_fresh is False
    reg = {f.name: f for f in F.registry()}
    assert not reg["exec_tf"].hot and reg["htf_align"].hot and "block" in (reg["htf_align"].loud_values or ())
    for n in ("htf_confirm_tfs", "htf_pre_max_gap", "htf_with_max_bars", "htf_require_fresh", "htf_align_min"):
        assert n in reg


def test_exec_tf_env(monkeypatch):
    v, src = F.resolve("exec_tf", Config(), {}, environ={"AUREON_EXEC_TF": "M30"})
    assert v == "m30" and src == "env"
    v, src = F.resolve("exec_tf", Config(), {}, environ={})
    assert v == "m5" and src == "default"


# ----------------------------------------------------------------------------- M30 conversions
def test_m30_bar_s_and_windows():
    T.set_exec_tf("M30")
    assert T.bar_s() == 1800 and T.bars_for(60) == 2 and T.bars_for(120) == 4
    t = T.rule_table()
    assert (t["hold"], t["window"], t["gap_x"]) == (2, 6, 0.25)
    assert R._window() == 4 and R._flat_window() == 2


def test_m30_strategy_overrides():
    T.set_exec_tf("M30")
    g = J20.rules_for("XAUUSD")
    assert g.confirm_bars == 2 and g.confirm_window == 6 and g.confirm_gap == 0.0 and g.confirm_gap_x_range == 0.25
    assert g.pullback_window == 2 and g.pre_sep_bars == 4 and g.pre_cooldown_bars == 2
    s = J20.rules_for("XAGUSD")
    assert s.confirm_bars == 2 and s.confirm_gap == J20.SILVER.confirm_gap and s.confirm_gap_x_range == 0.0
    r = J58.rules_for("XAUUSD")
    assert r.late_entry_bars == 0 and r.pullback_wait_bars == 4 and r.reentry_window == 6 and r.p_sep_bars == 4
    assert r.wz_bars == 2 and r.pre_timeout == 2 and r.max_bars == 16


def test_confirm_overrides_from_cfg():
    T.set_exec_tf("M30", SimpleNamespace(confirm_hold_bars=3, confirm_gap_x_range=0.4, confirm_window_bars=8))
    g = J20.rules_for("XAUUSD")
    assert (g.confirm_bars, g.confirm_window, g.confirm_gap_x_range) == (3, 8, 0.4)


def test_ladder_and_confirm_tfs():
    assert T.confirm_tfs("M5") == ("M10", "M15") and T.confirm_tfs("M30") == ("M45", "H1")
    assert T.confirm_tfs("H1") == ("H2", "H3") and T.confirm_tfs("H4") == ("H6", "H8")
    with pytest.raises(ValueError):
        T.confirm_tfs("D1")
    with pytest.raises(ValueError):
        T.set_exec_tf("M7")


def test_gap_x_range_confirmation():
    """At M30 the confirm gap is relative: a 1-point gap confirms on a quiet tape, not on a wild one."""
    T.set_exec_tf("M30")
    r = J20.rules_for("XAUUSD")
    assert r.confirm_gap == 0.0 and r.confirm_gap_x_range == 0.25
    assert "confirm_gap_x_range" in open(J20.__file__, encoding="utf-8").read()


# ----------------------------------------------------------------------------- broker: the fetch follows the exec TF
class RatesMT5:
    TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30, TIMEFRAME_H1 = 5, 15, 30, 60

    def __init__(self):
        self.calls = []

    def symbol_select(self, *a):
        return True

    def copy_rates_from_pos(self, sym, tf, start, n):
        self.calls.append((tf, n))
        t = np.arange(n, dtype="int64") * tf * 60
        return np.array([(x, 1.0, 2.0, 0.5, 1.5, 10) for x in t],
                        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8")])


def test_bars_fetch_uses_exec_tf(monkeypatch):
    fake = RatesMT5(); monkeypatch.setattr(broker, "_m", fake)
    broker.bars("XAUUSD", 300, "mt5", 3.0)
    broker.bars("XAUUSD", 300, "mt5", 3.0, tf="M30")
    broker.bars_tf("XAUUSD", "H1", 300, "mt5", 3.0)
    assert fake.calls == [(5, 300), (30, 300), (60, 300)]
    fake.calls.clear()
    m45 = broker.bars_tf("XAUUSD", "M45", 100, "mt5", 3.0)
    assert fake.calls == [(15, 303)] and (np.diff(m45["time"]) % 2700 == 0).all()


def test_synthetic_bars_scale_with_tf():
    b = broker.bars("XAUUSD", 50, "synthetic", 3.0, tf="M30")
    assert b.timeframe == "M5" and set(np.diff(b.df["time"])) <= {1800}


def test_agent_takes_exec_tf_at_init(harness):
    T.set_exec_tf("M5")
    _, ag, _ = harness()
    assert ag.exec_tf == "M5" and ag.bar_s == 300 and ag.chart_bars == 120 and ag._leg_slow_bars() == 48


def test_agent_at_m30(harness, monkeypatch):
    monkeypatch.setattr(Config, "exec_tf", "m30", raising=False)
    fake, ag0, _ = harness()
    cfg = ag0.cfg; cfg.exec_tf = "m30"
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.strategies import get_strategy
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), ag0.notify, ag0.journal, ())
    assert ag.exec_tf == "M30" and ag.bar_s == 1800 and ag.chart_bars == 60 and ag._leg_slow_bars() == 8
    if ag.g is not None:
        assert ag.g.pre_timeout_bars == 2


# ----------------------------------------------------------------------------- synthetic M45
def m15(days=2, start=86400 * 20000, drop=()):
    t = start + np.arange(days * 96) * 900
    t = np.array([x for i, x in enumerate(t) if i not in drop])
    px = 100 + np.arange(len(t), dtype=float)
    return pd.DataFrame({"time": t, "open": px, "high": px + 2, "low": px - 1, "close": px + 0.5, "volume": 1.0})


def test_m45_aligned_to_server_day():
    d = T.synth_m45(m15(days=2))
    assert len(d) == 64                                            # 32 groups per day
    assert ((d["time"] - (d["time"] // 86400) * 86400) % 2700 == 0).all()
    assert d["time"].iloc[32] % 86400 == 0                         # day 2 starts a new group at 00:00
    first = m15().iloc[:3]
    row = d.iloc[0]
    assert row["open"] == first["open"].iloc[0] and row["close"] == first["close"].iloc[-1]
    assert row["high"] == first["high"].max() and row["low"] == first["low"].min() and row["volume"] == 3.0


def test_m45_day_boundary_group_is_cut():
    """1440 / 45 = 32 exactly: no group straddles midnight."""
    d = T.synth_m45(m15(days=3))
    starts = (d["time"] % 86400).to_numpy()
    assert starts.max() == 31 * 2700 and (starts + 2700 <= 86400).all()


def test_m45_drops_incomplete_last_group():
    df = m15(days=1).iloc[:-1]                                     # the last group has only two M15 bars (still forming)
    d = T.synth_m45(df)
    assert len(d) == 31 and d["time"].iloc[-1] == df["time"].iloc[0] + 30 * 2700


def test_m45_gap_skips_the_group():
    d = T.synth_m45(m15(days=1, drop=(4,)))                        # bar 4 = the middle of group 1
    assert len(d) == 31 and (d["time"] != m15()["time"].iloc[0] + 2700).all()


def test_m45_from_m5_via_resample():
    m5 = pd.DataFrame({"time": 86400 * 20000 + np.arange(288) * 300, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
    d = T.resample_tf(m5, "M45")
    assert len(d) == 32 and (np.diff(d["time"]) == 2700).all()


def test_closed_only_drops_forming_bar():
    df = pd.DataFrame({"time": [0, 1800, 3600], "close": [1, 2, 3]})
    assert list(T.closed_only(df, "M30", 3600 + 1799)["time"]) == [0, 1800]
    assert list(T.closed_only(df, "M30", 5400)["time"]) == [0, 1800, 3600]
