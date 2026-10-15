"""v2.11.2 — a network loss never ends the process (netguard, gateway supervisor, card queue, exit_reason, service paths)."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aureon_mt5 import broker, ops  # noqa: E402
from aureon_mt5.common.loop_health import LoopMonitor  # noqa: E402
from aureon_mt5.config import Config  # noqa: E402
from aureon_mt5.journal import Journal  # noqa: E402
from aureon_mt5.notify import Notifier  # noqa: E402
from aureon_mt5.selfheal import HealthGuard  # noqa: E402
from aureon_mt5.strategies import get_strategy  # noqa: E402
from tests.test_v181 import FakeMT5, guard  # noqa: E402

DEAD_HOSTS = ("discord.com", "ntfy.sh", "api.ntfy.sh", "127.0.0.1")


class Clock:
    def __init__(self, t=1000.0): self.t = t

    def __call__(self): return self.t


@pytest.fixture
def no_dns(monkeypatch):
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if any(str(host) == h or str(host).endswith("." + h) for h in DEAD_HOSTS):
            raise socket.gaierror(11001, "getaddrinfo failed")
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return fake


@pytest.fixture
def exits(monkeypatch):
    """Record (instead of performing) every way out of the process."""
    hits = []
    monkeypatch.setattr(os, "_exit", lambda code=0: hits.append(("os._exit", code)))
    monkeypatch.setattr(sys, "exit", lambda code=0: hits.append(("sys.exit", code)))
    monkeypatch.setattr(ops, "hard_exit", lambda code=75, agents=None, **k: hits.append(("ops.hard_exit", code)))
    import asyncio
    real_stop = asyncio.BaseEventLoop.stop

    def stop(self):                                            # asyncio's own run_until_complete stops its loop: not an exit
        caller = sys._getframe(1).f_code.co_filename
        if os.sep + "asyncio" + os.sep not in caller:
            hits.append(("loop.stop", caller))
        return real_stop(self)
    monkeypatch.setattr(asyncio.BaseEventLoop, "stop", stop)
    yield hits
    ops.STOP.clear()


# ============================================================================= the reproduction
def test_network_loss_never_ends_the_process(tmp_path, monkeypatch, no_dns, exits):
    """2026-10-15 18:55 IST: DNS gone for discord.com. Five simulated minutes later the process must still be there:
    the guardian ran and moved the stop, nothing called exit / os._exit / loop.stop, no thread died."""
    from aureon_mt5 import netguard
    monkeypatch.setattr(netguard, "GATEWAY_BACKOFF", (0.05, 0.2))
    # MT5 keeps ticking
    fake = FakeMT5("long", 4200.0, 0.0, 4202.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    j = Journal(str(tmp_path))
    netguard.configure(journal=j, log_dir=str(tmp_path))
    n = Notifier("https://discord.com/api/webhooks/1/x").configure(cfg)
    from aureon_mt5.agent import SymbolAgent
    import numpy as np, pandas as pd
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), n, j, ())
    df = pd.DataFrame({"time": np.arange(200) * 300, "close": 4202.0, "high": 4202.0, "low": 4202.0})
    # the Discord client: the real bot runner, logging in against a dead discord.com
    from aureon_mt5 import bot
    mono, wall = Clock(), Clock(time.time())
    monitor = bot.run_bot(cfg if cfg.bot_token else _with_token(cfg), {"XAUUSD": ag}, j, n, time.time())
    time.sleep(0.3)                                             # login against the dead host has failed by now
    monitor.clock = mono; monitor.created = mono.t
    if monitor.disconnected_since is not None:
        monitor.disconnected_since = mono.t                     # gateway down from t0 on the simulated clock
    hg = HealthGuard(monitor=monitor, agents={}, log_dir=str(tmp_path), journal=j, notify=n, exit_hook=lambda c: ops.hard_exit(c, {}),
                     clock=mono, wall=wall, bot_expected=True)
    sl_seen = []
    for step in range(11):                                      # 0 … 5 min, every 30 s
        mono.t += 30; wall.t += 30
        fake.price = 4202.0 + step * 1.5
        guard(ag, fake, df, 1, close=fake.price)               # the guardian on the existing stop
        sl_seen.append(fake.pos.sl)
        n.send(f"ema5080:XAUUSD:TRAIL:{step}", "XAUUSD · TRAIL", ["x"], event="FILLED")   # webhook: DNS failure
        hg.tick(mono.t)
        time.sleep(0.05)                                       # let the gateway supervisor retry for real
    assert sl_seen[-1] > 0 and len(set(sl_seen)) > 1, "the guardian must keep moving the stop"
    assert exits == [], f"process exit attempted: {exits}"
    alive = {t.name for t in threading.enumerate()}
    assert "discord-bot" in alive, "the Discord thread died"
    assert netguard.degraded(), "network loss must be a state, not an exit"
    starts = [r for r in j.read(0) if r.get("event") == "network_degraded"]
    assert starts, "the flip is journaled"
    assert netguard.STATE["client_starts"] >= 2, "the supervisor really retried client.start() against the dead host"
    ops.STOP.set()                                              # planned stop ends the supervisor
    for t in threading.enumerate():
        if t.name == "discord-bot":
            t.join(timeout=5)
            assert not t.is_alive(), "STOP must end the supervisor"


def _with_token(cfg):
    cfg.bot_token = "x" * 59
    return cfg


# ============================================================================= gateway supervisor
def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_supervisor_restarts_after_exception_and_clean_return_with_capped_backoff():
    from aureon_mt5 import netguard
    calls, delays, exits_seen = [], [], []
    stop = {"now": False}

    async def start():
        calls.append(1)
        if len(calls) % 2:
            raise OSError("getaddrinfo failed")                # DNS
        return None                                            # discord.py returned cleanly
    async def sleep(d):
        delays.append(d)
        if len(delays) >= 9:
            stop["now"] = True
    clock = Clock(0.0)
    runs = _run(netguard.supervise_client(start, stop_requested=lambda: stop["now"], sleep=sleep, clock=clock,
                                          on_exit=exits_seen.append))
    assert runs == 9 and len(calls) == 9
    assert delays == [2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert exits_seen[0].startswith("OSError") and exits_seen[1] == "client returned"


def test_supervisor_resets_backoff_after_a_long_run_and_stops_only_on_stop():
    from aureon_mt5 import netguard
    clock, delays = Clock(0.0), []
    n = {"i": 0}

    async def start():
        n["i"] += 1
        clock.t += 300 if n["i"] == 4 else 1                   # the 4th connection stayed up 5 min
        raise RuntimeError("ConnectionClosed")
    async def sleep(d):
        delays.append(d)
    runs = _run(netguard.supervise_client(start, stop_requested=lambda: n["i"] >= 6, sleep=sleep, clock=clock))
    assert delays == [2, 4, 8, 2, 4] and runs == 6


def test_supervisor_never_starts_when_stop_already_requested():
    from aureon_mt5 import netguard

    async def start():
        raise AssertionError("must not start")
    assert _run(netguard.supervise_client(start, stop_requested=lambda: True)) == 0


def test_hard_exit_sets_stop_and_records_the_exit(tmp_path):
    from aureon_mt5 import netguard
    j = Journal(str(tmp_path)); netguard.configure(journal=j, log_dir=str(tmp_path), positions=lambda: 1)
    ops.write_restart_marker(str(tmp_path), "manual", "/restart by me")
    codes = []
    ops.hard_exit(75, {}, _exit=codes.append)
    assert codes == [75] and ops.STOP.is_set()
    m = ops.pop_restart_marker(str(tmp_path))
    assert m["exit"]["reason"] == "restart · manual · /restart by me" and m["exit"]["positions_open"] == 1
    assert [r for r in j.read(0) if r["event"] == "process_exit"][0]["planned"] is True


# ============================================================================= webhook DNS failure → queue → flush
class _R:
    def __init__(self, code): self.status_code = code; self.text = ""

    def json(self): return {"id": "1"}


def test_webhook_dns_failure_queues_trade_cards_and_flushes_summary_first(tmp_path, monkeypatch):
    import requests
    from aureon_mt5 import netguard
    posted, up = [], {"ok": False}

    def post(url, json=None, **k):
        if not up["ok"]:
            raise requests.exceptions.ConnectionError("NameResolutionError: getaddrinfo failed for discord.com")
        posted.append(json); return _R(204)
    monkeypatch.setattr(requests, "post", post)
    cfg = Config(); cfg.log_dir = str(tmp_path)
    n = Notifier("https://discord.com/api/webhooks/1/x").configure(cfg)
    n.send("m:XAUUSD:ASK:1", "XAUUSD · ENTER LONG", ["a"], event="ASK")
    n.send("alert:XAUUSD:A1:ORDER", "ORDER PLACED", ["b"])
    assert netguard.webhook_down() and netguard.degraded()
    t0 = time.monotonic()
    n.send("m:XAUUSD:CLOSE:1", "XAUUSD · CLOSED WIN", ["c"], event="CLOSED")       # no POST attempted while down
    n.send("m:XAUUSD:TREND:1", "TREND", ["info card"])
    assert time.monotonic() - t0 < 0.5
    assert [i["title"] for i in n.queue.items()] == ["XAUUSD · ENTER LONG", "ORDER PLACED", "XAUUSD · CLOSED WIN"]
    assert "m:XAUUSD:ASK:1" in n.sent                         # no duplicate later: delivery is the queue's job
    assert n.flush() == {"ok": False, "queued": 3}            # still down: kept
    up["ok"] = True
    res = n.flush()
    assert res["ok"] and res["sent"] == 3
    titles = [p["embeds"][0]["title"] for p in posted]
    assert titles[0].startswith("AUREON · MT5 · BACK ONLINE · network · down") and titles[0].endswith("3 cards queued")
    assert titles[1:] == ["XAUUSD · ENTER LONG", "ORDER PLACED", "XAUUSD · CLOSED WIN"]
    assert "TREND" in posted[0]["embeds"][0]["description"]   # the missed info card is listed in the summary
    assert n.queue.items() == [] and not netguard.webhook_down()


def test_flush_stops_at_first_failure_and_keeps_the_rest(tmp_path, monkeypatch):
    import requests
    codes = iter([204, 204, 500])
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k: _R(next(codes)))
    cfg = Config(); cfg.log_dir = str(tmp_path)
    n = Notifier("https://discord.com/api/webhooks/1/x").configure(cfg)
    for i in range(3):
        n.queue.append({"t": time.time(), "key": f"k{i}", "title": f"T{i}", "body": "", "embed": {"title": f"T{i}"}, "png": None})
    assert n.flush() == {"ok": False, "sent": 1, "queued": 2}
    assert [i["title"] for i in n.queue.items()] == ["T1", "T2"]


def test_card_queue_is_capped_at_500(tmp_path):
    from aureon_mt5.netguard import CardQueue
    q = CardQueue(str(tmp_path / "card_queue.jsonl"))
    for i in range(510):
        q.append({"title": f"T{i}"})
    items = q.items()
    assert len(items) == 500 and items[0]["title"] == "T10" and items[-1]["title"] == "T509"


def test_post_failures_are_logged_once_a_minute(tmp_path, monkeypatch, capsys):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("dns")))
    n = Notifier("https://discord.com/api/webhooks/1/x")
    for i in range(5):
        n.raw("x")
    assert capsys.readouterr().out.count("discord post failed") == 1


# ============================================================================= degraded: no new entries (flag re-enables)
def test_degraded_suppresses_asks_and_flag_reenables(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from aureon_mt5 import netguard
    fake = FakeMT5("long", 4200.0, 0.0, 4200.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    j = Journal(str(tmp_path))
    from aureon_mt5.agent import SymbolAgent
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Notifier(None), j, ())
    ag.notify.ask = Mock()
    netguard.webhook_result(False); netguard.webhook_result(False)
    assert netguard.degraded()
    assert ag._entry_ask("ema5080:XAUUSD:P_LONG:1", "XAUUSD · ENTER LONG", ["x"], fields=[]) is False
    assert not ag.notify.ask.called
    rows = [r for r in j.read(0) if r["event"] == "ask_suppressed_network"]
    assert rows and rows[0]["key"] == "ema5080:XAUUSD:P_LONG:1" and "webhook failed 2× in a row" in rows[0]["reasons"]
    assert netguard.STATE["suppressed"][-1]["title"] == "XAUUSD · ENTER LONG"
    from aureon_mt5 import features as F
    F.set_override(str(tmp_path), cfg, "network_degraded_entries", "on", "me")
    assert cfg.network_degraded_entries is True and F.is_loud(F.get("network_degraded_entries"), True)
    assert ag._entry_ask("ema5080:XAUUSD:P_LONG:2", "XAUUSD · ENTER LONG", ["x"], fields=[]) is True
    assert ag.notify.ask.called


def test_degraded_rules_gateway_webhook_dns():
    from aureon_mt5 import netguard
    mono = Clock(0.0)
    mon = LoopMonitor(None, clock=mono); mon.gateway_up("ready"); mon.gateway_down()
    netguard.MONITOR = mon
    mono.t = 59; assert netguard.update() is None and not netguard.degraded()
    mono.t = 61; assert netguard.update() is True and "gateway down 61 s" in netguard.STATE["reasons"]
    mon.gateway_up(); assert netguard.update() is False
    netguard.webhook_result(False); assert not netguard.degraded()
    netguard.webhook_result(False); assert netguard.degraded()
    netguard.webhook_result(True); assert not netguard.degraded()
    netguard.dns_check([("discord", "discord.com")], resolver=lambda h, p: (_ for _ in ()).throw(socket.gaierror(11001, "x")))
    netguard.update(); assert netguard.degraded() and "discord.com does not resolve" in netguard.STATE["reasons"]
    from aureon_mt5 import server
    assert server.network_degraded()                          # mirrored into the v2.10.1 entry gate


# ============================================================================= push pause
def test_push_backend_paused_after_5_failures_with_one_card():
    from types import SimpleNamespace
    from aureon_mt5 import push as P
    now = [1000.0]
    cfg = SimpleNamespace(push=("NTFY",), push_images=False, push_loss_alert_pts=5.0, ntfy_url="https://ntfy.sh", ntfy_topic="t",
                          ntfy_token="", console_token="", console_url="", ask_ttl_min=20)

    class Http:
        def post(self, *a, **k): raise OSError("getaddrinfo failed")
    cards = []
    p = P.Push(cfg, None, http=Http(), clock=lambda: now[0])
    p.on_pause = lambda b, err: cards.append((b, err))
    for i in range(7):
        futs = p.notify(key=f"k{i}", title="t", event="FILLED", klass="trade")
        for f in futs:
            f.result(5)
    assert len(cards) == 1 and cards[0][0] == "ntfy" and "getaddrinfo" in cards[0][1]
    assert p.backends() == []                                 # paused: notify() is a no-op, never blocks
    now[0] += 601
    assert p.backends() == ["ntfy"]
    p.stop()


# ============================================================================= exit_reason
def _main_mod(monkeypatch, tmp_path, body):
    import main as M
    from aureon_mt5 import netguard
    j = Journal(str(tmp_path)); netguard.configure(journal=j, log_dir=str(tmp_path), positions=lambda: 2)
    monkeypatch.setattr(M, "main", body)
    return M, j


def test_exit_reason_on_exception_is_unplanned_and_nonzero(tmp_path, monkeypatch):
    from aureon_mt5 import netguard

    def boom():
        raise ValueError("boom")
    M, j = _main_mod(monkeypatch, tmp_path, boom)
    assert M.run() == 1
    rec = [r for r in j.read(0) if r["event"] == "process_exit"][0]
    assert rec["reason"] == "unhandled ValueError: boom" and rec["exc_type"] == "ValueError" and rec["planned"] is False
    assert "boom" in rec["where"] and rec["positions_open"] == 2 and rec["uptime_s"] >= 0
    m = netguard.pop_exit_marker(str(tmp_path))
    assert m["reason"] == "unhandled ValueError: boom"
    assert netguard.last_exit_text(m).startswith("last exit: unhandled ValueError: boom · ValueError · at test_v2112_netguard.py")


def test_exit_reason_on_sigint_and_normal_stop(tmp_path, monkeypatch):
    from aureon_mt5 import netguard

    def ctrl_c():
        raise KeyboardInterrupt
    M, j = _main_mod(monkeypatch, tmp_path, ctrl_c)
    assert M.run() == 0 and ops.STOP.is_set()
    assert netguard.pop_exit_marker(str(tmp_path))["reason"] == "SIGINT (Ctrl-C)"
    netguard.reset(); ops.STOP.clear()
    M, j = _main_mod(monkeypatch, tmp_path, lambda: None)
    assert M.run() == 0
    assert netguard.pop_exit_marker(str(tmp_path))["reason"] == "normal stop"


def test_exit_reason_on_sigterm_and_restart_code(tmp_path, monkeypatch):
    import signal
    from aureon_mt5 import netguard

    def term():
        os.kill(os.getpid(), signal.SIGTERM); time.sleep(1)
    M, j = _main_mod(monkeypatch, tmp_path, term)
    assert M.run() == 0 and netguard.pop_exit_marker(str(tmp_path))["reason"] == "SIGTERM (service stop)"
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    netguard.reset()

    def mt5_down():
        sys.exit(ops.EXIT_RESTART)
    M, j = _main_mod(monkeypatch, tmp_path, mt5_down)
    assert M.run() == 75
    rec = netguard.pop_exit_marker(str(tmp_path))
    assert rec["reason"] == "SystemExit(75)" and rec["planned"] is True


def test_boot_cards_show_last_exit(tmp_path):
    from aureon_mt5 import netguard, server
    rec = {"reason": "unhandled OSError: x", "exc_type": "OSError", "where": "main.py:1 main"}
    assert server.down_text({"t": time.time() - 5, "exit": rec}).endswith("last exit: unhandled OSError: x · OSError · at main.py:1 main")
    cards = []
    n = types.SimpleNamespace(card=lambda t, d, **k: cards.append(d))
    cfg = Config(); cfg.log_dir = str(tmp_path)
    server.cold_start(cfg, Journal(str(tmp_path)), n, {}, run=lambda c, t=10: (False, ""), last_exit=rec)
    assert "last exit: unhandled OSError: x" in cards[0]


# ============================================================================= service-safe paths
@pytest.mark.parametrize("path,hit", [(r"C:\Users\k\OneDrive\Desktop\Aureon_agentic_bot", "onedrive"),
                                      ("/Users/k/Dropbox/aureon", "dropbox"), ("/home/k/Google Drive/aureon", "google drive"),
                                      (r"C:\Aureon", None)])
def test_validator_warns_on_sync_folders(path, hit):
    from aureon_mt5 import config as C
    cfg = Config(); cfg.log_dir = "logs"
    probs = C.sync_folder_problems(cfg, root=path)
    if hit is None:
        assert probs == []
    else:
        assert probs and probs[0]["name"] == "sync_folder" and hit in probs[0]["issue"] and "C:\\Aureon" in probs[0]["issue"]
        assert probs[0]["group"] == "server"


def test_validate_includes_sync_folder_rule(monkeypatch):
    from aureon_mt5 import config as C
    monkeypatch.setattr(C, "REPO_ROOT", "/home/k/OneDrive/Desktop/Aureon")
    assert any(p["name"] == "sync_folder" for p in C.validate(Config(), {}))
    assert C.apply_fallbacks(Config(), [p for p in C.validate(Config(), {}) if p["name"] == "sync_folder"]) == []


# ============================================================================= DNS self-check
def test_dns_self_check_renders_failures():
    from aureon_mt5 import netguard

    def resolver(host, port):
        if host == "discord.com":
            raise socket.gaierror(11001, "getaddrinfo failed")
        return [("ok",)]
    cfg = Config(); cfg.ntfy_url = "https://ntfy.sh"; cfg.mt5_host = "mt5.broker.example"
    hosts = netguard.dns_hosts(cfg)
    assert hosts == [("discord", "discord.com"), ("ntfy", "api.ntfy.sh"), ("mt5", "mt5.broker.example")]
    res = netguard.dns_check(hosts, resolver=resolver)
    line = netguard.dns_line(res)
    assert line.startswith("DNS: discord.com ❌") and "getaddrinfo failed" in line and "api.ntfy.sh ✅" in line
    assert netguard.STATE["dns_fail"] is True
    cfg.mt5_host = ""
    assert netguard.dns_line(netguard.dns_check(netguard.dns_hosts(cfg), resolver=lambda h, p: [1])).endswith("mt5 —")


def test_dns_check_bounded_by_timeout():
    from aureon_mt5 import netguard
    t0 = time.monotonic()
    res = netguard.dns_check([("discord", "discord.com")], resolver=lambda h, p: time.sleep(2), timeout=0.2)
    assert res["discord"]["ok"] is False and time.monotonic() - t0 < 2.5


def test_heartbeat_card_carries_the_dns_line(tmp_path, monkeypatch):
    from aureon_mt5 import netguard
    monkeypatch.setattr(netguard, "dns_check", lambda hosts, **k: {"discord": {"host": "discord.com", "ok": False, "error": "getaddrinfo failed"}})
    from aureon_mt5.watchdog import Watchdog
    sent = []
    n = types.SimpleNamespace(send=lambda key, title, lines, **k: sent.append(lines) or True)
    cfg = Config(); cfg.log_dir = str(tmp_path)
    wd = Watchdog(n, Journal(str(tmp_path)), cfg, {})
    wd._heartbeat(time.time()) if hasattr(wd, "_heartbeat") else wd.heartbeat(time.time())
    assert any("DNS: discord.com ❌ getaddrinfo failed" in l for l in sent[0])


# ============================================================================= console stays best-effort
def test_console_thread_restarts_when_uvicorn_dies(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from aureon_mt5 import console as K
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.listen(1)      # port taken → bind fails
    cfg = types.SimpleNamespace(console_token="t" * 40, console_bind=f"127.0.0.1:{port}", console_allow_public=False)
    from fastapi import FastAPI
    srv = K.ConsoleServer(cfg, FastAPI())
    assert srv.start(backoff=(0.05, 0.1)) == ""
    for _ in range(100):
        if srv.restarts >= 2:
            break
        time.sleep(0.05)
    assert srv.restarts >= 2 and srv.thread.is_alive()
    srv.stop()
    s.close()
    assert not srv.thread.is_alive()


def test_flip_card_fires_even_when_another_thread_saw_the_flip_first():
    from aureon_mt5 import netguard
    seen = []
    netguard.webhook_result(False); netguard.webhook_result(False)       # agent thread: flips, no callback
    assert netguard.degraded()
    netguard.update(on_flip=seen.append)                                 # main loop, 30 s later
    netguard.update(on_flip=seen.append)
    netguard.webhook_result(True)
    netguard.update(on_flip=seen.append)
    assert seen == [True, False]
