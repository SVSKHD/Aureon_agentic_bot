"""v2.6.2 — event publisher for the follower copier.

When AUREON_PUBLISH=1 and REDIS_URL set, append to Redis Stream aureon:events (XADD, MAXLEN ~ 10000):
open, sl_change, partial, close, heartbeat, halt, unhalt.

Publishing happens AFTER the MT5 confirm + state + journal steps. A Redis failure never blocks trading —
events are spooled to logs/outbox.jsonl and replayed when Redis returns.

redis is an optional dependency."""
from __future__ import annotations

import json
import os
import time as _time
import uuid
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STREAM = "aureon:events"
POSITIONS_KEY = "aureon:positions"
MAXLEN = 10000


class Publisher:
    """Publishes trade events to Redis Stream for the follower copier."""

    def __init__(self, cfg, journal, redis_client=None):
        self.cfg = cfg
        self.journal = journal
        self._redis = redis_client
        self._outbox_path = os.path.join(cfg.log_dir, "outbox.jsonl")
        self._connected = False
        if cfg.publish and cfg.redis_url and redis_client is None:
            try:
                import redis
                self._redis = redis.from_url(cfg.redis_url, decode_responses=True)
                self._redis.ping()
                self._connected = True
            except Exception:
                self._redis = None
        elif redis_client is not None:
            self._connected = True

    def publish(self, event_type: str, payload: dict):
        """Publish a trade event. Spools to outbox on Redis failure."""
        if not self.cfg.publish:
            return
        event = {"event_id": str(uuid.uuid4()), "type": event_type, "ts": int(_time.time()),
                 "version": self.cfg.__class__.__dict__.get("VERSION", ""),
                 **payload}
        if self._redis and self._connected:
            try:
                self._redis.xadd(STREAM, event, maxlen=MAXLEN, approximate=True)
                return
            except Exception:
                self._connected = False
        # Spool to outbox
        self._spool(event)

    def _spool(self, event: dict):
        os.makedirs(os.path.dirname(self._outbox_path), exist_ok=True)
        with open(self._outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def replay_outbox(self):
        """Replay spooled events to Redis. Called periodically."""
        if not self._redis or not os.path.exists(self._outbox_path):
            return 0
        try:
            self._redis.ping()
            self._connected = True
        except Exception:
            return 0
        replayed = 0
        remaining = []
        try:
            with open(self._outbox_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        self._redis.xadd(STREAM, event, maxlen=MAXLEN, approximate=True)
                        replayed += 1
                    except Exception:
                        remaining.append(line)
        except Exception:
            return replayed
        # Rewrite with remaining (failed) events
        if remaining:
            with open(self._outbox_path, "w", encoding="utf-8") as f:
                f.writelines(remaining)
        elif os.path.exists(self._outbox_path):
            os.remove(self._outbox_path)
        return replayed

    def snapshot(self, positions: list[dict]):
        """Reconciliation snapshot: aureon:positions hash with current open positions."""
        if not self._redis or not self._connected or not self.cfg.publish:
            return
        try:
            data = {str(p.get("ticket", "")): json.dumps(p, default=str) for p in positions}
            if data:
                self._redis.delete(POSITIONS_KEY)
                self._redis.hset(POSITIONS_KEY, mapping=data)
            else:
                self._redis.delete(POSITIONS_KEY)
        except Exception:
            pass

    # ---- convenience methods for specific events
    def on_open(self, ticket: int, symbol: str, side: str, lots: float, price: float, sl: float | None, actor: str, mode: str):
        self.publish("open", {"master_ticket": ticket, "symbol": symbol, "side": side,
                              "lots": lots, "price": price, "sl": sl, "actor": actor, "mode": mode})

    def on_sl_change(self, ticket: int, symbol: str, sl: float):
        self.publish("sl_change", {"master_ticket": ticket, "symbol": symbol, "sl": sl})

    def on_partial(self, ticket: int, symbol: str, volume: float):
        self.publish("partial", {"master_ticket": ticket, "symbol": symbol, "volume": volume})

    def on_close(self, ticket: int, symbol: str, price: float, points: float):
        self.publish("close", {"master_ticket": ticket, "symbol": symbol, "price": price, "points": points})

    def on_heartbeat(self):
        self.publish("heartbeat", {})

    def on_halt(self):
        self.publish("halt", {})

    def on_unhalt(self):
        self.publish("unhalt", {})
