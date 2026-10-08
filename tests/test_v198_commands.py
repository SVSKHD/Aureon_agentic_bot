"""v1.9.8 — slash commands always acknowledge within Discord's 3 s, never block the event loop, always report errors.
Case: 8 Oct 2026 15:30/15:31 IST, /status and /pull-history showed 'The application did not respond' while the bot was alive."""
import ast, asyncio, os, sys, threading, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5 import broker, telemetry
from aureon_mt5 import bot
from aureon_mt5.bot import Reply, respond, status_card, report_card
from aureon_mt5.common.loop_health import LoopMonitor
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.agent import SymbolAgent
from aureon_mt5.strategies import get_strategy
from tests.test_v181 import FakeMT5

BOT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aureon_mt5", "bot.py")


class FakeInter:
    def __init__(self):
        self.calls = []; self.t0 = time.monotonic()
        self.response = types.SimpleNamespace(defer=self._defer)
        self.followup = types.SimpleNamespace(send=self._send)
        self.user = "tester"
    async def _defer(self, **kw): self.calls.append(("defer", time.monotonic() - self.t0, kw))
    async def _send(self, **kw): self.calls.append(("send", time.monotonic() - self.t0, kw))


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else body.splitlines()[0]); return True


plain = lambda r: {"card": r.card, "content": r.content}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    fake = FakeMT5(price=4212.0)
    monkeypatch.setattr(broker, "_m", fake); monkeypatch.setattr(broker, "_offset_h", 3.0)
    telemetry.setup(str(tmp_path))
    cfg = Config(); cfg.dry = False; cfg.log_dir = str(tmp_path)
    ag = SymbolAgent("XAUUSD", cfg, get_strategy("ema5080"), Notifier(None), Journal(str(tmp_path)), ())
    ag._refresh_snapshot(True)                      # one fast poll fills the cache
    return fake, ag, cfg


# ------------------------------------------------------------------ every command goes through respond()
def test_every_command_uses_the_defer_wrapper():
    tree = ast.parse(open(BOT_SRC, encoding="utf-8").read())
    cmds = [f for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef)
            and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "command" for d in f.decorator_list)]
    assert len(cmds) >= 10
    for f in cmds:
        awaits = [n for n in ast.walk(f) if isinstance(n, ast.Await)]
        assert len(awaits) == 1, f"/{f.name} must await exactly one thing: run(...)"
        call = awaits[0].value
        assert isinstance(call, ast.Call) and getattr(call.func, "id", "") == "run", f"/{f.name} does not go through run()"
        assert "response" not in ast.unparse(f), f"/{f.name} touches inter.response directly"


def test_defer_happens_before_slow_work():
    inter = FakeInter()
    def slow(): time.sleep(0.3); return Reply(content="ok")
    asyncio.run(respond(inter, "x", slow, to_kwargs=plain))
    assert [c[0] for c in inter.calls] == ["defer", "send"]
    assert inter.calls[0][1] < 0.1 and inter.calls[0][2]["thinking"] is True


# ------------------------------------------------------------------ slow MT5 → still acked, answered from cache
def test_slow_mt5_status_answers_from_cache(agent, monkeypatch):
    fake, ag, cfg = agent
    def slow(*a, **k): time.sleep(5); return None
    for name in ("positions", "tick_age", "market_open", "connected", "closed_deals"):
        monkeypatch.setattr(broker, name, slow)
    inter = FakeInter()
    t = time.monotonic()
    asyncio.run(respond(inter, "status", lambda: Reply(card=status_card("XAUUSD", ag, cfg)), to_kwargs=plain))
    assert time.monotonic() - t < 3.0
    kinds = [c[0] for c in inter.calls]; assert kinds == ["defer", "send"]
    card = inter.calls[1][2]["card"]
    assert "as of" in card["description"] and "XAUUSD" in card["title"]
    assert any("Position #1" == f["name"] for f in card["fields"])          # position came from the snapshot


def test_status_card_makes_no_mt5_call(agent, monkeypatch):
    fake, ag, cfg = agent
    def boom(*a, **k): raise AssertionError("MT5 called from a status command")
    for name in ("positions", "tick_age", "market_open", "connected", "mt5"):
        monkeypatch.setattr(broker, name, boom)
    for card in (status_card("XAUUSD", ag, cfg), bot.parallel_card({"XAUUSD": ag}, cfg), bot.agents_card({"XAUUSD": ag}, cfg, time.time()),
                 bot.market_card("XAUUSD", ag, cfg), bot.present_card("XAUUSD", {"XAUUSD": ag}, cfg)):
        assert card["title"]


# ------------------------------------------------------------------ errors always reach Discord
def test_exception_sends_error_followup(tmp_path):
    telemetry.setup(str(tmp_path)); n = Cap()
    inter = FakeInter()
    def bad(): raise RuntimeError("journal unreadable")
    asyncio.run(respond(inter, "status", bad, notify=n, journal=Journal(str(tmp_path)), to_kwargs=plain))
    sent = [c for c in inter.calls if c[0] == "send"]
    assert len(sent) == 1 and sent[0][2]["content"].startswith("⚠️ command failed: /status") and "journal unreadable" in sent[0][2]["content"]
    assert any("COMMAND FAILED" in t for t in n.titles)


def test_timeout_sends_error_followup():
    inter = FakeInter()
    asyncio.run(respond(inter, "report", lambda: time.sleep(1.0), timeout=0.2, to_kwargs=plain))
    assert "timed out" in inter.calls[-1][2]["content"]


# ------------------------------------------------------------------ MT5 lock busy → cached answer
def test_blocked_lock_report_answers_from_cache(agent):
    fake, ag, cfg = agent
    held = threading.Event(); release = threading.Event()
    def hog():
        with broker._lock:
            held.set(); release.wait(5)
    th = threading.Thread(target=hog); th.start(); held.wait(2)
    try:
        t = time.monotonic()
        card, source = report_card(cfg, {"XAUUSD": ag}, ag.journal, current=True, lock_timeout=0.5)
        assert time.monotonic() - t < 2.0
        assert source == "cache" and "MT5 busy" in card["description"]
        inter = FakeInter()                       # /status is unaffected by the held lock
        asyncio.run(respond(inter, "status", lambda: Reply(card=status_card("XAUUSD", ag, cfg)), to_kwargs=plain))
        assert inter.calls[-1][2]["card"] is not None
    finally:
        release.set(); th.join()


def test_report_live_when_lock_free(agent):
    fake, ag, cfg = agent
    card, source = report_card(cfg, {"XAUUSD": ag}, ag.journal, current=True)
    assert source == "live" and "MT5 busy" not in card["description"]


# ------------------------------------------------------------------ the event loop stays free while work runs
def test_slow_command_does_not_block_other_commands():
    a, b = FakeInter(), FakeInter()
    async def both():
        await asyncio.gather(respond(a, "report", lambda: (time.sleep(1.0), Reply(content="slow"))[1], to_kwargs=plain),
                             respond(b, "status", lambda: Reply(content="fast"), to_kwargs=plain))
    asyncio.run(both())
    assert b.calls[-1][1] < 0.5 and a.calls[-1][1] >= 1.0


# ------------------------------------------------------------------ loop-lag monitor
def test_loop_lag_monitor_fires_once_and_recovers():
    n = Cap(); now = [100.0]
    m = LoopMonitor(n, clock=lambda: now[0])
    assert m.check() is None                       # not started
    m.last_beat = 100.0; now[0] = 101.5
    assert m.check() is None                       # 0.5 s overdue: fine
    now[0] = 104.5                                 # ticker 3.5 s overdue → frozen loop
    assert m.check() == "UNRESPONSIVE" and m.check() is None
    m.last_beat = 104.6; m.lag = 0.0; now[0] = 104.7
    assert m.check() == "RESPONSIVE"
    assert [t for t in n.titles if "BOT" in t] == ["AUREON · MT5 · BOT UNRESPONSIVE", "AUREON · MT5 · BOT RESPONSIVE"]


def test_gateway_down_over_60s_fires():
    n = Cap(); now = [0.0]
    m = LoopMonitor(n, clock=lambda: now[0]); m.last_beat = 0.0
    m.gateway_down(); now[0] = 30.0; m.last_beat = 29.5
    assert m.check() is None
    now[0] = 61.0; m.last_beat = 60.5
    assert m.check() == "UNRESPONSIVE" and "gateway" in m.reason
    m.gateway_up(); assert m.check() == "RESPONSIVE"


def test_ticker_measures_lag():
    m = LoopMonitor(Cap(), interval=0.05)
    async def go():
        t = asyncio.create_task(m.ticker())
        await asyncio.sleep(0.12); time.sleep(0.3); await asyncio.sleep(0.1)   # block the loop 0.3 s
        t.cancel()
    asyncio.run(go())
    assert m.last_beat is not None and m.max_lag >= 0.2
