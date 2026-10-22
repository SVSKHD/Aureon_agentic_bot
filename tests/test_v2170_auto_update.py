"""v2.17.0 commit 3 — auto-update on merge: flat → apply, in a position → pending then applied when flat, suite red → held
(not retried until the next merge), hold / resume, the poll interval, local edits → patch (real git), /update admin."""
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from aureon_mt5 import auto_update as AU, broker
from aureon_mt5.bot import update_action
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal

A, B, C = "a" * 40, "b" * 40, "c" * 40


class Note:
    def __init__(self):
        self.cards = []

    def card(self, title, body="", **kw):
        self.cards.append((title, body))


class FakeSh:
    """git / pytest stand-in: HEAD, origin/master, the merge commit text and the suite result are set by the test."""
    def __init__(self, head=A, origin=B, green=True, out="1300 passed in 60s"):
        self.head, self.origin, self.green, self.out, self.calls = head, origin, green, out, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return ok(self.head if cmd[2] == "HEAD" else self.origin)
        if cmd[:2] == ["git", "log"]:
            if "%H %ct" in cmd[3]:
                return ok(f"{self.head} 1792400000")
            return ok("Merge pull request #33 from SVSKHD/v2.17.0\nv2.17.0: auto-trade, decide-timeframe, auto-update, pending entry")
        if cmd[:2] == ["git", "reset"]:
            self.head = cmd[-1]; return ok()
        if "pytest" in cmd:
            return SimpleNamespace(returncode=0 if self.green else 1, stdout=self.out, stderr="")
        return ok()


def rig(tmp_path, sh=None, flat=True, now=None):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.auto_update = True; cfg.update_poll_min = 2.0
    n = Note(); j = Journal(str(tmp_path)); sh = sh or FakeSh(); box = {"flat": flat, "restarts": 0, "t": now or 1_792_400_000.0}

    def update():
        old = sh.head; sh.head = sh.origin
        return {"status": "updated", "old": old, "new": sh.origin, "discarded": [], "patch": "", "error": ""}

    up = AU.AutoUpdater(cfg, j, n, {}, sh=sh, root=str(tmp_path), clock=lambda: box["t"],
                        flat=lambda: (box["flat"], "flat" if box["flat"] else "XAUUSD: a position is open"),
                        restart_fn=lambda: box.__setitem__("restarts", box["restarts"] + 1), update=update)
    return up, sh, n, j, box


def test_flat_applies_and_restarts(tmp_path):
    up, sh, n, j, box = rig(tmp_path)
    assert up.tick() == "applied" and box["restarts"] == 1
    assert n.cards[-1][0] == "UPDATE APPLIED · bbbbbbb · v2.17.0: auto-trade, decide-timeframe, auto-update, pending entry"
    assert any("pytest" in c for c in sh.calls)
    row = [r for r in j.read(0) if r["event"] == "update_applied"][-1]
    assert row["sha"] == B and row["old"] == A
    assert up.state["last_sha"] == B and "auto-update on · last bbbbbbb" in up.boot_line()


def test_in_position_pending_then_applies_when_flat(tmp_path):
    up, sh, n, j, box = rig(tmp_path, flat=False)
    assert up.tick() == "pending" and box["restarts"] == 0
    assert n.cards == [("UPDATE PENDING · will apply when flat",
                        "origin/master `bbbbbbbb` · v2.17.0: auto-trade, decide-timeframe, auto-update, pending entry\n"
                        "XAUUSD: a position is open")]
    assert up.tick() is None and len(n.cards) == 1                            # still in the trade: no second card, nothing applied
    assert not any("pytest" in c for c in sh.calls)
    box["flat"] = True                                                       # the guardian closed it
    assert up.tick() == "applied" and box["restarts"] == 1 and n.cards[-1][0].startswith("UPDATE APPLIED · bbbbbbb")
    assert [r for r in j.read(0) if r["event"] == "update_applied"][-1]["why"] == "flat again"


def test_suite_red_is_held_and_not_retried_until_the_next_merge(tmp_path):
    sh = FakeSh(green=False, out="....F\nFAILED tests/test_x.py::test_y - assert 1 == 2\n1 failed, 4 passed")
    up, sh, n, j, box = rig(tmp_path, sh=sh)
    assert up.tick() == "held" and box["restarts"] == 0
    assert n.cards[-1][0] == "UPDATE HELD · tests failed · FAILED tests/test_x.py::test_y - assert 1 == 2"
    assert ["git", "reset", "--hard", A] in sh.calls and sh.head == A          # back on the running build
    box["t"] += 300
    assert up.tick().startswith("held") and box["restarts"] == 0             # the same sha: not retried
    sh.origin = C; sh.green = True; box["t"] += 300                          # the next merge
    assert up.tick() == "applied" and box["restarts"] == 1


def test_hold_resume_and_forced_check(tmp_path):
    up, sh, n, j, box = rig(tmp_path)
    assert up.command("hold", "koushik").startswith("auto-update **on hold**")
    assert up.tick() is None and box["restarts"] == 0 and "HOLD" in up.boot_line()
    assert up.command("resume", "koushik").startswith("auto-update **resumed**")
    box["t"] += 1; up.state["last_check"] = box["t"]                          # resumed but polled just now
    assert up.tick() is None
    assert up.command("", "koushik") == "UPDATE APPLIED — restarting" and box["restarts"] == 1


def test_poll_interval_and_off_switch(tmp_path):
    up, sh, n, j, box = rig(tmp_path)
    sh.origin = A                                                            # master did not move
    assert up.tick() == "current"
    box["t"] += 60; assert up.tick() is None                                 # < 2 min
    box["t"] += 61; assert up.tick() == "current"
    up.cfg.auto_update = False; box["t"] += 600
    assert up.tick() is None and "auto-update off (AUREON_AUTO_UPDATE=0)" in up.boot_line()


def test_pending_order_is_not_flat(tmp_path, monkeypatch):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = False; cfg.paper = False
    ag = SimpleNamespace(snapshot={"at": time.time(), "positions": [], "connected": True})
    up = AU.AutoUpdater(cfg, Journal(str(tmp_path)), Note(), {"XAUUSD": ag})
    monkeypatch.setattr(broker, "pending_orders", lambda s: [{"ticket": 55}])
    assert up._flat() == (False, "XAUUSD: a pending order is open (#55)")
    monkeypatch.setattr(broker, "pending_orders", lambda s: None)
    assert up._flat()[0] is False
    monkeypatch.setattr(broker, "pending_orders", lambda s: [])
    assert up._flat() == (True, "flat")
    ag.snapshot["positions"] = [{"ticket": 1}]
    assert up._flat()[0] is False


def test_restart_exit75_when_supervised_else_reexec():
    got = []
    AU.restart({}, exit_hook=lambda code: got.append(("exit", code)), environ={"AUREON_SUPERVISED": "1"})
    AU.restart({}, execv=lambda exe, args: got.append(("execv", args[1:])), environ={}, argv=["main.py", "--dry"])
    assert got == [("exit", 75), ("execv", ["main.py", "--dry"])]
    from aureon_mt5 import ops
    ops.STOP.clear()


def test_pr_title_and_first_failure():
    assert AU.pr_title("Merge pull request #31 from SVSKHD/v2.16.2-resilience", "resilience: x\n\nmore") == "resilience: x"
    assert AU.pr_title("v2.16.0: direct commit", "") == "v2.16.0: direct commit"
    assert AU.first_failure("ERROR tests/a.py - ImportError: x\nFAILED b") == "ERROR tests/a.py - ImportError: x"


def test_update_command_is_admin_only(tmp_path):
    up, *_ = rig(tmp_path)
    nobody = SimpleNamespace(roles=[])
    assert update_action(up.cfg, nobody, "hold", admin_role="42", updater=up) == "Denied: configured administrator role required"
    admin = SimpleNamespace(roles=[SimpleNamespace(id=42)])
    assert update_action(up.cfg, admin, "hold", admin_role="42", updater=up).startswith("auto-update **on hold**")


def test_maintenance_window_uses_the_updater(tmp_path):
    from aureon_mt5 import server as srv
    cfg = Config(); cfg.log_dir = str(tmp_path)
    m = srv.Maintenance(cfg, Journal(str(tmp_path)), Note(), {}, positions_block=lambda a: False)
    m.updater = SimpleNamespace(check=lambda **kw: f"checked ({kw.get('why')})")
    assert m.update_code() == "checked (maintenance window)"


# ----------------------------------------------------------------------------- real git: local edits → patch, then discarded
def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_local_edits_saved_as_a_patch_and_env_untouched(tmp_path):
    origin = tmp_path / "origin.git"; work = tmp_path / "work"; seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(seed, "config", k, v)
    (seed / ".gitignore").write_text(".env\nlogs/\n"); (seed / "app.py").write_text("v = 1\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "one"); _git(seed, "push", "-q", "origin", "HEAD:master")
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (seed / "app.py").write_text("v = 2\n"); _git(seed, "commit", "-qam", "two"); _git(seed, "push", "-q", "origin", "HEAD:master")
    new = _git(seed, "rev-parse", "HEAD")
    (work / "app.py").write_text("v = 1  # hand edit on the server\n"); (work / ".env").write_text("AUREON_MODE=ema921\n")
    logs = work / "logs"
    cfg = Config(); cfg.log_dir = str(logs); cfg.auto_update = True
    restarts = []

    def sh(cmd, **kw):
        if "pytest" in cmd:
            return SimpleNamespace(returncode=0, stdout="1 passed", stderr="")
        return subprocess.run(cmd, **kw)

    up = AU.AutoUpdater(cfg, Journal(str(logs)), Note(), {}, sh=sh, root=str(work), flat=lambda: (True, "flat"),
                        restart_fn=lambda: restarts.append(1))
    assert up.check() == "applied" and restarts == [1]
    assert _git(work, "rev-parse", "HEAD") == new and (work / "app.py").read_text() == "v = 2\n"
    patches = [p for p in os.listdir(logs) if p.startswith("local_changes_") and p.endswith(".patch")]
    assert len(patches) == 1 and "hand edit on the server" in (logs / patches[0]).read_text()
    assert (work / ".env").read_text() == "AUREON_MODE=ema921\n"
