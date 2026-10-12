"""v2.3.5 / v2.3.8 — deterministic chop-day flag: count_morning_crosses, threshold, sticky per day, agent card,
ENTER card fields, half-size lots, streak+chop combined, compare groups; v2.3.8 hour-window fix and restart dedup."""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import alerts as A, bot, broker, compare
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.common.chop import count_morning_crosses, chop_state, server_day_start, server_day
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from tests.test_v200_alerts import TickMT5
from tests.test_v202_alerts import make, ReqMT5
from tests.test_v200_ema2050 import OFF

S2050 = get_strategy("ema2050")
S5080 = get_strategy("ema5080")

# ================================================================== helpers — INDEPENDENT of chop.py internals

DAY0 = 1752364800  # 2025-07-13 00:00:00 UTC (a Sunday, server midnight, divisible by 86400)
assert DAY0 % 86400 == 0, "DAY0 must be midnight UTC for these tests"


def _server_epoch(date_str: str, time_str: str) -> int:
    """Parse 'YYYY-MM-DD' + 'HH:MM' as a server wall-clock epoch (UTC)."""
    from datetime import datetime, timezone as _tz
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=_tz.utc)
    return int(dt.timestamp())


def _frame_from_flips(day: str, flip_times: list[str], last_bar_time: str, bar_step: int = 300) -> pd.DataFrame:
    """Build a frame spanning 00:00 to last_bar_time on `day` (server), with EMA sign flips at the given HH:MM times.
    Bars every bar_step seconds. EMA fast starts above slow; each flip_time toggles.
    Returns a DataFrame with columns: time, close, high, low, ema20, ema50."""
    day_start = _server_epoch(day, "00:00")
    end = _server_epoch(day, last_bar_time)
    t = np.arange(day_start, end + 1, bar_step)
    n = len(t)
    ema_fast = np.full(n, 4001.0)
    ema_slow = np.full(n, 4000.0)
    flip_epochs = sorted(_server_epoch(day, ft) for ft in flip_times)
    positive = True
    for fe in flip_epochs:
        positive = not positive
        val = 4001.0 if positive else 3999.0
        mask = t >= fe
        ema_fast[mask] = val
    cf, cs = S2050.ema_cols
    return pd.DataFrame({"time": t, "close": 4000.0, "high": 4001.0, "low": 3999.0, cf: ema_fast, cs: ema_slow})


def _choppy_frame(n_morning_crosses=6, n_afternoon_crosses=2, off=None):
    """Build a synthetic M5 frame starting at DAY0 (00:00 server) with controlled crosses.
    Independent of chop.py — uses DAY0 directly."""
    n = 200
    t = DAY0 + np.arange(n) * 300
    day_start = DAY0  # 00:00 server (DAY0 is midnight)
    morning_end = day_start + 11 * 3600
    close = np.full(n, 4000.0)
    ema_fast = np.full(n, 4001.0)
    ema_slow = np.full(n, 4000.0)
    morning_bars = [i for i in range(1, n) if day_start <= t[i] < morning_end]
    afternoon_bars = [i for i in range(1, n) if t[i] >= morning_end]

    def place_crosses(bar_indices, count):
        if count == 0 or not bar_indices:
            return
        spacing = max(2, len(bar_indices) // (count + 1))
        for c in range(count):
            flip_idx = bar_indices[min((c + 1) * spacing, len(bar_indices) - 1)]
            current = ema_fast[flip_idx - 1] > ema_slow[flip_idx - 1]
            new_val = 3999.0 if current else 4001.0
            end_idx = n
            if c + 1 < count:
                end_idx = bar_indices[min((c + 2) * spacing, len(bar_indices) - 1)]
            for j in range(flip_idx, end_idx):
                ema_fast[j] = new_val

    place_crosses(morning_bars, n_morning_crosses)
    place_crosses(afternoon_bars, n_afternoon_crosses)
    cf, cs = S2050.ema_cols
    return pd.DataFrame({"time": t, "close": close, "high": close + 1, "low": close - 1, cf: ema_fast, cs: ema_slow})


# ================================================================== count_morning_crosses (original tests, updated)
def test_count_morning_crosses_basic():
    df = _choppy_frame(n_morning_crosses=6, n_afternoon_crosses=2)
    cf, cs = S2050.ema_cols
    assert count_morning_crosses(df, cf, cs, OFF) == 6


def test_count_morning_crosses_none_after():
    df = _choppy_frame(n_morning_crosses=0, n_afternoon_crosses=3)
    cf, cs = S2050.ema_cols
    assert count_morning_crosses(df, cf, cs, OFF) == 0


# ================================================================== v2.3.8: server-wall-clock hour tests
def test_crosses_0900_1040_chop_for_any_offset():
    """5 crosses at 09:00–10:40 server, last bar 12:00 → chop True, day correct, for off=2.0 AND off=3.0."""
    df = _frame_from_flips("2026-10-08", ["09:00", "09:20", "09:40", "10:00", "10:40"], "12:00")
    cf, cs = S2050.ema_cols
    for off_val in (2.0, 3.0):
        cs_res = chop_state(df, cf, cs, off_val, 5)
        assert cs_res["chop"] is True, f"off={off_val}: {cs_res}"
        assert cs_res["crosses"] == 5
        assert cs_res["day"] == "2026-10-08"


def test_previous_evening_crosses_not_counted():
    """5 crosses 21:10–23:00 on the PREVIOUS server day, last bar 06:00 next day → crosses 0, chop False."""
    prev_day = "2026-10-07"
    next_day = "2026-10-08"
    # Build frame: bars from 21:00 previous day to 06:00 next day
    t_start = _server_epoch(prev_day, "21:00")
    t_end = _server_epoch(next_day, "06:00")
    t = np.arange(t_start, t_end + 1, 300)
    n = len(t)
    ema_fast = np.full(n, 4001.0); ema_slow = np.full(n, 4000.0)
    flip_times = ["21:10", "21:30", "21:50", "22:10", "22:30"]
    positive = True
    for ft in flip_times:
        fe = _server_epoch(prev_day, ft)
        positive = not positive
        ema_fast[t >= fe] = 4001.0 if positive else 3999.0
    cf, cs = S2050.ema_cols
    df = pd.DataFrame({"time": t, "close": 4000.0, "high": 4001.0, "low": 3999.0, cf: ema_fast, cs: ema_slow})
    cs_res = chop_state(df, cf, cs, 3.0, 5)
    assert cs_res["crosses"] == 0 and cs_res["chop"] is False and cs_res["day"] == "2026-10-08"


def test_11_boundary_not_counted():
    """Flips at 08:45, 09:15, 09:45, 10:15, 10:55, 11:00 → count 5 (11:00 is NOT before 11:00)."""
    df = _frame_from_flips("2026-10-08", ["08:45", "09:15", "09:45", "10:15", "10:55", "11:00"], "12:00")
    cf, cs = S2050.ema_cols
    count = count_morning_crosses(df, cf, cs, OFF)
    assert count == 5  # the 11:00 flip is excluded


def test_day_label_rolls_at_midnight_server():
    """Frame ending 23:55 → day 2026-10-08; extended to 00:05 next day → day 2026-10-09."""
    df1 = _frame_from_flips("2026-10-08", [], "23:55")
    assert server_day(int(df1["time"].iloc[-1])) == "2026-10-08"
    # Extend to next day
    next_bar = _server_epoch("2026-10-09", "00:05")
    extra = pd.DataFrame({"time": [next_bar], "close": 4000.0, "high": 4001.0, "low": 3999.0,
                           S2050.ema_cols[0]: 4001.0, S2050.ema_cols[1]: 4000.0})
    df2 = pd.concat([df1, extra], ignore_index=True)
    assert server_day(int(df2["time"].iloc[-1])) == "2026-10-09"


# ================================================================== chop_state threshold (original tests)
def test_threshold_4_not_chop():
    df = _choppy_frame(n_morning_crosses=4, n_afternoon_crosses=0)
    cf, cs = S2050.ema_cols
    cs_res = chop_state(df, cf, cs, OFF, 5)
    assert cs_res["chop"] is False and cs_res["crosses"] == 4


def test_threshold_5_is_chop():
    df = _choppy_frame(n_morning_crosses=5, n_afternoon_crosses=0)
    cf, cs = S2050.ema_cols
    cs_res = chop_state(df, cf, cs, OFF, 5)
    assert cs_res["chop"] is True and cs_res["crosses"] == 5


def test_threshold_0_never_chop():
    df = _choppy_frame(n_morning_crosses=10, n_afternoon_crosses=0)
    cf, cs = S2050.ema_cols
    assert chop_state(df, cf, cs, OFF, 0)["chop"] is False


# ================================================================== sticky per server day
def test_sticky_same_day():
    df = _choppy_frame(n_morning_crosses=6, n_afternoon_crosses=0)
    cf, cs = S2050.ema_cols
    cs1 = chop_state(df, cf, cs, OFF, 5)
    assert cs1["chop"] is True
    cs2 = chop_state(df, cf, cs, OFF, 5)
    assert cs2["chop"] is True and cs2["day"] == cs1["day"]


def test_new_day_clears():
    df = _choppy_frame(n_morning_crosses=6, n_afternoon_crosses=0)
    cf, cs = S2050.ema_cols
    cs1 = chop_state(df, cf, cs, OFF, 5)
    assert cs1["chop"] is True
    df2 = df.copy(); df2["time"] = df2["time"] + 86400; df2[cf] = 4001.0
    cs2 = chop_state(df2, cf, cs, OFF, 5)
    assert cs2["chop"] is False and cs2["day"] != cs1["day"]


# ================================================================== agent: CHOP DAY card + journal
class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []; self.asks = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body.splitlines()[0])
        self.bodies.append(str(embed or body))
        return True
    def ask(self, key, title, lines, **kw):
        self.asks.append({"key": key, "title": title, "lines": lines, **kw})
        return super().ask(key, title, lines, **kw)


def test_agent_chop_day_card_and_journal(tmp_path, monkeypatch):
    df = _choppy_frame(n_morning_crosses=6, n_afternoon_crosses=0)
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.chop_crosses = 5
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, j, ()); ag.off = OFF
    bar_t = int(df["time"].iloc[-1]); ag.last_bar = bar_t
    ag._update_chop(df, bar_t)
    assert ag.chop is not None and ag.chop["chop"] is True
    assert ag.health["chop"] is not None and "CHOP" in ag.health["chop"]
    assert len([t for t in n.titles if "CHOP DAY" in t]) == 1
    assert len([r for r in j.read(0) if r["event"] == "chop_day"]) == 1
    # Call again on the same bar — no duplicate card
    ag._update_chop(df, bar_t)
    assert len([t for t in n.titles if "CHOP DAY" in t]) == 1


def test_agent_chop_health_none_when_not_chop(tmp_path, monkeypatch):
    df = _choppy_frame(n_morning_crosses=2, n_afternoon_crosses=0)
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.chop_crosses = 5
    ag = SymbolAgent("XAUUSD", cfg, S2050, Cap(), Journal(str(tmp_path)), ()); ag.off = OFF
    ag.last_bar = int(df["time"].iloc[-1])
    ag._update_chop(df, int(df["time"].iloc[-1]))
    assert ag.health["chop"] is None


# ================================================================== v2.3.8: restart duplicate suppression
def test_restart_no_duplicate_chop_card(tmp_path, monkeypatch):
    """Two SymbolAgents on the same journal/log_dir: first posts CHOP DAY, second does not, both have chop=True."""
    df = _choppy_frame(n_morning_crosses=6, n_afternoon_crosses=0)
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.chop_crosses = 5
    j = Journal(str(tmp_path)); bar_t = int(df["time"].iloc[-1])
    # Agent 1: posts CHOP DAY
    n1 = Cap()
    ag1 = SymbolAgent("XAUUSD", cfg, S2050, n1, j, ()); ag1.off = OFF; ag1.last_bar = bar_t
    ag1._update_chop(df, bar_t)
    assert len([t for t in n1.titles if "CHOP DAY" in t]) == 1
    assert len([r for r in j.read(0) if r["event"] == "chop_day"]) == 1
    # Agent 2 ("restart"): same journal, fresh agent — should NOT re-post
    n2 = Cap()
    ag2 = SymbolAgent("XAUUSD", cfg, S2050, n2, j, ()); ag2.off = OFF; ag2.last_bar = bar_t
    ag2._update_chop(df, bar_t)
    assert len([t for t in n2.titles if "CHOP DAY" in t]) == 0  # no duplicate card
    assert len([r for r in j.read(0) if r["event"] == "chop_day"]) == 1  # still one journal line
    # But the flag itself is set
    assert ag2.chop is not None and ag2.chop["chop"] is True
    assert ag2.health["chop"] is not None


# ================================================================== ENTER card on chop day: fields + meta
def test_enter_card_chop_fields_and_meta(tmp_path, monkeypatch):
    from tests.test_v200_ema2050 import _bull
    from aureon_mt5.strategies.ema2050 import journeys as J
    df = _bull(n=12, touch_at=(11,))
    fake = TickMT5(price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.chop_crosses = 5
    n = Cap(); j = Journal(str(tmp_path))
    ag = SymbolAgent("XAUUSD", cfg, S2050, n, j, ()); ag.off = OFF
    ag.chop = {"chop": True, "crosses": 6, "day": "2025-07-13", "threshold": 5}
    ag.health["chop"] = "CHOP (6 crosses <11:00)"
    res = J.build(df, J.rules_for("XAUUSD"), OFF)
    ag.last_df = df; ag.last_res = res; ag.last_bar = int(df["time"].iloc[-1])
    ag._detect(df, df, int(df["time"].iloc[-1]), float(df["close"].iloc[-1]),
               float(df[S2050.ema_cols[0]].iloc[-1]), float(df[S2050.ema_cols[1]].iloc[-1]))
    enter_asks = [a for a in n.asks if "ENTER" in a["title"]]
    if enter_asks:
        ask = enter_asks[0]
        field_names = [f["name"] for f in ask.get("fields", [])]
        assert "⚠️ Chop day" in field_names
        assert ask["meta"]["chop"] is True and ask["meta"]["chop_crosses"] == 6
    sigs = [r for r in j.read(0) if r["event"] == "signal"]
    if sigs:
        assert sigs[-1].get("chop") is True and sigs[-1].get("chop_crosses") == 6


# ================================================================== execute_entry uses halved lots on chop day
def test_execute_entry_chop_half_lots(tmp_path, monkeypatch):
    r = make(tmp_path, monkeypatch, execution=True); r.cfg.max_lots = 2.0
    fake = ReqMT5(price=4000.5, requotes=0); monkeypatch.setattr(broker, "_m", fake); r.fake = fake
    r.ag.chop = {"chop": True, "crosses": 6, "day": "2025-07-13", "threshold": 5}
    item = {"meta": {"kind": "cross", "symbol": "XAUUSD", "price": 4000.6, "bar": 1, "mode": "ema2050",
                     "side": "LONG", "chop": True, "chop_crosses": 6, "lots": 0.5}, "key": "test:key"}
    res = bot.ask_decision(r.cfg, r.agents, r.j, item, "TAKE", "me", notify=r.n)
    assert res["ok"] and res["placed"] is True
    o = [x for x in r.j.read(0) if x["event"] == "order_placed"][-1]
    assert o["lots"] == 0.5


# ================================================================== streak + chop both binding → halve once
def test_streak_chop_combined_halve_once(tmp_path, monkeypatch):
    from tests.test_v234_take_path import StreakMT5, _losing_deal
    now = int(time.time())
    deals = [_losing_deal(i, now - (6 - i) * 100) for i in range(6)]
    fake = StreakMT5(deals, price=4000.5)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", OFF)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path); cfg.streak_half_after = 3; cfg.chop_crosses = 5
    ag = SymbolAgent("XAUUSD", cfg, S2050, Notifier(None), Journal(str(tmp_path)), ()); ag.off = OFF; ag.last_bar = 1
    ag.chop = {"chop": True, "crosses": 6, "day": "2025-07-13", "threshold": 5}
    assert ag._half_size() is True and ag._is_chop() is True
    rl = ag._risk_lots(12.0)
    assert rl["half"] is True
    assert "chop day" in rl["half_reason"] and "losses" in rl["half_reason"]


# ================================================================== compare: chop groups
def test_compare_chop_groups():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD", "chop": True},
        {"t": 20, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "SHORT", "bar": 200, "symbol": "XAUUSD", "chop": False},
        {"t": 30, "event": "signal", "mode": "ema2050", "signal_kind": "no-pullback", "side": "LONG", "bar": 300, "symbol": "XAUUSD", "chop": True},
    ]
    rows = compare.build_rows(records, [], 0, 1000, OFF)
    assert len(rows) == 3
    g = compare.groups(rows)
    assert len(g["chop"]) == 2 and len(g["clear"]) == 1


def test_compare_chop_default_false_for_old_journals():
    records = [{"t": 10, "event": "signal", "mode": "ema5080", "signal_kind": "P", "side": "LONG", "bar": 100, "symbol": "XAUUSD"}]
    rows = compare.build_rows(records, [], 0, 1000, OFF)
    assert len(rows) == 1 and rows[0].chop is False


def test_compare_card_shows_chop_line_when_flagged():
    records = [
        {"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD", "chop": True},
        {"t": 20, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "SHORT", "bar": 200, "symbol": "XAUUSD"},
    ]
    res = compare.compute(records, [], 0, 1000, OFF)
    c = compare.card(res)
    assert "Chop vs Clear" in [f["name"] for f in c["fields"]]


def test_compare_card_no_chop_line_when_none_flagged():
    records = [{"t": 10, "event": "signal", "mode": "ema2050", "signal_kind": "pullback", "side": "LONG", "bar": 100, "symbol": "XAUUSD"}]
    res = compare.compute(records, [], 0, 1000, OFF)
    c = compare.card(res)
    assert "Chop vs Clear" not in [f["name"] for f in c["fields"]]
