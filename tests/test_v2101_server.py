"""v2.10.1 Part F — the box belongs to Aureon. Every OS call goes through a faked run_ps; nothing real is executed."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from unittest.mock import Mock
import pytest
from aureon_mt5 import features as F, ops, server, tuning, tuning_auto as TA
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from tests.test_v181 import harness  # noqa: F401

IST = server.IST
NIGHT = datetime(2026, 10, 15, 23, 30, tzinfo=IST).timestamp()
DAY = datetime(2026, 10, 15, 14, 0, tzinfo=IST).timestamp()


class PS:
    """Fake run_ps: answers by substring; records every command."""
    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}; self.fail = fail; self.calls = []
    def __call__(self, cmd, timeout=5):
        self.calls.append(cmd)
        if self.fail:
            return False, "boom"
        for k, v in self.answers.items():
            if k in cmd:
                return True, v
        return True, ""


@pytest.fixture
def cfg(tmp_path):
    c = Config(); c.log_dir = str(tmp_path); return c


@pytest.fixture(autouse=True)
def reset_state():
    keep = dict(server.STATE)
    for k in ("drift", "updates_pending", "boot", "mt5_age", "disk_free_gb", "maintenance_window"):
        server.STATE[k] = None
    server.STATE["network_degraded"] = False
    yield
    server.STATE.clear(); server.STATE.update(keep); server.STATE["network_degraded"] = False


def maint(cfg, ps, block=False):
    flags = {"open": block}
    m = server.Maintenance(cfg, Journal(cfg.log_dir), Mock(), {}, run=ps, positions_block=lambda a: flags["open"], sh=Mock())
    return m, flags


PENDING = {"Test-Path": "True", "Updates.Count": "3"}


# ------------------------------------------------------------------ harden
def test_harden_prints_its_plan():
    ps = PS({"aureon_server.ps1": "PLAN  Windows Update active hours 5:00–23:00\nPLAN  sleep disabled"})
    out = server.harden(plan=True, run=ps)
    assert "-Action plan" in ps.calls[0] and "PLAN  Windows Update active hours" in out
    assert server.harden(run=PS(fail=True)).startswith("harden unavailable")


# ------------------------------------------------------------------ maintenance window
def test_pending_reboot_with_position_open_never_reboots(cfg):
    ps = PS(PENDING); m, _ = maint(cfg, ps, block=True)
    assert m.tick(NIGHT) is None and not any("shutdown" in c for c in ps.calls)


def test_flat_in_session_waits(cfg):
    ps = PS(PENDING); m, _ = maint(cfg, ps)
    assert m.tick(DAY) is None and not any("shutdown" in c for c in ps.calls)


def test_flat_outside_session_reboots_with_marker(cfg):
    ps = PS(PENDING); m, _ = maint(cfg, ps)
    assert m.tick(NIGHT) == "reboot scheduled"
    assert any(c.startswith("shutdown /r /t 60") for c in ps.calls)
    mk = ops.pop_restart_marker(cfg.log_dir, max_age=1e12)
    assert mk["kind"] == "maintenance" and "updates applied: 3" in mk["reason"]
    assert "MAINTENANCE" in m.notify.card.call_args_list[0].args[0] and "rebooting for Windows Update in 60 s" in m.notify.card.call_args_list[0].args[1]


def test_position_opens_during_countdown_aborts_and_reschedules(cfg):
    ps = PS(PENDING); m, flags = maint(cfg, ps)
    m.tick(NIGHT); flags["open"] = True
    assert m.tick(NIGHT + 20) == "aborted"
    assert "shutdown /a" in ps.calls and m.state["reboot_at"] is None and m.state["last_check"] == 0
    assert ops.pop_restart_marker(cfg.log_dir, max_age=1e12) is None
    assert "MAINTENANCE ABORTED" in m.notify.card.call_args.args[0]


def test_force_after_48h_only_when_flat(cfg):
    ps = PS(PENDING); m, flags = maint(cfg, ps)
    m.state["pending_since"] = DAY - 49 * 3600
    flags["open"] = True
    assert m.tick(DAY) is None                                          # pending 49 h but a position is open
    flags["open"] = False; m.state["last_check"] = 0
    assert m.tick(DAY) == "reboot scheduled"                            # in session, flat, forced


def test_hourly_cadence(cfg):
    ps = PS({"Test-Path": "False", "Updates.Count": "0"}); m, _ = maint(cfg, ps)
    m.update_code = lambda: "current"; m.pip_if_changed = lambda: "unchanged"
    m.tick(NIGHT); n = len(ps.calls); m.tick(NIGHT + 600)
    assert len(ps.calls) == n


def test_update_code_red_suite_stays_on_old(cfg, monkeypatch):
    m, _ = maint(cfg, PS())
    monkeypatch.setattr("aureon_mt5.common.updater.update_to_origin", lambda *a, **k: {"status": "updated", "old": "a" * 40, "new": "b" * 40, "discarded": []})
    m.sh = Mock(return_value=types.SimpleNamespace(returncode=1, stdout="2 failed, 800 passed", stderr=""))
    exit_hook = Mock(); m.exit_hook = exit_hook
    assert m.update_code() == "red" and not exit_hook.called and m.state["bad_sha"] == "b" * 40
    assert any(c.args[0][:3] == ["git", "reset", "--hard"] for c in m.sh.call_args_list)
    m.sh = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="810 passed", stderr=""))
    assert m.update_code() == "skipped (known red)"


def test_update_code_green_restarts(cfg, monkeypatch):
    m, _ = maint(cfg, PS())
    monkeypatch.setattr("aureon_mt5.common.updater.update_to_origin", lambda *a, **k: {"status": "updated", "old": "a" * 40, "new": "c" * 40, "discarded": []})
    m.sh = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="810 passed", stderr="")); m.exit_hook = Mock()
    assert m.update_code() == "updated" and m.exit_hook.call_args.args[0] == 75
    assert ops.pop_restart_marker(cfg.log_dir, max_age=1e12)["kind"] == "auto-update"


def test_rotate_logs_keeps_journal(tmp_path):
    old = time.time() - 40 * 86400
    keep = ["journal.jsonl", "guardian_state.json", "incidents.json", "features.json", "heartbeat.txt"]
    gone = ["stall-20260901-000000.txt", "service.log.3", "local_changes_abc.patch"]
    for n in keep + gone:
        p = tmp_path / n; p.write_text("x"); os.utime(p, (old, old))
    (tmp_path / "charts").mkdir(); c = tmp_path / "charts" / "a.png"; c.write_text("x"); os.utime(c, (time.time() - 8 * 86400,) * 2)
    assert server.rotate_logs(str(tmp_path), 30) == 4
    assert all((tmp_path / n).exists() for n in keep) and not any((tmp_path / n).exists() for n in gone)


# ------------------------------------------------------------------ MT5 manager
def test_mt5_hang_restarts_and_verifies(cfg):
    cfg.dry = False; cfg.mt5_path = "C:\\MT5\\terminal64.exe"
    b = types.SimpleNamespace(connected=lambda: False, reset=Mock(), connect=Mock(return_value=True),
                              account=lambda: {"login_masked": "***123", "trade_mode": "demo"},
                              mt5=lambda: types.SimpleNamespace(symbol_info=lambda s: object()))
    ps = PS({"Measure-Object": "1"}); n = Mock(); t = [0.0]
    mgr = server.Mt5Manager(cfg, Journal(cfg.log_dir), n, b, run=ps, clock=lambda: t[0], sleep=lambda s: None)
    assert mgr.tick(0.0) is None and mgr.tick(30.0) is None
    assert mgr.tick(61.0) == "up"
    assert any("Stop-Process -Name terminal64" in c for c in ps.calls) and any("Start-Process" in c for c in ps.calls)
    assert "MT5 RESTARTED" in n.card.call_args.args[0] and "login ***123 (demo)" in n.card.call_args.args[1]


# ------------------------------------------------------------------ clock + network
def test_clock_drift_resync(tmp_path):
    ps = PS({"stripchart": "Tracking time.windows.com\n12:00:01, +04.1234567s", "resync": "ok"}); n = Mock()
    assert server.check_clock(Journal(str(tmp_path)), n, run=ps) == pytest.approx(4.1234567)
    assert any("w32tm /resync" in c for c in ps.calls) and n.card.call_args.args[0].startswith("AUREON · MT5 · CLOCK DRIFT 4.1 s · resynced")
    ps2 = PS({"stripchart": "12:00:01, +00.4000000s"}); n2 = Mock()
    server.check_clock(Journal(str(tmp_path)), n2, run=ps2)
    assert not n2.card.called and not any("resync" in c for c in ps2.calls)


def test_network_degraded_rules(tmp_path):
    w = server.NetworkWatch(Journal(str(tmp_path)))
    assert w.tick(discord_ok=False, deadman_ok=False, mt5_ok=True) and server.network_degraded()
    assert not w.tick(discord_ok=True, deadman_ok=False, mt5_ok=True)
    assert not w.tick(discord_ok=False, deadman_ok=False, mt5_ok=False)       # MT5 down too: not a network-only problem


def test_network_degraded_blocks_entries_not_guardian(harness):
    from tests.test_v280_flip_giveback import mkdf, step, exits
    fake, ag, _ = harness(price=4200.0)
    server.STATE["network_degraded"] = True
    ag.notify.ask = Mock()
    assert ag._entry_ask("k", "T", ["x"], fields=[]) is False and not ag.notify.ask.called
    ag.cfg.giveback_pct = 50                                                    # the guardian path is unchanged
    fake.price = 4220.0; step(ag, fake, mkdf(4220.0, 4190.0, 4180.0))
    fake.price = 4209.0; step(ag, fake, mkdf(4209.0, 4190.0, 4180.0, t0=300))
    assert fake.closed and exits(ag)[-1]["reason"] == "giveback"


def test_notifier_backlog_summary(monkeypatch):
    n = Notifier("https://hook"); posted = []
    class R:
        def __init__(self, code): self.status_code = code; self.text = ""
    codes = iter([500, 500, 204, 204])
    import requests
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k: (posted.append(json), R(next(codes)))[1])
    n.card("A"); n.card("B")
    assert n.backlog == ["A", "B"] and not n.last_ok
    n.card("C")                                     # v2.11.2: webhook down (2 failures) → no blocking POST, C is missed too
    assert len(posted) == 2 and n.backlog == ["A", "B", "C"]
    n.flush()                                       # main loop, every 30 s: one summary on reconnect
    t = posted[-1]["embeds"][0]
    assert n.backlog == [] and t["title"].startswith("AUREON · MT5 · BACK ONLINE · network · down") and "• A" in t["description"]


# ------------------------------------------------------------------ /server
def test_server_card_all_unknown_when_calls_fail(cfg):
    git = Mock(side_effect=OSError("no git"))
    c = server.server_card(cfg, {}, Journal(cfg.log_dir), run=PS(fail=True), git=git, broker=None)
    vals = {f["name"]: f["value"] for f in c["fields"]}
    for k in ("Uptime", "Last boot", "RAM", "CPU", "Clock drift"):
        assert vals[k] == "unknown", k
    assert "pid unknown · login unknown · tick age unknown" == vals["MT5"] and "`unknown` vs origin/master `unknown`" == vals["Repo"]


def test_server_card_with_values(cfg):
    server.STATE.update(drift=0.31, updates_pending=2, maintenance_window="inside the session (05:30-23:00 IST)",
                        boot={"reason": "Windows Update (1074)", "down_s": 840})
    ps = PS({"FreePhysicalMemory": "5.2 / 16.0 GB free", "LoadPercentage": "7", "Get-Process terminal64": "4242"})
    git = Mock(return_value=types.SimpleNamespace(stdout="b7d3ea8\n"))
    b = types.SimpleNamespace(account=lambda: {"login_masked": "***123", "trade_mode": "demo"})
    ag = types.SimpleNamespace(snapshot={"tick_age": 2})
    c = server.server_card(cfg, {"XAUUSD": ag}, Journal(cfg.log_dir), run=ps, git=git, broker=b, started=time.time() - 7300)
    vals = {f["name"]: f["value"] for f in c["fields"]}
    assert vals["Uptime"] == "2 h 1 min" and vals["MT5"] == "pid 4242 · login ***123 (demo) · tick age XAUUSD 2s"
    assert vals["Clock drift"] == "+0.31 s" and vals["Repo"].endswith("✅") and "2 pending" in vals["Updates"]
    server.STATE.update(drift=None, updates_pending=None, maintenance_window=None, boot=None)


# ------------------------------------------------------------------ evidence-gated tuning
def res(total, losers, gate=None):
    d = {"total_points": total, "trades_detail": [{"points": -1.0}] * losers + [{"points": 2.0}]}
    if gate is not None:
        d["gate_acceptable"] = gate
    return d


def make_tuner(cfg, *, defaults=(10.0, 4), combo=(15.0, 2), gate=None, pins=True, mode="apply", shadow=None):
    cfg.tuning_auto = mode; cfg.tuning_shadow_sessions = 3; cfg.tuning_auto_per_week = 1
    seq = iter(shadow or [])
    def replay(over, days):
        if days == 1 and shadow is not None:
            d, c = next(seq)
            return {"defaults": res(*d), "combo": res(*c)}
        return {"defaults": res(*defaults), "combo": res(*combo, gate=gate)}
    n = Mock()
    return TA.TuningAuto(cfg, Journal(cfg.log_dir), n, replay=replay, pins=lambda: pins), n


def prop(cfg, param="giveback_pct", old=0.0, new=40.0):
    p = tuning.propose(cfg.log_dir, param, old, new, "replay says so")
    assert isinstance(p, dict), p
    return p


@pytest.mark.parametrize("param,old,new,why", [("max_lots", 1.0, 2.0, "never"), ("claude_mode", "advisory", "manage", "never"),
                                               ("exit_losing", False, True, "never"), ("setup_max_dd", 10.0, 14.0, "loosening"),
                                               ("pre_stop", 6.0, 7.0, "loosening")])
def test_never_list(cfg, param, old, new, why):
    assert why in TA.never_reason(param, old, new)


def test_tighter_stop_is_not_never():
    assert TA.never_reason("setup_max_dd", 10.0, 8.0) is None


@pytest.mark.parametrize("kw,why", [({"combo": (9.0, 2)}, "points"), ({"combo": (15.0, 4)}, "stop-outs"), ({"pins": False}, "pins")])
def test_replay_gates_block(cfg, kw, why):
    t, n = make_tuner(cfg, **kw)
    st = t.evaluate(prop(cfg))
    assert st["status"] == "proposal" and why in st["why"]


def test_regime_signoff_gate(cfg):
    t, _ = make_tuner(cfg, gate=False)
    st = t.evaluate(prop(cfg, "regime_choppy", 0.45, 0.5))
    assert st["status"] == "proposal" and "sign-off" in st["why"]


def test_non_registry_param_stays_proposal(cfg):
    t, _ = make_tuner(cfg)
    st = t.evaluate({"id": 9, "param": "secure_at", "old": 10.0, "new": 11.0})
    assert st["status"] == "proposal" and "not a registry feature" in st["why"]


def test_happy_path_shadow_then_apply_and_veto(cfg):
    t, n = make_tuner(cfg, shadow=[((1.0, 1), (2.0, 0))] * 3)
    p = prop(cfg)
    t.tick()
    assert TA.load(cfg.log_dir)["items"][str(p["id"])]["status"] == "shadow" and cfg.giveback_pct == 0.0
    for _ in range(3):
        t.tick(session_ended=True)
    st = TA.load(cfg.log_dir)["items"][str(p["id"])]
    assert st["status"] == "applied" and cfg.giveback_pct == 40.0 and F.load_overrides(cfg.log_dir)["giveback_pct"] == 40.0
    title = [c.args[0] for c in n.card.call_args_list if c.args[0].startswith("STRATEGY CHANGE APPLIED")][0]
    assert title == "STRATEGY CHANGE APPLIED · giveback_pct 0.0→40.0"
    out = t.veto(p["id"], "op")
    assert out.startswith("↩️") and cfg.giveback_pct == 0.0 and "giveback_pct" not in F.load_overrides(cfg.log_dir)


def test_shadow_mode_never_applies(cfg):
    t, n = make_tuner(cfg, mode="shadow", shadow=[((1.0, 1), (2.0, 0))] * 3)
    prop(cfg); t.tick()
    for _ in range(3):
        t.tick(session_ended=True)
    assert cfg.giveback_pct == 0.0 and list(TA.load(cfg.log_dir)["items"].values())[0]["status"] == "shadow_passed"


def test_shadow_disagreement_returns_to_proposal(cfg):
    t, _ = make_tuner(cfg, shadow=[((1.0, 1), (2.0, 0)), ((3.0, 0), (1.0, 1))])
    prop(cfg); t.tick(); t.tick(session_ended=True); t.tick(session_ended=True)
    st = list(TA.load(cfg.log_dir)["items"].values())[0]
    assert st["status"] == "proposal" and "disagreed" in st["why"] and cfg.giveback_pct == 0.0


def test_weekly_cap(cfg):
    t, _ = make_tuner(cfg, shadow=[((1.0, 1), (2.0, 0))] * 6)
    prop(cfg, "giveback_pct", 0.0, 40.0); prop(cfg, "giveback_arm", 10.0, 12.0)
    t.tick()
    for _ in range(3):
        t.tick(session_ended=True)
    items = TA.load(cfg.log_dir)["items"].values()
    assert sum(1 for s in items if s["status"] == "applied") == 1 and any("weekly cap" in s["why"] for s in items)


def test_off_does_nothing(cfg):
    t, _ = make_tuner(cfg, mode="off")
    prop(cfg)
    assert t.tick() == [] and TA.load(cfg.log_dir)["items"] == {}
