"""v2.10.1 Part E — unattended operation: env check, cold start, MT5 before the bot, repair auto tier."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from aureon_mt5 import config as C, features as F, repair, server
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from tests.test_v2100_repair import FakeGit, good_fix, failure, env  # noqa: F401  (env is a fixture)

IST = server.IST


def cfg_at(tmp_path, **kw):
    c = Config(); c.log_dir = str(tmp_path); c.webhook = "https://hook"; c.bot_token = "t"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ------------------------------------------------------------------ E3 env check
def test_num_falls_back_and_records(monkeypatch):
    monkeypatch.setattr(C, "PARSE_PROBLEMS", [])
    monkeypatch.setenv("AUREON_X_TEST", "abc")
    assert C._num("AUREON_X_TEST", "7", float) == 7.0
    monkeypatch.setenv("AUREON_X_TEST", "2.5")
    assert C._num("AUREON_X_TEST", "3", int) == 3
    assert [p["issue"] for p in C.PARSE_PROBLEMS] == ["not a number", "not a whole number"]


@pytest.mark.parametrize("env,issue", [({"AUREON_GIVEBACK_PCT": "95"}, "outside"), ({"AUREON_REGIME_GATE": "maybe"}, "one of"),
                                      ({"AUREON_SETUP_STOP": "sometimes"}, "on/off"), ({"AUREON_SETUP_MAX_DD": "1"}, "outside")])
def test_registry_ranges(tmp_path, env, issue):
    probs = C.validate(cfg_at(tmp_path), environ=env)
    assert len(probs) == 1 and issue in probs[0]["issue"] and probs[0]["env"] == list(env)[0]


def test_contradictions_and_discord(tmp_path):
    c = cfg_at(tmp_path, claude_exit_losing=True, claude_mode="advisory", setup_stop=True, setup_max_dd=4.0,
               regime_dead=0.5, regime_choppy=0.45, webhook=None, bot_token=None)
    names = {p["name"] for p in C.validate(c, environ={"AUREON_ADMIN_ROLE_ID": "admins"})}
    assert {"exit_losing", "setup_max_dd", "regime_thresholds", "discord_webhook", "discord_token", "admin_role"} <= names


def test_fallbacks_set_safe_defaults(tmp_path):
    c = cfg_at(tmp_path, giveback_pct=95.0, setup_stop=True, setup_max_dd=4.0, regime_dead=0.5)
    probs = C.validate(c, environ={"AUREON_GIVEBACK_PCT": "95"})
    changed = C.apply_fallbacks(c, probs)
    assert c.giveback_pct == 0.0 and c.setup_max_dd == 10.0 and c.regime_dead == 0.25
    assert {"giveback_pct", "setup_max_dd", "regime_thresholds"} <= set(changed)


def test_defaults_clean_and_env_check_shows_overrides(tmp_path):
    c = cfg_at(tmp_path)
    assert C.validate(c, environ={}) == []
    F.set_override(c.log_dir, c, "giveback_pct", "50", "op")
    t = C.env_check_table(c, F.load_overrides(c.log_dir), environ={})
    assert "giveback_pct 50% (discord)" in t and "problems** · none" in t


# ------------------------------------------------------------------ E1 cold start
def fake_ps(boot="2026-10-15T22:11:00.0000000Z", events=None, fail=False):
    def run(cmd, timeout=5):
        if fail:
            return False, "boom"
        if "LastBootUpTime" in cmd:
            return True, boot
        if "Get-WinEvent" in cmd:
            return True, json.dumps(events if events is not None else
                                    [{"Id": 1074, "TimeCreated": "/Date(1792102800000)/",
                                      "Message": "The process C:\\Windows\\system32\\svchost.exe has initiated the restart ... "
                                                 "Reason Code: 0x80020010\nShutdown Type: restart\nComment: Windows Update"},
                                     {"Id": 6006, "TimeCreated": "/Date(1792102700000)/", "Message": "The Event log service was stopped."}])
        return False, ""
    return run


def test_cold_start_card(tmp_path):
    c = cfg_at(tmp_path); j = Journal(str(tmp_path)); n = Mock()
    hb = tmp_path / "heartbeat.txt"; hb.write_text("2026-10-16T03:27:00+05:30 v2.10.1 lag=0.0s gateway=up health=ok")
    now = time.time(); os.utime(hb, (now - 840, now - 840))
    j.save_state("XAUUSD", {77: {"secured": 10.0, "entry": 4100.0}})
    out = server.cold_start(c, j, n, {"XAUUSD": [{"ticket": 77, "direction": "long", "sl": 4110.0}]}, run=fake_ps(), now=now)
    text = n.card.call_args.args[1]
    assert n.card.call_args.args[0] == "AUREON · MT5 · COLD START"
    assert "rebooted 03:41 IST · down 14 min · reason: Windows Update (1074)" in text
    assert "#77 long SL 4110.0 secured 10.0 (re-adopted)" in text and "last heartbeat: 2026-10-16T03:27" in text
    row = [r for r in j.read(0) if r["event"] == "cold_start"][-1]
    assert row["down_s"] == 840 and row["reason"] == "Windows Update (1074)"


@pytest.mark.parametrize("events,want", [([{"Id": 6008, "TimeCreated": "/Date(1792102800000)/", "Message": "x"}], "unexpected shutdown (6008)"),
                                         ([], "unknown")])
def test_cold_start_reasons(tmp_path, events, want):
    n = Mock()
    server.cold_start(cfg_at(tmp_path), Journal(str(tmp_path)), n, {}, run=fake_ps(events=events), now=time.time())
    assert f"reason: {want}" in n.card.call_args.args[1]


def test_cold_start_degrades_to_unknown(tmp_path):
    n = Mock()
    out = server.cold_start(cfg_at(tmp_path), Journal(str(tmp_path)), n, {}, run=fake_ps(fail=True))
    assert "rebooted unknown · down ? min · reason: unknown" in n.card.call_args.args[1] and "no open positions" in n.card.call_args.args[1]


def test_down_text():
    assert server.down_text({"t": 100.0}, now=142.4) == "down 42 s" and server.down_text({}) == "down ? s"


# ------------------------------------------------------------------ E2 MT5 before the bot
def test_connect_mt5_backoff_card_and_give_up():
    t = [0.0]; sleeps = []; resets = []
    n = Mock()
    ok = server.connect_mt5(lambda: False, boot_wait=400, notify=n, sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)),
                            clock=lambda: t[0], reset=lambda: resets.append(1))
    assert ok is False and sleeps[:4] == [5.0, 10.0, 20.0, 30.0] and max(sleeps) == 30.0
    assert n.card.call_count == 1 and "MT5 NOT UP" in n.card.call_args.args[0] and len(resets) == len(sleeps) + 1


def test_connect_mt5_succeeds_after_retries():
    calls = iter([False, False, True])
    assert server.connect_mt5(lambda: next(calls), boot_wait=300, sleep=lambda s: None, clock=lambda: 0.0)


def test_mt5_path_cached(tmp_path):
    exe = server.remember_mt5_path(str(tmp_path), types.SimpleNamespace(path="C:\\MT5"))
    assert open(server.mt5_path_file(str(tmp_path))).read() == exe and exe.endswith("terminal64.exe")


# ------------------------------------------------------------------ E4 repair auto tier
def ready(env, **git_kw):
    env.cfg.repair_agent = "auto"; env.cfg.repair_auto_per_day = 2; env.cfg.repair_verify_s = 600
    env.rec.append(failure(env.clock[0] + 1)); env.clock[0] += 31; env.det.tick(env.clock[0])
    g = FakeGit(good_fix, **git_kw)
    r = repair.Repairer(env.cfg, env.j, env.n, run=g, root=str(env.tmp)); r.propose(1)
    return r, g


NIGHT = datetime(2026, 10, 15, 23, 30, tzinfo=IST)
DAY = datetime(2026, 10, 15, 14, 0, tzinfo=IST)
FLAT = lambda a: False
OPEN = lambda a: True


def test_auto_happy_path_applies_announces_and_marks(env):
    r, g = ready(env); restart = Mock()
    out = repair.maybe_auto_apply(r, 1, {}, now=NIGHT, positions_block=FLAT, restart=restart)
    assert out.startswith("✅") and restart.call_args.kwargs["marker_kind"] == "repair-auto"
    titles = [c.args[0] for c in env.n.card.call_args_list]
    assert "REPAIR AUTO-APPLY #1" in titles
    v = repair.read_verify(env.cfg.log_dir)
    assert v["n"] == 1 and v["fingerprint"] == "KeyError @ eod_review.py:tick"
    assert [x for x in env.j.read(0) if x["event"] == "repair_auto_applied"]


@pytest.mark.parametrize("case,guard", [("open", "position"), ("session", "inside the trading session"),
                                        ("blocked", "rolled back before"), ("cap", "daily cap")])
def test_each_guard_holds_auto(env, case, guard):
    r, g = ready(env); restart = Mock(); now = NIGHT; pb = FLAT
    if case == "open":
        pb = OPEN
    elif case == "session":
        now = DAY
    elif case == "blocked":
        d = repair.load(env.cfg.log_dir); d["auto_blocked"] = ["KeyError @ eod_review.py:tick"]; repair.save(env.cfg.log_dir, d)
    elif case == "cap":
        env.j.log("repair_auto_applied", id=9); env.j.log("repair_auto_applied", id=10)
        now = datetime.now(IST).replace(hour=23, minute=30)
    out = repair.maybe_auto_apply(r, 1, {}, now=now, positions_block=pb, restart=restart)
    assert out.startswith("held") and guard in out and not restart.called
    assert any("AUTO-APPLY HELD" in c.args[0] for c in env.n.card.call_args_list)


def test_unhealthy_incident_may_apply_in_session(env):
    r, g = ready(env)
    repair.update(env.cfg.log_dir, 1, kind="journal")
    assert repair.maybe_auto_apply(r, 1, {}, now=DAY, positions_block=FLAT, restart=Mock()).startswith("✅")


def test_live_suite_failure_reverts_even_in_auto(env):
    r, g = ready(env, live_tests_ok=False); restart = Mock()
    out = repair.maybe_auto_apply(r, 1, {}, now=NIGHT, positions_block=FLAT, restart=restart)
    assert "reverted" in out and not restart.called and repair.read_verify(env.cfg.log_dir) is None


def test_propose_mode_never_auto_applies(env):
    r, g = ready(env); env.cfg.repair_agent = "propose"
    assert repair.maybe_auto_apply(r, 1, {}, now=NIGHT, positions_block=FLAT, restart=Mock()) == "not auto"


def test_verifier_verifies_after_window(env):
    repair.write_verify(env.cfg.log_dir, {"n": 1, "fingerprint": "fp", "sha": "abc", "pushed": True, "t": 0})
    clock = [1000.0]; n = Mock()
    v = repair.Verifier(env.cfg, env.j, n, health_ok=lambda: True, run=FakeGit(), root=str(env.tmp), clock=lambda: clock[0], restart=Mock())
    assert v.tick(1100.0) is None
    assert v.tick(1000.0 + 601) == "verified" and n.card.call_args.args[0] == "REPAIR VERIFIED #1"
    assert repair.read_verify(env.cfg.log_dir) is None and v.tick(5000.0) is None


def test_verifier_rolls_back_once_on_recurrence(env):
    env.rec.append(failure(env.clock[0] + 1)); env.clock[0] += 31; env.det.tick(env.clock[0])     # incident #1 (the repaired one)
    repair.update(env.cfg.log_dir, 1, status="applied")
    repair.write_verify(env.cfg.log_dir, {"n": 1, "fingerprint": "KeyError @ eod_review.py:tick", "sha": "abc", "pushed": True, "t": 0})
    g = FakeGit(); restart = Mock(); n = Mock()
    v = repair.Verifier(env.cfg, env.j, n, run=g, root=str(env.tmp), clock=lambda: env.clock[0], restart=restart)
    env.rec.append(failure(env.clock[0] + 1)); env.clock[0] += 31; env.det.tick(env.clock[0])     # it comes back → #2
    assert v.tick(env.clock[0]) == "rolled back" and v.tick(env.clock[0] + 999) is None
    assert restart.call_count == 1 and restart.call_args.kwargs["marker_kind"] == "repair-rollback"
    assert ["git", "revert", "--no-edit", "abc"] in [c for c, _ in g.calls]
    d = repair.load(env.cfg.log_dir)
    assert "KeyError @ eod_review.py:tick" in d["auto_blocked"] and repair.get(env.cfg.log_dir, 1)["status"] == "needs_human"
    assert n.card.call_args.args[0] == "REPAIR ROLLED BACK #1"


def test_verifier_rolls_back_when_unhealthy(env):
    repair.write_verify(env.cfg.log_dir, {"n": 3, "fingerprint": "fp", "sha": "abc", "pushed": False, "t": 0})
    restart = Mock()
    v = repair.Verifier(env.cfg, env.j, Mock(), health_ok=lambda: False, run=FakeGit(), root=str(env.tmp), clock=lambda: 0.0, restart=restart)
    assert v.tick(700.0) == "rolled back" and restart.call_count == 1
