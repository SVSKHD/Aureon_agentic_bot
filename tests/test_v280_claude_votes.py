"""v2.8.0 Part 3 — Claude votes at open and in drawdown. CLI mocked; MT5 is the FakeMT5 from test_v181."""
import ast, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import broker
from aureon_mt5.codex_advisor import CodexAdvisor
from aureon_mt5.common.timeutil import now_server
from tests.test_claude_advisor import frame, rig, verdicts  # noqa: F401  (rig is a fixture)
from tests.test_v181 import guard
from tests.test_v280_claude_read import ImageCLI, read_v

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def vote(decision, p_win=0.62):
    return {**read_v(decision, "BUY"), "p_win": p_win}


def jobs(adv):
    out = []
    while not adv.q.empty():
        out.append(adv.q.get_nowait())
    return out


# ------------------------------------------------------------------ 8. open read
def test_open_read_once_per_new_ticket(rig):
    r = rig(mode="manage"); r.cfg.claude_open_read = True
    r.adv.caller = ImageCLI(vote("HOLD"))
    guard(r.ag, r.fake, frame(price=r.fake.price), sgn=1, new_bar=False)
    guard(r.ag, r.fake, frame(price=r.fake.price), sgn=1, new_bar=False)
    js = jobs(r.adv)
    assert len(js) == 1 and js[0]["type"] == "read" and js[0]["event"] == "READ" and js[0]["trigger"] == "open"
    r.adv.process(js[0]); r.adv.drain(r.ag, broker.positions("XAUUSD"))
    v = verdicts(r.j)[-1]
    assert v["claude_event"] == "READ" and v["trigger"] == "open" and v["p_win"] == pytest.approx(0.62) and not v["acted"]
    assert "OPEN READ" in r.adv.caller.calls[0]["prompt"]


def test_open_read_off_and_codex_no_call(rig):
    r = rig(mode="manage"); r.cfg.claude_open_read = False
    guard(r.ag, r.fake, frame(price=r.fake.price), sgn=1, new_bar=False)
    assert r.adv.q.empty()
    r.cfg.claude_open_read = True; r.cfg.codex_mode = "advisory"
    cx = CodexAdvisor(r.cfg, r.n, r.j, caller=lambda *a, **k: None, start=False)
    assert cx.request_open_read(r.ag, broker.positions("XAUUSD")[0], {}, now_server(3.0), frame()) is False and cx.q.empty()


# ------------------------------------------------------------------ 9. open EXIT is shown only, even in manage + profit
def test_open_read_exit_never_closes(rig):
    r = rig(mode="manage", price=4208.0); r.cfg.claude_open_read = True
    r.adv.caller = ImageCLI(vote("EXIT"))
    guard(r.ag, r.fake, frame(price=4208.0), sgn=1, new_bar=False)
    job = jobs(r.adv)[0]
    r.adv.process(job); r.adv.drain(r.ag, broker.positions("XAUUSD"))
    v = verdicts(r.j)[-1]
    assert not r.fake.closed and not v["acted"] and "open read" in v["note"]


# ------------------------------------------------------------------ 10. drawdown re-arm rules
def dd(r, close, st):
    p = dict(broker.positions("XAUUSD")[0]); p["points"] = close - 4200.0
    st["dist_to_cap"] = st["cap_pts"] - max(0.0, 4200.0 - close)
    fired = r.adv.maybe_drawdown(r.ag, p, st, now_server(3.0) - 300, frame(price=close))
    jobs(r.adv)
    return fired


def test_drawdown_reads_arm_step_last_chance_and_cap(rig):
    r = rig(mode="manage")
    st = {"setup_alive": True, "cap_pts": 8.0, "mae": -5.0}
    assert dd(r, 4198.0, st) is False                         # −2 < DD_READ_AT 3
    assert dd(r, 4197.0, st) is True                          # first close ≤ −3
    assert dd(r, 4196.0, st) is False                         # −4: not 2 deeper
    assert dd(r, 4195.0, st) is True                          # −5: new trough
    assert dd(r, 4193.5, st) is True                          # −6.5: step not met, but 1.5 from the −8 cap → last chance
    assert dd(r, 4192.5, st) is False                         # never a 4th read (DD_READS_PER_TRADE=3)
    assert st["dd_reads"] == 3
    r.adv.forget("XAUUSD", 1)
    assert (r.ag.symbol, 1) not in r.adv.dd_fired


def test_drawdown_last_chance_only_once(rig):
    r = rig(mode="manage"); r.cfg.claude_dd_reads_per_trade = 10
    st = {"setup_alive": True, "cap_pts": 8.0}
    assert dd(r, 4197.0, st) and dd(r, 4193.5, st)            # −3, then last chance (−6.5, 1.5 from cap; also 3.5 deeper)
    assert dd(r, 4193.0, st) is False                         # still last-chance zone, already used, step not met


def test_no_drawdown_read_when_setup_dead(rig):
    r = rig(mode="manage")
    assert dd(r, 4196.0, {"setup_alive": False, "cap_pts": 10.0}) is False
    assert dd(r, 4196.0, {"cap_pts": 10.0}) is False          # unknown setup → no read


def test_drawdown_snapshot_and_prompt(rig):
    r = rig(mode="manage"); r.adv.caller = ImageCLI(vote("HOLD"))
    st = {"setup_alive": True, "cap_pts": 10.0, "setup": {"ema_order": True}, "bars_in_dd": 4, "mae": -4.5}
    p = dict(broker.positions("XAUUSD")[0]); p["points"] = -4.0
    st["dist_to_cap"] = 6.0
    assert r.adv.maybe_drawdown(r.ag, p, st, now_server(3.0) - 300, frame(price=4196.0))
    job = r.adv.q.get_nowait()
    snap = job["snapshot"]
    assert snap["trigger"] == "drawdown" and snap["bars_in_drawdown"] == 4 and snap["dist_to_cap"] == 6.0 and snap["setup_alive"]
    r.adv.process(job)
    assert "DRAWDOWN READ" in r.adv.caller.calls[0]["prompt"] and "stop at −10" in r.adv.caller.calls[0]["prompt"]


# ------------------------------------------------------------------ 11. drawdown EXIT matrix
def dd_exit(r, *, exit_losing, price=4196.0):
    r.cfg.claude_exit_losing = exit_losing
    r.fake.price = price
    guard(r.ag, r.fake, frame(price=price), sgn=1, new_bar=False)
    st = r.ag.state[1]; st["setup_alive"] = True; st["cap_pts"] = 10.0; st["dist_to_cap"] = 6.0
    r.adv.caller = ImageCLI(vote("EXIT"))
    assert r.adv.maybe_drawdown(r.ag, broker.positions("XAUUSD")[0], st, now_server(3.0) - 300, frame(price=price))
    r.adv.process(r.adv.q.get_nowait()); r.adv.drain(r.ag, broker.positions("XAUUSD"))
    return verdicts(r.j)[-1]


def test_dd_exit_losing_off_is_shown(rig):
    r = rig(mode="manage")
    v = dd_exit(r, exit_losing=False)
    assert not r.fake.closed and not v["acted"] and "losing" in v["note"]


def test_dd_exit_losing_on_manage_cuts(rig):
    r = rig(mode="manage")
    v = dd_exit(r, exit_losing=True)
    assert r.fake.closed and v["acted"]
    assert [x for x in r.j.read(0) if x["event"] == "exit"][-1]["reason"] == "claude_cut_loss"


def test_dd_exit_losing_on_advisory_is_shown(rig):
    r = rig(mode="advisory")
    v = dd_exit(r, exit_losing=True)
    assert not r.fake.closed and not v["acted"]


def test_dd_exit_beyond_cap_not_cut(rig):
    r = rig(mode="manage", sl=4180.0)
    v = dd_exit(r, exit_losing=True, price=4189.0)            # −11, beyond the −10 cap: the stop's job, not Claude's
    assert not r.fake.closed and not v["acted"]


def test_claude_read_exit_never_cuts(rig):
    r = rig(mode="manage", price=4196.0); r.cfg.claude_exit_losing = True
    r.adv.caller = ImageCLI(read_v("EXIT", "BUY"))
    guard(r.ag, r.fake, frame(price=4196.0), sgn=1, new_bar=False)
    r.ag.last_df = frame(price=4196.0); r.ag._refresh_snapshot(True)
    res = r.adv.read_now(r.ag)
    r.adv.drain(r.ag, broker.positions("XAUUSD"))
    assert res["verdict"]["decision"] == "EXIT" and not r.fake.closed


# ------------------------------------------------------------------ 12. the guardian closed it first
def test_guardian_close_same_poll_is_fast_exit_cancel(rig):
    r = rig(mode="manage"); r.cfg.claude_exit_losing = True
    r.fake.price = 4196.0
    guard(r.ag, r.fake, frame(price=4196.0), sgn=1, new_bar=False)
    st = r.ag.state[1]; st.update(setup_alive=True, cap_pts=10.0, dist_to_cap=6.0)
    r.adv.caller = ImageCLI(vote("EXIT"))
    r.adv.maybe_drawdown(r.ag, broker.positions("XAUUSD")[0], st, now_server(3.0) - 300, frame(price=4196.0))
    r.adv.process(r.adv.q.get_nowait())
    r.ag.closed_by_aureon.add(1)                              # the guardian's own exit ran on this poll
    r.adv.drain(r.ag, broker.positions("XAUUSD"))
    assert [x for x in r.j.read(0) if x["event"] == "claude_fast_exit_cancel"]
    assert not [x for x in r.j.read(0) if x["event"] == "exit"]


# ------------------------------------------------------------------ 13. hard rule
def test_advisor_has_one_close_path_and_no_broker():
    src = open(os.path.join(ROOT, "aureon_mt5", "claude_advisor.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    mods = {getattr(n, "module", None) or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert "broker" not in names and not any("broker" in m for m in mods)
    assert src.count("agent._close(") == 1


def test_read_grades_bucket_open_p_win_by_trigger():
    from aureon_mt5.advisor_board import read_buckets, read_buckets_text, read_grades
    logs = [{"event": "claude_verdict", "claude_event": "READ", "trigger": "open", "decision": "HOLD", "p_win": 0.7, "ticket": 1},
            {"event": "claude_verdict", "claude_event": "READ", "trigger": "open", "decision": "HOLD", "p_win": 0.3, "ticket": 2},
            {"event": "claude_verdict", "claude_event": "READ", "trigger": "drawdown", "decision": "EXIT", "p_win": 0.2, "ticket": 2},
            {"event": "claude_verdict", "claude_event": "P", "decision": "TAKE", "ticket": None},
            {"event": "closed", "ticket": 1, "final_points": 12.0}, {"event": "closed", "ticket": 2, "final_points": -6.0}]
    rows = read_grades(logs)
    assert [(r["trigger"], r["points"]) for r in rows] == [("open", 12.0), ("open", -6.0), ("drawdown", -6.0)]
    b = read_buckets(rows)
    assert b["open"]["buckets"][">=0.6"]["wins"] == 1 and b["open"]["buckets"]["<0.4"]["pts"] == -6.0
    assert b["drawdown"]["by_decision"]["EXIT"]["n"] == 1 and "open (n=2)" in read_buckets_text(b)
