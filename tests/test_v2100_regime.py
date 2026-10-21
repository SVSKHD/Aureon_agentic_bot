"""v2.10.0 Part D — regime gate: trade when it is clean, never into whipsaw, no fixed count."""
import json, os, re, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import bot, broker, features as F, regime as R
from aureon_mt5.config import Config
from tests.test_v181 import harness  # noqa: F401
from tests.test_claude_advisor import rig, frame as cframe  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def trend(n=400, seed=0):
    rs = np.random.RandomState(seed); t = np.arange(n)
    c = 4000 + 0.6 * t + rs.normal(0, 0.3, n); o = np.concatenate([[c[0]], c[:-1]])
    df = pd.DataFrame({"time": 1_792_000_000 + t * 300, "open": o, "high": np.maximum(o, c) + 0.6, "low": np.minimum(o, c) - 0.6, "close": c})
    df["ema_fast"] = df.close.ewm(span=50, adjust=False).mean(); df["ema_slow"] = df.close.ewm(span=80, adjust=False).mean()
    return df


def tangled(n_trend=200, n_chop=160, seed=4):
    """A trend, then a tangle: price oscillates every 8 bars with shrinking amplitude; EMA 50/80 cross every 8 bars."""
    rs = np.random.RandomState(seed); t = np.arange(n_trend + n_chop); k = np.maximum(0, t - n_trend)
    amp = np.where(t < n_trend, 0, np.maximum(0.15, 4.0 * (1 - k / 110)))
    c = np.where(t < n_trend, 4000 + 0.6 * t, 4000 + 0.6 * n_trend) + amp * np.sin(2 * np.pi * k / 8) + rs.normal(0, 0.1, len(t))
    o = np.concatenate([[c[0]], c[:-1]])
    df = pd.DataFrame({"time": 1_792_000_000 + t * 300, "open": o, "high": np.maximum(o, c) + 0.3, "low": np.minimum(o, c) - 0.3, "close": c})
    ef = df.close.ewm(span=50, adjust=False).mean(); es = df.close.ewm(span=80, adjust=False).mean()
    g = np.where(t < n_trend, (ef - es).to_numpy(), np.where((k // 8) % 2 == 0, 1, -1) * np.maximum(0.05, 1.5 * (1 - k / 110)))
    df["ema_slow"] = es; df["ema_fast"] = es + g
    return df


def run_states(df, start=150):
    tr = R.RegimeTracker(Config())
    return {i: tr.update(df.iloc[: i + 1], int(df["time"].iloc[i]))["state"] for i in range(start, len(df))}


# ------------------------------------------------------------------ classification
def test_clean_trend_stays_clean():
    assert set(run_states(trend()).values()) == {"CLEAN"}


def test_tangle_goes_choppy_within_24_bars_then_dead():
    st = run_states(tangled())
    first_cross = 200 + 8
    choppy_at = min(i for i, s in st.items() if s == "CHOPPY")
    assert choppy_at - first_cross <= 24 + 8                 # 3 crosses need 24 bars; CHOPPY follows within them (+8 hysteresis)
    dead_at = min(i for i, s in st.items() if s == "DEAD")
    assert choppy_at < dead_at <= 320 and "CLEAN" not in {s for i, s in st.items() if i >= choppy_at}


def test_hysteresis():
    c = Config()
    s, k = R.classify(0.9, "CHOPPY", 0, c); assert (s, k) == ("CHOPPY", 1)            # one clean bar does not reopen
    for _ in range(2):
        s, k = R.classify(0.9, s, k, c)
    assert s == "CHOPPY" and k == 3
    s, k = R.classify(0.5, s, k, c); assert (s, k) == ("CHOPPY", 0)                  # streak broken
    for _ in range(4):
        s, k = R.classify(0.6, s, k, c)
    assert s == "CLEAN"                                                                # four do
    assert R.classify(0.95, "DEAD", 0, c)[0] == "CHOPPY"                              # DEAD never jumps to CLEAN
    assert R.classify(0.3, "DEAD", 0, c)[0] == "DEAD"
    assert R.classify(0.2, "CHOPPY", 0, c)[0] == "DEAD" and R.classify(0.4, "CLEAN", 0, c)[0] == "CHOPPY"


def test_no_trade_day_after_ny_open_and_coverage():
    from aureon_mt5.regime import IST
    day = datetime(2026, 10, 15, 5, 30, tzinfo=IST).timestamp()
    log = [(day + i * 300, "CHOPPY" if i % 10 < 6 else "CLEAN") for i in range(160)]       # 60 % bad
    assert not R.no_trade_day(log, datetime(2026, 10, 15, 18, 55, tzinfo=IST), 0.6)        # before NY open
    assert R.no_trade_day(log, datetime(2026, 10, 15, 19, 5, tzinfo=IST), 0.6)
    log2 = [(t, "CHOPPY" if i % 2 else "CLEAN") for i, (t, _) in enumerate(log)]           # 50 %
    assert not R.no_trade_day(log2, datetime(2026, 10, 15, 19, 5, tzinfo=IST), 0.6)


def test_failed_p_counted():
    gap = np.array([1.0] * 10 + [-1.0] * 3 + [1.0] * 11)                                   # short P at 5: cross at 10, back at 13
    assert R.failed_ps(gap, [(5, -1)]) == 1
    assert R.failed_ps(np.array([1.0] * 24), [(5, -1)]) == 1                               # never crossed within 6
    assert R.failed_ps(np.array([1.0] * 24), [(20, -1)]) == 0                              # too young to judge


# ------------------------------------------------------------------ the gate on the agent
def agent_in(harness, state, gate):
    fake, ag, _ = harness(price=4200.0)
    ag.cfg.regime_gate = gate; ag.regime_tracker.state = state; ag.regime_tracker.reasons = ["3 crosses in 2 h", "ADX 14 flat (12)"]
    asks, sends = [], []
    ag.notify.ask = lambda key, title, lines, **kw: asks.append((key, title, kw)) or True
    ag.notify.send = lambda key, title, lines=None, png=None, **kw: sends.append((key, title, lines, kw)) or True
    return ag, asks, sends


def test_off_journals_only(harness):
    fake, ag, _ = harness(price=4200.0)
    df = tangled()
    for i in range(150, 260):
        ag._update_regime(df.iloc[: i + 1], int(df["time"].iloc[i]))
    rows = [r for r in ag.journal.read(0) if r["event"] == "regime_change"]
    assert rows and rows[-1]["new"] in ("CHOPPY", "DEAD") and rows[-1]["reasons"]
    ag2, asks, sends = agent_in(harness, "CHOPPY", "off")
    assert ag2._entry_ask("k", "XAUUSD · MT5 · P PRE-CROSS · LONG", ["x"], fields=[]) and asks and "Regime" not in str(asks[0][2]["fields"])


def test_show_adds_line_and_still_asks(harness):
    ag, asks, sends = agent_in(harness, "CHOPPY", "show")
    assert ag._entry_ask("k", "T", ["x"], fields=[])
    assert asks[0][2]["fields"][-1]["name"] == "Regime" and "CHOPPY" in asks[0][2]["fields"][-1]["value"]


def test_block_cards_not_tradeable(harness):
    ag, asks, sends = agent_in(harness, "DEAD", "block")
    assert ag._entry_ask("k", "XAUUSD · MT5 · CONFIRMED CROSS · LONG", ["place it"], fields=[]) is False
    assert not asks and "CHOP · not tradeable" in sends[0][1] and "3 crosses in 2 h" in sends[0][2][1]
    assert [r for r in ag.journal.read(0) if r["event"] == "entry_blocked_regime"]
    ag.regime_tracker.state = "CLEAN"
    assert ag._entry_ask("k2", "T", ["x"], fields=[]) and asks                               # a CLEAN regime reopens at once


def test_every_entry_site_returns_before_tracking_and_claude():
    src = open(os.path.join(ROOT, "aureon_mt5", "agent.py"), encoding="utf-8").read()
    sites = [m.start() for m in re.finditer(r"asked = self\._entry_ask\(", src)]
    assert len(sites) == 4                     # P / cross / RE (ema5080 + ema2050) + the v2.15.0 ema9200 candidate
    for pos in sites:
        tail = src[pos:pos + 2500]
        guard = tail.index("if not asked:")
        assert guard < tail.index("self.track_ask(") and guard < tail.index("adv.request_entry(")
    assert "self.notify.ask(re_key" not in src and "self.notify.ask(ask_key" not in src


def test_block_refuses_take_with_reasons(harness):
    ag, _, _ = agent_in(harness, "CHOPPY", "block")
    item = {"meta": {"symbol": "XAUUSD", "side": "LONG", "kind": "P", "price": 4200.0, "bar": 1, "mode": "ema5080"}, "key": "k"}
    r = bot.ask_decision(ag.cfg, {"XAUUSD": ag}, ag.journal, item, "TAKE", "me")
    assert r["line"] == "REFUSED" and "3 crosses in 2 h" in r["text"] and not r["placed"]
    assert [x for x in ag.journal.read(0) if x["event"] == "take_refused_regime"]


def test_block_dead_card_once(harness):
    fake, ag, _ = harness(price=4200.0); ag.cfg.regime_gate = "block"
    sent = []
    ag.notify.send = lambda key, title, lines=None, png=None, **kw: sent.append(title) or True
    df = tangled()
    for i in range(150, len(df)):
        ag._update_regime(df.iloc[: i + 1], int(df["time"].iloc[i]))
    assert sum("NO-TRADE REGIME" in t for t in sent) == 1


@pytest.mark.parametrize("test_name", ["test_giveback_closes_at_half_on_close", "test_structure_confirms_closes_with_evidence",
                                       "test_structure_required_no_evidence_holds"])
def test_open_position_guardian_unaffected_by_block(harness, monkeypatch, test_name, request):
    """The v2.8.0 flip / giveback tests, with regime_gate=block and a DEAD regime: identical outcomes."""
    import tests.test_v280_flip_giveback as T
    orig = harness
    def blocked_harness(*a, **kw):
        fake, ag, df = orig(*a, **kw)
        ag.cfg.regime_gate = "block"; ag.regime_tracker.state = "DEAD"
        ag._update_regime = lambda *a_, **k_: None                       # keep it DEAD for the whole test
        return fake, ag, df
    fn = getattr(T, test_name)
    if test_name == "test_giveback_closes_at_half_on_close":
        fn(blocked_harness, "classic")
    else:
        fn(blocked_harness, monkeypatch)


# ------------------------------------------------------------------ snapshot + live thresholds
def test_regime_in_snapshot_only_when_gate_on(rig):
    from aureon_mt5.claude_advisor import build_snapshot
    r = rig(mode="advisory"); df = cframe(); t = int(df["time"].iloc[-1])
    r.ag.regime = {"state": "CHOPPY", "score": 0.4, "reasons": ["3 crosses in 2 h"], "since_bars": 5, "no_trade_day": False}
    assert "regime" not in build_snapshot(r.ag, "P", "LONG", t, df)
    r.cfg.regime_gate = "show"
    snap = build_snapshot(r.ag, "P", "LONG", t, df)
    assert snap["regime"]["state"] == "CHOPPY" and snap["regime"]["since_bars"] == 5 and "REGIME block" in r.adv.prompt_for(snap)
    from aureon_mt5.claude_advisor import _compact
    assert _compact(snap)["regime"]["state"] == "CHOPPY"


def test_thresholds_settable_live(tmp_path):
    c = Config(); c.log_dir = str(tmp_path)
    F.set_override(c.log_dir, c, "regime_choppy", "0.5", "op"); F.set_override(c.log_dir, c, "regime_clean_bars", "2", "op")
    assert R.classify(0.48, "CLEAN", 0, c)[0] == "CHOPPY"
    s, k = R.classify(0.9, "CHOPPY", 0, c); s, k = R.classify(0.9, s, k, c)
    assert s == "CLEAN"


# ------------------------------------------------------------------ backtest support
def random_walk_after_trend(seed=11):
    rs = np.random.RandomState(seed); n = 600; t = np.arange(n)
    c = np.where(t < 200, 4000 + 0.6 * t, 4120 + np.cumsum(np.where(t < 200, 0, rs.normal(0, 0.35, n))))
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": 1_792_000_000 + t * 300, "open": o, "high": np.maximum(o, c) + 0.25,
                         "low": np.minimum(o, c) - 0.25, "close": c})


def test_replay_scores_blocked_entries(tmp_path):
    """Entries placed in a 400-bar random walk (pure whipsaw for an EMA system): the gate blocks them and the backtest
    still simulates each one under the same guardian to score the gate."""
    from tools import replay_guardian as RG
    from tools.guardian_sim import hhmm
    df = random_walk_after_trend()
    p = tmp_path / "bars.json"; p.write_text(json.dumps({"bars": df.to_dict(orient="list")}))
    args = ["--bars", str(p), "--set", "regime_gate=block", "--out", str(tmp_path)]
    for i in (260, 301, 340, 365, 390, 415, 150, 267, 448):
        args += ["--trade", f"{hhmm(df['time'].iloc[i]).replace(' ', 'T')},{'long' if i % 2 else 'short'}"]
    out = RG.main(args)["combo"]
    assert out["entries_blocked"] >= 6 and out["entries_blocked"] == out["would_have_lost"] + out["would_have_won"]
    assert out["would_have_lost"] >= 3 * out["would_have_won"] and out["gate_acceptable"] is True
    assert out["no_trade_days"] >= 1 and all(b["regime"] in ("CHOPPY", "DEAD") for b in out["blocked"])
    assert "blocked losers >= 3 x blocked winners" in out["sign_off"]


def test_trend_fixture_entries_are_clean():
    """The 2-day fixture trends: every analysis entry is CLEAN, so the gate blocks nothing there."""
    from aureon_mt5 import regime as R2
    from tools.replay_guardian import prepare
    b = prepare(pd.DataFrame(json.load(open(os.path.join(ROOT, "tests", "fixtures", "xauusd_2day_m5.json")))["bars"]))
    states, _ = R2.replay_states(b)
    assert [states[i] for i in (163, 257, 286, 338)] == ["CLEAN"] * 4
