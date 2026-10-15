"""v2.7.2 — self-heal, dead-man's switch, heartbeat.txt, stall dumps, exit-75 restarts. No MT5 / Discord needed."""
import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aureon_mt5 import ops, selfheal
from aureon_mt5.common.loop_health import LoopMonitor
from aureon_mt5.selfheal import EXIT_RESTART, HealthGuard
from aureon_mt5.config import VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


def _agent(alive=True, at=None, market_open=True, bar_wall=None):
    return SimpleNamespace(is_alive=lambda: alive, snapshot={"at": at, "market_open": market_open}, last_bar_wall=bar_wall)


def _setup(tmp_path, **kw):
    mono, wall = Clock(1000.0), Clock(1_700_000_000.0)
    mon = LoopMonitor(Mock(), clock=mono)
    mon.last_beat = mono.t; mon.gateway_up("ready")
    agents = {"XAUUSD": _agent(at=wall.t, bar_wall=wall.t)}
    ping, exit_hook = Mock(return_value=True), Mock()
    g = HealthGuard(monitor=mon, agents=agents, log_dir=str(tmp_path), journal=Mock(), notify=Mock(),
                    deadman_url="https://hc.example/ping", ping=ping, exit_hook=exit_hook, clock=mono, wall=wall, **kw)
    return g, mon, agents, ping, exit_hook, mono, wall


def _advance(mon, mono, wall, s, beat=True):
    mono.t += s; wall.t += s
    if beat:
        mon.last_beat = mono.t; mon.lag = 0.0


# ------------------------------------------------------------------ dead-man's switch
def test_deadman_pings_only_when_all_conditions_hold_every_60s(tmp_path):
    g, mon, agents, ping, _, mono, wall = _setup(tmp_path)
    assert g.failures() == []
    g.tick(); assert ping.call_count == 1
    _advance(mon, mono, wall, 30); agents["XAUUSD"].snapshot["at"] = wall.t; g.tick()
    assert ping.call_count == 1                                   # not yet 60 s
    _advance(mon, mono, wall, 31); agents["XAUUSD"].snapshot["at"] = wall.t; g.tick()
    assert ping.call_count == 2


@pytest.mark.parametrize("breaker", ["lag", "gateway", "agent_dead", "stale_bar", "agent_frozen"])
def test_deadman_stops_on_each_failed_condition(tmp_path, breaker):
    g, mon, agents, ping, _, mono, wall = _setup(tmp_path)
    g.tick(); assert ping.call_count == 1
    _advance(mon, mono, wall, 61)
    ag = agents["XAUUSD"]; ag.snapshot["at"] = wall.t
    if breaker == "lag":
        mon.last_beat = mono.t - 7                                # ticker 6 s overdue → lag ≥ 5 s
    elif breaker == "gateway":
        mon.gateway_down()
    elif breaker == "agent_dead":
        ag.is_alive = lambda: False
    elif breaker == "stale_bar":
        ag.last_bar_wall = wall.t - 20 * 60                       # market open, no bar for 20 min
    elif breaker == "agent_frozen":
        ag.snapshot["at"] = wall.t - 400                          # agent stuck (e.g. MT5 call) for > 5 min
    g.tick()
    assert ping.call_count == 1 and g.last_failures


def test_stale_bar_ignored_while_market_closed(tmp_path):
    g, mon, agents, ping, *_ = _setup(tmp_path)
    agents["XAUUSD"].snapshot["market_open"] = False
    agents["XAUUSD"].last_bar_wall = g.wall() - 3 * 86400       # weekend
    assert g.failures() == []


def test_no_bot_means_loop_and_gateway_are_not_conditions(tmp_path):
    wall = Clock(1_700_000_000.0)
    ping = Mock(return_value=True)
    g = HealthGuard(monitor=None, agents={"XAUUSD": _agent(at=wall.t, bar_wall=wall.t)}, log_dir=str(tmp_path),
                    deadman_url="u", ping=ping, clock=Clock(5.0), wall=wall, bot_expected=False)
    g.tick(); assert ping.call_count == 1


def test_gateway_never_connected_is_unhealthy(tmp_path):
    mono = Clock(0.0)
    mon = LoopMonitor(Mock(), clock=mono); mon.last_beat = 0.0
    g = HealthGuard(monitor=mon, agents={}, log_dir=str(tmp_path), deadman_url="u", ping=Mock(), clock=mono, wall=Clock(1.0))
    assert "gateway disconnected" in g.failures()


# ------------------------------------------------------------------ self-restart
def test_self_restart_on_loop_lag_over_30s_calls_exit_hook_once(tmp_path):
    g, mon, agents, ping, exit_hook, mono, wall = _setup(tmp_path)
    _advance(mon, mono, wall, 25, beat=False); g.tick()
    assert not exit_hook.called                                   # 24 s lag: below threshold
    _advance(mon, mono, wall, 10, beat=False); g.tick(); g.tick()
    exit_hook.assert_called_once_with(EXIT_RESTART)
    assert g.journal.log.call_args.args[0] == "self_restart" and "loop lag" in g.journal.log.call_args.kwargs["reason"]
    assert "frozen" in g.notify.card.call_args.args[0]
    assert ops.pop_restart_marker(str(tmp_path))["kind"] == "self-heal"


def test_gateway_down_never_restarts_the_process(tmp_path):
    """v2.11.2 (replaces the v2.7.2 'self-restart after 5 min gateway down'): on 2026-10-15 a DNS loss turned that rule
    into exit 75 with no service to bring the bot back. A gateway that stays down is network state, never an exit."""
    g, mon, agents, ping, exit_hook, mono, wall = _setup(tmp_path)
    mon.gateway_down()
    for _ in range(12):
        _advance(mon, mono, wall, 300); g.tick()
    assert not exit_hook.called and "gateway disconnected" in g.last_failures


def test_gateway_flap_does_not_restart(tmp_path):
    g, mon, agents, ping, exit_hook, mono, wall = _setup(tmp_path)
    for _ in range(10):
        mon.gateway_down(); _advance(mon, mono, wall, 120); mon.gateway_up(); g.tick()
    assert not exit_hook.called and mon.reconnects_since(4 * 3600) == 10


# ------------------------------------------------------------------ heartbeat.txt
def test_heartbeat_written_every_30s(tmp_path):
    g, mon, agents, ping, _, mono, wall = _setup(tmp_path)
    g.tick()
    path = tmp_path / "heartbeat.txt"
    first = path.read_text(encoding="utf-8")
    assert f"v{VERSION}" in first and "lag=0.0s" in first and "gateway=up" in first and "health=ok" in first
    _advance(mon, mono, wall, 10); agents["XAUUSD"].is_alive = lambda: False; g.tick()
    assert path.read_text(encoding="utf-8") == first              # < 30 s: not rewritten
    _advance(mon, mono, wall, 25); g.tick()
    assert "FAIL: XAUUSD agent dead" in path.read_text(encoding="utf-8")
    assert selfheal.read_heartbeat(str(tmp_path)).startswith(first.strip()[:4])


# ------------------------------------------------------------------ stall dump
def test_stall_dump_created_once_per_stall(tmp_path):
    g, mon, agents, ping, _, mono, wall = _setup(tmp_path)
    _advance(mon, mono, wall, 5, beat=False); g.tick()            # 4 s overdue > 3 s
    _advance(mon, mono, wall, 1, beat=False); g.tick()
    dumps = list(tmp_path.glob("stall-*.txt"))
    assert len(dumps) == 1 and len(g.stall_dumps) == 1
    text = dumps[0].read_text(encoding="utf-8")
    assert "MainThread" in text and "event loop lag" in text
    _advance(mon, mono, wall, 1); g.tick()                        # recovered
    _advance(mon, mono, wall, 5, beat=False); wall.t += 1; g.tick()
    assert len(g.stall_dumps) == 2


def test_dump_stacks_names_a_blocked_thread(tmp_path):
    ev = threading.Event()
    def blocked_in_mt5_call(): ev.wait(5)
    t = threading.Thread(target=blocked_in_mt5_call, name="agent-XAUUSD"); t.start()
    try:
        text = open(selfheal.dump_stacks(str(tmp_path / "s.txt"), "test"), encoding="utf-8").read()
    finally:
        ev.set(); t.join()
    assert "agent-XAUUSD" in text and "blocked_in_mt5_call" in text


# ------------------------------------------------------------------ gateway logging / heartbeat card
def test_gateway_disconnect_resume_logged_with_duration(monkeypatch):
    logged = []
    from aureon_mt5 import telemetry
    monkeypatch.setattr(telemetry, "info", logged.append)
    mono = Clock(0.0)
    mon = LoopMonitor(Mock(), clock=mono)
    mon.gateway_up("ready"); mon.gateway_down(); mono.t = 42.5; mon.gateway_up("resumed")
    assert any("DOWN" in m for m in logged) and any("after 42.5 s down" in m for m in logged)
    assert mon.reconnects_since(4 * 3600) == 1 and "reconnects 4 h: 1" in mon.summary()


def test_heartbeat_card_shows_gateway_reconnects(tmp_path):
    from aureon_mt5.watchdog import Watchdog
    cfg = SimpleNamespace(log_dir=str(tmp_path), watchdog=True, heartbeat_hours=4, heartbeat_stale_min=15, dry=True,
                          server_utc_offset=3)
    notify = Mock()
    broker = SimpleNamespace(connected=lambda: True, market_open=lambda *a: True)
    wd = Watchdog(notify, Mock(), cfg, {}, clock=lambda: 1_700_000_000.0, broker=broker)
    mono = Clock(0.0); mon = LoopMonitor(Mock(), clock=mono); mon.gateway_up("ready")
    for _ in range(3):
        mon.gateway_down(); mono.t += 5; mon.gateway_up()
    wd.monitor = mon
    wd.tick()
    lines = notify.send.call_args.args[2]
    assert any("reconnects last 4 h: 3" in l for l in lines)


# ------------------------------------------------------------------ /restart = exit 75, marker survives the process
def test_restart_process_exits_75_with_marker(tmp_path):
    ag = SimpleNamespace(stop=Mock(), join=Mock())
    hook = Mock()
    assert ops.restart_process({"XAUUSD": ag}, Mock(), Mock(), "op", "why", argv=["main.py"], exit_hook=hook,
                               log_dir=str(tmp_path), update=lambda: {"status": "current", "discarded": []})
    hook.assert_called_once_with(75)
    m = ops.pop_restart_marker(str(tmp_path))
    assert m["kind"] == "manual" and m["reason"] == "why"
    assert ops.pop_restart_marker(str(tmp_path)) is None          # consumed


def test_mode_restart_refused_when_cli_pins_mode(tmp_path):
    ag = SimpleNamespace(stop=Mock(), join=Mock())
    hook, notify = Mock(), Mock()
    out = ops.restart_process({"XAUUSD": ag}, Mock(), notify, "op", "m", mode="ema2050", argv=["main.py", "--mode", "ema5080"],
                              exit_hook=hook, log_dir=str(tmp_path), update=lambda: {"status": "current", "discarded": []})
    assert out is False and not hook.called and not ag.stop.called
    assert "RESTART ABORTED" in notify.card.call_args.args[0]


def test_hard_exit_stops_agents_then_exits_with_code():
    ag = SimpleNamespace(stop=Mock(), join=Mock())
    ex = Mock()
    ops.hard_exit(75, {"XAUUSD": ag}, join_timeout=0.1, _exit=ex)
    ag.stop.assert_called_once(); ag.join.assert_called_once_with(timeout=0.1); ex.assert_called_once_with(75)


def test_restart_exits_real_process_with_75(tmp_path):
    import subprocess
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from aureon_mt5 import ops\n"
            "ops.restart_process({}, type('J', (), {'log': lambda *a, **k: None})(), type('N', (), {'card': lambda *a, **k: None})(),"
            " 'op', 'x', argv=['main.py'], log_dir=%r, update=lambda: {'status': 'current', 'discarded': []})\n"
            "print('not reached')\n") % (ROOT, str(tmp_path))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "not reached" not in r.stdout


# ------------------------------------------------------------------ off-loop pool / no blocking on the loop
def test_off_loop_uses_dedicated_16_worker_pool():
    from aureon_mt5 import bot
    names = []
    async def go():
        await bot.off_loop(lambda: names.append(threading.current_thread().name))
    asyncio.run(go())
    assert names[0].startswith("aureon-offloop") and bot.off_loop_pool()._max_workers == 16


def test_stuck_calls_do_not_starve_commands():
    from aureon_mt5 import bot
    ev = threading.Event()
    async def go():
        stuck = [asyncio.ensure_future(bot.off_loop(ev.wait, 5, timeout=0.2)) for _ in range(8)]
        await asyncio.gather(*stuck, return_exceptions=True)          # 8 threads still blocked in "MT5"
        t0 = time.monotonic()
        assert await bot.off_loop(lambda: "ok", timeout=2) == "ok"
        return time.monotonic() - t0
    try:
        assert asyncio.run(go()) < 1.0
    finally:
        ev.set()


def test_bot_has_no_asyncio_to_thread_left():
    src = open(os.path.join(ROOT, "aureon_mt5", "bot.py"), encoding="utf-8").read()
    assert "await asyncio.to_thread(" not in src


def test_command_failure_report_runs_off_the_loop(monkeypatch):
    """telemetry.failure posts a webhook; it must not run on the event loop thread."""
    from aureon_mt5 import bot, telemetry
    seen = {}
    def failure(*a, **k): seen["thread"] = threading.current_thread().name
    monkeypatch.setattr(telemetry, "failure", failure)

    class Inter:
        def __init__(self):
            async def defer(**k): pass
            async def send(**k): pass
            self.response = SimpleNamespace(defer=defer); self.followup = SimpleNamespace(send=send)
    def boom(): raise RuntimeError("x")
    async def go():
        await bot.respond(Inter(), "t", boom, notify=Mock(), to_kwargs=lambda r: {})
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert seen["thread"].startswith("aureon-offloop")


# ------------------------------------------------------------------ fallback script
def test_deadman_fallback_alerts_once_and_recovers(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import deadman_fallback as fb
    finally:
        sys.path.pop(0)
    sent = []
    send = lambda t: sent.append(t) or True
    hb = tmp_path / "heartbeat.txt"; hb.write_text("2026 v2.7.2 lag=0.0s", encoding="utf-8")
    now = os.path.getmtime(hb)
    assert fb.check(str(tmp_path), send, now=now + 60) == "ok"
    assert fb.check(str(tmp_path), send, now=now + 400) == "down"
    assert fb.check(str(tmp_path), send, now=now + 700) == "still-down"
    assert len(sent) == 1 and "Aureon down" in sent[0]
    os.utime(hb, (now + 800, now + 800))
    assert fb.check(str(tmp_path), send, now=now + 810) == "back" and "back" in sent[-1]
    (tmp_path / "heartbeat.txt").unlink()
    assert fb.check(str(tmp_path), send, now=now + 900) == "down"


def test_service_script_and_docs_exist():
    ps1 = open(os.path.join(ROOT, "scripts", "aureon_service.ps1"), encoding="utf-8").read()
    for needle in ("nssm", "75", "heartbeat.txt", "service.log", "@(5, 30, 120)", "taskkill"):
        assert needle in ps1
    assert os.path.exists(os.path.join(ROOT, "docs", "SERVICE.md"))
    assert "AUREON_DEADMAN_URL" in open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
