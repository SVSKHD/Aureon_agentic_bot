"""v2.10.0 Part C — zero-lag context (no rule change) and the guardian backtest harness."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.common import zl
from aureon_mt5.common.context_chart import context_chart
from tests.test_v181 import harness  # noqa: F401
from tests.test_claude_advisor import rig  # noqa: F401
from tools import replay_guardian as RG

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "xauusd_2day_m5.json")


def frame(n=400, seed=3):
    rs = np.random.RandomState(seed)
    c = 4100 + np.cumsum(rs.normal(0, 1.0, n)) + 15 * np.sin(np.arange(n) / 30)
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": 1_792_000_000 + np.arange(n) * 300, "open": o, "high": np.maximum(o, c) + 0.8,
                         "low": np.minimum(o, c) - 0.8, "close": c, "volume": rs.randint(50, 500, n)})


def test_no_lookahead():
    df = frame()
    full = zl.add_zl(df)
    for k in (120, 250, 399):
        part = zl.add_zl(df.iloc[: k + 1].reset_index(drop=True))
        for col in zl.ZL_COLS:
            a, b = full[col].iloc[k], part[col].iloc[k]
            assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(b, rel=1e-9, abs=1e-9), (col, k)


def test_zlema_leads_ema_on_a_step():
    c = pd.Series([100.0] * 100 + [110.0] * 30)
    e = c.ewm(span=50, adjust=False).mean(); z = zl.zlema(c, 50)
    assert z.iloc[110] > e.iloc[110] and z.iloc[99] == pytest.approx(100.0)


def test_adx_strong_trend_vs_chop():
    n = 200; t = np.arange(n)
    trend = pd.DataFrame({"time": t * 300, "open": 100 + t * 0.5, "close": 100 + t * 0.5 + 0.3, "high": 100 + t * 0.5 + 0.5,
                          "low": 100 + t * 0.5 - 0.2})
    chop = pd.DataFrame({"time": t * 300, "open": 100 + np.sin(t), "close": 100 + np.sin(t + 1), "high": 101.5 + 0 * t,
                         "low": 98.5 + 0 * t})
    a_t, dp, dm = zl.adx(trend, 14); a_c, _, _ = zl.adx(chop, 14)
    assert a_t.iloc[-1] > 50 > a_c.iloc[-1] and dp.iloc[-1] > dm.iloc[-1]


def test_zl_block_and_cross_against():
    df = zl.add_zl(frame())
    b = zl.zl_block(df)
    for k in ("zl50", "zl80", "zl_gap", "zl_cross_bars_ago", "hma55_slope", "adx5", "adx14", "di_plus", "di_minus", "atr14",
              "kama10_slope", "vwap_dist"):
        assert k in b
    g = pd.DataFrame({"zl50": [3.0, 2.0, 1.0, -1.0], "zl80": [0.0, 0.0, 0.0, 0.0]})
    assert zl.zl_cross_against(g, "long") and not zl.zl_cross_against(g, "short")
    assert zl.zl_block(frame()) is None                       # columns absent → no block


def test_snapshot_has_zl_only_when_on(rig):
    from aureon_mt5.claude_advisor import build_snapshot
    r = rig(mode="advisory")
    df = frame(); df["ema_fast"] = df["close"].ewm(span=50).mean(); df["ema_slow"] = df["close"].ewm(span=80).mean()
    t = int(df["time"].iloc[-1])
    assert "zl" not in build_snapshot(r.ag, "P", "LONG", t, df)
    snap = build_snapshot(r.ag, "P", "LONG", t, zl.add_zl(df))
    assert snap["zl"]["adx14"] is not None
    assert "ZL block" in r.adv.prompt_for(snap) and "ZL block" not in r.adv.prompt_for(build_snapshot(r.ag, "P", "LONG", t, df))


def test_chart_with_zl_renders(tmp_path):
    df = zl.add_zl(frame()); df["ema_fast"] = df["close"].ewm(span=50).mean(); df["ema_slow"] = df["close"].ewm(span=80).mean()
    path = context_chart(df, "XAUUSD", "EMA 50/80", 50, 80, ("ema_fast", "ema_slow"), 3.0, str(tmp_path), title_extra="zl")
    assert path and os.path.exists(path)


def test_flip_use_zl_only_adds_evidence(harness, monkeypatch):
    fake, ag, _ = harness(price=4201.0)
    monkeypatch.setattr(ag, "_m15_state", lambda: None)
    df = frame(); df["ema_fast"] = 4181.0; df["ema_slow"] = 4180.0
    df["close"] = 4201.0; df["high"] = 4203.0; df["low"] = 4199.0
    df["zl50"] = 1.0; df["zl80"] = 0.0; df.loc[df.index[-1], "zl50"] = -1.0          # zl50 crossed below zl80 on the last bar
    p = broker.positions("XAUUSD")[0]
    gap = (df["ema_fast"] - df["ema_slow"]).to_numpy()
    assert "ZL cross against" not in ag._flip_evidence(p, df, 4201.0, 4181.0, 4180.0, gap)
    ag.cfg.flip_use_zl = True
    assert ag._flip_evidence(p, df, 4201.0, 4181.0, 4180.0, gap) == ["ZL cross against"]


# ------------------------------------------------------------------ backtest harness

def test_replay_reproduces_15_oct_lines(tmp_path):
    args = ["--bars", FIX, "--trade", "10-15T01:30,long,4103.5", "--signal", "10-15T02:05,short", "--out", str(tmp_path)]
    base = RG.main(args)["combo"]["trades_detail"][0]
    assert (base["exit_time"], base["reason"], base["points"]) == ("10-15 02:05", "p_flip", -1.2)
    held = RG.main(args + ["--set", "flip_require_structure=1"])["combo"]["trades_detail"][0]
    assert held["reason"] != "p_flip" and "opposite_p_ignored" in held["events"]
    ss = RG.main(args + ["--set", "setup_stop=1"])["combo"]["trades_detail"][0]
    assert (ss["exit_time"], ss["reason"]) == ("10-15 01:55", "sl_hit") and ss["points"] == pytest.approx(-3.83, abs=0.05)
    assert os.listdir(tmp_path)                               # logs/replay/<date>.json written


def test_replay_three_day_synthetic_known_outcomes(tmp_path):
    n = 864; t = np.arange(n)
    c = np.concatenate([4000 + 0.25 * t[:300], 4075 + 0 * t[300:310], 4075 - 0.25 * (t[310:] - 310)])
    o = np.concatenate([[c[0]], c[:-1]])
    df = pd.DataFrame({"time": 1_792_000_000 + t * 300, "open": o, "high": np.maximum(o, c) + 0.3, "low": np.minimum(o, c) - 0.3,
                       "close": c})
    p = tmp_path / "bars.json"; p.write_text(json.dumps({"bars": df.to_dict(orient="list")}))
    from tools.guardian_sim import hhmm
    when = lambda i: hhmm(df["time"].iloc[i]).replace(" ", "T")
    out = RG.main(["--bars", str(p), "--trade", f"{when(150)},long", "--trade", f"{when(320)},long", "--out", str(tmp_path), "--compare",
                   "--set", "giveback_pct=50"])
    rows = {r["entry_time"]: r for r in out["defaults"]["trades_detail"] if r["source"] == "trade"}
    win = rows[hhmm(df["time"].iloc[150])]; lose = rows[hhmm(df["time"].iloc[320])]
    assert win["points"] >= 10 and lose["points"] < 0                    # rode the up-leg · stopped in the down-leg
    assert out["delta"]["trades"] == 0 and "total_points" in out["combo"]
