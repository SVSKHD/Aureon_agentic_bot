"""v2.3.1 — ema2050 parity with research/ema2050_scan.py.

Rule tests (always run): touch counted from the bar AFTER the cross · max_chase only on the fallback · no IST window ·
MT5 CSV exports with tick_volume + real_volume + spread · the live agent has no second implementation.
Parity / acceptance tests run when the research inputs are in the repo:
  research/ema2050_scan.py                 the research scanner
  tests/fixtures/xauusd_m5_jul2026.csv     July 2026 XAUUSD M5 (time, open, high, low, close, tick_volume, …)
The scanner is expected to expose `scan(df)` (or `entries(df)`) taking an M5 frame with time/open/high/low/close and returning
the entries — ints (server epoch), or dicts / objects with `entry_time` (or `time`)."""
import ast, dataclasses, importlib.util, os, sys
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5.common import source
from aureon_mt5.strategies import get_strategy
from aureon_mt5.strategies.ema2050 import journeys as J
from tests.test_v200_ema2050 import DAY0, OFF, RULES, _bull, build, frame, labels

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(ROOT, "research", "ema2050_scan.py")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "xauusd_m5_jul2026.csv")
S = get_strategy("ema2050")


# ------------------------------------------------------------------ 1) touch window starts the bar AFTER the cross
def test_touch_on_the_cross_bar_does_not_count():
    res = build(_bull(touch_at=(5,)))                                         # bar 5 = the cross bar itself
    x = res["cross_infos"][0]
    assert not x.touched_before_confirm and x.entry_index == 19 and x.entry_kind == "no-pullback"


@pytest.mark.parametrize("k", [6, 7])                                        # (cross, confirm] = bars 6..7
def test_touch_after_cross_up_to_confirm_enters_at_confirm(k):
    res = build(_bull(touch_at=(k,)))
    x = res["cross_infos"][0]
    assert x.touched_before_confirm and x.entry_index == 7 and res["journeys"][0].notes == "pullback"


def test_first_touch_after_confirm_then_fallback_window():
    assert build(_bull(touch_at=(19,)))["cross_infos"][0].entry_index == 19      # confirm+12 touch = pullback, not fallback
    assert build(_bull(touch_at=(19,)))["cross_infos"][0].entry_kind == "pullback"
    assert build(_bull(touch_at=(20,)))["cross_infos"][0].entry_index == 19      # beyond the window: the fallback at confirm+12


# ------------------------------------------------------------------ 3) max_chase only on the fallback
def test_max_chase_only_on_fallback():
    far_touch = build(_bull(touch_at=(12,), close_at={12: 4010.0}))["cross_infos"][0]
    assert far_touch.entry_index == 12 and far_touch.entry_kind == "pullback"          # 10 from EMA20, still a touch entry
    far_fallback = build(_bull(close_at={19: 4005.1}))["cross_infos"][0]
    assert far_fallback.entry_index is None and far_fallback.fails[19].startswith("too far from EMA20")
    assert build(_bull(close_at={19: 4005.0}))["cross_infos"][0].entry_index == 19     # exactly 5 is allowed


def test_fallback_needs_the_order_to_hold():
    g = [-2] * 5 + [2.0] * 13 + [-2.0] * 10                                    # flips back to bear at bar 18 < confirm+12
    res = build(frame(g, closes=[4000.5] * 28, lows=[4002.0] * 28))
    assert res["cross_infos"][0].entry_index is None


# ------------------------------------------------------------------ 2) no IST window, server 21:00-23:59 only, news kept
@pytest.mark.parametrize("hour", [0, 1, 2])                                  # server 00-02 = IST 02:30-04:30, outside the old window
def test_entries_allowed_outside_old_ist_window(hour):
    df = _bull(touch_at=(11,)); df["time"] = int(DAY0 + hour * 3600) + np.arange(len(df)) * 300
    assert build(df)["cross_infos"][0].entry_index == 11


def test_server_21_to_24_and_news_still_block():
    assert J.entry_hour_block(DAY0 + 21 * 3600, RULES, OFF).startswith("no-entry hour (server 21:")
    assert J.entry_hour_block(DAY0 + 20 * 3600 + 3300, RULES, OFF) is None
    assert RULES.news_filter and RULES.news_entry_block_min == 60 and RULES.news_rearm_min == 30


# ------------------------------------------------------------------ 4) CSV exports with tick_volume + real_volume (+ spread)
def _write_mt5_csv(path, n=300, t0=DAY0):
    t = t0 + np.arange(n) * 300
    c = 4000 + np.cumsum(np.sin(np.arange(n) / 7.0))
    pd.DataFrame({"time": pd.to_datetime(t, unit="s").strftime("%Y.%m.%d %H:%M"), "open": c, "high": c + 1, "low": c - 1, "close": c,
                  "tick_volume": np.arange(n) + 10, "spread": 20, "real_volume": 0}).to_csv(path, index=False)
    return t


def test_fetch_csv_tick_and_real_volume(tmp_path):
    p = tmp_path / "x.csv"; t = _write_mt5_csv(p)
    for fetch in (source.fetch_csv, __import__("aureon_mt5.strategies.ema2050.source", fromlist=["fetch_csv"]).fetch_csv):
        b = fetch(str(p), "XAUUSD", "M5")
        assert list(b.df.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert b.df["time"].tolist() == t.tolist() and b.df["volume"].iloc[0] == 10
    p2 = tmp_path / "y.csv"
    pd.DataFrame({"Time": t[:5], "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 7}).to_csv(p2, index=False)
    assert source.fetch_csv(str(p2), "XAUUSD", "M5").df["volume"].tolist() == [7] * 5


def test_csv_source_through_the_range_replay(tmp_path):
    import run as runpy
    p = tmp_path / "x.csv"; _write_mt5_csv(p, n=3 * 288, t0=DAY0 - 2 * 86400)
    days = runpy.replay_range(S, "XAUUSD", "csv", date(2026, 10, 6), date(2026, 10, 7), str(p), OFF, ())
    assert [str(d["day"]) for d in days] == ["2026-10-06", "2026-10-07"]


# ------------------------------------------------------------------ one implementation: live agent == replay
def test_live_agent_uses_the_replay_functions():
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    for k in ("touch_tol", "max_chase", "pullback_window", "confirm_gap", "entry_hours_ist"):
        assert k not in src, f"agent.py re-implements the ema2050 rule {k}"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_detect_2050")
    assert any(isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "events"
               for n in ast.walk(fn))                                          # it reads build()'s events — no rules of its own
    df = _bull(touch_at=(11,))
    raw = df[["time", "open", "high", "low", "close"]].assign(volume=0)
    from aureon_mt5.common.source import Bars
    live = S.analyse({"M5": Bars("XAUUSD", "M5", raw)}, "XAUUSD", OFF, ())["M5"]
    direct = J.build(S.add_emas(raw), RULES, OFF)
    assert [j.entry_time for j in live["journeys"]] == [j.entry_time for j in direct["journeys"]]


# ------------------------------------------------------------------ research parity + acceptance (need the research inputs)
needs_research = pytest.mark.skipif(not (os.path.exists(SCANNER) and os.path.exists(FIXTURE)),
                                    reason="research/ema2050_scan.py and tests/fixtures/xauusd_m5_jul2026.csv are not in the repo yet")


def _scanner_entries(df) -> list[int]:
    spec = importlib.util.spec_from_file_location("ema2050_scan", SCANNER)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    fn = getattr(mod, "scan", None) or getattr(mod, "entries", None)
    assert fn is not None, "research/ema2050_scan.py exposes neither scan(df) nor entries(df)"
    out = []
    for e in fn(df.copy()):
        v = e if isinstance(e, (int, np.integer)) else (e.get("entry_time", e.get("time")) if isinstance(e, dict)
                                                        else getattr(e, "entry_time", getattr(e, "time", None)))
        out.append(int(pd.Timestamp(v).timestamp()) if not isinstance(v, (int, np.integer, float)) else int(v))
    return sorted(out)


@needs_research
def test_replay_entry_times_equal_the_research_scanner():
    import run as runpy
    raw = source.fetch_csv(FIXTURE, "XAUUSD", "M5").df
    days = runpy.replay_range(S, "XAUUSD", "csv", date(2026, 7, 1), date(2026, 7, 31), FIXTURE, OFF, ())
    replay = sorted(j.entry_time for d in days for j in d["journeys"] if j.exit_reason != "open")
    research = [t for t in _scanner_entries(raw) if date(2026, 7, 1) <= pd.Timestamp(t, unit="s").date() <= date(2026, 7, 31)]
    assert replay == research


@needs_research
def test_acceptance_july_2026():
    import run as runpy
    days = runpy.replay_range(S, "XAUUSD", "csv", date(2026, 7, 1), date(2026, 7, 31), FIXTURE, OFF, ())
    closed = [j for d in days for j in d["journeys"] if j.exit_reason != "open"]
    assert len(closed) == 63 and round(sum(j.result for j in closed), 1) == 222.5
    assert sum(j.exit_reason == "stop" for j in closed) == 8
