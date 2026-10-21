"""v2.14.0 Part E — /decide-timeframe."""
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aureon_mt5 import bot, console as K, decide_tf as D, features as F
from aureon_mt5.common import tf_synth as T
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = "42"


def user(admin=True):
    return SimpleNamespace(roles=[SimpleNamespace(id=42 if admin else 7)], __str__=lambda s: "koushik")


class Note:
    def __init__(self):
        self.cards = []

    def card(self, title, body, **kw):
        self.cards.append((title, body, kw)); return True


def summary(trades, net, blocked=None):
    r = {"trades": trades, "total_points": net, "max_drawdown": 12.0,
         "trades_detail": [{"points": -25.0, "reason": "stop"}] + [{"points": 3.0, "reason": "stop"}] * (trades - 1)}
    if blocked is not None:
        r.update(asks_blocked_by_htf=blocked, htf_would_have_won=1, htf_would_have_lost=blocked - 1,
                 by_htf_entry={"pre": {"trades": 1, "total_points": 3.0}, "with": {"trades": trades - 1, "total_points": net - 3}})
    return r


def rig(tmp_path, *, positions=(), connected=True):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.follow_stop = 30.0
    calls, restarts = [], []

    def runner(sets):
        calls.append(sets)
        return summary(5, 21.0, blocked=3) if "htf_align=block" in sets else summary(8, 9.0)
    ag = SimpleNamespace(snapshot={"at": time.time(), "positions": list(positions), "connected": connected})
    j = Journal(str(tmp_path)); n = Note()
    go = lambda **kw: D.decide(cfg, {"XAUUSD": ag}, j, n, kw.pop("u", user()), kw.pop("tf", "M30"), admin_role=ADMIN,
                               runner=runner, avg_range_fn=lambda tf, d: 8.4,
                               restart=lambda u, reason: restarts.append(reason), **kw)
    return SimpleNamespace(cfg=cfg, go=go, calls=calls, restarts=restarts, j=j, n=n, ag=ag)


# ----------------------------------------------------------------------------- the ladder + rescaling
@pytest.mark.parametrize("tf,confirm", [("M5", ["M10", "M15"]), ("M10", ["M15", "M20"]), ("M15", ["M20", "M30"]),
                                        ("M20", ["M30", "M45"]), ("M30", ["M45", "H1"]), ("H1", ["H2", "H3"]),
                                        ("H2", ["H3", "H4"]), ("H4", ["H6", "H8"])])
def test_ladder_for_every_exec_tf(tf, confirm):
    p = D.plan(Config(), tf, 8.0)
    assert p["confirm"] == confirm and p["synthetic"] == (["M45"] if "M45" in confirm else [])
    if "M45" in confirm:
        assert "M45*" in D.plan_text(p) and "synthetic from M15" in D.plan_text(p)


def test_non_exec_tf_refused():
    with pytest.raises(ValueError):
        D.plan(Config(), "M45", 8.0)
    with pytest.raises(ValueError):
        D.plan(Config(), "D1", 8.0)


@pytest.mark.parametrize("avg,step,secure", [(3.0, 10.0, 3.0), (6.4, 10.0, 3.0), (9.6, 15.0, 5.0), (17.0, 25.0, 8.0)])
def test_rescale(avg, step, secure):
    """M5 (~3 pt bars) → floor 10 / 3 · M30 (~10) → 15 / 5 · H1 (~17) → 25 / 8."""
    k = D.rescale(avg)
    assert k["follow_step"] == step and k["follow_secure_at"] == secure and k["follow_stop_x_range"] == 1.5


def test_plan_text():
    p = D.plan(SimpleNamespace(follow_stop=30.0), "M30", 9.6, days=30)
    t = D.plan_text(p)
    assert t.startswith("exec M30 (current: M5) · confirm M45*, H1 · hold 2 bars · gap 0.25× range · window 6 bars")
    assert "follow stop = max(FOLLOW_STOP 30, 1.5× avg range of M30 over 30 d = 14.4) = 30" in t
    assert "step 15" in t and "secure at +5" in t and "regime windows 4/2 bars" in t


def test_knobs_are_the_exact_overrides():
    k = D.knobs(D.plan(Config(), "M30", 9.6))
    assert k == {"exec_tf": "m30", "htf_confirm_tfs": "M45,H1", "htf_align": "show", "follow_step": "15",
                 "follow_secure_at": "5", "follow_stop_x_range": "1.5", "confirm_hold_bars": "2",
                 "confirm_gap_x_range": "0.25", "confirm_window_bars": "6"}
    for name, value in k.items():                                  # every knob parses through its registry row
        F.parse(F.get(name), value)


# ----------------------------------------------------------------------------- replay + cache
def test_compare_runs_block_and_off_then_caches(tmp_path):
    r = rig(tmp_path)
    out = r.go()
    assert len(r.calls) == 2 and "htf_align=block" in r.calls[0] and "htf_align=off" in r.calls[1]
    assert all("exec_tf=M30" in c and "guardian_profile=follow" in c for c in r.calls)
    assert "HTF block: 5 trades · net +21.0" in out and "HTF off  : 8 trades · net +9.0" in out
    assert "blocked by HTF: 3 (would have won 1, lost 2)" in out
    out2 = r.go()
    assert len(r.calls) == 2 and "(cached)" in out2
    r.go(days=14)
    assert len(r.calls) == 4                                       # a new key per (tf, days, date)


def test_cache_key_has_the_date(tmp_path):
    cfg = SimpleNamespace(log_dir=str(tmp_path)); p = D.plan(Config(), "M30", 9.0)
    n = []
    run = lambda sets: n.append(1) or summary(3, 1.0)
    d1 = datetime(2026, 10, 16, 9, tzinfo=D.IST)
    D.compare(cfg, p, runner=run, now=d1); D.compare(cfg, p, runner=run, now=d1)
    D.compare(cfg, p, runner=run, now=d1 + timedelta(days=1))
    assert len(n) == 4


# ----------------------------------------------------------------------------- apply
def test_apply_false_changes_nothing(tmp_path):
    r = rig(tmp_path)
    out = r.go()
    assert "apply:false — nothing changed" in out and F.load_overrides(r.cfg.log_dir) == {}
    assert not r.restarts and not r.n.cards and not [x for x in r.j.read(0) if x["event"] == "timeframe_decided"]


def test_apply_true_writes_journals_cards_restarts(tmp_path):
    r = rig(tmp_path)
    out = r.go(apply=True)
    ov = F.load_overrides(r.cfg.log_dir)
    assert set(ov) == set(D.knobs(D.plan(r.cfg, "M30", 8.4)))
    assert ov["exec_tf"] == "m30" and ov["htf_align"] == "show" and ov["follow_step"] == 15.0
    assert r.cfg.htf_align == "show"                               # never block from here
    row = [x for x in r.j.read(0) if x["event"] == "timeframe_decided"][-1]
    assert row["tf"] == "M30" and row["confirm"] == ["M45", "H1"] and row["knobs"]["exec_tf"] == "m30"
    assert row["replay_summary"]["block"]["trades"] == 5
    assert r.n.cards and "TIMEFRAME DECIDED · M30 → M45, H1" in r.n.cards[0][0] and "show" in r.n.cards[0][1]
    assert r.restarts == ["decide-timeframe M30"] and "restarting" in out


def test_open_position_refused_without_force(tmp_path):
    r = rig(tmp_path, positions=[{"ticket": 1}])
    out = r.go(apply=True)
    assert "a position is open" in out and F.load_overrides(r.cfg.log_dir) == {} and not r.restarts
    r.go(apply=True, force=True)
    assert r.restarts and F.load_overrides(r.cfg.log_dir)["exec_tf"] == "m30"


def test_non_admin_denied(tmp_path):
    r = rig(tmp_path)
    assert r.go(u=user(admin=False), apply=True).startswith("Denied") and not r.calls and not r.restarts


def test_no_tf_shows_ladder_and_last_decision(tmp_path):
    r = rig(tmp_path)
    t = r.go(tf="")
    assert "Timeframe ladder" in t and "▶ M5 → M10, M15" in t and "M30 → M45*, H1" in t and "last decision: none" in t
    r.go(apply=True)
    assert "last decision: M30 → M45, H1" in r.go(tf="")


def test_bad_tf(tmp_path):
    r = rig(tmp_path)
    assert r.go(tf="M45").startswith("⚠️") and not r.calls


# ----------------------------------------------------------------------------- wiring
def test_command_registered_and_documented():
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    assert 'name="decide-timeframe"' in src and "decide_tf.decide(" in src
    assert "decide-timeframe" in bot.HELP["Control"]


def test_console_state_timeframe():
    cfg = SimpleNamespace(exec_tf="m30", htf_confirm_tfs=(), htf_align="show")
    assert K._timeframe(cfg) == {"exec": "M30", "confirm": ["M45", "H1"], "htf_align": "show"}
    src = open(os.path.join(ROOT, "aureon_mt5", "console.py"), encoding="utf-8").read()
    assert '"timeframe": _timeframe(cfg)' in src
