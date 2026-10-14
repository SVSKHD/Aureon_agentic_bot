"""v1.9.8 — Discord bot health: event-loop lag and gateway connection.

A 1 s asyncio ticker (on the bot loop) records when it last ran and how late it woke up. A plain watchdog thread
evaluates it, so the alert still goes out when the loop itself is frozen. Cards are posted on state change only:
BOT UNRESPONSIVE (lag > 2 s or gateway down > 60 s) and BOT RESPONSIVE (recovered).

v2.7.2: every disconnect / resume is logged with its duration; reconnect times are kept so the heartbeat card can say
"gateway reconnects last 4 h: N". Before the first on_ready the gateway counts as down (gateway_down_for)."""
from __future__ import annotations

import asyncio
import threading
import time as _time

LAG_LIMIT_S = 2.0


def _log(msg: str):
    try:
        from .. import telemetry
        telemetry.info(msg)
    except Exception:
        pass

GATEWAY_LIMIT_S = 60.0


class LoopMonitor:
    def __init__(self, notify, *, lag_limit: float = LAG_LIMIT_S, gateway_limit: float = GATEWAY_LIMIT_S,
                 interval: float = 1.0, clock=_time.monotonic):
        self.notify, self.lag_limit, self.gateway_limit, self.interval, self.clock = notify, lag_limit, gateway_limit, interval, clock
        self.last_beat: float | None = None      # monotonic time of the last ticker wake-up
        self.lag: float = 0.0                    # how late the last wake-up was, seconds
        self.max_lag: float = 0.0                # worst lag seen since start
        self.latency: float | None = None        # Discord gateway heartbeat latency, seconds
        self.disconnected_since: float | None = None
        self.created = clock()
        self.ever_connected = False              # v2.7.2: on_ready seen at least once
        self.reconnects: list[float] = []        # monotonic times of each resume after a disconnect
        self.disconnects = 0
        self.unresponsive = False
        self.reason = ""
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- loop side
    async def ticker(self, client=None):
        while True:
            t0 = self.clock()
            await asyncio.sleep(self.interval)
            now = self.clock()
            self.lag = max(0.0, now - t0 - self.interval)
            self.max_lag = max(self.max_lag, self.lag)
            self.last_beat = now
            if client is not None:
                try:
                    lat = float(client.latency)
                    self.latency = lat if lat == lat and lat != float("inf") else None
                except Exception:
                    self.latency = None

    def gateway_down(self, why: str = "disconnect"):
        if self.disconnected_since is None:
            self.disconnected_since = self.clock()
            self.disconnects += 1
            _log(f"discord gateway DOWN ({why})")

    def gateway_up(self, how: str = "resumed"):
        now = self.clock()
        if self.disconnected_since is not None:
            dur = now - self.disconnected_since
            self.reconnects = [t for t in self.reconnects if now - t <= 86400] + [now]
            _log(f"discord gateway UP ({how}) after {dur:.1f} s down")
        elif not self.ever_connected:
            _log(f"discord gateway UP ({how}) {now - self.created:.1f} s after start")
        self.ever_connected = True
        self.disconnected_since = None

    def reconnects_since(self, seconds: float, now: float | None = None) -> int:
        now = self.clock() if now is None else now
        return sum(1 for t in self.reconnects if now - t <= seconds)

    def gateway_down_for(self, now: float | None = None) -> float:
        """Seconds the gateway has been down (counting from start until the first on_ready). 0 when up."""
        now = self.clock() if now is None else now
        if self.disconnected_since is not None:
            return now - self.disconnected_since
        if not self.ever_connected:
            return now - self.created
        return 0.0

    def gateway_state(self, now: float | None = None) -> str:
        if self.disconnected_since is not None:
            return f"DOWN {self.gateway_down_for(now):.0f}s"
        return "up" if self.ever_connected else "connecting"

    # -------------------------------------------------------------- readings
    def current_lag(self, now: float | None = None) -> float:
        """Measured lag, or — if the ticker has not run for a while — how overdue it is (a frozen loop)."""
        if self.last_beat is None:
            return 0.0
        now = self.clock() if now is None else now
        return max(self.lag, now - self.last_beat - self.interval)

    def summary(self) -> str:
        lag = self.current_lag()
        lat = f"{self.latency * 1000:.0f} ms" if self.latency is not None else "—"
        gw = "DOWN" if self.disconnected_since is not None else "up"
        return (f"loop lag {lag:.1f} s (max {self.max_lag:.1f}) · gateway {gw} · latency {lat}"
                f" · reconnects 4 h: {self.reconnects_since(4 * 3600)}")

    # -------------------------------------------------------------- evaluation (watchdog thread)
    def check(self, now: float | None = None) -> str | None:
        """Returns 'UNRESPONSIVE' / 'RESPONSIVE' when the state changed (and posts the card), else None."""
        if self.last_beat is None:
            return None                                    # bot not up yet
        now = self.clock() if now is None else now
        lag = self.current_lag(now)
        down = self.disconnected_since is not None and now - self.disconnected_since > self.gateway_limit
        bad = lag > self.lag_limit or down
        if bad and not self.unresponsive:
            self.unresponsive = True
            self.reason = (f"event loop lag {lag:.1f} s (> {self.lag_limit:g} s)" if lag > self.lag_limit
                           else f"gateway disconnected {now - self.disconnected_since:.0f} s (> {self.gateway_limit:g} s)")
            self._card("AUREON · MT5 · BOT UNRESPONSIVE", self.reason + " — slash commands may time out; alerts and guardian keep running")
            return "UNRESPONSIVE"
        if not bad and self.unresponsive:
            self.unresponsive = False
            self._card("AUREON · MT5 · BOT RESPONSIVE", f"recovered · {self.summary()}")
            return "RESPONSIVE"
        return None

    def _card(self, title: str, text: str):
        try:
            self.notify.card(title, text, footer="Aureon MT5 · bot health")
        except Exception:
            pass

    def start_watchdog(self, period: float = 1.0):
        if self._thread is not None:
            return
        def run():
            while True:
                try:
                    self.check()
                except Exception:
                    pass
                _time.sleep(period)
        self._thread = threading.Thread(target=run, daemon=True, name="bot-watchdog"); self._thread.start()
