"""v1.11.0 — weekly compare (Detector vs Claude vs Me). Measurement only; grading comes from a FAKE scorecard here,
exactly as the real one would be reused (reports.graded_signals → reports.scorecard)."""
import asyncio, os, sys, time, types
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import pytest
from aureon_mt5 import broker, compare, reports
from aureon_mt5.compare import Metrics, verdict
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier

OFF = 3.0
IST = compare.IST
T0 = int(datetime(2026, 10, 5, 12, 0, tzinfo=IST).timestamp())     # Monday 12:00 IST (journal wall clock)
B = int(T0 + OFF * 3600)                                             # same moment as an MT5 server-epoch bar
SINCE, UNTIL = T0 - 3600, T0 + 5 * 86400

# i, signal_kind, side, grade, points, claude verdict, me
TABLE = [
    (0, "P",       "LONG",  "WIN10", 10.0, "TAKE",  "button"),
    (1, "cross",   "LONG",  "WIN20", 20.0, "TAKE",  "skip"),
    (2, "reentry", "SHORT", "STOP",  -6.0, "TAKE",  "mt5"),
    (3, "P",       "SHORT", "STOP",  -6.0, "SKIP",  None),
    (4, "cross",   "SHORT", "FLAT",   2.0, "SKIP",  "button"),
    (5, "reentry", "LONG",  "STOP",  -6.0, "timeout", None),
    (6, "P",       "LONG",  "OPEN",  None, "TAKE",  None),
    (7, "cross",   "LONG",  None,    None, "SKIP",  None),          # scorecard has no grade → UNGRADED
    (8, "P",       "SHORT", "WIN10", 12.0, "stale", None),
]
EV = {"P": "P", "cross": "CROSS", "reentry": "RE"}


def bar(i): return B + i * 3600


def journal_records():
    recs = []
    for i, k, side, grade, pts, cl, me in TABLE:
        t = T0 + i * 3600 + 300
        recs.append({"t": t, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": side, "signal_kind": k, "price": 4200.0, "bar": bar(i)})
        if cl in ("TAKE", "SKIP"):
            recs.append({"t": t + 30, "event": "claude_verdict", "source": "claude", "claude_event": EV[k], "symbol": "XAUUSD", "bar": bar(i),
                         "decision": cl, "side": "BUY" if side == "LONG" else "SELL", "stale": False, "acted": False, "model": "opus"})
        elif cl == "timeout":
            recs.append({"t": t + 90, "event": "claude_verdict", "claude_event": EV[k], "symbol": "XAUUSD", "bar": bar(i), "decision": None,
                         "error": "timeout", "stale": False, "model": "opus"})
        elif cl == "stale":
            recs.append({"t": t + 700, "event": "claude_verdict", "claude_event": EV[k], "symbol": "XAUUSD", "bar": bar(i), "decision": "TAKE",
                         "stale": True, "model": "opus"})
        if me == "button":
            recs.append({"t": t + 60, "event": "decision", "symbol": "XAUUSD", "bar": bar(i), "decision": "TAKE"})
        elif me == "skip":
            recs.append({"t": t + 60, "event": "decision", "symbol": "XAUUSD", "bar": bar(i), "decision": "SKIP"})
        elif me == "mt5":
            recs.append({"t": t + 600, "event": "position_seen", "symbol": "XAUUSD", "side": side, "ticket": 77, "open_time": bar(i) + 300 + 3 * 300})
    # noise that must never be counted: ema2050 signal, LATE entry, a signal outside the window
    recs.append({"t": T0 + 50, "event": "signal", "symbol": "XAUUSD", "mode": "ema2050", "side": "LONG", "signal_kind": "cross", "bar": B + 50})
    recs.append({"t": T0 + 60, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": "LONG", "signal_kind": "late", "bar": B + 60})
    recs.append({"t": SINCE - 10, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": "LONG", "signal_kind": "P", "bar": B - 99999})
    return recs


def fake_items():
    """Scorecard-shaped items (attribute names exercise the adapter's fallbacks: direction, time)."""
    out = []
    for i, k, side, grade, pts, *_ in TABLE:
        if grade is None:
            continue
        out.append(types.SimpleNamespace(kind=k, direction=side.lower(), time=bar(i), grade=grade, points=pts, session="london", symbol="XAUUSD"))
    return out


def graded():
    return [reports._from_scorecard(g) for g in fake_items()]


def res():
    return compare.compute(journal_records(), graded(), SINCE, UNTIL, OFF)


def m(n=0, exp=None, total=0.0):
    return Metrics(n=n, expectancy=exp, total=total)


# ------------------------------------------------------------------ exact table
EXPECTED = {   # n, wins, expectancy, total, stops, worst_streak, biggest_loss, open, ungraded
    "detector":    (7, 3, 3.71, 26.0, 3, 2, -6.0, 1, 1),
    "claude_take": (3, 2, 8.0, 24.0, 1, 1, -6.0, 1, 0),
    "claude_skip": (2, 0, -2.0, -4.0, 1, 1, -6.0, 0, 1),
    "me_taken":    (3, 1, 2.0, 6.0, 1, 1, -6.0, 0, 0),
    "me_skipped":  (4, 2, 5.0, 20.0, 2, 2, -6.0, 1, 1),
    "dis_ct_ms":   (1, 1, 20.0, 20.0, 0, 0, 0.0, 1, 0),
    "dis_cs_mt":   (1, 0, 2.0, 2.0, 0, 0, 0.0, 0, 0),
}


@pytest.mark.parametrize("key", list(EXPECTED))
def test_exact_numbers_every_row(key):
    x = res().metrics[key]
    assert (x.n, x.wins, x.expectancy, x.total, x.stops, x.worst_streak, x.biggest_loss, x.open, x.ungraded) == EXPECTED[key]
    if x.n:
        assert x.win_rate == pytest.approx(EXPECTED[key][1] / EXPECTED[key][0])


def test_same_signal_set_and_noise_excluded():
    r = res()
    assert len(r.rows) == 9 and {x.bar for x in r.rows} == {bar(i) for i, *_ in TABLE}
    for k in ("claude_take", "claude_skip", "me_taken", "me_skipped", "no_verdict"):
        assert {id(x) for x in r.groups[k]} <= {id(x) for x in r.rows}


def test_no_verdict_excluded_from_claude_rows():
    r = res()
    nv = {x.bar for x in r.groups["no_verdict"]}
    assert nv == {bar(5), bar(8)}                     # timeout + STALE
    assert not nv & {x.bar for x in r.groups["claude_take"] + r.groups["claude_skip"]}


def test_open_and_ungraded_counted_not_scored():
    det = res().metrics["detector"]
    assert det.open == 1 and det.ungraded == 1 and det.n == 7


def test_me_taken_via_button_and_mt5():
    r = res()
    by = {x.bar: x for x in r.rows}
    assert by[bar(0)].me_via == "button" and by[bar(2)].me_via == "mt5" and by[bar(1)].me == "SKIPPED"


@pytest.mark.parametrize("bars_after,taken", [(6, True), (7, False)])
def test_mt5_match_window(bars_after, taken):
    recs = [{"t": T0, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": "LONG", "signal_kind": "P", "bar": B},
            {"t": T0 + 9000, "event": "position_seen", "symbol": "XAUUSD", "side": "LONG", "open_time": B + 300 + bars_after * 300}]
    rows = compare.build_rows(recs, [], SINCE, UNTIL, OFF)
    assert (rows[0].me == "TAKEN") is taken


def test_opposite_side_trade_not_matched():
    recs = [{"t": T0, "event": "signal", "symbol": "XAUUSD", "mode": "ema5080", "side": "LONG", "signal_kind": "P", "bar": B},
            {"t": T0 + 900, "event": "position_seen", "symbol": "XAUUSD", "side": "SHORT", "open_time": B + 600}]
    assert compare.build_rows(recs, [], SINCE, UNTIL, OFF)[0].me == "SKIPPED"


# ------------------------------------------------------------------ verdicts
def test_not_enough_data_only_line():
    v = res().verdict
    assert v == ["Not enough data yet — 5/30 signals. Keep advisory running."]


def test_verdict_helps():
    v = verdict({"detector": m(40, 1.0, 40.0), "claude_take": m(20, 2.0, 40.0), "claude_skip": m(15, -1.0, -15.0), "me_taken": m(10, 1.5, 15.0)})
    assert v[0].startswith("✅ Claude helps") and "added 0.5" in v[1]


def test_verdict_skipping_good_trades():
    v = verdict({"detector": m(40, 1.0, 40.0), "claude_take": m(20, 2.0, 40.0), "claude_skip": m(15, 1.0, 15.0), "me_taken": m(0)})
    assert v[0].startswith("⚠️ Claude is skipping good trades") and "none of the graded" in v[1]


@pytest.mark.parametrize("take", [m(20, 0.5, 10.0), m(20, 1.5, 30.0)])      # (2) fails · (3) fails (30 < 0.8×40)
def test_verdict_mixed(take):
    v = verdict({"detector": m(40, 1.0, 40.0), "claude_take": take, "claude_skip": m(15, -1.0, -15.0), "me_taken": m(10, 0.8, 8.0)})
    assert v[0].startswith("➖ Mixed") and "cost 0.2" in v[1]


def test_disagreements():
    assert res().disagree == {"n": 2, "claude": 1, "you": 1, "tie": 0}


def test_disagreement_loss_and_tie():
    rows = [compare.Row("XAUUSD", "P", "LONG", 1, "London", "GRADED", "STOP", -6.0, None, "TAKE", "SKIPPED"),
            compare.Row("XAUUSD", "P", "LONG", 2, "London", "GRADED", "STOP", -6.0, None, "SKIP", "TAKEN"),
            compare.Row("XAUUSD", "P", "LONG", 3, "London", "GRADED", "FLAT", 0.0, None, "SKIP", "TAKEN")]
    assert compare.disagreements(compare.groups(rows)) == {"n": 3, "claude": 1, "you": 1, "tie": 1}


# ------------------------------------------------------------------ pullbacks
def test_pullback_scoring():
    V = 1_000_000
    def pv(tk, dec, at, **kw):
        return {"t": T0 + tk, "event": "claude_verdict", "claude_event": "pullback", "symbol": "XAUUSD", "ticket": tk, "decision": dec,
                "open_profit": at, "bar": V, "acted": kw.get("acted", False), "stale": kw.get("stale", False), "tighten_to": kw.get("tt")}
    def cl(tk, final, entry=4200.0):
        return {"t": V + 3000, "event": "closed", "symbol": "XAUUSD", "ticket": tk, "direction": "long", "entry": entry, "final_points": final}
    recs = [pv(1, "HOLD", 5.0), cl(1, 8.0), pv(2, "HOLD", 5.0), cl(2, 2.0),
            pv(3, "CLOSE", 6.0), cl(3, 1.0), pv(4, "CLOSE", 6.0), cl(4, 9.0),
            pv(5, "TIGHTEN", 8.0, tt=4205.0), cl(5, 2.0), pv(6, "TIGHTEN", 8.0, tt=4203.0), cl(6, 9.0),
            pv(7, "CLOSE", 6.0, acted=True), cl(7, 6.0), pv(8, "HOLD", 4.0, stale=True), pv(9, "HOLD", 4.0)]
    bars = pd.DataFrame({"time": [V + 300, V + 600, V + 900], "high": [4210.0] * 3, "low": [4206.0, 4204.0, 4206.0]})
    # ticket 5: low 4204 ≤ 4205 → hit → +5 vs final +2 → right (+3) · ticket 6: 4203 never reached → continued → right (0)
    ps = compare.pullbacks(recs, T0 - 10, T0 + 100, 0.0, bars)
    assert (ps.n, ps.right, ps.diff_points, ps.applied, ps.stale, ps.ungraded) == (6, 4, 5.0, 1, 1, 1)
    assert ps.by_decision == {"HOLD": {"n": 2, "right": 1}, "CLOSE": {"n": 2, "right": 1}, "TIGHTEN": {"n": 2, "right": 2}}


def test_tighten_without_bars_is_ungraded():
    recs = [{"t": T0, "event": "claude_verdict", "claude_event": "pullback", "symbol": "XAUUSD", "ticket": 5, "decision": "TIGHTEN",
             "open_profit": 8.0, "bar": 1, "tighten_to": 4205.0},
            {"t": T0 + 60, "event": "closed", "ticket": 5, "direction": "long", "entry": 4200.0, "final_points": 2.0}]
    ps = compare.pullbacks(recs, T0 - 10, T0 + 100, 0.0, None)
    assert ps.n == 0 and ps.ungraded == 1


# ------------------------------------------------------------------ grading source (adapter, never a second method)
def test_graded_signals_missing_scorecard(monkeypatch):
    monkeypatch.delattr(reports, "scorecard", raising=False)
    g, note = reports.graded_signals(Config(), {"XAUUSD": object()}, None, 0)
    assert g == [] and "scorecard not available" in note


def test_graded_signals_unrecognised_fields(monkeypatch):
    monkeypatch.setattr(reports, "scorecard", lambda cfg, ag, j, since: ([types.SimpleNamespace(kind="P", grade="WIN10", foo=1)], None), raising=False)
    g, note = reports.graded_signals(Config(), {"XAUUSD": object()}, None, 0)
    assert g == [] and "fields not recognised" in note and "foo" in note


def test_graded_signals_maps_fields(monkeypatch):
    monkeypatch.setattr(reports, "scorecard", lambda cfg, ag, j, since: (fake_items(), None), raising=False)
    g, note = reports.graded_signals(Config(), {"XAUUSD": object()}, None, 0)
    assert note is None and len(g) == 8 and g[0].kind == "P" and g[0].side == "LONG" and g[0].points == 10.0 and g[2].kind == "RE"


def test_mismatched_bar_times_are_reported():
    bad = [reports._from_scorecard(types.SimpleNamespace(**{**vars(x), "time": x.time + 7})) for x in fake_items()]
    r = compare.compute(journal_records(), bad, SINCE, UNTIL, OFF)
    assert "none matched" in r.grading_note and r.metrics["detector"].n == 0


# ------------------------------------------------------------------ breakdown, card, chart, history
def test_breakdown_by_type_and_session():
    bd = compare.breakdown(res().rows)
    assert bd["P"]["detector"].n == 3 and bd["CROSS"]["detector"].n == 2 and bd["RE"]["detector"].n == 2
    assert bd["London"]["detector"].n == 7 and bd["Asia"]["detector"].n == 0


def test_card_and_chart(tmp_path):
    r = compare.compute(journal_records(), graded(), SINCE, UNTIL, OFF, png_path=str(tmp_path / "c.png"))
    c = compare.card(r)
    assert os.path.exists(r.png) and "Detector · all" in c["description"] and "Not enough data" in c["description"]
    assert "models opus" in c["footer"] and f"rules {compare.rules_hash()}" in c["footer"]
    assert any(f["name"] == "Disagreements" and "Claude right 1, you right 1" in f["value"] for f in c["fields"])
    assert len(c["description"]) <= 4096
    assert "P" in compare.breakdown_card(r)["description"]


def test_history_and_trend(tmp_path):
    r = res()
    for w in ("2026-W38", "2026-W39", "2026-W40"):
        compare.append_history(str(tmp_path), w, r)
    h = compare.read_history(str(tmp_path))
    assert len(h) == 3 and compare.latest_summary(str(tmp_path))["rows"]["detector"]["n"] == 7
    assert compare.trend_line(h, r).count("+3.7") == 4


# ------------------------------------------------------------------ Saturday hook
def _cfg(tmp_path):
    c = Config(); c.log_dir = str(tmp_path); c.mode = "ema5080"; c.dry = True; c.server_utc_offset = OFF; c.compare_claude_review = False
    return c


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body); return True


def test_saturday_hook_once_restart_safe(tmp_path):
    import main
    cfg = _cfg(tmp_path); n = Cap(); calls = []
    g = lambda *a, **k: (calls.append(a[3:5]), compare.compute([], [], a[3], a[4], OFF))[1]
    fri = datetime(2026, 10, 9, 23, 0, tzinfo=IST); sat_early = datetime(2026, 10, 10, 9, 59, tzinfo=IST)
    sat = datetime(2026, 10, 10, 10, 0, tzinfo=IST)
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), n, now=fri, gather=g) is None
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), n, now=sat_early, gather=g) is None
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), n, now=sat, gather=g) == "2026-W41"
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), Cap(), now=sat.replace(hour=15), gather=g) is None   # "restart"
    assert len(calls) == 1 and sum("COMPARE · 2026-W41" in t for t in n.titles) == 1
    since, until = calls[0]
    assert datetime.fromtimestamp(since, tz=IST).strftime("%a %H:%M") == "Mon 00:00"
    assert datetime.fromtimestamp(until, tz=IST).strftime("%a %H:%M") == "Sat 00:00"


def test_saturday_hook_runs_for_both_modes_only(tmp_path):
    import main
    cfg = _cfg(tmp_path); cfg.mode = "ema2050"                      # v2.0.0: ema2050 is active -> the Saturday card runs for it too
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), Cap(), now=datetime(2026, 10, 10, 11, 0, tzinfo=IST)) == "2026-W41"
    cfg.mode = "ema1234"
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), Cap(), now=datetime(2026, 10, 10, 11, 0, tzinfo=IST)) is None


def test_no_network_request(tmp_path, monkeypatch):
    import main, requests
    hits = []
    for name in ("post", "get", "put", "patch", "request"):
        monkeypatch.setattr(requests, name, lambda *a, **k: hits.append(a) or (_ for _ in ()).throw(AssertionError("network")))
    cfg = _cfg(tmp_path)
    week = main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), Cap(), now=datetime(2026, 10, 10, 10, 30, tzinfo=IST))
    assert week == "2026-W41" and hits == []
    assert compare.latest_summary(str(tmp_path))["week"] == "2026-W41"


def test_self_review_off_by_default_and_never_writes_rules(tmp_path):
    import main
    cfg = _cfg(tmp_path); assert Config().compare_claude_review is False
    before = open(compare.RULES_PATH, "rb").read()
    calls = []
    claude = types.SimpleNamespace(cfg=types.SimpleNamespace(claude_review_model="haiku"),
                                   _call=lambda *a, **k: calls.append(a) or types.SimpleNamespace(ok=True, text="1\n2\n3\n4\n5\n6"))
    main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), Cap(), claude, now=datetime(2026, 10, 10, 10, 30, tzinfo=IST),
                               gather=lambda *a, **k: res())
    time.sleep(0.2); assert calls == []                                     # flag off → no Claude call
    n = Cap()
    text = compare.self_review(claude, res(), n, "2026-W41")
    assert len(calls) == 1 and text.count("\n") == 4 and any("SELF-REVIEW" in t for t in n.titles)
    assert open(compare.RULES_PATH, "rb").read() == before


# ------------------------------------------------------------------ /compare acks fast with a slow scorecard
def test_compare_command_acks_fast(tmp_path, monkeypatch):
    from aureon_mt5.bot import Reply, respond
    from tests.test_v198_commands import FakeInter
    monkeypatch.setattr(reports, "scorecard", lambda *a, **k: (time.sleep(2.0), (fake_items(), None))[1], raising=False)
    j = Journal(str(tmp_path))
    for r in journal_records():
        t = r.pop("t"); ev = r.pop("event"); j.log(ev, **r)
    cfg = _cfg(tmp_path)
    inter = FakeInter()
    work = lambda: Reply(content=compare.card(compare.gather(cfg, {"XAUUSD": object()}, j, *compare.window_days(7)))["title"])
    asyncio.run(respond(inter, "compare", work, timeout=30, to_kwargs=lambda r: {"content": r.content}))
    assert inter.calls[0][0] == "defer" and inter.calls[0][1] < 0.5 and inter.calls[-1][1] >= 2.0


def test_gather_when_mt5_busy_uses_cache_or_says_so(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(reports, "scorecard", lambda *a, **k: (fake_items(), None), raising=False)
    monkeypatch.setattr(compare, "_GRADE_CACHE", {"at": 0.0, "since": None, "graded": [], "note": None})
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path))
    held, release = threading.Event(), threading.Event()
    def hog():
        with broker._lock:
            held.set(); release.wait(5)
    th = threading.Thread(target=hog); th.start(); held.wait(2)
    try:
        r = compare.gather(cfg, {"XAUUSD": object()}, j, SINCE, UNTIL, lock_timeout=0.2, png_dir=str(tmp_path))
        assert r.grading_note.startswith("MT5 busy — grading skipped")
    finally:
        release.set(); th.join()
    compare.gather(cfg, {"XAUUSD": object()}, j, SINCE, UNTIL, png_dir=str(tmp_path))       # fills the cache
    held.clear(); release.clear(); th = threading.Thread(target=hog); th.start(); held.wait(2)
    try:
        r = compare.gather(cfg, {"XAUUSD": object()}, j, SINCE, UNTIL, lock_timeout=0.2, png_dir=str(tmp_path))
        assert r.grading_note.startswith("MT5 busy — grades from cache")
    finally:
        release.set(); th.join()


# ------------------------------------------------------------------ journal additions (no behaviour change)
def test_position_seen_and_final_points_journaled(tmp_path, monkeypatch):
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.strategies import get_strategy
    from tests.test_v181 import FakeMT5, guard
    fake = FakeMT5(price=4203.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    j = Journal(str(tmp_path)); ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Notifier(None), j, ())
    df = pd.DataFrame({"time": [i * 300 for i in range(200)], "close": 4203.0, "high": 4203.0, "low": 4203.0})
    guard(ag, fake, df, sgn=1); guard(ag, fake, df, sgn=1)
    seen = [r for r in j.read(0) if r["event"] == "position_seen"]
    assert len(seen) == 1 and seen[0]["side"] == "LONG" and seen[0]["ticket"] == 1
    fake.closed = True; guard(ag, fake, df, sgn=1)
    c = [r for r in j.read(0) if r["event"] == "closed"][-1]
    assert c["final_points"] == 3.0 and c["final_approx"] is True          # no deal history in the fake → last polled points


def test_saturday_hook_failure_backs_off(tmp_path):
    import main
    from aureon_mt5 import telemetry
    telemetry.setup(str(tmp_path)); main._COMPARE_RETRY_AT.clear()
    cfg = _cfg(tmp_path); n = Cap(); calls = []
    def boom(*a, **k):
        calls.append(1); raise RuntimeError("scorecard exploded")
    sat = datetime(2026, 10, 10, 10, 0, tzinfo=IST)
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), n, now=sat, gather=boom) is None
    assert main.compare_saturday_tick(cfg, {}, Journal(str(tmp_path)), n, now=sat, gather=boom) is None
    assert len(calls) == 1 and any("COMPARE FAILED" in t for t in n.titles) and compare.read_history(str(tmp_path)) == []
    main._COMPARE_RETRY_AT.clear()
