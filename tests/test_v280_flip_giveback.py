"""v2.8.0 Part 2 — the guardian looks at the setup before it flips; giveback from peak. No real MT5 / Claude."""
import json, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.codex_advisor import CodexAdvisor
from aureon_mt5.common import claude_cli as cc
from aureon_mt5.common.timeutil import now_server
from tests.test_v181 import harness  # noqa: F401  (fixture)
from tests.test_claude_advisor import frame, rig  # noqa: F401
from tests.test_v280_claude_read import ImageCLI, read_v

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "xauusd_20261015_m5.json")


def mkdf(close, ef, es, n=200, rng=4.0, slope=0.0, t0=0):
    c = np.full(n, float(close))
    f = ef - slope * (n - 1 - np.arange(n))                   # ends at ef; positive slope = rising
    return pd.DataFrame({"time": t0 + np.arange(n) * 300, "open": c, "high": c + rng / 2, "low": c - rng / 2, "close": c,
                         "ema_fast": f, "ema_slow": np.full(n, float(es))})


def step(ag, fake, df, close=None, new_bar=True):
    ef, es = float(df["ema_fast"].iloc[-1]), float(df["ema_slow"].iloc[-1])
    c = float(df["close"].iloc[-1]) if close is None else close
    ag.last_bar = int(df["time"].iloc[-1])
    ag._guard(new_bar, int(df["time"].iloc[-1]), df, c, ef, es, 1 if ef > es else -1)


def p_short(ag, df):
    ag.latest_signal = {"bar_time": int(df["time"].iloc[-1]), "symbol": "XAUUSD", "strategy": "ema5080", "kind": "P",
                        "direction": "SHORT", "consumed": False}


def exits(ag):
    return [r for r in ag.journal.read(0) if r["event"] == "exit"]


def stub_advisor(ag):
    adv = MagicMock(); adv.request_flip_read.return_value = True
    ag.claude = adv
    return adv


# ------------------------------------------------------------------ 1. default = v2.7.2
def test_default_any_opposite_p_flips(harness):
    fake, ag, _ = harness(price=4201.0)
    df = mkdf(4201.0, 4181.0, 4180.0)
    step(ag, fake, df); p_short(ag, df); step(ag, fake, df)
    assert fake.closed and exits(ag)[-1]["reason"] == "p_flip" and "flip_evidence" not in exits(ag)[-1]


# ------------------------------------------------------------------ 2. no evidence → held
def test_structure_required_no_evidence_holds(harness, monkeypatch):
    fake, ag, _ = harness(price=4201.0); ag.cfg.flip_require_structure = True
    monkeypatch.setattr(ag, "_m15_state", lambda: "BULLISH")
    adv = stub_advisor(ag)
    df = mkdf(4201.0, 4181.0, 4180.0, rng=4.0)               # gap 1 < 0.5 × 4
    step(ag, fake, df); p_short(ag, df); step(ag, fake, df)
    assert not fake.closed and ag.latest_signal["consumed"]
    ign = [r for r in ag.journal.read(0) if r["event"] == "opposite_p_ignored"]
    assert len(ign) == 1 and ign[0]["evidence"] == []
    assert adv.request_flip_read.call_count == 1 and adv.request_flip_read.call_args.kwargs["evidence"] == []
    assert "FLIP_HOLD" in " ".join(ag.notify.sent)


# ------------------------------------------------------------------ 3. enough evidence → closes
def test_structure_confirms_closes_with_evidence(harness, monkeypatch):
    fake, ag, _ = harness(price=4201.0); ag.cfg.flip_require_structure = True
    monkeypatch.setattr(ag, "_m15_state", lambda: "BEARISH · PULLBACK")
    df = mkdf(4201.0, 4181.0, 4180.0)
    step(ag, fake, df)
    fake.price = 4179.0
    df2 = mkdf(4179.0, 4181.0, 4180.0, t0=300); p_short(ag, df2); step(ag, fake, df2)
    assert fake.closed
    ex = exits(ag)[-1]
    assert ex["reason"] == "p_flip" and "close through EMA80" in ex["flip_evidence"] and "M15 against" in ex["flip_evidence"]


# ------------------------------------------------------------------ 4. M15 failures are "no evidence"
def test_m15_state_none_on_failure(harness, monkeypatch):
    fake, ag, _ = harness(price=4201.0)
    assert ag._m15_state() is None                            # FakeMT5 has no M15 rates → exception inside → None
    def boom(): raise RuntimeError("mt5 hiccup")
    monkeypatch.setattr(ag, "_m15_state", boom)
    df = mkdf(4201.0, 4181.0, 4180.0)
    p = broker.positions("XAUUSD")[0]
    assert ag._flip_evidence(p, df, 4201.0, 4181.0, 4180.0, (df["ema_fast"] - df["ema_slow"]).to_numpy()) == []


# ------------------------------------------------------------------ 5. the 15-Oct trade
def fixture_df():
    d = json.load(open(FIX, encoding="utf-8"))
    return pd.DataFrame(d["bars"]), d["meta"]


def bar_i(df, hhmm):
    from datetime import datetime, timezone
    hh, mm = map(int, hhmm.split(":"))
    return next(i for i, t in enumerate(df["time"]) if (lambda x: x.day == 15 and x.hour == hh and x.minute == mm)(
        datetime.fromtimestamp(int(t), tz=timezone.utc)))


def test_15_oct_p_short_is_held(harness, monkeypatch):
    bars, meta = fixture_df()
    i = bar_i(bars, "02:05")
    fake, ag, _ = harness(entry=meta["entry"], price=float(bars["close"][i]))
    ag.cfg.flip_require_structure = True
    monkeypatch.setattr(ag, "_m15_state", lambda: "BULLISH")
    df = bars.iloc[: i + 1].reset_index(drop=True)
    step(ag, fake, bars.iloc[:i].reset_index(drop=True), close=float(bars["close"][i - 1]))
    p_short(ag, df); step(ag, fake, df)
    assert not fake.closed
    ev = [r for r in ag.journal.read(0) if r["event"] == "opposite_p_ignored"][-1]["evidence"]
    assert ev == []                                           # EMA80 4098.7 under the 4102.3 close, EMA50 rising, gap 1.0 < 0.5 × 2.8


# ------------------------------------------------------------------ 6-10. giveback
def setup_peak(ag, fake, peak_price=4220.0):
    fake.price = peak_price
    step(ag, fake, mkdf(peak_price, 4190.0, 4180.0))          # peak +20, secured/ridden by the staircase
    return ag.state[1]


@pytest.mark.parametrize("profile", ["classic", "leg"])
def test_giveback_closes_at_half_on_close(harness, profile):
    fake, ag, _ = harness(price=4200.0); ag.cfg.giveback_pct = 50; ag._resolved_profile = profile
    st = setup_peak(ag, fake); assert st["peak"] == 20.0
    fake.price = 4211.0; step(ag, fake, mkdf(4211.0, 4190.0, 4180.0, t0=300))
    assert not fake.closed                                    # kept 55 %
    fake.price = 4209.0; step(ag, fake, mkdf(4209.0, 4190.0, 4180.0, t0=600))
    assert fake.closed and exits(ag)[-1]["reason"] == "giveback"


def test_giveback_not_armed_below_arm(harness):
    fake, ag, _ = harness(price=4200.0); ag.cfg.giveback_pct = 50
    setup_peak(ag, fake, 4208.0)                              # peak +8 < arm 10
    fake.price = 4201.0; step(ag, fake, mkdf(4201.0, 4190.0, 4180.0, t0=300))
    assert not fake.closed


def test_giveback_uses_close_not_tick(harness):
    fake, ag, _ = harness(price=4200.0); ag.cfg.giveback_pct = 50
    setup_peak(ag, fake)
    fake.price = 4209.0                                       # tick +9 …
    step(ag, fake, mkdf(4212.0, 4190.0, 4180.0, t0=300), close=4212.0)   # … bar closed +12
    assert not fake.closed


def test_giveback_off_by_default(harness):
    fake, ag, _ = harness(price=4200.0)
    assert ag.cfg.giveback_pct == 0
    setup_peak(ag, fake)
    fake.price = 4209.0; step(ag, fake, mkdf(4209.0, 4190.0, 4180.0, t0=300))
    assert not fake.closed


# ------------------------------------------------------------------ 11. flip read
def flip_read(r, decision, price):
    r.fake.price = price
    from tests.test_v181 import guard
    guard(r.ag, r.fake, frame(price=price), sgn=1, new_bar=False)
    st = r.ag.state[1]; pos = broker.positions("XAUUSD")[0]
    r.adv.caller = ImageCLI(read_v(decision, "BUY"))
    df = frame(price=price)
    ok_ = r.adv.request_flip_read(r.ag, pos, st, now_server(3.0) - 300, df, evidence=[])
    if not ok_:
        return None
    job = r.adv.q.get_nowait()
    r.adv.process(job); r.adv.drain(r.ag, broker.positions("XAUUSD"))
    return [v for v in r.j.read(0) if v["event"] == "claude_verdict"][-1]


def test_flip_read_exit_in_profit_closes_in_manage(rig):
    r = rig(mode="manage")
    v = flip_read(r, "EXIT", 4208.0)
    assert r.fake.closed and v["acted"] and v["claude_event"] == "READ" and v["trigger"] == "opposite P held"


def test_flip_read_exit_at_loss_shown(rig):
    r = rig(mode="manage")
    v = flip_read(r, "EXIT", 4197.0)
    assert not r.fake.closed and not v["acted"] and "losing" in v["note"]


def test_flip_read_hold_shown_enter_dropped(rig):
    r = rig(mode="manage")
    assert flip_read(r, "HOLD", 4208.0)["decision"] == "HOLD" and not r.fake.closed
    r2 = rig(mode="manage")
    v = flip_read(r2, "ENTER", 4208.0)
    assert v["decision"] is None and "ENTER" in v["error"] and not r2.fake.closed


def test_flip_read_advisory_never_applies(rig):
    r = rig(mode="advisory")
    v = flip_read(r, "EXIT", 4208.0)
    assert not r.fake.closed and not v["acted"]


def test_flip_read_codex_no_call(rig):
    r = rig(mode="advisory"); r.cfg.codex_mode = "advisory"
    calls = []
    cx = CodexAdvisor(r.cfg, r.n, r.j, caller=lambda *a, **k: calls.append(1), start=False)
    pos = broker.positions("XAUUSD")[0]
    assert cx.request_flip_read(r.ag, pos, {}, now_server(3.0) - 300, frame(), evidence=[]) is False
    assert cx.q.empty() and not calls
