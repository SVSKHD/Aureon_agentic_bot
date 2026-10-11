"""v2.1.0 — run.py --claude shadow replay: no look-ahead (snapshot built from df.iloc[:bar+1] only), cache hit makes zero calls,
--claude-max honoured with unscored count, filtered-column math, SKIP precision, calibration math, pullback variant, report shape."""
import json, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import claude_replay as CR
from aureon_mt5.common import claude_cli as cc
from aureon_mt5.config import Config
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from tests.test_v200_ema2050 import _bull, _trade, frame, OFF, RULES

S2050 = get_strategy("ema2050"); S5080 = get_strategy("ema5080")


class CountingCLI:
    def __init__(self, decide=None):
        self.calls = []; self.decide = decide or (lambda prompt, event: ("TAKE", 0.7))
    def __call__(self, prompt, model, *, event, bin, workdir, timeout):
        self.calls.append({"prompt": prompt, "model": model, "event": event, "bin": bin})
        dec, p = self.decide(prompt, event)
        if event == "pullback":
            return cc.CliResult(True, verdict=cc.validate({"decision": dec, "confidence": "medium", "reason": "r", "tighten_to": p}, event), latency_s=0.5)
        return cc.CliResult(True, verdict=cc.validate({"decision": dec, "side": "BUY", "confidence": "medium", "reason": "r", "p_win": p,
                                                       "evidence": ["gap"]}, event), latency_s=0.5)


def day_frame():
    """A day with three ENTER bars: LONG +15 (secured), SHORT 0.0 (exit on the opposite confirmed cross), LONG −12 (stop)."""
    g = [-2] * 5 + [2.0] * 25 + [-0.5, -1.0, -2.0] + [-3.0] * 7 + [0.5, 1.0, 2.0] + [2.0] * 40
    n = len(g); lows = [4002.0] * n; highs = [4001.0] * n; closes = [4000.5] * n
    lows[6] = 4001.0                                        # pullback touch -> entry at bar 7
    highs[8] = 4010.6; lows[9] = 4012.0; highs[9] = 4017.6; lows[10] = 4010.4; highs[10] = 4012.0   # +15 secured exit at bar 10
    closes[9] = 4012.5; closes[10] = 4010.5                 # bar 8 pullback at +0 (CLOSE refused: not in profit), bar 10 pullback at +10 (CLOSE applies)
    lows[41] = 4001.0                                       # second bull cross at 40, confirmed 42, touch 41 -> entry 42
    lows[44] = 3987.0; highs[44] = 4001.0                   # -12 stop at bar 44
    df = frame(g, closes=closes, lows=lows, highs=highs)
    df["open"] = df["close"]; df["volume"] = 0
    return df


def rig(tmp_path, cli=None, **kw):
    cfg = Config(); cfg.log_dir = str(tmp_path / "logs"); cfg.claude_workdir = str(tmp_path / "wd"); cfg.claude_entry_model = "opus"
    cli = cli or CountingCLI()
    sr = CR.ShadowReplay(cfg, S2050, "XAUUSD", OFF, (), out_dir=str(tmp_path / "out"), caller=cli, sleep=lambda s: None, **kw)
    return sr, cli


# ------------------------------------------------------------------ no look-ahead
def test_cut_frame_and_snapshot_never_see_future_bars(tmp_path):
    df = day_frame(); bar_t = int(df["time"].iloc[7])
    cut = CR.cut_frame(df, bar_t)
    assert len(cut) == 8 and int(cut["time"].iloc[-1]) == bar_t
    with pytest.raises(ValueError):
        CR.cut_frame(df, bar_t + 7)
    seen = {}
    def spy(agent, event, side, bt, d, *, position=None, st=None, kind=None):
        seen["len"] = len(d); seen["last"] = int(d["time"].iloc[-1]); seen["states"] = len(agent.last_res["bar_states"])
        return {"event": event, "side": side}
    shadow = CR.ShadowAgent(S2050, "XAUUSD", OFF, (), Config())
    snap = CR.snapshot_for_bar(shadow, df, bar_t, "ENTER", "LONG", build=spy)
    assert seen == {"len": 8, "last": bar_t, "states": 8} and snap["replay"] is True and snap["trades_today"] == 0
    with pytest.raises(AssertionError):
        CR.snapshot_for_bar(shadow, df, bar_t, "ENTER", "LONG", build=lambda *a, **k: (_ for _ in ()).throw(AssertionError("x")))


def test_real_snapshot_from_cut_frame_matches_live_fields(tmp_path):
    df = day_frame(); bar_t = int(df["time"].iloc[7])
    shadow = CR.ShadowAgent(S2050, "XAUUSD", OFF, (), Config())
    snap = CR.snapshot_for_bar(shadow, df, bar_t, "ENTER", "LONG")
    for k in ("ema_fast", "ema_slow", "gap", "confirm_state", "crosses_today", "bars_since_cross", "dist_to_fast_ema_pts", "atr20", "bars_ohlc_ist"):
        assert k in snap, k
    assert snap["confirm_state"] == "confirmed" and snap["crosses_today"] == 1 and snap["bars_since_cross"] == 2
    json.dumps(snap)


# ------------------------------------------------------------------ cache, budget, calls as live
def test_cache_hit_makes_zero_calls_and_refresh_calls_again(tmp_path):
    df = day_frame(); res = J.build(df, RULES, OFF)
    N = len(res["journeys"]); assert N == 3 and [j.exit_reason for j in res["journeys"]] == ["secured", "opposite_cross", "stop"]
    sr, cli = rig(tmp_path)
    sr.score_day("2026-10-07", df, res["journeys"])
    assert len(cli.calls) == N and sr.res.cache_hits == 0
    assert cli.calls[0]["event"] == "ENTER" and cli.calls[0]["model"] == "opus" and cli.calls[0]["prompt"].startswith("MODE: ema2050")
    assert os.path.exists(sr.path) and len(CR.load_cache(sr.path)) == N
    sr2, cli2 = rig(tmp_path)                                 # same inputs: zero calls
    sr2.score_day("2026-10-07", df, res["journeys"])
    assert cli2.calls == [] and sr2.res.cache_hits == N and [r["claude"] for r in sr2.res.rows] == ["TAKE"] * N
    sr3, cli3 = rig(tmp_path, refresh=True)
    sr3.score_day("2026-10-07", df, res["journeys"])
    assert len(cli3.calls) == N
    sr4, cli4 = rig(tmp_path, model="sonnet")                 # a different model is a different key
    sr4.score_day("2026-10-07", df, res["journeys"])
    assert len(cli4.calls) == N


def test_claude_max_caps_calls_and_counts_unscored(tmp_path):
    df = day_frame(); res = J.build(df, RULES, OFF)
    sr, cli = rig(tmp_path, max_calls=1)
    sr.score_day("2026-10-07", df, res["journeys"])
    assert len(cli.calls) == 1 and sr.res.unscored == 2 and sr.res.rows[1]["unscored"] and sr.res.rows[1]["claude"] is None
    sc = sr.score()
    assert sc["unscored"] == 2 and sc["no_verdict"] == 2
    sr2, cli2 = rig(tmp_path, max_calls=5)                    # next run continues from the cache: only the unscored bars are called
    sr2.score_day("2026-10-07", df, res["journeys"])
    assert len(cli2.calls) == 2 and sr2.res.cache_hits == 1 and sr2.res.unscored == 0


# ------------------------------------------------------------------ filtered column, SKIP precision, months
def test_filtered_column_math_and_skip_precision(tmp_path):
    df = day_frame(); res = J.build(df, RULES, OFF)
    pts = [j.result for j in res["journeys"]]
    assert pts[0] > 0 and pts[1] == 0.0 and pts[2] == -12.0
    cli = CountingCLI(decide=lambda prompt, event: ("SKIP", 0.3) if '"crosses_today": 3' in prompt else ("TAKE", 0.8))
    sr, _ = rig(tmp_path, cli=cli)
    sr.score_day("2026-10-07", df, res["journeys"])
    assert [r["claude"] for r in sr.res.rows] == ["TAKE", "TAKE", "SKIP"]
    sc = sr.score(); m = sc["metrics"]
    assert m["detector"].n == 3 and m["detector"].total == round(sum(pts), 2) and m["detector"].stops == 1
    assert m["claude_take"].n == 2 and m["claude_take"].total == round(pts[0] + pts[1], 2)
    assert m["claude_skip"].n == 1 and m["claude_skip"].total == -12.0
    assert m["filtered"].n == 2 and m["filtered"].total == round(pts[0] + pts[1], 2) and m["filtered"].stops == 0
    assert sc["skip_precision"] == 1.0 and sc["skips"] == 1
    mo = next(iter(sc["months"].values()))
    assert mo == {"det_n": 3, "det_pts": pytest.approx(sum(pts)), "filt_n": 2, "filt_pts": pytest.approx(pts[0] + pts[1])}
    d = sc["disagreements"]; assert len(d) == 1 and d[0]["claude"] == "SKIP" and d[0]["points"] == -12.0 and d[0]["evidence"] == ["gap"]


def test_calibration_math():
    rows = [{"p_win": 0.9, "points": 5.0}, {"p_win": 0.85, "points": -3.0}, {"p_win": 0.1, "points": -6.0}, {"p_win": None, "points": 9.0},
            {"p_win": 0.5, "points": 2.0}, {"p_win": 0.55, "points": 1.0}]
    c = {x["bucket"]: x for x in CR.calibration(rows)}
    assert c["0.8–1.0"] == {"bucket": "0.8–1.0", "n": 2, "win_rate": 0.5, "mean_p": 0.88}
    assert c["0.0–0.2"]["n"] == 1 and c["0.0–0.2"]["win_rate"] == 0.0
    assert c["0.4–0.6"] == {"bucket": "0.4–0.6", "n": 2, "win_rate": 1.0, "mean_p": 0.53}
    assert c["0.2–0.4"]["n"] == 0 and c["0.2–0.4"]["win_rate"] is None


# ------------------------------------------------------------------ pullback variant
def test_pullback_variant_asks_once_per_pullback_and_applies_close_only_to_filtered(tmp_path):
    df = day_frame(); res = J.build(df, RULES, OFF)
    cli = CountingCLI(decide=lambda prompt, event: ("CLOSE", None) if event == "pullback" else ("TAKE", 0.7))
    sr, _ = rig(tmp_path, cli=cli, variant="entry+pullback")
    sr.score_day("2026-10-07", df, res["journeys"])
    pb = [c for c in cli.calls if c["event"] == "pullback"]
    assert len(pb) >= 1 and all(c["event"] in ("ENTER", "pullback") for c in cli.calls)
    assert [r["points"] for r in sr.res.rows] == [j.result for j in res["journeys"]]          # the replayed trades are untouched
    first = sr.res.rows[0]
    assert first["filtered_points"] is not None and first["filtered_points"] != first["points"]  # CLOSE in profit applied to the filtered column only
    assert sr.res.pullbacks and sr.res.pullbacks[0]["claude"] == "CLOSE" and sr.res.pullbacks[0]["points_at"] > 0
    sc = sr.score()
    assert sc["metrics"]["detector"].total == round(sum(j.result for j in res["journeys"]), 2)
    others = sum((r["points"] if r["filtered_points"] is None else r["filtered_points"]) for r in sr.res.rows[1:])
    assert sc["metrics"]["filtered"].total == round(first["filtered_points"] + others, 2)


# ------------------------------------------------------------------ report + run.py wiring
def test_report_written_and_run_flags(tmp_path):
    df = day_frame(); res = J.build(df, RULES, OFF)
    sr, cli = rig(tmp_path)
    sr.score_day("2026-10-07", df, res["journeys"])
    text, path = CR.write_report(sr, sr.score(), "XAUUSD · test")
    assert os.path.exists(path) and path.endswith("claude_replay_ema2050_XAUUSD.md")
    for key in ("Detector · all", "Claude TAKE", "Claude SKIP", "Detector-filtered-by-Claude", "SKIP precision", "Calibration", "Per month", "Disagreements",
                "unscored ENTER bars 0"):
        assert key in text, key
    import run as runpy
    import argparse
    src = open(runpy.__file__, encoding="utf-8").read()
    for flag in ("--claude", "--claude-max", "--claude-sleep", "--claude-model", "--claude-refresh"):
        assert flag in src
    assert "def shadow_replay" in src


def test_ema5080_events_and_no_budget_file_touched(tmp_path):
    from tests.test_v187_trend_reentry import downtrend_with_pullback
    df = downtrend_with_pullback(); df["time"] = 1_791_331_200 + 10 * 3600 + np.arange(len(df)) * 300; df["volume"] = 0
    from aureon_mt5.strategies.ema5080 import journeys as J5
    res = J5.build(df, J5.rules_for("XAUUSD"), OFF)
    cfg = Config(); cfg.log_dir = str(tmp_path / "logs"); cfg.claude_workdir = str(tmp_path / "wd")
    cli = CountingCLI()
    sr = CR.ShadowReplay(cfg, S5080, "XAUUSD", OFF, (), out_dir=str(tmp_path / "out"), caller=cli, sleep=lambda s: None)
    sr.score_day("d", df, res["journeys"])
    assert len(cli.calls) == len([j for j in res["journeys"]]) and all(c["event"] in ("P", "CROSS", "RE") for c in cli.calls)
    assert all(c["prompt"].startswith("MODE: ema5080") for c in cli.calls)
    assert not os.path.exists(os.path.join(cfg.log_dir, "claude_budget.json"))                  # the live daily budget is untouched
