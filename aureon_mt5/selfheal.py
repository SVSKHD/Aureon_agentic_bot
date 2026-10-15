"""v2.7.2 — self-healing and outside-visible liveness.

One plain background thread (`HealthGuard`, never the Discord event loop) that every few seconds:
  * writes logs/heartbeat.txt every 30 s (ISO time · version · loop lag · gateway state) — read by /status and by
    scripts/deadman_fallback.py (Task Scheduler) which alerts Telegram when the file goes stale;
  * pings AUREON_DEADMAN_URL (healthchecks.io or any URL) every 60 s ONLY while every health condition holds:
    loop lag < 5 s, Discord gateway connected, every agent thread alive, last processed bar fresh while the market is open.
    Process dead or frozen → pings stop → the external service alerts (email / phone);
  * dumps every thread's stack to logs/stall-<time>.txt when the loop lags > 3 s (once per stall episode);
  * self-restarts on a hang: loop lag > 30 s, or gateway disconnected > 5 min while the process is otherwise alive →
    journal + logs/aureon.log + one webhook card (webhooks don't need the gateway) → exit(75); the supervisor
    (scripts/aureon_service.ps1) brings the process back. Guardian state is restart-safe (guardian_state.json + journal).

Reads only in-memory state (LoopMonitor, agent snapshots) — no MT5 call here, so a stuck terminal cannot freeze it.
Pure and clock-injected: tests drive `tick(now)` directly."""
from __future__ import annotations

import os
import sys
import threading
import time as _time
import traceback
from datetime import datetime, timezone

from . import telemetry
from .config import VERSION

EXIT_RESTART = 75                 # "restart me" — the supervisor restarts on any exit; 75 marks a requested restart

DEADMAN_LAG_S = 5.0               # deadman pings only while loop lag is below this
DEADMAN_EVERY_S = 60.0
HEARTBEAT_EVERY_S = 30.0
STALL_DUMP_LAG_S = 3.0
RESTART_LAG_S = 30.0
RESTART_GATEWAY_S = 300.0
SNAPSHOT_STALE_S = 300.0          # an agent that has not refreshed its snapshot for 5 min is frozen (stuck MT5 call)


def http_ping(url: str, timeout: float = 10.0) -> bool:
    """GET the dead-man URL. Never raises."""
    try:
        import requests  # type: ignore
        r = requests.get(url, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception as e:
        telemetry.info(f"deadman ping failed: {e!r}")
        return False


def dump_stacks(path: str, reason: str = "") -> str:
    """Write every thread's current stack (sys._current_frames) to `path`. Returns the path."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    names = {t.ident: t.name for t in threading.enumerate()}
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Aureon v{VERSION} stall dump · {datetime.now(timezone.utc).isoformat()} · {reason}\n\n")
        for ident, frame in sys._current_frames().items():
            f.write(f"--- thread {names.get(ident, '?')} ({ident}) ---\n")
            f.write("".join(traceback.format_stack(frame)))
            f.write("\n")
    return path


def read_heartbeat(log_dir: str) -> str | None:
    """The heartbeat.txt line (for /status), or None."""
    try:
        with open(os.path.join(log_dir, "heartbeat.txt"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


class HealthGuard:
    def __init__(self, *, monitor, agents: dict, log_dir: str, journal=None, notify=None, deadman_url: str | None = None,
                 stale_bar_s: float = 900.0, ping=http_ping, exit_hook=None, clock=_time.monotonic, wall=_time.time,
                 period: float = 2.0, bot_expected: bool = True):
        self.monitor, self.agents, self.log_dir = monitor, agents, log_dir
        self.journal, self.notify, self.deadman_url = journal, notify, deadman_url
        self.stale_bar_s, self.ping, self.exit_hook = stale_bar_s, ping, exit_hook
        self.clock, self.wall, self.period = clock, wall, period
        self.bot_expected = bot_expected            # False without DISCORD_TOKEN: loop/gateway conditions are skipped
        self.started_wall = wall()
        self.last_ping: float | None = None
        self.last_heartbeat: float | None = None
        self.pings = 0
        self.in_stall = False
        self.stall_dumps: list[str] = []
        self.restarting = False
        self.last_failures: list[str] = []
        self._thread: threading.Thread | None = None
        self.detector = None                        # v2.10.0: repair.Detector, ticked here (no extra thread)

    # -------------------------------------------------------------- health conditions
    def _gateway_up(self) -> bool:
        m = self.monitor
        return m is not None and getattr(m, "ever_connected", False) and m.disconnected_since is None

    def failures(self, now: float | None = None) -> list[str]:
        """Every unmet health condition (empty = healthy)."""
        now = self.clock() if now is None else now
        wall = self.wall()
        out = []
        if self.bot_expected:
            m = self.monitor
            if m is None:
                out.append("bot not running")
            else:
                if m.last_beat is None:
                    out.append("event loop not started")
                elif m.current_lag(now) >= DEADMAN_LAG_S:
                    out.append(f"loop lag {m.current_lag(now):.1f} s")
                if not self._gateway_up():
                    out.append("gateway disconnected")
        for sym, ag in list(self.agents.items()):
            if not ag.is_alive():
                out.append(f"{sym} agent dead"); continue
            snap = getattr(ag, "snapshot", None) or {}
            at = snap.get("at")
            if at is None:
                if wall - self.started_wall > SNAPSHOT_STALE_S:
                    out.append(f"{sym} agent never reported")
                continue
            if wall - at > SNAPSHOT_STALE_S:
                out.append(f"{sym} agent frozen {wall - at:.0f} s"); continue
            if snap.get("market_open"):
                bar_wall = getattr(ag, "last_bar_wall", None)
                ref = bar_wall if bar_wall is not None else self.started_wall
                if wall - ref > self.stale_bar_s:
                    out.append(f"{sym} no new bar {int((wall - ref) // 60)} min")
        return out

    def gateway_text(self) -> str:
        m = self.monitor
        if m is None:
            return "off" if not self.bot_expected else "not running"
        return m.gateway_state() if hasattr(m, "gateway_state") else ("up" if self._gateway_up() else "DOWN")

    # -------------------------------------------------------------- one evaluation
    def tick(self, now: float | None = None):
        now = self.clock() if now is None else now
        lag = self.monitor.current_lag(now) if self.monitor is not None else 0.0
        self._stall(lag, now)
        if self._maybe_restart(lag, now):
            return
        fails = self.failures(now)
        self.last_failures = fails
        if self.last_heartbeat is None or now - self.last_heartbeat >= HEARTBEAT_EVERY_S:
            self.last_heartbeat = now
            self.write_heartbeat(lag, fails)
        if self.detector is not None:
            try:
                self.detector.tick()
            except Exception as e:
                telemetry.info(f"repair detector failed: {e!r}")
        if self.deadman_url and not fails and (self.last_ping is None or now - self.last_ping >= DEADMAN_EVERY_S):
            self.last_ping = now
            if self.ping(self.deadman_url):
                self.pings += 1

    def write_heartbeat(self, lag: float, fails: list[str]):
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = datetime.fromtimestamp(self.wall(), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        line = (f"{stamp} v{VERSION} lag={lag:.1f}s gateway={self.gateway_text()} "
                f"health={'ok' if not fails else 'FAIL: ' + '; '.join(fails)}")
        path = os.path.join(self.log_dir, "heartbeat.txt")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(line + "\n")
            os.replace(tmp, path)
        except OSError as e:
            telemetry.info(f"heartbeat write failed: {e!r}")

    def _stall(self, lag: float, now: float):
        if lag > STALL_DUMP_LAG_S and not self.in_stall:
            self.in_stall = True
            stamp = datetime.fromtimestamp(self.wall(), tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = os.path.join(self.log_dir, f"stall-{stamp}.txt")
            try:
                dump_stacks(path, f"event loop lag {lag:.1f} s")
                self.stall_dumps.append(path)
                telemetry.info(f"loop stall {lag:.1f} s — stacks dumped to {path}")
            except Exception as e:
                telemetry.info(f"stall dump failed: {e!r}")
        elif lag <= STALL_DUMP_LAG_S:
            self.in_stall = False

    def _maybe_restart(self, lag: float, now: float) -> bool:
        if self.restarting or self.monitor is None or not self.bot_expected:
            return self.restarting
        why = None
        if self.monitor.last_beat is not None and lag > RESTART_LAG_S:
            why = f"event loop lag {lag:.0f} s (> {RESTART_LAG_S:.0f} s)"
        else:
            down = self.monitor.gateway_down_for(now) if hasattr(self.monitor, "gateway_down_for") else 0.0
            if down > RESTART_GATEWAY_S:
                why = f"Discord gateway disconnected {down:.0f} s (> {RESTART_GATEWAY_S:.0f} s)"
        if why is None:
            return False
        self.restarting = True
        telemetry.info(f"SELF-RESTART: {why}")
        if self.journal is not None:
            try:
                self.journal.log("self_restart", reason=why)
            except Exception:
                pass
        if self.notify is not None:
            try:
                self.notify.card("⚠️ Aureon frozen — restarting", f"{why}\nExiting with code {EXIT_RESTART}; the supervisor restarts it.",
                                 footer="Aureon MT5 · self-heal")
            except Exception:
                pass
        try:
            from .ops import write_restart_marker
            write_restart_marker(self.log_dir, "self-heal", why)
        except Exception:
            pass
        if self.exit_hook is not None:
            self.exit_hook(EXIT_RESTART)
        return True

    # -------------------------------------------------------------- thread
    def start(self):
        if self._thread is not None:
            return self._thread
        def run():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    telemetry.info(f"health guard tick failed: {e!r}")
                _time.sleep(self.period)
        self._thread = threading.Thread(target=run, daemon=True, name="health-guard")
        self._thread.start()
        return self._thread
