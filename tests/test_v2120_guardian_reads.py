"""v2.12.0 — Claude sees what the guardian is about to do (Part A) and gets a vote before a close-based exit (Part B).
The CLI is always mocked; MT5 is FakeMT5."""
import json, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import pytest
from aureon_mt5 import broker
from aureon_mt5.claude_advisor import GUARDIAN_RULES, PRE_EXIT_RULES, build_snapshot, _compact
from aureon_mt5.common.timeutil import now_server
from tests.test_claude_advisor import frame, rig  # noqa: F401  (rig is a fixture)
from tests.test_v181 import harness  # noqa: F401  (fixture)
from tests.test_v280_claude_read import ImageCLI, read_v
from tests.test_v280_flip_giveback import mkdf, step, p_short, exits


def sltp_sends(fake):
    return [r for r in fake.sends if r["action"] == fake.TRADE_ACTION_SLTP]


def pos():
    return broker.positions("XAUUSD")[0]


# ============================================================================= Part A — guardian_intent
def _p(entry=4200.0, price=4205.0, sl=4194.0, direction="long"):
    s = 1 if direction == "long" else -1
    return {"ticket": 1, "direction": direction, "price_open": entry, "current": price, "points": (price - entry) * s,
            "sl": sl, "tp": 0.0}


def test_intent_pre_phase_nearest_is_pre_stop_or_timeout(harness):
    fake, ag, _ = harness(price=4202.0)
    st = {"pre": True, "secured": 0.0, "peak": 2.0, "bars": 3, "mae": -1.0}
    g = ag.guardian_intent(_p(price=4202.0, sl=4194.0), st, mkdf(4202.0, 4180.0, 4190.0), 4202.0, 4180.0, 4190.0)
    assert g["phase"] == "pre" and g["profile"] in ("classic", "leg")
    ne = g["next_exit"]
    assert ne["rule"] == "pre_stop" and ne["level"] == 4194.0 and ne["distance_pts"] == 8.0
    st["bars"] = 11                                               # one bar from the 12-bar P timeout, trade at +2 on the close
    g = ag.guardian_intent(_p(price=4201.0, sl=4194.0), st, mkdf(4201.0, 4180.0, 4190.0), 4201.0, 4180.0, 4190.0)
    assert g["next_exit"]["rule"] == "pre_timeout" and g["next_exit"]["bars_left"] == 1 and g["next_exit"]["distance_pts"] == 1.0


def test_intent_protect_phase_slow_ema(harness):
    fake, ag, _ = harness(price=4203.0)
    st = {"pre": False, "secured": 0.0, "peak": 5.0, "bars": 4}
    g = ag.guardian_intent(_p(price=4203.0, sl=4189.0), st, mkdf(4203.0, 4195.0, 4201.0), 4203.0, 4195.0, 4201.0)
    assert g["phase"] == "protect"
    assert g["next_exit"] == {"rule": "ema_slow_stop", "level": 4201.0, "distance_pts": 2.0, "bars_left": None, "fires_on": "close"}


def test_intent_secured_and_ride_phases(harness):
    fake, ag, _ = harness(price=4214.0)
    ag.cfg.giveback_pct = 50
    st = {"pre": False, "secured": 10.0, "peak": 14.0, "bars": 9}
    g = ag.guardian_intent(_p(price=4214.0, sl=4210.0), st, mkdf(4214.0, 4212.0, 4205.0), 4214.0, 4212.0, 4205.0)
    assert g["phase"] == "secured" and g["trail_level"] == 4210.0 and g["giveback_level"] == 4207.0
    assert g["next_exit"]["rule"] == "ema_fast_turn" and g["next_exit"]["distance_pts"] == 2.0
    st.update(secured=20.0, peak=26.0)
    g = ag.guardian_intent(_p(price=4224.0, sl=4220.0), st, mkdf(4224.0, 4210.0, 4205.0), 4224.0, 4210.0, 4205.0)
    assert g["phase"] == "ride"
    assert g["next_exit"]["rule"] == "ride_step" and g["next_exit"]["fires_on"] == "tick" and g["next_exit"]["distance_pts"] == 4.0


def test_intent_leg_profile_phases(harness):
    fake, ag, _ = harness(price=4212.0)
    st = {"pre": False, "secured": 3.0, "peak": 12.0, "bars": 6, "guardian_profile": "leg", "leg_phase": "B", "trail_pts": 3.0}
    g = ag.guardian_intent(_p(price=4212.0, sl=4203.0), st, mkdf(4212.0, 4209.0, 4204.0), 4212.0, 4209.0, 4204.0)
    assert g["phase"] == "ride" and g["profile"] == "leg" and g["trail_level"] == 4203.0
    assert g["next_exit"]["rule"] == "ema_slow_close" and g["next_exit"]["level"] == 4204.0
    st2 = {"pre": False, "secured": 0.0, "peak": 6.0, "bars": 3, "guardian_profile": "leg", "leg_phase": "A",
           "soft_trail_armed": True, "soft_lock": 2.5}
    g2 = ag.guardian_intent(_p(price=4204.0, sl=4194.0), st2, mkdf(4204.0, 4199.0, 4190.0), 4204.0, 4199.0, 4190.0)
    assert g2["phase"] == "protect" and g2["next_exit"]["rule"] == "soft_trail" and g2["next_exit"]["distance_pts"] == 1.5


def test_intent_short_side_and_recent_actions(harness):
    fake, ag, _ = harness(direction="short", entry=4200.0, price=4197.0, sl=4206.0)
    st = {"pre": False, "secured": 0.0, "peak": 3.0, "bars": 2}
    ag.note_action(st, "sl_move", "PROTECT → 4206.00", t=1); ag.note_action(st, "structure_sl", "a", t=2)
    ag.note_action(st, "FLIP_HOLD", "b", t=3); ag.note_action(st, "exit_deferred", "c", t=4)
    g = ag.guardian_intent(_p(direction="short", price=4197.0, sl=4206.0), st, mkdf(4197.0, 4200.0, 4199.0), 4197.0, 4200.0, 4199.0)
    assert [a["rule"] for a in g["recent_actions"]] == ["structure_sl", "FLIP_HOLD", "exit_deferred"]
    assert g["next_exit"]["rule"] == "ema_slow_stop" and g["next_exit"]["distance_pts"] == 2.0     # 4199 − 4197 on a short


def test_close_and_sl_moves_feed_recent_actions(harness):
    fake, ag, _ = harness(price=4203.0, sl=0.0)
    step(ag, fake, mkdf(4203.0, 4190.0, 4180.0))               # PROTECT moves the SL
    assert ag.state[1]["recent_actions"][0]["rule"] == "sl_move"


# ============================================================================= Part A — in every snapshot
def test_guardian_block_in_read_pullback_open_drawdown_snapshots(rig):
    r = rig(mode="advisory")
    from tests.test_v181 import guard
    guard(r.ag, r.fake, frame(price=4208.0), sgn=1, new_bar=False)
    st = r.ag.state[1]; st["peak"] = 12.0
    df = frame(price=4208.0); bar = now_server(3.0) - 300
    snap = build_snapshot(r.ag, "READ", "BUY", bar, df, position=pos(), st=st)
    assert set(snap["guardian"]) >= {"phase", "next_exit", "trail_level", "giveback_level", "structure_stop", "cap", "recent_actions"}
    assert "guardian" in _compact(snap)
    assert GUARDIAN_RULES in r.adv.prompt_for(snap)
    assert r.adv.maybe_pullback(r.ag, pos(), st, bar, df, ["giveback"])
    assert "guardian" in r.adv.q.get_nowait()["snapshot"]
    r.cfg.claude_open_read = True
    assert r.adv.request_open_read(r.ag, pos(), st, bar, df)
    assert r.adv.q.get_nowait()["snapshot"]["guardian"]["next_exit"]["rule"]
    st.update(setup_alive=True, cap_pts=10.0, dist_to_cap=4.0)
    r.fake.price = 4195.0
    assert r.adv.maybe_drawdown(r.ag, pos(), st, bar, frame(price=4195.0))
    assert "guardian" in r.adv.q.get_nowait()["snapshot"]


# ============================================================================= Part B — the pre-exit read
def give_rig(rig, mode, decision="HOLD", **kw):
    r = rig(mode="advisory", price=4200.0, **kw)
    r.ag._resolved_profile = "classic"
    r.cfg.giveback_pct = 50; r.cfg.pre_exit_read = mode
    r.adv.caller = ImageCLI(read_v(decision, "BUY"))
    r.fake.price = 4220.0
    step(r.ag, r.fake, mkdf(4220.0, 4190.0, 4180.0))            # peak +20
    while not r.adv.q.empty():
        r.adv.q.get_nowait()                                    # other reads (pullback) are not under test
    return r


def give_back(r, t0, price=4209.0):
    r.fake.price = price
    step(r.ag, r.fake, mkdf(price, 4190.0, 4180.0, t0=t0))


def pre_exit_jobs(r):
    out = []
    while not r.adv.q.empty():
        j = r.adv.q.get_nowait()
        if j.get("trigger") == "pre_exit":
            out.append(j)
    return out


def run_job(r, job):
    r.adv.process(job); r.adv.drain(r.ag, broker.positions("XAUUSD"))


def test_off_closes_exactly_as_before_and_asks_nothing(rig):
    r = give_rig(rig, "off")
    give_back(r, 600)
    assert r.fake.closed and exits(r.ag)[-1]["reason"] == "giveback" and "claude_pre_exit" not in exits(r.ag)[-1]
    assert pre_exit_jobs(r) == []


def test_show_closes_and_journals_a_pre_exit_read(rig):
    r = give_rig(rig, "show", decision="HOLD")
    give_back(r, 600)
    assert r.fake.closed and exits(r.ag)[-1]["reason"] == "giveback"
    [job] = pre_exit_jobs(r)
    assert job["pre_exit_show"] and job["rule"] == "giveback" and job["snapshot"]["pending_exit"]["rule"] == "giveback"
    assert "PRE-EXIT READ" in r.adv.read_prompt(job["snapshot"], job["extra"])
    run_job(r, job)
    v = [x for x in r.j.read(0) if x["event"] == "claude_verdict"][-1]
    assert v["trigger"] == "pre_exit" and v["rule"] == "giveback" and v["deferral_n"] == 0 and not v["acted"]


def test_defer_hold_twice_then_unconditional_close_sl_untouched(rig):
    r = give_rig(rig, "defer", decision="HOLD")
    give_back(r, 600)
    assert not r.fake.closed and r.ag.state[1]["pending_exit"]["rule"] == "giveback"
    assert any("EXIT_PENDING" in k for k in r.n.sent)
    [job] = pre_exit_jobs(r)
    sl_before, sends_before = r.fake.pos.sl, len(sltp_sends(r.fake))
    run_job(r, job)                                             # HOLD 1/2
    st = r.ag.state[1]
    assert not r.fake.closed and st["deferrals"] == 1 and st["pending_exit"]["state"] == "held"
    assert r.fake.pos.sl == sl_before and len(sltp_sends(r.fake)) == sends_before      # a HOLD never touches the SL
    d = [x for x in r.j.read(0) if x["event"] == "exit_deferred"]
    assert d[-1]["rule"] == "giveback" and d[-1]["deferral_n"] == 1
    assert any("EXIT_DEFERRED" in k for k in r.n.sent)
    give_back(r, 900)                                           # still true on the next close → asks again
    [job2] = pre_exit_jobs(r)
    assert job2["deferral_n"] == 1 and not r.fake.closed
    run_job(r, job2)                                            # HOLD 2/2
    assert r.ag.state[1]["deferrals"] == 2 and r.fake.pos.sl == sl_before
    give_back(r, 1200)                                          # third close: no more asking
    assert pre_exit_jobs(r) == [] and r.fake.closed
    ex = exits(r.ag)[-1]
    assert ex["reason"] == "giveback" and ex["claude_pre_exit"] == "max_deferrals" and ex["deferrals"] == 2


def test_defer_exit_closes_now_with_the_original_rule(rig):
    r = give_rig(rig, "defer", decision="EXIT")
    give_back(r, 600)
    [job] = pre_exit_jobs(r)
    run_job(r, job)
    assert r.fake.closed
    ex = exits(r.ag)[-1]
    assert ex["reason"] == "giveback" and ex["claude_pre_exit"] == "exit" and "pending_exit" not in r.ag.state[1]


def test_defer_timeout_closes(rig):
    r = give_rig(rig, "defer")
    give_back(r, 600)
    pre_exit_jobs(r)                                            # the worker never answers
    r.ag.state[1]["pending_exit"]["t"] -= r.cfg.pre_exit_wait_s + 1
    step(r.ag, r.fake, mkdf(4209.0, 4190.0, 4180.0, t0=600), new_bar=False)          # the next poll, same bar
    assert r.fake.closed and exits(r.ag)[-1]["claude_pre_exit"] == "timeout"


def test_defer_budget_used_closes_as_error(rig):
    r = give_rig(rig, "defer", max_calls=0)
    give_back(r, 600)
    [job] = pre_exit_jobs(r)
    run_job(r, job)
    assert r.fake.closed and exits(r.ag)[-1]["claude_pre_exit"] == "error"


def test_never_list_and_cap_close_without_a_read(rig):
    r = rig(mode="advisory", price=4200.0, sl=4194.0)
    r.ag._resolved_profile = "classic"; r.cfg.pre_exit_read = "defer"
    r.fake.price = 4193.0                                       # pre phase, −7 on the close: pre_stop (never deferred)
    step(r.ag, r.fake, mkdf(4193.0, 4180.0, 4190.0, t0=0))
    st = r.ag.state[1]; st["pre"] = True
    step(r.ag, r.fake, mkdf(4193.0, 4180.0, 4190.0, t0=300))
    assert r.fake.closed and exits(r.ag)[-1]["reason"] == "pre_stop" and pre_exit_jobs(r) == []
    # within 2 pts of the cap: giveback at −5 with cap 6 → closes without asking
    r2 = give_rig(rig, "defer")
    give_back(r2, 600, price=4195.0)
    assert r2.fake.closed and exits(r2.ag)[-1]["reason"] == "giveback" and pre_exit_jobs(r2) == []


def test_pending_cleared_on_close_cancel_and_restart(rig):
    r = give_rig(rig, "defer")
    give_back(r, 600); [job] = pre_exit_jobs(r); run_job(r, job)          # HOLD → pending held
    give_back(r, 900, price=4216.0)                             # kept 80 % on the next close: the rule is not true any more
    assert "pending_exit" not in r.ag.state[1]
    c = [x for x in r.j.read(0) if x["event"] == "exit_cancelled"]
    assert c and c[-1]["rule"] == "giveback"
    give_back(r, 1200)                                          # fires again → a fresh ask (deferrals 1 used)
    assert r.ag.state[1]["pending_exit"]["state"] == "waiting"
    pre_exit_jobs(r)
    r.ag._save_state_now()
    from aureon_mt5.agent import SymbolAgent
    from aureon_mt5.strategies import get_strategy
    ag2 = SymbolAgent("XAUUSD", r.cfg, get_strategy("ema5080"), r.n, r.j, (), claude=r.adv)
    assert "pending_exit" not in ag2.state[1]                   # restart: no pending exit
    st = r.ag.state[1]
    r.ag._close(pos(), "news", "t", "k", st, "news_flat", None)
    assert "pending_exit" not in st                             # any close ends it


def test_p_flip_deferred_when_structure_gate_flips(rig, monkeypatch):
    r = rig(mode="advisory", price=4201.0)
    r.ag._resolved_profile = "classic"; r.cfg.pre_exit_read = "defer"; r.cfg.flip_require_structure = True
    monkeypatch.setattr(r.ag, "_m15_state", lambda: "BEARISH · PULLBACK")
    r.adv.caller = ImageCLI(read_v("EXIT", "BUY"))
    step(r.ag, r.fake, mkdf(4201.0, 4181.0, 4180.0))
    r.fake.price = 4199.0
    df2 = mkdf(4199.0, 4201.0, 4200.0, t0=300); p_short(r.ag, df2); step(r.ag, r.fake, df2)
    assert not r.fake.closed and r.ag.state[1]["pending_exit"]["rule"] == "p_flip"
    [job] = pre_exit_jobs(r)
    run_job(r, job)
    assert r.fake.closed and exits(r.ag)[-1]["reason"] == "p_flip" and exits(r.ag)[-1]["claude_pre_exit"] == "exit"


def test_pre_exit_rules_paragraph():
    t = PRE_EXIT_RULES.format(rule="giveback", detail="gave back 55%", max_defer=2, used=1)
    assert "rule `giveback` (gave back 55%)" in t and "broker stop stays" in t and "at most 2" in t


# ============================================================================= B3 grading
def test_pre_exit_grading_and_report_section():
    from aureon_mt5.advisor_board import pre_exit_grades, pre_exit_summary, pre_exit_text
    logs = [
        {"event": "position_seen", "ticket": 1, "side": "LONG", "entry": 4200.0},
        {"event": "position_seen", "ticket": 2, "side": "SHORT", "entry": 4300.0},
        {"event": "claude_verdict", "claude_event": "READ", "trigger": "pre_exit", "decision": "HOLD", "ticket": 1, "symbol": "XAUUSD",
         "bar": 10_000, "open_profit": 9.0, "rule": "giveback", "deferral_n": 0},
        {"event": "exit_deferred", "ticket": 1},
        {"event": "exit", "ticket": 1, "points": 14.0, "bar": 10_000 + 3 * 300},          # exited 3 bars later at +14
        {"event": "claude_verdict", "claude_event": "READ", "trigger": "pre_exit", "decision": "EXIT", "ticket": 2, "symbol": "XAUUSD",
         "bar": 20_000, "open_profit": 4.0, "rule": "ema_fast_turn", "deferral_n": 0},
        {"event": "closed", "ticket": 1, "final_points": 14.0},
    ]
    g = pre_exit_grades(logs, grade_bars=6, price_at=lambda sym, t: 4290.0 if t == 20_000 + 6 * 300 else None)
    assert g[0]["deferral_gain_pts"] == 5.0 and g[0]["graded"]                 # held from +9 to +14
    assert g[1]["deferral_gain_pts"] == 6.0                                       # short @4300: +4 at the rule, +10 six bars later
    s = pre_exit_summary(logs, g, cap_pts=6.0)
    assert s["deferrals"] == 1 and s["avg_gain_per_deferral"] == 5.0 and s["deferred_hit_cap"] == 0
    assert "avg +5.00 pts per deferral" in pre_exit_text(s) and "holding would have made +6.00" in pre_exit_text(s)


# ============================================================================= B4 exit_audit
def test_exit_audit_table(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import exit_audit as EA
    t0 = 1_800_000_000
    j = [{"t": t0 - 100, "event": "position_seen", "ticket": 1, "side": "LONG", "entry": 4200.0},
         {"t": t0, "event": "exit", "ticket": 1, "points": 6.0, "reason": "giveback"},
         {"t": t0 - 100, "event": "position_seen", "ticket": 2, "side": "LONG", "entry": 4300.0},
         {"t": t0 + 5000, "event": "exit", "ticket": 2, "points": 2.0, "reason": "giveback"},
         {"t": t0 + 9000, "event": "exit", "ticket": 3, "points": -3.0, "reason": "ema_fast_turn"}]
    off = 3 * 3600
    rows = []
    for k in range(-2, 60):                                     # after ticket 1's close (4206) price runs to 4218
        rows.append({"time": t0 + off + k * 300, "open": 4206, "high": 4206 + max(0, k) * 1.0, "low": 4205, "close": 4206})
    for k in range(-2, 13):                                     # ticket 2 closed 4302: price only reaches 4305
        rows.append({"time": t0 + 5000 + off + k * 300 + 7, "open": 4302, "high": 4305, "low": 4300, "close": 4302})
    bars = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    res = EA.audit(j, bars, after=12, early=10.0, offset_h=3.0)
    assert res["giveback"]["n"] == 2 and res["giveback"]["avg_points"] == 4.0 and res["giveback"]["early_share"] == 50
    assert res["ema_fast_turn"]["unmeasured"] == 1
    out = EA.table(res, days=7, after=12, early=10.0)
    assert "giveback" in out.splitlines()[2] and "50%" in out.splitlines()[2] and "(1 without bars)" in out
    (tmp_path / "journal.jsonl").write_text("")
    assert "no software exits" in EA.main(["--days", "7", "--log-dir", str(tmp_path)])


def test_no_advising_claude_means_close_by_the_rule(rig):
    r = give_rig(rig, "defer")
    r.adv.mode = "review"                                       # Claude attached but not advising: nobody to ask
    give_back(r, 600)
    assert r.fake.closed and "claude_pre_exit" not in exits(r.ag)[-1] and pre_exit_jobs(r) == []
