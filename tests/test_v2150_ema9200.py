"""v2.15.0 — the ema9200 mode: registry, mode enumerations, candidate rules, the live card + journal, the dataset export."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import ops
from aureon_mt5.strategies import available, get_strategy, resolve_mode
from aureon_mt5.strategies.ema9200 import journeys as J
from tests.test_v181 import harness  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import export_decisions_9200 as X  # noqa: E402


# ----------------------------------------------------------------------------- registry + enumerations
def test_mode_is_registered_with_aliases():
    assert "ema9200" in available()
    assert resolve_mode("9200") == resolve_mode("ema9/200") == resolve_mode("ema9200") == "ema9200"
    S = get_strategy("ema9200")
    assert S.name == "ema9200" and S.ema_cols == ("ema9", "ema200") and (S.fast, S.slow) == (9, 200)


def test_mode_command_and_write_mode_accept_it(tmp_path):
    env = tmp_path / ".env"; env.write_text("AUREON_MODE=ema5080\n")
    ops.write_mode(str(env), "ema9200")
    assert "AUREON_MODE=ema9200" in env.read_text()
    with pytest.raises(ValueError):
        ops.write_mode(str(env), "ema1234")
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    assert 'if mode not in ("ema2050", "ema5080", "ema9200"):' in src


def test_advisor_labels():
    from aureon_mt5.claude_advisor import EMA_NAMES
    assert EMA_NAMES["ema9200"] == ("EMA 9", "EMA 200")


def test_other_modes_untouched_by_tf_overrides():
    from aureon_mt5.common import tf_synth as T
    T.set_exec_tf("M30")
    assert T.strategy_overrides("ema9200") == {}
    assert T.strategy_overrides("ema2050") and T.strategy_overrides("ema5080")


def test_guardian_profile():
    g = J.guardian_for("XAUUSD")
    assert (g.pre_stop, g.secure_at, g.secure_level, g.ride_step) == (30.0, 40.0, 30.0, 10.0)
    assert g.p_phase is False and g.slow_ema_sl is False and g.early_at is None
    assert J.guardian_for("XAGUSD") is None


# ----------------------------------------------------------------------------- candidate rules
def bars(close, opn=None, high=None, low=None, e9=None, e200=None):
    c = np.asarray(close, float)
    return (c, np.asarray(opn if opn is not None else c, float), np.asarray(high if high is not None else c + 1, float),
            np.asarray(low if low is not None else c - 1, float), np.asarray(e9, float), np.asarray(e200, float))


def test_pullback_long_and_short():
    r = J.Rules()
    c, o, h, l, e9, e200 = bars([105.0], e9=[100.0], e200=[90.0])
    assert J.candidate(c, o, h, l, e9, e200, 0, r) == (1, "pullback")
    c, o, h, l, e9, e200 = bars([95.0], e9=[100.0], e200=[110.0])
    assert J.candidate(c, o, h, l, e9, e200, 0, r) == (-1, "pullback")
    c, o, h, l, e9, e200 = bars([109.0], e9=[100.0], e200=[90.0])          # 9 above EMA9: too far
    assert J.candidate(c, o, h, l, e9, e200, 0, r) == (0, "")
    c, o, h, l, e9, e200 = bars([99.0], e9=[100.0], e200=[90.0])           # wrong side of EMA9 for the bias
    assert J.candidate(c, o, h, l, e9, e200, 0, r) == (0, "")


def test_momentum_bar():
    r = J.Rules()
    c, o, h, l, e9, e200 = bars([140.0], opn=[108.0], e9=[100.0], e200=[90.0])   # body +32, close beyond EMA9
    assert J.candidate(c, o, h, l, e9, e200, 0, r) == (1, "momentum")
    c, o, h, l, e9, e200 = bars([135.0], opn=[108.0], e9=[100.0], e200=[90.0])   # body 27
    assert J.candidate(c, o, h, l, e9, e200, 0, r) == (0, "")


def test_quiet_hours_and_friday():
    r = J.Rules()
    mon = 1_760_918_400                                 # Mon 2025-10-20 00:00 (server epoch)
    assert J.server_weekday(mon) == 0 and J.server_weekday(mon + 4 * 86400) == 4
    assert J.entry_hour_block(mon + 21 * 3600, r).startswith("no-entry hour")
    assert J.entry_hour_block(mon + 12 * 3600, r) is None
    assert J.entry_hour_block(mon + 4 * 86400 + 17 * 3600, r) == "Friday after 17:00 server"


def synthetic(n=1500, seed=3):
    rng = np.random.default_rng(seed)
    close = 3300 + np.cumsum(np.repeat(rng.normal(0, 1.6, n // 24 + 1), 24)[:n] + rng.normal(0, 7, n))
    op = np.r_[close[0], close[:-1]]
    t = 1_745_798_400 + np.arange(n) * 1800
    return pd.DataFrame({"time": t, "open": op, "high": np.maximum(op, close) + 3, "low": np.minimum(op, close) - 3, "close": close})


def test_build_cooldown_and_summary():
    from aureon_mt5.strategies.ema9200.analysis import add_emas
    df = add_emas(synthetic())
    res = J.build(df, J.Rules(), 3.0)
    s = res["summary"]
    assert s["candidates"] > 0 and s["trades"] == len([j for j in res["journeys"] if j.exit_reason != "open"])
    offered = [e for e in res["events"] if e.label in ("EB", "ES")]
    for a, b in zip(offered, offered[1:]):               # one ask per cluster: 4 bars or $10 of travel
        assert b.index - a.index >= 4 or abs(df["close"][b.index] - df["close"][a.index]) >= 10
    f = [e for e in res["events"] if e.label == "F"]
    assert f and {e.reason.split(" (")[0] for e in f} <= {"in position", "cooldown", "no-entry hour", "Friday after 17:00 server", "news"}
    e = offered[0]
    assert set(e.info) >= {"dist9", "dist200", "last3", "ema9", "ema200", "bias", "session"} and len(e.info["last3"]) == 3


def test_decisions_replay_takes_only_true_bars():
    from aureon_mt5.strategies.ema9200.analysis import add_emas
    df = add_emas(synthetic())
    offered = [e for e in J.build(df, J.Rules(), 3.0)["events"] if e.label in ("EB", "ES")]
    pick = {offered[0].time: True, offered[1].time: False}
    res = J.build(df, J.Rules(), 3.0, decisions=pick)
    assert [j.entry_time for j in res["journeys"]] == [offered[0].time]


# ----------------------------------------------------------------------------- the live card + journal
def agent9200(harness):
    _, ag0, _ = harness(price=3300.0)
    from aureon_mt5.agent import SymbolAgent
    ag = SymbolAgent("XAUUSD", ag0.cfg, get_strategy("ema9200"), ag0.notify, ag0.journal, ())
    asks, tracked = [], []
    ag.notify.ask = lambda key, title, lines, **kw: asks.append((key, title, lines, kw)) or True
    ag.track_ask = lambda *a: tracked.append(a)
    ag._spread_pts = lambda: 0.2
    return ag, asks, tracked


def test_detect_offers_candidate_and_journals(harness):
    from aureon_mt5.strategies.ema9200.analysis import add_emas
    ag, asks, tracked = agent9200(harness)
    df = add_emas(synthetic(400))
    last = len(df) - 1; bar_t = int(df["time"].iloc[last]); close = float(df["close"].iloc[last])
    info = {"ema9": close - 3, "ema200": close - 40, "dist9": 3.0, "dist200": 40.0, "last3": ["G+2.0", "R-1.0", "G+4.0"],
            "session": "london", "bias": "bull"}
    res = {"events": [J.CrossEvent(last, bar_t, "bull", "EB", "pullback", info)]}
    ag._detect_9200(res, df, bar_t, close, close - 3, close - 40)
    key, title, lines, kw = asks[0]
    assert "CANDIDATE LONG (PULLBACK)" in title and "expires at the next M30 close" in lines[1]
    names = [f["name"] for f in kw["fields"]]
    assert {"Side", "Setup", "EMA 9", "EMA 200", "Last 3 bars", "Session", "Stop", "Manage"} <= set(names)
    assert kw["meta"]["mode"] == "ema9200" and kw["meta"]["sl"] == pytest.approx(close - 30)
    row = [r for r in ag.journal.read(0) if r["event"] == "signal"][-1]
    assert {k: row[k] for k in ("side", "signal_kind", "dist9", "dist200", "last3", "spread_pts")} == {
        "side": "LONG", "signal_kind": "pullback", "dist9": 3.0, "dist200": 40.0, "last3": ["G+2.0", "R-1.0", "G+4.0"], "spread_pts": 0.2}
    assert row["price"] == close and row["bar"] == bar_t and tracked


def test_detect_skipped_candidate_is_quiet(harness):
    from aureon_mt5.strategies.ema9200.analysis import add_emas
    ag, asks, tracked = agent9200(harness)
    df = add_emas(synthetic(400)); last = len(df) - 1; bar_t = int(df["time"].iloc[last])
    res = {"events": [J.CrossEvent(last, bar_t, "bear", "F", "cooldown (2 bars since the last ask)", {})]}
    ag._detect_9200(res, df, bar_t, 3300.0, 3301.0, 3350.0)
    assert not asks and not tracked
    row = [r for r in ag.journal.read(0) if r["event"] == "candidate_skipped"][-1]
    assert (row["side"], row["bar"], row["price"], row["why"]) == ("SHORT", bar_t, 3300.0, "cooldown (2 bars since the last ask)")


# ----------------------------------------------------------------------------- the dataset export
def test_export_decisions(tmp_path):
    rows = [{"event": "signal", "mode": "ema9200", "symbol": "XAUUSD", "bar": 100},
            {"event": "signal", "mode": "ema9200", "symbol": "XAUUSD", "bar": 200},
            {"event": "signal", "mode": "ema9200", "symbol": "XAUUSD", "bar": 300},       # never answered → false
            {"event": "signal", "mode": "ema5080", "symbol": "XAUUSD", "bar": 400},       # other mode: ignored
            {"event": "decision", "mode": "ema9200", "symbol": "XAUUSD", "bar": 100, "decision": "TAKE"},
            {"event": "decision", "mode": "ema9200", "symbol": "XAUUSD", "bar": 200, "decision": "EXPIRED"},
            {"event": "decision", "mode": "ema9200", "symbol": "XAUUSD", "bar": 300, "decision": "SKIP"},
            {"event": "decision", "mode": "ema9200", "symbol": "XAUUSD", "bar": 300, "decision": "TAKE"}]   # last wins
    assert X.decisions(rows) == {100: True, 200: False, 300: True}
    assert X.decisions(rows, since=150, until=250) == {200: False}
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    X.main(["--log-dir", str(tmp_path)])
    assert json.load(open(tmp_path / "decisions_9200.json")) == {"100": True, "200": False, "300": True}


def test_replay_tool_end_to_end_with_exported_decisions(tmp_path):
    from aureon_mt5.strategies.ema9200.analysis import add_emas
    df = synthetic(1500); csv = tmp_path / "m30.csv"; df.to_csv(csv, index=False)
    offered = [e for e in J.build(add_emas(df), J.Rules(), 3.0)["events"] if e.label in ("EB", "ES")]
    rows = [{"event": "signal", "mode": "ema9200", "bar": e.time} for e in offered[:5]]
    rows.append({"event": "decision", "mode": "ema9200", "bar": offered[0].time, "decision": "TAKE"})
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    X.main(["--log-dir", str(tmp_path)])
    run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "tools", "replay_9200.py"), "--csv", str(csv), *a],
                                    capture_output=True, text=True, timeout=120, cwd=ROOT)
    base = run(); dec = run("--decisions", str(tmp_path / "decisions_9200.json"))
    assert base.returncode == 0 and dec.returncode == 0, base.stderr + dec.stderr
    assert "candidates" in base.stdout and "trades 1 " in dec.stdout
    assert (tmp_path / "m30_9200.png").exists() and (tmp_path / "m30_9200.html").exists()


def test_alert_guardian_context_works_in_ema9200(harness):
    """/alert context: ema9200 has no entry_hours_ist — it uses its own server-hour / Friday rule (was an AttributeError)."""
    from aureon_mt5.alerts import guardian_context
    ag, _, _ = agent9200(harness)
    ag.snapshot = {"positions": [], "state": {}}; ag.news = ()
    mon = 1_760_918_400
    _, ctx = guardian_context(ag, mon + 12 * 3600)
    assert ctx["window_open"] is True and ctx["news_block"] is False
    fields, ctx = guardian_context(ag, mon + 22 * 3600)
    assert ctx["window_open"] is False and "no-entry hour" in [f["value"] for f in fields if f["name"] == "Entry window"][0]
