"""v2.6.2 — event publisher. Tests use a stub, no redis needed."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from aureon_mt5.publisher import Publisher
from aureon_mt5.config import Config
from aureon_mt5.journal import Journal


class FakeRedis:
    """Stub for redis.Redis."""
    def __init__(self, fail=False):
        self.stream = []; self.positions = {}; self.fail = fail
    def ping(self):
        if self.fail: raise ConnectionError("down")
    def xadd(self, stream, fields, maxlen=None, approximate=False):
        if self.fail: raise ConnectionError("down")
        self.stream.append(fields)
    def hset(self, key, mapping=None):
        self.positions = dict(mapping or {})
    def delete(self, key):
        self.positions = {}


def _cfg(tmp_path, publish=True):
    cfg = Config(); cfg.log_dir = str(tmp_path); cfg.publish = publish; cfg.redis_url = "redis://localhost"
    return cfg


# ================================================================== every event type
def test_all_event_types(tmp_path):
    cfg = _cfg(tmp_path); j = Journal(str(tmp_path)); r = FakeRedis()
    pub = Publisher(cfg, j, redis_client=r)
    pub.on_open(1, "XAUUSD", "LONG", 1.0, 4200.0, 4188.0, "human", "ema2050")
    pub.on_sl_change(1, "XAUUSD", 4210.0)
    pub.on_partial(1, "XAUUSD", 0.05)
    pub.on_close(1, "XAUUSD", 4215.0, 15.0)
    pub.on_heartbeat()
    pub.on_halt()
    pub.on_unhalt()
    assert len(r.stream) == 7
    types = [e["type"] for e in r.stream]
    assert types == ["open", "sl_change", "partial", "close", "heartbeat", "halt", "unhalt"]


# ================================================================== idempotent event_ids
def test_unique_event_ids(tmp_path):
    cfg = _cfg(tmp_path); r = FakeRedis()
    pub = Publisher(cfg, Journal(str(tmp_path)), redis_client=r)
    pub.on_heartbeat(); pub.on_heartbeat()
    ids = [e["event_id"] for e in r.stream]
    assert len(set(ids)) == 2  # unique


# ================================================================== outbox spool + replay
def test_outbox_spool_and_replay(tmp_path):
    cfg = _cfg(tmp_path); r = FakeRedis(fail=True)
    pub = Publisher(cfg, Journal(str(tmp_path)), redis_client=r)
    pub.on_open(1, "XAUUSD", "LONG", 1.0, 4200.0, 4188.0, "human", "ema2050")
    assert len(r.stream) == 0  # failed, spooled
    assert os.path.exists(os.path.join(str(tmp_path), "outbox.jsonl"))
    # Redis comes back
    r.fail = False
    replayed = pub.replay_outbox()
    assert replayed == 1 and len(r.stream) == 1
    assert not os.path.exists(os.path.join(str(tmp_path), "outbox.jsonl"))


# ================================================================== snapshot
def test_snapshot(tmp_path):
    cfg = _cfg(tmp_path); r = FakeRedis()
    pub = Publisher(cfg, Journal(str(tmp_path)), redis_client=r)
    pub.snapshot([{"ticket": 1, "symbol": "XAUUSD", "direction": "long", "sl": 4188.0}])
    assert "1" in r.positions


# ================================================================== publish=0 does nothing
def test_publish_disabled(tmp_path):
    cfg = _cfg(tmp_path, publish=False); r = FakeRedis()
    pub = Publisher(cfg, Journal(str(tmp_path)), redis_client=r)
    pub.on_heartbeat()
    assert len(r.stream) == 0


# ================================================================== ordering: events have timestamps
def test_ordering(tmp_path):
    cfg = _cfg(tmp_path); r = FakeRedis()
    pub = Publisher(cfg, Journal(str(tmp_path)), redis_client=r)
    pub.on_open(1, "XAUUSD", "LONG", 1.0, 4200.0, 4188.0, "human", "ema2050")
    pub.on_sl_change(1, "XAUUSD", 4210.0)
    assert int(r.stream[0]["ts"]) <= int(r.stream[1]["ts"])
