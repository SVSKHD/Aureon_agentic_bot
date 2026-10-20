"""v2.15.0 restart pre-flight (aureon_mt5/preflight.py)."""
import json
import os
from types import SimpleNamespace

from aureon_mt5 import preflight as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Broker:
    """connect() / account() answer from scripted lists (last value repeats)."""

    def __init__(self, connects, accounts):
        self.connects, self.accounts, self.resets = list(connects), list(accounts), 0

    def _next(self, xs):
        return xs.pop(0) if len(xs) > 1 else xs[0]

    def reset(self):
        self.resets += 1

    def connect(self):
        return self._next(self.connects)

    def account(self):
        return self._next(self.accounts)

    def mt5(self):
        return SimpleNamespace(version=lambda: (500, 4755, "10 Oct 2026"))


ACCT = {"login_masked": "***123", "trade_mode": "demo", "balance": 1.0}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Note:
    def __init__(self):
        self.cards = []

    def card(self, title, body, **kw):
        self.cards.append((title, body))


def cfg(tmp_path, **kw):
    return SimpleNamespace(log_dir=str(tmp_path), mt5_path=kw.get("mt5_path", ""), mt5_boot_wait=kw.get("boot_wait", 300))


# ----------------------------------------------------------------------------- terminal
def test_terminal_running_is_left_alone(tmp_path):
    calls = []
    run = lambda cmd, t=5: calls.append(cmd) or (True, "1")
    assert P.ensure_terminal(cfg(tmp_path), run=run, windows=True) == "running" and len(calls) == 1


def test_terminal_started_from_mt5_path(tmp_path):
    calls = []
    run = lambda cmd, t=5: calls.append(cmd) or (True, "0")
    out = P.ensure_terminal(cfg(tmp_path, mt5_path=r"C:\MT5\terminal64.exe"), run=run, windows=True, exists=lambda p: True)
    assert out.startswith("started") and "Start-Process -FilePath 'C:\\MT5\\terminal64.exe'" in calls[-1]


def test_terminal_cached_path_is_the_fallback(tmp_path):
    (tmp_path / "mt5_path.txt").write_text(r"D:\Broker\terminal64.exe")
    calls = []
    run = lambda cmd, t=5: calls.append(cmd) or (True, "0")
    assert P.ensure_terminal(cfg(tmp_path), run=run, windows=True, exists=lambda p: True).startswith("started")
    assert "D:\\Broker\\terminal64.exe" in calls[-1]


def test_terminal_without_path_skips_with_warning(tmp_path):
    run = lambda cmd, t=5: (True, "0")
    assert P.ensure_terminal(cfg(tmp_path), run=run, windows=True).startswith("skipped: terminal64.exe not running")
    assert P.ensure_terminal(cfg(tmp_path), run=run, windows=False) == "skipped: not Windows"


# ----------------------------------------------------------------------------- MT5 + login
def test_waits_for_login_not_just_initialize(tmp_path):
    c = Clock()
    b = Broker([False, True], [None, None, ACCT])            # init fails, then ok but logged out twice, then the account
    acct, waited, why = P.wait_mt5(b, log_dir=str(tmp_path), sleep=c.sleep, clock=c)
    assert acct == ACCT and waited == 15.0 and b.resets == 4
    assert os.path.exists(tmp_path / "heartbeat.txt")       # the supervisor's hang check sees us alive


def test_gives_up_after_90_s_every_5_s(tmp_path):
    c = Clock(); sleeps = []
    b = Broker([True], [None])
    acct, waited, why = P.wait_mt5(b, log_dir=str(tmp_path), sleep=lambda s: (sleeps.append(s), c.sleep(s)), clock=c)
    assert acct is None and waited == 90.0 and set(sleeps) == {5.0} and len(sleeps) == 18
    assert "not logged in" in why


def test_heartbeat_touch_keeps_the_last_real_line(tmp_path):
    hb = tmp_path / "heartbeat.txt"
    hb.write_text("2026-10-17T21:00:00 v2.14.0 lag=0.1s gateway=up health=ok\n")
    os.utime(hb, (1000, 1000))
    P.touch_heartbeat(str(tmp_path))
    assert hb.read_text().startswith("2026-10-17T21:00:00") and os.path.getmtime(hb) > 1000


# ----------------------------------------------------------------------------- across restarts
def test_not_up_card_once_across_restarts_then_back_online(tmp_path):
    n = Note(); wall = [10_000.0]
    c_ = cfg(tmp_path)
    for _ in range(4):                                      # 4 starts, ~100 s apart, MT5 never answers
        c = Clock()
        r = P.run(c_, Broker([False], [None]), n, run_ps=lambda *a: (True, "1"), windows=True, sleep=c.sleep, clock=c,
                  wall=lambda: wall[0])
        assert not r["ok"]
        wall[0] += 100
    st = json.load(open(tmp_path / "preflight.json"))
    assert st["attempts"] == 4 and st["carded"] and [t for t, _ in n.cards] == ["AUREON · MT5 · MT5 NOT UP"]
    c = Clock()
    r = P.run(c_, Broker([True], [ACCT]), n, run_ps=lambda *a: (True, "1"), windows=True, sleep=c.sleep, clock=c,
              wall=lambda: wall[0])
    assert r["ok"] and r["down_s"] == 400 and r["build"] == "500 build 4755"
    assert json.load(open(tmp_path / "preflight.json")) == {}


def test_back_online_card(tmp_path):
    n = Note()
    res = {"ok": True, "terminal": "started C:\\MT5\\terminal64.exe", "account": ACCT, "waited": 15.0, "down_s": 360.0,
           "build": "500 build 4755"}
    text = P.back_online(n, res, {"XAUUSD": [{"ticket": 77, "direction": "long"}], "XAGUSD": []}, closed=True)
    assert n.cards[0][0] == "AUREON · MT5 · BACK ONLINE after restart"
    assert "MT5 500 build 4755 · account ***123 (demo)" in text and "open positions re-adopted: 1 — #77 XAUUSD LONG" in text
    assert "outage 6 min" in text and "market closed — agents sleeping until the next open" in text


def test_steps_logged_to_aureon_log(tmp_path, monkeypatch):
    from aureon_mt5 import telemetry
    lines = []
    monkeypatch.setattr(telemetry, "info", lines.append)            # telemetry.info → logs/aureon.log
    c = Clock()
    P.run(cfg(tmp_path), Broker([False, True], [ACCT]), None, run_ps=lambda *a: (True, "1"), windows=True, sleep=c.sleep, clock=c)
    assert lines[0] == "preflight: start" and "preflight: terminal: running" in lines
    assert any(x.startswith("preflight: attempt 1: MetaTrader5.initialize() failed") for x in lines)
    assert any(x.startswith("preflight: MT5 up after 5 s · 500 build 4755 · account ***123 (demo)") for x in lines)


# ----------------------------------------------------------------------------- wiring
def test_main_runs_preflight_before_agents():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = src.index("preflight.run(cfg, broker, health_notify)")
    assert i < src.index("def start_agents():") and "sys.exit(_o.EXIT_RESTART)" in src[i:i + 400]
    assert "prev_heartbeat = _server.last_heartbeat(cfg.log_dir)" in src[:i] and "heartbeat=prev_heartbeat" in src
    j = src.index("_pf.back_online(")
    assert src.rindex("start_agents()", 0, j) > i


def test_logon_task_script():
    ps = open(os.path.join(ROOT, "scripts", "install_logon_tasks.ps1"), encoding="utf-8").read()
    assert "Aureon-MT5" in ps and '"Aureon"' in ps and "AtLogOn" in ps and "PT30S" in ps
    assert "RestartInterval (New-TimeSpan -Minutes 1)" in ps and "Autologon" in ps and "standby-timeout-ac 0" in ps
    assert "& $nssm" not in ps and "nssm install" not in ps.lower() and "New-Service" not in ps   # interactive session only


def test_ask_ttl_reads_env(monkeypatch):
    """AUREON_ASK_TTL_MIN was documented but hard-coded to 20; the expiry paths are wall-clock minutes (TF-independent)."""
    import importlib
    from aureon_mt5 import config as C
    monkeypatch.setenv("AUREON_ASK_TTL_MIN", "30")
    try:
        assert importlib.reload(C).Config().ask_ttl_min == 30
    finally:
        monkeypatch.delenv("AUREON_ASK_TTL_MIN")
        importlib.reload(C)
    assert C.Config().ask_ttl_min == 20
