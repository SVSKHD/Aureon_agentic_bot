"""v2.18.1 — ops additions: /update force [notests] + /update status, the bounded repair helper (/repair), attach / detach
symbols while live (/attach, /detach, /symbols)."""
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from aureon_mt5 import auto_update as AU, netguard, ops, ops_repair as R, symbols_ctl as SC
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.bot import ops_repair_action, parallel_card, symbols_action
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.strategies import get_strategy
from tests.test_v2170_auto_update import A, B, FakeSh, Note, rig

ADMIN = SimpleNamespace(roles=[SimpleNamespace(id=42)])


class Note(Note):
    def flush(self):
        return 0


# ============================================================================= 6a /update force · status
def holding(n=1):
    return {"XAUUSD": SimpleNamespace(snapshot={"positions": [{"ticket": 7 + i} for i in range(n)]})}


def test_force_ignores_the_flat_gate_and_carries_positions(tmp_path):
    up, sh, n, j, box = rig(tmp_path, flat=False); up.agents = holding(1)
    assert up.tick() == "pending" and box["restarts"] == 0                   # the normal path waits
    assert up.command("force", "koushik") == "UPDATE FORCED — restarting" and box["restarts"] == 1
    assert n.cards[-1][0] == "UPDATE FORCED · aaaaaaa → bbbbbbb · 1 positions carried"
    assert any("pytest" in c for c in sh.calls)                              # the suite still ran
    m = ops.pop_restart_marker(str(tmp_path))
    assert m["kind"] == "update-force" and "1 position(s) carried" in m["reason"]
    row = [r for r in j.read(0) if r["event"] == "update_applied"][-1]
    assert row["forced"] is True and row["suite_skipped"] is False


def test_force_notests_needs_a_confirmation_within_60s(tmp_path):
    up, sh, n, j, box = rig(tmp_path, flat=False); up.agents = holding(2)
    assert up.command("force notests confirm") .startswith("⛔ no pending")
    assert "confirm with `/update force notests confirm` within 60 s" in up.command("force notests")
    box["t"] += 61
    assert up.command("force notests confirm").startswith("⛔") and box["restarts"] == 0
    up.command("force notests"); box["t"] += 30
    assert up.command("force notests confirm") == "UPDATE FORCED — restarting" and box["restarts"] == 1
    assert n.cards[-1][0] == "UPDATE FORCED · aaaaaaa → bbbbbbb · 2 positions carried · SUITE SKIPPED"
    assert n.cards[-1][0].endswith("SUITE SKIPPED") and not any("pytest" in c for c in sh.calls)
    assert "SUITE SKIPPED" in ops.pop_restart_marker(str(tmp_path))["reason"]


def test_force_is_refused_while_dns_is_down(tmp_path, monkeypatch):
    monkeypatch.setitem(netguard.STATE, "dns_fail", True)
    up, sh, n, j, box = rig(tmp_path)
    assert up.command("force") == "UPDATE REFUSED · network degraded" and box["restarts"] == 0
    assert n.cards[-1][0] == "UPDATE REFUSED · network degraded" and not any(c[:2] == ["git", "fetch"] for c in sh.calls)
    monkeypatch.setitem(netguard.STATE, "dns_fail", False)
    monkeypatch.setitem(netguard.STATE, "dns", {"discord": {"ok": True}, "ntfy": {"ok": False}})     # ntfy alone is no reason
    assert not AU.dns_down()


def test_force_with_a_red_suite_holds_and_never_restarts(tmp_path):
    sh = FakeSh(green=False, out="FAILED tests/test_x.py::t - assert 0\n1 failed")
    up, sh, n, j, box = rig(tmp_path, sh=sh, flat=False)
    assert up.command("force").startswith("UPDATE HELD") and box["restarts"] == 0 and sh.head == A


def test_status_and_the_repair_hook(tmp_path):
    seen = []
    sh = FakeSh(green=False, out="FAILED tests/test_y.py::t - ModuleNotFoundError: x\n1 failed")
    up, sh, n, j, box = rig(tmp_path, sh=sh); up.on_problem = lambda kind, detail: seen.append((kind, detail[:40]))
    up.tick()
    st = up.command("status")
    assert st.startswith("**/update status** · auto-update on") and "HEAD `aaaaaaaa` · origin/master `bbbbbbbb` (behind)" in st
    assert "held: bbbbbbbb" in st and "last error: tests failed on bbbbbbbb: FAILED tests/test_y.py::t" in st
    assert seen and seen[0][0] == "UPDATE HELD"


# ============================================================================= 6b the repair helper
CTX = {"log_tail": "", "updater_error": "", "git_status": "", "heads": [A, A], "heads_raw": f"{A}\n{A}",
       "dns": {"github.com": True, "discord.com": True}, "pip_check": "No broken requirements found.", "pip_ok": True, "test_output": ""}


@pytest.mark.parametrize("ctx,actions,cause", [
    (dict(dns={"github.com": False, "discord.com": False}, updater_error="git fetch origin master failed"), [], "github.com does not resolve"),
    (dict(updater_error="git fetch failed: fatal: Unable to create '/srv/aureon/.git/index.lock': File exists.", heads=[A, B]),
     ["fetch_origin", "reset_to_origin", "rerun_suite", "restart_bot"], "stale .git/index.lock"),
    (dict(updater_error="tests failed on bbbbbbbb: FAILED tests/test_x.py::test_y - AssertionError: 1 == 2"), ["rerun_suite"],
     "FAILED tests/test_x.py::test_y"),
    (dict(test_output="ERROR tests/test_z.py - ModuleNotFoundError: No module named 'pyfiglet'"),
     ["pip_install_requirements", "rerun_suite", "restart_bot"], "missing / broken package"),
])
def test_rules_propose_the_expected_actions(ctx, actions, cause):
    d = R.diagnose({**CTX, **ctx}, use_claude=False)
    assert d["actions"] == actions and cause in d["cause"] and set(d["actions"]) <= set(R.ACTIONS) and d["source"] == "rules"


def test_unknown_actions_are_rejected_before_anything_runs():
    with pytest.raises(R.Rejected):
        R.validate(["fetch_origin", "rm -rf /"])
    claude = lambda *a, **k: SimpleNamespace(ok=True, text='{"cause": "x", "actions": ["fetch_origin", "edit_env"]}', raw="")
    with pytest.raises(R.Rejected):
        R.diagnose(dict(CTX), cfg=Config(), caller=claude)
    ok = lambda *a, **k: SimpleNamespace(ok=True, text='{"cause": "behind", "actions": ["fetch_origin"]}', raw="")
    assert R.diagnose(dict(CTX), cfg=Config(), caller=ok) == {"cause": "behind", "actions": ["fetch_origin"], "source": "claude"}


class Sh:
    """git / pip / pytest stand-in for the helper; records every command; `fail` names the command that fails."""
    def __init__(self, fail=None, log=None):
        self.calls, self.fail = [], fail

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        bad = self.fail and self.fail in " ".join(cmd)
        out = {"rev-parse": f"{A}\n{B}", "status": "", "check": "No broken requirements found."}.get(cmd[1] if len(cmd) > 1 else "", "")
        if "pytest" in cmd:
            out = "1300 passed" if not bad else "FAILED tests/t.py::x\n1 failed"
        return SimpleNamespace(returncode=1 if bad else 0, stdout=out, stderr="")


def helper(tmp_path, sh=None, updater_error="", resolver=None, **kw):
    cfg = Config(); cfg.log_dir = str(tmp_path / "logs"); cfg.claude_mode = "off"
    os.makedirs(cfg.log_dir, exist_ok=True)
    up = SimpleNamespace(state={"last_error": updater_error})
    restarts = []
    h = R.OpsRepair(cfg, Journal(cfg.log_dir), Note(), updater=up, sh=sh or Sh(), root=str(tmp_path), resolver=resolver or (lambda h, p: True),
                    restart=lambda: restarts.append(1), run_ps=lambda cmd, t: (True, "mt5 restarted"), **kw)
    return h, restarts


def test_diagnosis_then_go_stops_at_the_first_failure(tmp_path):
    sh = Sh(fail="pytest")
    h, restarts = helper(tmp_path, sh=sh, updater_error="tests failed on bbbbbbbb: ERROR tests/t.py - ModuleNotFoundError: x")
    out = ops_repair_action(h.cfg, ADMIN, "", admin_role="42", helper=h)
    assert out.startswith("**REPAIR · diagnosis** (rules)") and "pip_install_requirements → rerun_suite → restart_bot" in out
    assert h.notify.cards[-1][0] == "REPAIR · diagnosis"
    res = h.command("go", "koushik")
    assert res.startswith("REPAIR · failed") and "❌ rerun_suite" in res and restarts == []      # stopped before restart_bot
    assert h.notify.cards[-1][0] == "REPAIR · failed"
    rows = [r for r in h.journal.read(0) if r["event"] == "repair_action"]
    assert [r["rationale"] for r in rows] == ["pip_install_requirements", "rerun_suite"] and all(r["actor"] == "repair" for r in rows)
    assert h.command("go") .startswith("nothing to run")


def test_three_runs_an_hour(tmp_path):
    h, restarts = helper(tmp_path)
    for i in range(3):
        assert h.go("me", actions=["flush_card_queue"]).startswith("REPAIR · done")
    assert h.go("me", actions=["flush_card_queue"]).startswith("⛔ 3 repair runs in the last hour")
    h.clock = lambda: time.time() + 3601
    assert h.go("me", actions=["flush_card_queue"]).startswith("REPAIR · done")


def test_auto_runs_only_for_update_failed_and_held(tmp_path):
    h, restarts = helper(tmp_path, updater_error="fatal: Unable to create '/x/.git/index.lock': File exists.")
    assert h.on_problem("UPDATE HELD", "") is None                          # auto off by default
    assert h.command("auto on").startswith("repair auto **on**")
    assert h.on_problem("CLOSED", "") is None and h.on_problem("ENTER", "") is None    # never a trade card
    out = h.on_problem("UPDATE FAILED", "")
    assert out.startswith("REPAIR · done") and restarts == [1]
    acts = [r["rationale"] for r in h.journal.read(0) if r["event"] == "repair_action"]
    assert acts == ["fetch_origin", "reset_to_origin", "rerun_suite", "restart_bot"]
    assert ops.pop_restart_marker(h.cfg.log_dir)["kind"] == "ops-repair"
    assert h.command("auto off").startswith("repair auto **off**")


def test_admin_only(tmp_path):
    h, _ = helper(tmp_path)
    assert ops_repair_action(h.cfg, SimpleNamespace(roles=[]), "go", admin_role="42", helper=h) == "Denied: configured administrator role required"


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_reset_to_origin_clears_a_stale_lock_saves_the_patch_and_never_touches_env_or_logs(tmp_path):
    origin = tmp_path / "o.git"; seed = tmp_path / "seed"; work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin)); _git(tmp_path, "clone", "-q", str(origin), str(seed))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(seed, "config", k, v)
    (seed / ".gitignore").write_text(".env\nlogs/\nguardian_state.json\n"); (seed / "a.py").write_text("v = 1\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "1"); _git(seed, "push", "-q", "origin", "HEAD:master")
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (seed / "a.py").write_text("v = 2\n"); _git(seed, "commit", "-qam", "2"); _git(seed, "push", "-q", "origin", "HEAD:master")
    (work / "a.py").write_text("v = 1  # local\n"); (work / ".env").write_text("AUREON_MODE=ema921\n")
    (work / "guardian_state.json").write_text("{}"); (work / "logs").mkdir(); (work / "logs" / "journal.jsonl").write_text("x\n")
    lock = work / ".git" / "index.lock"; lock.write_text(""); os.utime(lock, (time.time() - 600, time.time() - 600))
    cfg = Config(); cfg.log_dir = str(work / "logs"); cfg.claude_mode = "off"
    h = R.OpsRepair(cfg, Journal(cfg.log_dir), Note(), updater=SimpleNamespace(state={}), root=str(work), restart=lambda: None)
    ok, what = h._a_reset_to_origin()
    assert ok and not lock.exists() and (work / "a.py").read_text() == "v = 2\n" and "patch" in what
    assert (work / ".env").read_text() == "AUREON_MODE=ema921\n" and (work / "guardian_state.json").read_text() == "{}"
    assert (work / "logs" / "journal.jsonl").read_text() == "x\n"


def test_service_script_has_the_restart_mt5_action():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "aureon_service.ps1"),
               encoding="utf-8").read()
    assert '"restart-mt5"' in src and "Ensure-Mt5" in src.split('"restart-mt5" {')[1][:600]


# ============================================================================= 6c attach / detach
@pytest.fixture
def live(tmp_path, monkeypatch):
    cfg = Config(); cfg.dry = True; cfg.log_dir = str(tmp_path / "logs"); cfg.mode = "ema921"; cfg.symbols = ["XAUUSD"]
    cfg.paper = True; cfg.poll_seconds = 0.05; cfg.max_symbols = 3; cfg.claude_symbols = ("XAUUSD", "XAGUSD")
    S = get_strategy("ema921"); j = Journal(cfg.log_dir); note = Note()
    monkeypatch.setattr("aureon_mt5.agent.context_chart", lambda *a, **k: None)
    make = lambda sym: SymbolAgent(sym, cfg, S, Notifier(None), j, ())
    agents = {"XAUUSD": SimpleNamespace(is_alive=lambda: True, snapshot={"positions": []}, S=S, health={"status": "running"},
                                        g=None, stop=lambda: None, join=lambda timeout=None: None)}
    env = tmp_path / ".env"; env.write_text("AUREON_MODE=ema921\nAUREON_SYMBOLS=XAUUSD\nDISCORD_TOKEN=keep\n")
    box = {"open": True}
    ctl = SC.SymbolController(cfg, agents, make, j, note, env_path=str(env), strategy=S, market_open=lambda s: box["open"])
    yield ctl, agents, cfg, env, note, box
    for s, ag in list(agents.items()):
        if isinstance(ag, SymbolAgent):
            ag.stop(); ag.join(timeout=5)


def test_attach_starts_a_thread_and_persists(live):
    ctl, agents, cfg, env, note, box = live
    assert symbols_action(cfg, ADMIN, "attach", "XAGUSD", admin_role="42", ctl=ctl).startswith("✅ XAGUSD: attach requested")
    ctl.tick()
    ag = agents["XAGUSD"]
    assert isinstance(ag, SymbolAgent) and ag.is_alive() and cfg.symbols == ["XAUUSD", "XAGUSD"]
    assert note.cards[-1][0] == "ATTACHED · XAGUSD · rules: tp 200 / sl 100 pts · London"
    assert "XAGUSD" in str(parallel_card({"XAGUSD": ag}, cfg))
    assert SC.read_env_symbols(str(env)) == ["XAUUSD", "XAGUSD"] and "DISCORD_TOKEN=keep" in env.read_text()
    assert "`XAGUSD` RUNNING · tp 200 / sl 100 pts · London" in ctl.listing()
    assert ctl.attach("XAGUSD").startswith("⛔ XAGUSD: already running")


def test_attach_unknown_symbol_is_refused_and_starts_nothing(live):
    ctl, agents, cfg, env, note, box = live
    out = ctl.attach("EURJPY")
    ctl.tick()
    assert out.startswith("⛔ EURJPY: ema921 has no rules for EURJPY") and "EURJPY" not in agents and cfg.symbols == ["XAUUSD"]
    assert SC.read_env_symbols(str(env)) == ["XAUUSD"]


def test_attach_queued_while_closed_and_max_symbols(live):
    ctl, agents, cfg, env, note, box = live
    box["open"] = False
    ctl.attach("EURUSD"); ctl.tick()
    assert "EURUSD" not in agents and ctl.state_of("EURUSD") == "PENDING ATTACH"
    assert note.cards[-1][0] == "ATTACH QUEUED · EURUSD · market closed"
    assert ctl.attach("GBPUSD").startswith("✅") and ctl.attach("AUDUSD").startswith("⛔ AUDUSD: AUREON_MAX_SYMBOLS=3")
    box["open"] = True; ctl.tick()
    assert agents["EURUSD"].is_alive() and ctl.state_of("EURUSD") == "RUNNING"
    assert note.cards[-1][0].startswith("ATTACHED · ") and "no advisor (not in AUREON_CLAUDE_SYMBOLS)" in note.cards[-1][1]


def test_detach_with_a_paper_position_waits_then_completes(live):
    ctl, agents, cfg, env, note, box = live
    ctl.attach("XAGUSD"); ctl.tick(); ag = agents["XAGUSD"]
    st = ag._state921(); st["paper"] = [{"id": "P1", "side": "LONG"}]
    assert ctl.detach("XAGUSD", now=True).startswith("⛔ XAGUSD has 1 open position(s)")
    assert ctl.detach("XAGUSD") == "✅ XAGUSD: no new entries · DETACH PENDING · managing 1 position(s)"
    ctl.tick()
    assert ag.detaching and ag.is_alive() and "XAGUSD" in agents and ctl.state_of("XAGUSD") == "DETACH PENDING"
    assert note.cards[-1][0] == "DETACH PENDING · XAGUSD · managing 1 position"
    st["paper"] = []                                                            # the guardian closed it
    ctl.tick()
    assert "XAGUSD" not in agents and not ag.is_alive() and cfg.symbols == ["XAUUSD"]
    assert note.cards[-1][0] == "DETACHED · XAGUSD" and SC.read_env_symbols(str(env)) == ["XAUUSD"]


def test_detach_blocks_new_entries_at_once(live):
    ctl, agents, cfg, env, note, box = live
    ctl.attach("XAGUSD"); ctl.tick(); ag = agents["XAGUSD"]
    ag._state921()["paper"] = [{"id": "P1"}]
    ctl.detach("XAGUSD")
    called = []
    ag.S = SimpleNamespace(analyse=lambda *a, **k: called.append(1), name="ema921")
    ag._detect(None, None, 0, 0.0, 0.0, 0.0)
    assert called == []


def test_symbols0_can_never_be_detached(live):
    ctl, agents, cfg, env, note, box = live
    assert ctl.detach("XAUUSD").startswith("⛔ XAUUSD is symbols[0]")
    assert ctl.detach("EURUSD") == "⛔ EURUSD is not running"


def test_env_round_trip(tmp_path):
    env = tmp_path / ".env"; env.write_text("A=1\nAUREON_SYMBOLS=XAUUSD\nB=2\n")
    assert SC.write_env_symbols(str(env), ["XAUUSD", "XAGUSD", "EURUSD"])
    assert SC.read_env_symbols(str(env)) == ["XAUUSD", "XAGUSD", "EURUSD"]
    text = env.read_text()
    assert "A=1" in text and "B=2" in text and text.count("AUREON_SYMBOLS=") == 1
    assert SC.write_env_symbols(str(tmp_path / "missing.env"), ["XAUUSD"]) is False and not (tmp_path / "missing.env").exists()
