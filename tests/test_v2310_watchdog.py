"""v2.3.10 — Discord watchdog: stale/disconnect/dead incidents, persistence, heartbeat."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal
from aureon_mt5.notify import Notifier
from aureon_mt5.watchdog import Watchdog


class FakeBroker:
    def __init__(self, connected=True, market_open=True):
        self._connected = connected; self._market_open = market_open
    def connected(self): return self._connected
    def market_open(self, *a, **k): return self._market_open


class Cap(Notifier):
    def __init__(self): super().__init__(None); self.titles = []; self.bodies = []
    def raw(self, body, png=None, *, embed=None):
        self.titles.append(embed["title"] if embed else str(body).splitlines()[0])
        self.bodies.append(str(embed or body))
        return True


class FakeAgent:
    def __init__(self, alive=True, last_bar_wall=None):
        self.health = {"status": "running", "last_bar": "12:00", "chop": None}
        self.S = types.SimpleNamespace(display_name="EMA 20/50", name="ema2050")
        self.off = 3.0
        self.last_bar_wall = last_bar_wall
        self.snapshot = {"tick_age": 5, "positions": []}
        self.alerts = None
        self._alive = alive
    def is_alive(self): return self._alive


def _cfg(tmp_path, watchdog=True):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.dry = True; cfg.watchdog = watchdog
    cfg.heartbeat_stale_min = 15; cfg.heartbeat_hours = 4
    return cfg


# ================================================================== stale opens after 2 checks, one card
def test_stale_opens_after_two_checks_one_card(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 20 * 60)  # 20 min ago, stale
    br = FakeBroker()
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick()  # check 1: fail_count=1
    assert not any("WATCHDOG" in t for t in n.titles)
    wd.tick()  # check 2: fail_count=2 → opens
    assert any("WATCHDOG" in t and "NO NEW M5 BAR" in t for t in n.titles)
    assert len([t for t in n.titles if "WATCHDOG" in t]) == 1


# ================================================================== no repeat while open
def test_no_repeat_while_open(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 20 * 60)
    br = FakeBroker()
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick(); wd.tick()  # opens
    count = len([t for t in n.titles if "WATCHDOG" in t])
    wd.tick(); wd.tick(); wd.tick()  # still failing
    assert len([t for t in n.titles if "WATCHDOG" in t]) == count  # no new cards


# ================================================================== RECOVERED with duration
def test_recovered_with_duration(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    t = [1000000.0]
    ag = FakeAgent(last_bar_wall=t[0] - 20 * 60)
    br = FakeBroker()
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: t[0], broker=br)
    wd.tick(); wd.tick()  # opens
    # Now bar arrives — agent is fresh
    t[0] += 23 * 60
    ag.last_bar_wall = t[0]
    wd.tick()  # pass_count=1
    wd.tick()  # pass_count=2 → clears
    recovered = [t_ for t_ in n.titles if "RECOVERED" in t_]
    assert len(recovered) == 1
    clears = [e for e in j.read(0) if e["event"] == "watchdog_clear"]
    assert len(clears) == 1 and clears[0]["duration_min"] == 23


# ================================================================== no STALE while market closed
def test_no_stale_while_market_closed(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 60 * 60)  # very stale
    br = FakeBroker(market_open=False)
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick(); wd.tick(); wd.tick()
    assert not any("WATCHDOG" in t and "NO NEW M5 BAR" in t for t in n.titles)


# ================================================================== disconnect suppresses per-symbol stale cards
def test_disconnect_suppresses_stale_cards(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    cfg.dry = False  # must be non-dry for MT5 disconnect to register
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 20 * 60)
    br = FakeBroker(connected=False)  # MT5 disconnected
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick(); wd.tick()
    titles = n.titles
    assert any("MT5 DISCONNECTED" in t for t in titles)
    assert not any("NO NEW M5 BAR" in t for t in titles)


# ================================================================== dead agent incident
def test_dead_agent_incident(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    now = 1000000.0
    ag = FakeAgent(alive=False, last_bar_wall=now)
    br = FakeBroker()
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick(); wd.tick()
    assert any("AGENT THREAD DEAD" in t for t in n.titles)


# ================================================================== persistence across new Watchdog instance
def test_persistence_no_second_open_card(tmp_path):
    n1 = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 20 * 60)
    br = FakeBroker()
    wd1 = Watchdog(n1, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd1.tick(); wd1.tick()  # opens + saves
    assert any("WATCHDOG" in t for t in n1.titles)
    # "restart": new instance, incident still failing
    n2 = Cap()
    wd2 = Watchdog(n2, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd2.tick()
    # No second open card
    assert not any("WATCHDOG" in t and "NO NEW M5 BAR" in t for t in n2.titles)
    # Now it clears
    ag.last_bar_wall = now
    wd2.tick(); wd2.tick()
    recovered = [t for t in n2.titles if "RECOVERED" in t]
    assert len(recovered) == 1


def test_persistence_clear_at_startup(tmp_path):
    """Incident left from previous process, already clear at startup → one RECOVERED."""
    n1 = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 20 * 60)
    br = FakeBroker()
    wd1 = Watchdog(n1, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd1.tick(); wd1.tick()  # opens
    # "restart": condition now clear (bar is fresh)
    ag.last_bar_wall = now
    n2 = Cap()
    wd2 = Watchdog(n2, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd2.tick()  # first tick reconciles → RECOVERED
    recovered = [t for t in n2.titles if "RECOVERED" in t]
    assert len(recovered) == 1 and "cleared while Aureon was down" in " ".join(n2.bodies)


# ================================================================== heartbeat: once per slot, aligned, not doubled
def test_heartbeat_once_per_slot(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    # 08:01 IST → slot 08, should post
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    # Find a timestamp that is 08:01 IST
    dt = datetime(2026, 10, 13, 8, 1, 0, tzinfo=IST)
    now = dt.timestamp()
    ag = FakeAgent(last_bar_wall=now)
    br = FakeBroker()
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick()
    hb = [t for t in n.titles if "Aureon alive" in t]
    assert len(hb) == 1
    # Second tick same slot → no duplicate
    wd.tick()
    assert len([t for t in n.titles if "Aureon alive" in t]) == 1


def test_heartbeat_not_doubled_after_restart(tmp_path):
    n1 = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    dt = datetime(2026, 10, 13, 8, 1, 0, tzinfo=IST)
    now = dt.timestamp()
    ag = FakeAgent(last_bar_wall=now); br = FakeBroker()
    wd1 = Watchdog(n1, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd1.tick()  # posts heartbeat
    assert len([t for t in n1.titles if "Aureon alive" in t]) == 1
    # "restart"
    n2 = Cap()
    wd2 = Watchdog(n2, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd2.tick()
    assert len([t for t in n2.titles if "Aureon alive" in t]) == 0  # not doubled


def test_heartbeat_skipped_on_quiet_weekend(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    dt = datetime(2026, 10, 17, 8, 1, 0, tzinfo=IST)  # Saturday
    now = dt.timestamp()
    ag = FakeAgent(last_bar_wall=now); br = FakeBroker(market_open=False)
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick()
    assert len([t for t in n.titles if "Aureon alive" in t]) == 0


def test_heartbeat_posted_on_weekend_with_incident(tmp_path):
    """Heartbeat fires on a weekend when an incident is open."""
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path)
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    # Use 04:01 IST Saturday for the incident, then 08:01 for the heartbeat check
    dt_open = datetime(2026, 10, 17, 4, 1, 0, tzinfo=IST)  # Saturday 04:01
    t_open = dt_open.timestamp()
    ag = FakeAgent(alive=False, last_bar_wall=t_open); br = FakeBroker(market_open=False)
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: t_open, broker=br)
    wd.tick(); wd.tick()  # opens dead-agent incident + posts 04:xx heartbeat
    assert any(v.get("open") for v in wd._incidents.values())
    # Advance to 08:01 IST (next heartbeat slot)
    dt_hb = datetime(2026, 10, 17, 8, 1, 0, tzinfo=IST)
    t_hb = dt_hb.timestamp()
    n.titles.clear()
    wd.clock = lambda: t_hb
    wd.tick()
    # Should post heartbeat at slot 08 despite weekend (incident open)
    assert len([t for t in n.titles if "Aureon alive" in t]) == 1


# ================================================================== AUREON_WATCHDOG=0 → nothing posted
def test_watchdog_disabled(tmp_path):
    n = Cap(); j = Journal(str(tmp_path)); cfg = _cfg(tmp_path, watchdog=False)
    now = 1000000.0
    ag = FakeAgent(last_bar_wall=now - 60 * 60)
    br = FakeBroker(connected=False)
    wd = Watchdog(n, j, cfg, {"XAUUSD": ag}, clock=lambda: now, broker=br)
    wd.tick(); wd.tick(); wd.tick()
    assert n.titles == []
