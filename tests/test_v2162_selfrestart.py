"""v2.16.2 fix 1 — self-restart without a supervisor: in-process Discord rebuilds, exit 75 only when supervised."""
import asyncio
import os

from aureon_mt5 import netguard, selfheal as H

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Mon:
    """The LoopMonitor surface HealthGuard reads."""
    def __init__(self, clock):
        self.clock = clock; self.last_beat = clock(); self.ever_connected = True; self.disconnected_since = None

    def current_lag(self, now=None):
        return 0.0

    def gateway_down_for(self, now=None):
        return 0.0 if self.disconnected_since is None else (now if now is not None else self.clock()) - self.disconnected_since

    def gateway_state(self, now=None):
        return "up" if self.disconnected_since is None else "DOWN"


class Note:
    def __init__(self):
        self.cards = []

    def card(self, title, body="", **kw):
        self.cards.append((title, body))


class J:
    def __init__(self):
        self.rows = []

    def log(self, ev, **kw):
        self.rows.append((ev, kw))


def guard(tmp_path, supervised, rebuilds, exits):
    c = Clock(); m = Mon(c); n = Note(); j = J()
    hg = H.HealthGuard(monitor=m, agents={}, log_dir=str(tmp_path), journal=j, notify=n, exit_hook=exits.append, clock=c,
                       wall=c, bot_expected=True, is_supervised=supervised, rebuild_hook=lambda: rebuilds.append(c.t))
    return hg, m, c, n, j


def test_mode_from_env():
    assert H.self_restart_mode({"AUREON_SUPERVISED": "1"}) == "exit75"
    assert H.self_restart_mode({}) == "in-process" and H.self_restart_mode({"AUREON_SUPERVISED": "0"}) == "in-process"


def test_unsupervised_gateway_loss_rebuilds_in_process_with_backoff(tmp_path):
    rebuilds, exits = [], []
    hg, m, c, n, j = guard(tmp_path, False, rebuilds, exits)
    m.disconnected_since = c.t
    for _ in range(300 // 5):                                              # 0 … 300 s: nothing yet
        c.t += 5; hg.tick(c.t)
    assert not rebuilds and not exits
    for _ in range(int(400 // 1)):                                         # 300 … 700 s, ticking every second
        c.t += 1; hg.tick(c.t)
    gaps = [b - a for a, b in zip(rebuilds, rebuilds[1:])]
    assert rebuilds[0] - 1000 > 300 and gaps[:3] == [5.0, 30.0, 120.0] and set(gaps[3:]) <= {120.0}
    assert not exits and not hg.restarting                                  # never exits, the MT5 loop is untouched
    assert [r for r in j.rows if r[0] == "gateway_rebuild"][0][1]["attempt"] == 1
    m.disconnected_since = None; c.t += 1; hg.tick(c.t)                     # the gateway returns
    title, body = n.cards[-1]
    assert title == "AUREON · MT5 · BACK ONLINE" and "in-process rebuild" in body and "self_restart_mode=in-process" in body
    assert [r for r in j.rows if r[0] == "gateway_rebuilt"][0][1]["rebuilds"] == len(rebuilds)
    m.disconnected_since = c.t                                             # a new outage starts the schedule again
    c.t += 301; hg.tick(c.t)
    assert len(rebuilds) == len(gaps) + 2


def test_supervised_gateway_loss_still_exits_75(tmp_path):
    rebuilds, exits = [], []
    hg, m, c, n, j = guard(tmp_path, True, rebuilds, exits)
    m.disconnected_since = c.t
    c.t += 200; hg.tick(c.t)
    assert not exits
    c.t += 101; hg.tick(c.t)
    assert exits == [75] and not rebuilds and any("supervised, restarting" in r[1]["reason"] for r in j.rows if r[0] == "self_restart")


def test_supervise_client_schedule():
    starts = []

    async def start():
        starts.append(1)
        raise ConnectionError("gateway gone")
    slept = []

    async def sleep(s):
        slept.append(s)
    stop = lambda: len(starts) >= 6
    asyncio.run(netguard.supervise_client(start, stop_requested=stop, sleep=sleep, clock=lambda: 0.0, schedule=(5.0, 30.0, 120.0)))
    assert slept == [5.0, 30.0, 120.0, 120.0, 120.0]


def test_wiring():
    bot = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    assert "monitor.rebuild = rebuild" in bot and "schedule=sched" in bot
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "self_restart_mode={_selfheal_mode()}" in main and "self_restart_mode=_selfheal_mode()" in main
    assert 'AUREON_SUPERVISED = "1"' in open(os.path.join(ROOT, "scripts", "aureon_service.ps1"), encoding="utf-8").read()
