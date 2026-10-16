"""v2.14.0 Part C/D — the higher-timeframe alignment gate (AUREON_HTF_ALIGN) and the harness."""
import json
import os
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aureon_mt5 import bot, htf_align as H
from aureon_mt5.common import tf_synth as T
from tests.test_v181 import harness  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frame(gaps, rng=4.0):
    """A closed-bar frame whose EMA fast − slow follows `gaps` (avg range = rng)."""
    g = np.asarray(gaps, dtype=float)
    n = len(g)
    slow = np.full(n, 100.0)
    return pd.DataFrame({"time": np.arange(n) * 3600, "open": 100.0, "high": 100 + rng / 2, "low": 100 - rng / 2,
                         "close": 100.0, "ema_fast": slow + g, "ema_slow": slow})


def crossed_up(bars_ago, end=2.0, n=40):
    """Below for a while, crossed up `bars_ago` bars ago, widening to `end`."""
    g = [-3.0] * (n - bars_ago - 1) + list(np.linspace(0.3, end, bars_ago + 1))
    return frame(g)


# ----------------------------------------------------------------------------- states
def test_with_fresh_cross():
    st = H.htf_state(crossed_up(3), "LONG")
    assert st["state"] == "with" and st["bars_since_cross"] == 3 and st["gap"] > 0


def test_with_stale_cross():
    st = H.htf_state(crossed_up(20), "LONG", with_max_bars=8)
    assert st["state"] == "with_stale" and st["bars_since_cross"] == 20


def test_pre_converging_and_close():
    g = [-3.0] * 30 + [-2.0, -1.6, -1.3, -1.0]                    # still below, closing in, |gap| 1.0 / range 4 = 0.25
    st = H.htf_state(frame(g), "LONG")
    assert st["state"] == "pre" and st["gap_slope3"] > 0 and abs(st["gap_x_range"]) <= 0.35


def test_against_when_far_or_widening():
    far = [-3.0] * 30 + [-4.0, -3.5, -3.0, -2.5]                   # converging but 0.625× range away
    assert H.htf_state(frame(far), "LONG")["state"] == "against"
    widening = [-0.2] * 30 + [-0.3, -0.5, -0.8, -1.0]
    assert H.htf_state(frame(widening), "LONG")["state"] == "against"


def test_short_mirrors_long():
    g = [3.0] * 30 + [2.0, 1.6, 1.3, 1.0]
    assert H.htf_state(frame(g), "SHORT")["state"] == "pre"
    assert H.htf_state(crossed_up(3), "SHORT")["state"] == "against"
    assert H.htf_state(crossed_up(3), "SHORT")["bars_since_cross"] == -3


def test_unknown_without_data():
    assert H.htf_state(None, "LONG")["state"] == "unknown"
    assert H.htf_state(frame([1.0] * 10), "LONG")["state"] == "unknown"


def test_pre_max_gap_knob():
    g = [-3.0] * 30 + [-2.0, -1.6, -1.3, -1.0]
    assert H.htf_state(frame(g), "LONG", pre_max_gap=0.2)["state"] == "against"


# ----------------------------------------------------------------------------- gate
S = {"with": {"state": "with"}, "stale": {"state": "with_stale"}, "pre": {"state": "pre"},
     "against": {"state": "against"}, "unknown": {"state": "unknown"}}


def st(a, b):
    return {"M45": S[a], "H1": S[b]}


@pytest.mark.parametrize("a,b,ok", [("with", "with", True), ("with", "pre", True), ("pre", "pre", True),
                                    ("stale", "with", True), ("with", "against", False), ("pre", "unknown", False)])
def test_gate_all(a, b, ok):
    assert H.gate(st(a, b), mode_min="all")[0] is ok


def test_gate_any_needs_one_with():
    assert H.gate(st("with", "pre"), mode_min="any")[0]
    ok, why = H.gate(st("pre", "pre"), mode_min="any")
    assert not ok and "any" in why[0]
    assert not H.gate(st("with", "against"), mode_min="any")[0]


def test_require_fresh():
    assert H.gate(st("stale", "with"))[0]
    ok, why = H.gate(st("stale", "with"), require_fresh=True)
    assert not ok and why == ["M45 with_stale"]


def test_line_and_compact():
    states = {"M45": H.htf_state(frame([-3.0] * 30 + [-2.0, -1.6, -1.3, -1.0]), "LONG"), "H1": H.htf_state(crossed_up(2), "LONG")}
    ln = H.line(states)
    assert ln.startswith("HTF · M45 pre (gap -0.25× rng, converging)") and "H1 with (crossed 2 bars ago)" in ln
    c = H.compact(states)
    assert set(c) == {"M45", "H1"} and set(c["H1"]) == {"state", "gap_x_range", "gap_slope3", "bars_since_cross"}


def test_confirm_tfs_for():
    assert H.confirm_tfs_for(SimpleNamespace(exec_tf="m30", htf_confirm_tfs=())) == ["M45", "H1"]
    assert H.confirm_tfs_for(SimpleNamespace(exec_tf="m30", htf_confirm_tfs=("h1", "h2"))) == ["H1", "H2"]
    assert H.confirm_tfs_for(SimpleNamespace(exec_tf="m5", htf_confirm_tfs=())) == ["M10", "M15"]


# ----------------------------------------------------------------------------- the agent: off / show / block
def gated(harness, mode, ok=True):
    fake, ag, _ = harness(price=4200.0)
    ag.cfg.htf_align = mode
    asks, sends, calls = [], [], []
    ag.notify.ask = lambda key, title, lines, **kw: asks.append((key, title, kw)) or True
    ag.notify.send = lambda key, title, lines=None, png=None, **kw: sends.append((key, title, lines, kw)) or True
    states = {"M45": {"state": "pre" if ok else "against", "gap_x_range": -0.2, "gap_slope3": 0.5, "bars_since_cross": -9},
              "H1": {"state": "with", "gap_x_range": 0.6, "gap_slope3": 0.1, "bars_since_cross": 2}}

    def fake_align(side):
        calls.append(side)
        good, why = H.gate(states)
        return {"side": side, "states": states, "ok": good, "reasons": why, "line": H.line(states),
                "compact": H.compact(states), "frames": {}}
    ag.htf_alignment = fake_align
    return ag, asks, sends, calls


def test_off_never_looks(harness):
    ag, asks, sends, calls = gated(harness, "off", ok=False)
    assert ag._entry_ask("k", "XAUUSD · MT5 · P PRE-CROSS · LONG", ["x"], fields=[])
    assert not calls and asks and "HTF" not in str(asks[0][2]["fields"])


def test_show_adds_the_line_and_asks(harness):
    ag, asks, sends, calls = gated(harness, "show", ok=False)
    assert ag._entry_ask("k", "XAUUSD · MT5 · CONFIRMED CROSS · LONG", ["x"], fields=[])
    assert calls == ["LONG"]
    f = asks[0][2]["fields"][-1]
    assert f["name"] == "HTF" and "M45 against" in f["value"] and "H1 with" in f["value"]
    assert asks[0][2]["meta"]["htf_ok"] is False and asks[0][2]["meta"]["htf"]["H1"]["state"] == "with"


def test_block_no_ask_no_read(harness):
    ag, asks, sends, calls = gated(harness, "block", ok=False)
    assert ag._entry_ask("k", "XAUUSD · MT5 · ENTER SHORT", ["place it"], fields=[], meta={"side": "SHORT"}) is False
    assert calls == ["SHORT"] and not asks and "HTF against · not tradeable" in sends[0][1]
    assert "no ask, no Claude entry read" in sends[0][2][1]
    rows = [r for r in ag.journal.read(0) if r["event"] == "entry_blocked_htf"]
    assert rows and rows[0]["side"] == "SHORT" and rows[0]["reasons"] == ["M45 against"]


def test_block_lets_aligned_through(harness):
    ag, asks, sends, calls = gated(harness, "block", ok=True)
    assert ag._entry_ask("k", "XAUUSD · MT5 · ENTER LONG", ["x"], fields=[]) and asks and not sends


def test_every_entry_site_checks_the_return():
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    i = src.index("def _entry_ask(")
    body = src[i:src.index("def htf_frames(")]
    assert body.index('hg == "block" and not al["ok"]') < body.index("self.notify.ask(key, title, lines, **kw)")


def test_take_refused_when_blocked():
    from aureon_mt5.actions import Actions
    from aureon_mt5.notify import Notifier
    cfg = SimpleNamespace(ask_ttl_min=20, htf_align="block", log_dir="/tmp")
    acts = Actions(cfg, {}, SimpleNamespace(log=lambda *a, **k: None), Notifier(None))
    item = {"t": acts.clock(), "meta": {"symbol": "XAUUSD", "side": "LONG", "htf_ok": False, "htf_reasons": ["H1 against"]}}
    assert acts.refusal(item, "TAKE") == ["HTF against", "H1 against"]
    cfg.htf_align = "show"
    assert acts.refusal(item, "TAKE") == []
    item["meta"]["htf_ok"] = True; cfg.htf_align = "block"
    assert acts.refusal(item, "TAKE") == []


# ----------------------------------------------------------------------------- snapshot + prompt + chart
def test_snapshot_block_and_prompt_line():
    from tests.test_claude_advisor import frame as cframe
    from aureon_mt5.claude_advisor import build_snapshot, SNAPSHOT_KEEP, HTF_RULES
    from aureon_mt5.strategies import get_strategy
    comp = {"M45": {"state": "pre", "gap_x_range": -0.2, "gap_slope3": 0.4, "bars_since_cross": -7}}
    ag = SimpleNamespace(S=get_strategy("ema5080"), off=3.0, symbol="XAUUSD", news=(), journal=SimpleNamespace(read=lambda *a: []),
                         cfg=SimpleNamespace(htf_align="show"), htf_alignment=lambda side: {"compact": comp})
    df = cframe(); snap = build_snapshot(ag, "ENTER", "LONG", int(df["time"].iloc[-1]), df)
    assert snap["htf_align"] == comp and "htf_align" in SNAPSHOT_KEEP
    ag.cfg.htf_align = "off"
    assert "htf_align" not in build_snapshot(ag, "ENTER", "LONG", int(df["time"].iloc[-1]), df)
    src = open(os.path.join(ROOT, "aureon_mt5", "claude_advisor.py"), encoding="utf-8").read()
    assert '("htf_align", HTF_RULES)' in src and "`pre`" in HTF_RULES


def test_chart_panel(tmp_path):
    pytest.importorskip("matplotlib"); pytest.importorskip("PIL")
    from PIL import Image
    from aureon_mt5.common.htf_chart import add_htf_panel
    base = os.path.join(str(tmp_path), "c.png")
    Image.new("RGB", (800, 400), (0, 0, 0)).save(base)
    frames = {"M45": crossed_up(3, n=60), "H1": crossed_up(5, n=60)}
    out = add_htf_panel(base, frames, ("ema_fast", "ema_slow"))
    assert out and out.endswith("_htf.png") and Image.open(out).size[0] == 800 and Image.open(out).size[1] > 400
    assert add_htf_panel(base, {"H1": None}, ("ema_fast", "ema_slow")) is None


# ----------------------------------------------------------------------------- harness at M30 (seeded synthetic month)
def synthetic_month(path, seed=7, days=30):
    rng = np.random.default_rng(seed); step = 900; n = days * 86400 // step
    drift = np.repeat(rng.normal(0, 0.9, n // 32 + 1), 32)[:n]
    close = 3300 + np.cumsum(drift + rng.normal(0, 3.0, n)); op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) + rng.gamma(2, 1.2, n); lo = np.minimum(op, close) - rng.gamma(2, 1.2, n)
    t = 1754006400 + np.arange(n) * step
    keep = ((t // 86400 + 3) % 7) < 5
    bars = [{"time": int(a), "open": float(b), "high": float(c), "low": float(d), "close": float(e), "volume": 100}
            for a, b, c, d, e, k in zip(t, op, hi, lo, close, keep) if k]
    json.dump({"bars": bars}, open(path, "w"))
    return path


def test_harness_m30_block_vs_off(tmp_path, capsys):
    import sys
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import replay_guardian as RG
    p = synthetic_month(os.path.join(str(tmp_path), "m15.json"))
    res = RG.main(["--bars", p, "--out", str(tmp_path), "--compare", "--set", "exec_tf=M30", "--set", "htf_align=block",
                   "--set", "htf_confirm_tfs=M45,H1", "--set", "guardian_profile=follow", "--set", "follow_stop_x_range=1.5"])
    out = capsys.readouterr().out
    c, b = res["combo"], res["defaults"]
    assert "htf_align=off (same set)" in out and "asks_blocked_by_htf" in out and "would_have_won" in out
    assert "entered on `pre`" in out and "entered on `with`" in out
    assert c["asks_blocked_by_htf"] == c["htf_would_have_won"] + c["htf_would_have_lost"]
    assert c["trades"] + c["asks_blocked_by_htf"] == b["trades"]                 # every off trade is either taken or blocked
    assert c["by_htf_entry"]["pre"]["trades"] + c["by_htf_entry"]["with"]["trades"] == c["trades"]
    assert "asks_blocked_by_htf" not in b
    assert T.exec_tf() == "M5"                                                   # the harness puts the process back
