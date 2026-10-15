"""v2.10.0 Part B — repair agent: detect → propose → a human applies. Fake git / fixer / pytest; no real git, no CLI."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import deque
from unittest.mock import Mock
import pytest
from aureon_mt5 import repair
from aureon_mt5.bot import repair_action
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal

ADMIN = types.SimpleNamespace(roles=[types.SimpleNamespace(id=7)])
NOBODY = types.SimpleNamespace(roles=[])
BASE_AGENT = "class A:\n    def _guard(self):\n        return 1\n\n    def _close(self):\n        return 2\n\n    def helper(self):\n        return 3\n"
BASE_CFG = 'VERSION = "2.10.0"\n'
TB = 'Traceback (most recent call last):\n  File "/x/aureon_mt5/eod_review.py", line 40, in tick\n    boom()\nKeyError: \'k\'\n'


def failure(t, action="eod review", exc_type="KeyError", retcode=None, title="AUREON EOD FAILED"):
    return {"t": t, "title": title, "key": "k", "action": action, "retcode": retcode, "exc_type": exc_type, "exc": f"{exc_type}('k')", "tb": TB}


@pytest.fixture
def env(tmp_path):
    cfg = Config(); cfg.log_dir = str(tmp_path / "logs"); cfg.repair_agent = "off"
    j = Journal(cfg.log_dir); n = Mock(); clock = [1_800_000_000.0]
    rec = deque(); charts = deque()
    det = repair.Detector(cfg, j, n, {}, clock=lambda: clock[0], recent=rec, chart_failures=charts)
    return types.SimpleNamespace(cfg=cfg, j=j, n=n, clock=clock, rec=rec, charts=charts, det=det, tmp=tmp_path)


def tick(e, dt=31):
    e.clock[0] += dt
    return e.det.tick(e.clock[0])


# ------------------------------------------------------------------ detection
def test_exception_incident_deduped_by_fingerprint(env):
    env.rec.append(failure(env.clock[0] + 1))
    new = tick(env)
    assert len(new) == 1 and new[0]["fingerprint"] == "KeyError @ eod_review.py:tick" and new[0]["id"] == 1
    env.rec.append(failure(env.clock[0] + 1))
    assert tick(env) == []
    incs = repair.load(env.cfg.log_dir)["incidents"]
    assert len(incs) == 1 and incs[0]["count"] == 2
    assert env.n.card.call_args_list[0].args[0] == "INCIDENT #1 · KeyError @ eod_review.py:tick"


def test_off_cards_only_propose_calls_hook(env):
    hook = Mock(); env.det.on_new = hook
    env.rec.append(failure(env.clock[0] + 1)); tick(env)
    assert not hook.called and env.n.card.called
    env.cfg.repair_agent = "propose"
    env.rec.append(failure(env.clock[0] + 1, exc_type="ValueError")); tick(env)
    assert hook.call_count == 1 and hook.call_args.args[0]["fingerprint"].startswith("ValueError")


def test_command_twice_in_10_min(env):
    env.rec.append(failure(env.clock[0] + 1, action="/status")); assert tick(env) == []
    env.rec.append(failure(env.clock[0] + 1, action="/status"))
    new = tick(env)
    assert new and new[0]["fingerprint"] == "command /status"


def test_broker_reject_is_not_an_incident(env):
    env.rec.append(failure(env.clock[0] + 1, retcode=10016, title="AUREON GUARDIAN FAILURE"))
    assert tick(env) == []


def test_claude_parse_three_a_day(env):
    for _ in range(3):
        env.j.log("claude_verdict", error="invalid verdict: decision MAYBE")
    env.clock[0] = time.time()
    env.det._last_tick = 0
    new = env.det.tick(env.clock[0])
    assert any(i["fingerprint"] == "claude verdict parse" for i in new)


def test_journal_unreadable_line_and_charts(env):
    env.j.log("x")
    with open(env.j.path, "a", encoding="utf-8") as f:
        f.write("{broken\n")
    env.clock[0] = time.time()
    for _ in range(3):
        env.charts.append(env.clock[0])
    env.det._last_tick = 0
    fps = {i["fingerprint"] for i in env.det.tick(env.clock[0])}
    assert {"journal unreadable line", "chart render"} <= fps


def test_stale_frame_only_when_connected_and_open(env):
    now = env.clock[0] + 31
    ag = types.SimpleNamespace(snapshot={"market_open": True, "connected": True}, last_bar_wall=now - 1500)
    env.det.agents = {"XAUUSD": ag}
    assert tick(env)[0]["fingerprint"] == "stale frame XAUUSD"
    ag2 = types.SimpleNamespace(snapshot={"market_open": True, "connected": False}, last_bar_wall=now - 1500)
    env.det.agents = {"XAGUSD": ag2}
    assert tick(env) == []                                    # MT5 disconnected: the watchdog's, not an incident


# ------------------------------------------------------------------ propose
class FakeGit:
    """Plays git, the fixer and pytest. `fixer(wt)` edits files and returns the porcelain status lines."""
    def __init__(self, fixer=None, after_ok=True, apply_check=0, live_tests_ok=True, remote="origin\n", push=0):
        self.calls = []; self.fixer = fixer; self.after_ok = after_ok; self.apply_check = apply_check
        self.live_tests_ok = live_tests_ok; self.remote = remote; self.push = push; self.status = ""; self.pytest_n = 0
        self.prompt = None

    def __call__(self, cmd, cwd=None, input=None, **kw):
        self.calls.append((list(cmd), cwd))
        R = lambda code=0, out="", err="": types.SimpleNamespace(returncode=code, stdout=out, stderr=err)
        if cmd[:3] == ["git", "worktree", "add"]:
            wt = cmd[3]
            os.makedirs(os.path.join(wt, "aureon_mt5"), exist_ok=True)
            open(os.path.join(wt, "aureon_mt5", "agent.py"), "w").write(BASE_AGENT)
            open(os.path.join(wt, "aureon_mt5", "config.py"), "w").write(BASE_CFG)
            return R()
        if cmd[:2] == ["git", "show"]:
            return R(out=BASE_AGENT if cmd[2].endswith("agent.py") else BASE_CFG)
        if "pytest" in cmd:
            self.pytest_n += 1
            if cwd and "repair" in cwd:
                ok = self.pytest_n == 1 or self.after_ok
                return R(0 if ok else 1, "800 passed" if ok else "1 failed, 799 passed")
            return R(0 if self.live_tests_ok else 1, "810 passed" if self.live_tests_ok else "2 failed")
        if "-p" in cmd and "--allowedTools" in cmd:                 # the fixer
            self.prompt = input
            self.status = self.fixer(cwd) if self.fixer else ""
            return R(out=json.dumps({"result": "The KeyError came from a missing default.\n\nAdded .get() and a test."}))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return R(out=self.status)
        if cmd[:4] == ["git", "diff", "--cached", "--stat"]:
            return R(out=" aureon_mt5/eod_review.py | 2 +-\n tests/test_x.py | 5 +++++\n")
        if cmd[:3] == ["git", "diff", "--cached"]:
            return R(out="diff --git a/aureon_mt5/eod_review.py b/aureon_mt5/eod_review.py\n")
        if cmd[:3] == ["git", "apply", "--check"]:
            return R(self.apply_check, err="patch does not apply" if self.apply_check else "")
        if cmd[:2] == ["git", "remote"]:
            return R(out=self.remote)
        if cmd[:2] == ["git", "push"]:
            return R(self.push, err="rejected" if self.push else "")
        return R()


def good_fix(wt):
    open(os.path.join(wt, "aureon_mt5", "eod_review.py"), "w").write("x = 1\n")
    return " M aureon_mt5/eod_review.py\n?? tests/test_x.py\n"


def bad_path_fix(wt):
    return " M aureon_mt5/strategies/ema5080/journeys.py\n M aureon_mt5/eod_review.py\n"


def guardian_fix(wt):
    p = os.path.join(wt, "aureon_mt5", "agent.py")
    open(p, "w").write(BASE_AGENT.replace("return 1", "return 99"))
    return " M aureon_mt5/agent.py\n"


def helper_fix(wt):
    p = os.path.join(wt, "aureon_mt5", "agent.py")
    open(p, "w").write(BASE_AGENT.replace("return 3", "return 4"))
    return " M aureon_mt5/agent.py\n"


def with_incident(env):
    env.rec.append(failure(env.clock[0] + 1)); tick(env)
    return 1


def test_propose_from_origin_master_and_writes_patch(env):
    n = with_incident(env); g = FakeGit(good_fix)
    r = repair.Repairer(env.cfg, env.j, env.n, run=g, root=str(env.tmp))
    res = r.propose(n)
    add = next(c for c, _ in g.calls if c[:3] == ["git", "worktree", "add"])
    assert add[-1] == "origin/master" and add[5] == f"repair/{n}"
    assert res["ok"] and res["status"] == "ready" and os.path.exists(res["patch"])
    assert ["git", "reset", "--hard"] in [c for c, _ in g.calls]                 # worktree left clean
    card = env.n.card.call_args
    assert card.args[0] == f"REPAIR #{n} · ready" and "eod_review.py | 2 +-" in card.args[1] and "forbidden-path check: clean" in card.args[1]
    assert repair.get(env.cfg.log_dir, n)["status"] == "proposed"
    for word in ("aureon_mt5/strategies/**", "_guard*", "_close", "_move_sl", "broker.py", "journeys.py", "leg_guardian.py",
                 "setup.py", ".env*", "VERSIONING.md", "Do NOT commit", "Do NOT bump VERSION", "journal field name"):
        assert word in g.prompt, word


@pytest.mark.parametrize("fix,why", [(bad_path_fix, "strategies"), (guardian_fix, "guardian functions")])
def test_forbidden_changes_refused(env, fix, why):
    n = with_incident(env); g = FakeGit(fix)
    res = repair.Repairer(env.cfg, env.j, env.n, run=g, root=str(env.tmp)).propose(n)
    assert not res["ok"] and res["status"].startswith("REFUSED") and why in res["status"] and res["patch"] is None
    assert repair.get(env.cfg.log_dir, n)["status"] == "open"


def test_non_guardian_agent_change_allowed(env):
    n = with_incident(env)
    res = repair.Repairer(env.cfg, env.j, env.n, run=FakeGit(helper_fix), root=str(env.tmp)).propose(n)
    assert res["ok"]


def test_failing_tests_after_fix_not_proposed(env):
    n = with_incident(env)
    res = repair.Repairer(env.cfg, env.j, env.n, run=FakeGit(good_fix, after_ok=False), root=str(env.tmp)).propose(n)
    assert not res["ok"] and res["status"] == "tests failing after the fix"


def test_budget_used_no_call(env):
    n = with_incident(env); g = FakeGit(good_fix)
    res = repair.Repairer(env.cfg, env.j, env.n, run=g, root=str(env.tmp)).propose(n, budget=types.SimpleNamespace(take=lambda: False))
    assert not res["ok"] and "budget" in res["status"] and not any("worktree" in c for c, _ in g.calls)


# ------------------------------------------------------------------ apply
def proposed(env, **kw):
    n = with_incident(env); g = FakeGit(good_fix, **kw)
    r = repair.Repairer(env.cfg, env.j, env.n, run=g, root=str(env.tmp)); r.propose(n)
    return n, g, r


FLAT = lambda agents: False
OPEN = lambda agents: True


def test_apply_success_restarts_with_reason(env):
    n, g, r = proposed(env); restart = Mock()
    out = r.apply(n, agents={}, user="op", restart=restart, positions_block=FLAT)
    assert out.startswith("✅") and restart.call_args.args[4] == f"repair #{n}"
    cmds = [c for c, _ in g.calls]
    assert ["git", "commit", "-m", f"repair #{n}: KeyError @ eod_review.py:tick"] in cmds and ["git", "push", "origin", "HEAD:master"] in cmds
    assert repair.get(env.cfg.log_dir, n)["status"] == "applied"


def test_apply_check_fails(env):
    n, g, r = proposed(env, apply_check=1); restart = Mock()
    out = r.apply(n, agents={}, user="op", restart=restart, positions_block=FLAT)
    assert "git apply --check failed" in out and not restart.called and repair.get(env.cfg.log_dir, n)["status"] == "proposed"


def test_apply_tests_fail_reverts(env):
    n, g, r = proposed(env, live_tests_ok=False); restart = Mock()
    out = r.apply(n, agents={}, user="op", restart=restart, positions_block=FLAT)
    assert "reverted" in out and not restart.called
    assert any(c[:3] == ["git", "apply", "-R"] for c, _ in g.calls) and not any(c[:2] == ["git", "commit"] for c, _ in g.calls)


def test_apply_refused_with_open_position_unless_force(env):
    n, g, r = proposed(env); restart = Mock()
    assert r.apply(n, agents={}, user="op", restart=restart, positions_block=OPEN).startswith("⛔") and not restart.called
    assert r.apply(n, agents={}, user="op", force=True, restart=restart, positions_block=OPEN).startswith("✅")


def test_push_failure_reverts_commit(env):
    n, g, r = proposed(env, push=1); restart = Mock()
    out = r.apply(n, agents={}, user="op", restart=restart, positions_block=FLAT)
    assert "push failed" in out and ["git", "reset", "--hard", "HEAD~1"] in [c for c, _ in g.calls] and not restart.called


def test_non_admin_denied_and_discard(env):
    n, g, r = proposed(env)
    assert repair_action("apply", env.cfg, {}, None, r, NOBODY, n, admin_role="7").content.startswith("Denied")
    assert repair_action("propose", env.cfg, {}, None, r, NOBODY, n, admin_role="7").content.startswith("Denied")
    out = repair_action("discard", env.cfg, {}, None, r, ADMIN, n, admin_role="7").content
    assert "closed" in out and repair.get(env.cfg.log_dir, n)["status"] == "discarded"
    assert repair.listing(env.cfg.log_dir) == "no open incidents"


def test_propose_queued_on_claude_worker_when_claude_on(env):
    n = with_incident(env); claude = Mock()
    out = repair_action("propose", env.cfg, {}, claude, Mock(), ADMIN, n, admin_role="7").content
    assert "queued" in out and claude._enqueue.call_args.args[0]["type"] == "repair"
